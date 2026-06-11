"""vhs-tool encode — FFV1 + Opus from `vhs-tool export` → [VapourSynth] → x265 → mkvmerge.

Port of tools/7_encode.sh (Step 2 of the publish pipeline).

Uses the x265 CLI directly (not ffmpeg's libx265 wrapper) for full parameter
control. VapourSynth outputs 4:2:0 10-bit y4m, x265 encodes to a raw HEVC
bitstream, mkvmerge muxes everything into the final MKV.

Modes:
  Normal:      Encode to final x265 MKV
  --vspreview: Open the VapourSynth script in vspreview for interactive tuning
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from ..common import (
    ToolError,
    check_deps,
    frame_rate,
    run,
    seconds_to_ts,
    ts_to_seconds,
    video_duration,
)
from ..config import get_config
from ..encoding import X265_PROFILES

_CFG = get_config()


# =============================================================================
# Cut helpers — segment derivation, chapter shifting, audio cutting
# =============================================================================


@dataclass(frozen=True)
class Segment:
    start: float  # seconds
    end: float  # seconds


def build_cut_segments(
    markers: list[tuple[str, str]], duration: float
) -> tuple[list[Segment], list[Segment]]:
    """Validate cut markers and derive (keep, remove) segment lists.

    Rules: timestamps must be valid HH:MM:SS[.mmm], strictly increasing across
    all markers, types must alternate (begin/end). A leading 'end' implies an
    implicit begin at 0; a trailing 'begin' implies an implicit end at video
    duration.
    """
    parsed: list[tuple[str, float]] = []
    prev_kind = None
    prev_s = -1.0
    for kind, ts in markers:
        seconds = ts_to_seconds(ts)
        if kind == prev_kind:
            raise ToolError(f"Two consecutive --cut-{kind} markers — they must alternate begin/end")
        if seconds <= prev_s:
            raise ToolError(f"Cut timestamps must be strictly increasing (got {ts})")
        if seconds > duration:
            raise ToolError(f"Cut timestamp {ts} exceeds video duration ({duration}s)")
        parsed.append((kind, seconds))
        prev_kind, prev_s = kind, seconds

    # Walk markers → remove segments
    remove: list[Segment] = []
    i = 0
    while i < len(parsed):
        kind, seconds = parsed[i]
        if kind == "end":
            # Only legal as the first marker (alternation guarantees this)
            remove.append(Segment(0.0, seconds))
            i += 1
        elif i + 1 < len(parsed):
            remove.append(Segment(seconds, parsed[i + 1][1]))
            i += 2
        else:
            # Trailing standalone begin → cut to end of video
            remove.append(Segment(seconds, duration))
            i += 1

    # Complement → keep segments
    keep: list[Segment] = []
    pos = 0.0
    for seg in remove:
        if seg.start > pos:
            keep.append(Segment(pos, seg.start))
        pos = seg.end
    if pos < duration:
        keep.append(Segment(pos, duration))

    if not keep:
        raise ToolError("Cuts remove the entire video — nothing left to encode")
    return keep, remove


def adjust_chapter_ts(ts: str, remove: list[Segment]) -> str:
    """Shift a chapter timestamp left by the removed duration occurring before it.

    Errors if the chapter falls inside a cut region.
    """
    seconds = ts_to_seconds(ts)
    removed = 0.0
    for seg in remove:
        if seg.start <= seconds < seg.end:
            raise ToolError(
                f"Chapter at {ts} falls inside removed segment [{seg.start}s, {seg.end}s]"
            )
        if seconds >= seg.end:
            removed += seg.end - seg.start
        else:
            break
    return seconds_to_ts(seconds - removed)


def mkvmerge_tolerant(mkvmerge_args: list) -> None:
    """Run mkvmerge, tolerating exit code 1.

    mkvmerge exit codes: 0=ok, 1=warnings (output IS produced), 2=real error.
    Warnings are NORMAL for raw-HEVC mux and packet-boundary cuts.
    """
    result = run(["mkvmerge", *mkvmerge_args], check=False)
    if result.returncode > 1:
        raise ToolError(
            f"mkvmerge exited with code {result.returncode} "
            f"(args: {shlex.join(str(a) for a in mkvmerge_args)})"
        )


def cut_audio_with_mkvmerge(
    input_file: Path, output_file: Path, keep: list[Segment], work_dir: Path
) -> None:
    """Stream-copy cut audio with mkvmerge --split parts: (no re-encode).

    mkvmerge appends a number suffix to split outputs; we produce the cut into
    a temp dir and rename the single resulting file to the requested path.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix=".cut-", dir=work_dir))
    parts = ",".join(
        ("" if i == 0 else "+") + f"{seconds_to_ts(seg.start)}-{seconds_to_ts(seg.end)}"
        for i, seg in enumerate(keep)
    )
    mkvmerge_tolerant(["-o", tmpdir / "cut.mkv", "--split", f"parts:{parts}", input_file])
    # Match any .mkv mkvmerge produced (with or without -NNN suffix)
    produced = sorted(tmpdir.glob("*.mkv"))
    if not produced:
        raise ToolError(f"mkvmerge did not produce a cut file for {input_file}")
    shutil.move(produced[0], output_file)
    shutil.rmtree(tmpdir, ignore_errors=True)


# =============================================================================
# Video encoding — producer (vspipe/ffmpeg) piped into x265
# =============================================================================


def pipe_to_x265(
    producer_cmd: list, x265_opts: list[str], output: Path, env: dict[str, str] | None = None
) -> None:
    producer_argv = [str(c) for c in producer_cmd]
    x265_argv = ["x265", "--input", "-", "--y4m", *x265_opts, "--output", str(output)]

    producer = subprocess.Popen(producer_argv, stdout=subprocess.PIPE, env=env)
    try:
        consumer = subprocess.run(x265_argv, stdin=producer.stdout, check=False)
    finally:
        producer.stdout.close()  # let the producer see SIGPIPE if x265 died
    producer_rc = producer.wait()

    if producer_rc != 0:
        raise ToolError(f"{producer_argv[0]} exited with code {producer_rc}")
    if consumer.returncode != 0:
        raise ToolError(f"x265 exited with code {consumer.returncode}")


def print_media_summary(file: Path, max_lines: int) -> None:
    if shutil.which("mediainfo"):
        result = run(["mediainfo", file], capture=True)
        print("\n".join(result.stdout.splitlines()[:max_lines]))
    else:
        print(f"  {file} ({file.stat().st_size} bytes)")


# =============================================================================
# Argument parsing
# =============================================================================


class _CutMarkerAction(argparse.Action):
    """Collect --cut-begin/--cut-end into one ordered list (order matters)."""

    def __call__(self, parser, namespace, values, option_string=None):
        markers = getattr(namespace, self.dest, None) or []
        kind = "begin" if option_string == "--cut-begin" else "end"
        markers.append((kind, values))
        setattr(namespace, self.dest, markers)


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "encode",
        help="Postprocess and encode FFV1 + Opus files from `vhs-tool export`",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "input_base",
        help="Base path from `vhs-tool export` (expects .ffv1.mkv, .linear.opus, etc.)",
    )
    parser.add_argument(
        "-p",
        "--profile",
        choices=sorted(X265_PROFILES),
        help="Encode profile (required for encoding)",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Final output path without extension "
        f"(default: {_CFG.paths.final}/<input-basename>_<profile>)",
    )
    parser.add_argument("--vpy", help="VapourSynth script (.vpy) for filtering")
    parser.add_argument(
        "--vpy-output",
        type=int,
        default=1000,
        help="VapourSynth output index (default: 1000 — final processed clip)",
    )
    parser.add_argument(
        "--vspreview",
        action="store_true",
        help="Open VapourSynth script in vspreview and exit (no encoding)",
    )
    parser.add_argument(
        "--no-deinterlace",
        action="store_true",
        help="Skip QTGMC deinterlacing (for progressive-on-VHS content, "
        "e.g. film club material transferred from Super8/16mm)",
    )
    parser.add_argument(
        "--test",
        nargs=2,
        metavar=("START", "DURATION"),
        help="Test encode a segment (e.g. --test 00:05:00 00:00:30). "
        "Outputs video-only .test.mkv, skips audio mux",
    )
    parser.add_argument(
        "--cut-begin",
        dest="cut_markers",
        action=_CutMarkerAction,
        metavar="TS",
        help="Cut OUT video starting at TS (repeatable). Paired with the next "
        "--cut-end defines a removed segment. Standalone at end removes "
        "everything from TS to end of video.",
    )
    parser.add_argument(
        "--cut-end",
        dest="cut_markers",
        action=_CutMarkerAction,
        metavar="TS",
        help="Cut OUT video ending at TS (repeatable). Paired with the previous "
        "--cut-begin. Standalone at the start removes everything from 0 to TS. "
        "Markers must alternate begin/end and timestamps strictly increase. "
        "Requires --vpy. Chapters are shifted automatically; chapters inside "
        "cut regions error.",
    )
    # Metadata
    parser.add_argument("--title", help="Title metadata")
    parser.add_argument("--source", help='Source device (e.g. "Panasonic NV-VP30")')
    parser.add_argument("--publisher", help="Publisher name")
    parser.add_argument("--date", help="Date/year")
    parser.add_argument("--comment", help="Comment")
    parser.add_argument(
        "--lang",
        default=_CFG.defaults.lang,
        help=f"Audio language code (default: {_CFG.defaults.lang})",
    )
    # Files
    parser.add_argument(
        "--cover",
        dest="covers",
        action="append",
        metavar="IMAGE",
        help="Additional image to attach (repeatable)",
    )
    parser.add_argument(
        "--chapter",
        dest="chapters",
        action="append",
        metavar='"HH:MM:SS.mmm Title"',
        help='Chapter marker as "HH:MM:SS.mmm Title" (repeatable)',
    )
    parser.add_argument(
        "--chapters-file", help="Path to existing chapters file (OGM or XML format)"
    )
    parser.set_defaults(func=cmd_encode, cut_markers=None)


# =============================================================================
# Command
# =============================================================================


def cmd_encode(args: argparse.Namespace) -> int:
    check_deps("ffmpeg", "ffprobe", "mkvmerge", "x265")

    deinterlace = not args.no_deinterlace
    cut_markers: list[tuple[str, str]] = args.cut_markers or []
    chapters: list[str] = args.chapters or []
    covers: list[str] = args.covers or []

    # Discover input files
    input_base = args.input_base
    ffv1_file = Path(f"{input_base}.ffv1.mkv")
    hifi_opus = Path(f"{input_base}.hifi.opus")
    linear_opus = Path(f"{input_base}.linear.opus")

    if not ffv1_file.is_file():
        raise ToolError(f"FFV1 not found: {ffv1_file}")
    if not linear_opus.is_file():
        raise ToolError(f"Linear audio not found: {linear_opus}")
    has_hifi = hifi_opus.is_file()

    # Process cut markers (validate, build segments, env for VPY)
    keep_segments: list[Segment] = []
    remove_segments: list[Segment] = []
    if cut_markers:
        if not args.vpy:
            raise ToolError("--cut-begin/--cut-end require --vpy")
        if args.test:
            raise ToolError("--cut-* cannot be combined with --test")
        if args.chapters_file:
            raise ToolError(
                "--cut-* cannot be combined with --chapters-file "
                "(external chapters can't be auto-shifted)"
            )
        duration = video_duration(ffv1_file)
        keep_segments, remove_segments = build_cut_segments(cut_markers, duration)

    # Environment for the VapourSynth script
    vpy_env = os.environ.copy()
    vpy_env["VHS_INPUT"] = str(ffv1_file.resolve())
    vpy_env["VHS_DEINTERLACE"] = "1" if deinterlace else "0"
    vpy_env["ENCODE_PROFILE"] = args.profile or ""
    if keep_segments:
        vpy_env["VHS_KEEP_SEGMENTS"] = ",".join(
            f"{seg.start:.6f}-{seg.end:.6f}" for seg in keep_segments
        )

    # vspreview mode — open and exit
    if args.vspreview:
        if not args.vpy:
            raise ToolError("--vspreview requires --vpy")
        vpy_script = Path(args.vpy)
        if not vpy_script.is_file():
            raise ToolError(f"VapourSynth script not found: {vpy_script}")
        if shutil.which("vspreview") is None:
            raise ToolError("vspreview not found. Install: pip install vspreview")

        print("Opening vspreview...")
        print(f"  FFV1:   {ffv1_file}")
        print(f"  Script: {vpy_script}")
        print(f"  Deinterlace: {'yes (QTGMC)' if deinterlace else 'no (progressive)'}")
        os.environ.update(vpy_env)
        os.execvp("vspreview", ["vspreview", str(vpy_script.resolve())])

    # Encoding mode — validate remaining requirements
    if not args.profile:
        raise ToolError("--profile is required for encoding")
    x265_opts = X265_PROFILES[args.profile]

    output = args.output or str(Path(_CFG.paths.final) / f"{Path(input_base).name}_{args.profile}")

    vpy_script: Path | None = None
    if args.vpy:
        vpy_script = Path(args.vpy)
        if not vpy_script.is_file():
            raise ToolError(f"VapourSynth script not found: {vpy_script}")
        if shutil.which("vspipe") is None:
            raise ToolError("vspipe not found. Install VapourSynth.")

    output_dir = Path(output).parent
    output_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("VHS Encode")
    print("=" * 60)
    print(f"Input:        {input_base}")
    print(f"Output:       {output}")
    print(f"Encode:       {args.profile}")
    print(
        f"VapourSynth:  {f'{vpy_script} (output: {args.vpy_output})' if vpy_script else 'disabled'}"
    )
    print(f"Deinterlace:  {'yes (QTGMC)' if deinterlace else 'no (progressive)'}")
    print(f"HiFi:         {'yes' if has_hifi else 'no'}")
    if args.test:
        print(f"Test segment: {args.test[0]} + {args.test[1]}")
    if keep_segments:
        print(f"Cuts:         keep {len(keep_segments)} segment(s), remove {len(remove_segments)}")
        for seg in remove_segments:
            print(f"  remove: {seconds_to_ts(seg.start)} → {seconds_to_ts(seg.end)}")
    print("=" * 60)

    work_dir = Path(tempfile.mkdtemp(prefix=".vhs-encode-", dir=output_dir))
    try:
        return _encode(
            args,
            ffv1_file=ffv1_file,
            linear_opus=linear_opus,
            hifi_opus=hifi_opus if has_hifi else None,
            output=output,
            x265_opts=x265_opts,
            vpy_script=vpy_script,
            vpy_env=vpy_env,
            deinterlace=deinterlace,
            keep_segments=keep_segments,
            remove_segments=remove_segments,
            chapters=chapters,
            covers=covers,
            work_dir=work_dir,
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _encode(
    args: argparse.Namespace,
    *,
    ffv1_file: Path,
    linear_opus: Path,
    hifi_opus: Path | None,
    output: str,
    x265_opts: list[str],
    vpy_script: Path | None,
    vpy_env: dict[str, str],
    deinterlace: bool,
    keep_segments: list[Segment],
    remove_segments: list[Segment],
    chapters: list[str],
    covers: list[str],
    work_dir: Path,
) -> int:
    # -- Step 1: [VapourSynth] → x265 encode (video only) ----------------------
    print(f"\nStep 1: Encoding x265 ({args.profile})...")

    video_hevc = work_dir / "video.265"

    vspipe_range: list[str] = []
    seek_opts: list[str] = []
    if args.test:
        test_start, test_duration = args.test
        if vpy_script:
            # Convert timestamps to frame numbers for vspipe -s/-e.
            # QTGMC doubles fps (25i→50p), so calculate against the OUTPUT fps.
            fps = frame_rate(ffv1_file)
            out_fps = float(fps) * (2 if deinterlace else 1)
            start_s = ts_to_seconds(test_start)
            dur_s = ts_to_seconds(test_duration)
            start_frame = int(start_s * out_fps)
            end_frame = int((start_s + dur_s) * out_fps) - 1
            vspipe_range = ["-s", str(start_frame), "-e", str(end_frame)]
            out_desc = "50p" if deinterlace else f"{fps}fps"
            print(
                f"  Test segment: {test_start} + {test_duration} "
                f"→ frames {start_frame}–{end_frame} (output: {out_desc})"
            )
        else:
            # Non-VapourSynth path: ffmpeg seeks before piping to x265
            seek_opts = ["-ss", test_start, "-t", test_duration]
            print(f"  Test segment: {test_start} + {test_duration}")

    if vpy_script:
        # VapourSynth path: vspipe → x265 (direkt, kein ffmpeg dazwischen)
        print(f"  VapourSynth: {vpy_script} (output: {args.vpy_output})")
        producer = [
            "vspipe", "-c", "y4m", *vspipe_range,
            "-o", str(args.vpy_output), str(vpy_script.resolve()), "-",
        ]  # fmt: skip
        pipe_to_x265(producer, x265_opts, video_hevc, env=vpy_env)
    else:
        # Non-VapourSynth path: ffmpeg dekodiert FFV1 → y4m pipe → x265.
        # ffmpeg übernimmt Pixel-Format-Konvertierung und SAR aus FFV1-Metadaten.
        producer = [
            "ffmpeg", "-hide_banner", *seek_opts, "-i", str(ffv1_file),
            "-f", "yuv4mpegpipe", "-pix_fmt", "yuv420p10le", "-an", "-",
        ]  # fmt: skip
        pipe_to_x265(producer, x265_opts, video_hevc)

    # -- Test mode — skip full mux, output video-only ---------------------------
    if args.test:
        test_mkv = Path(f"{output}.test.mkv")
        mkvmerge_tolerant(["-o", test_mkv, video_hevc])
        print()
        print("=" * 60)
        print(f"Test encode complete: {test_mkv}")
        print()
        print_media_summary(test_mkv, 40)
        print("=" * 60)
        return 0

    # -- Step 2: mkvmerge — mux everything in one pass --------------------------
    print("\nStep 2: Muxing with mkvmerge...")

    final_mkv = Path(f"{output}.mkv")
    lang = args.lang

    # Cut audio (stream-copy) if cuts are active
    linear_audio: Path = linear_opus
    hifi_audio: Path | None = hifi_opus
    if keep_segments:
        print(f"  Cutting audio with mkvmerge (stream copy, {len(keep_segments)} segment(s))...")
        linear_audio = work_dir / "linear.cut.mkv"
        cut_audio_with_mkvmerge(linear_opus, linear_audio, keep_segments, work_dir)
        if hifi_opus:
            hifi_audio = work_dir / "hifi.cut.mkv"
            cut_audio_with_mkvmerge(hifi_opus, hifi_audio, keep_segments, work_dir)

    mkvmerge_cmd: list = ["-o", final_mkv]

    # Title
    if args.title:
        mkvmerge_cmd += ["--title", args.title]

    # Video track (raw HEVC bitstream — mkvmerge handles this directly)
    mkvmerge_cmd.append(video_hevc)

    # Audio: HiFi first (primary), then Linear
    if hifi_audio:
        mkvmerge_cmd += [
            "--language", f"0:{lang}",
            "--track-name", "0:HiFi",
            "--default-track-flag", "0:1",
            hifi_audio,
        ]  # fmt: skip
    mkvmerge_cmd += [
        "--language", f"0:{lang}",
        "--track-name", "0:Linear",
        "--default-track-flag", f"0:{0 if hifi_audio else 1}",
        linear_audio,
    ]  # fmt: skip

    # Metadata tags (Source, Publisher, Date, Comment)
    tags = [
        ("SOURCE", args.source),
        ("PUBLISHER", args.publisher),
        ("DATE_RELEASED", args.date),
        ("COMMENT", args.comment),
    ]
    tags = [(name, value) for name, value in tags if value]
    if tags:
        tags_file = work_dir / "tags.xml"
        simple = "".join(
            f"<Simple><Name>{name}</Name><String>{escape(value)}</String></Simple>"
            for name, value in tags
        )
        tags_file.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f"<Tags><Tag><Targets></Targets>{simple}</Tag></Tags>\n",
            encoding="utf-8",
        )
        mkvmerge_cmd += ["--global-tags", tags_file]

    # Chapters
    generated_chapters: Path | None = None
    if chapters:
        generated_chapters = work_dir / "chapters.txt"
        lines = []
        for i, entry in enumerate(chapters, start=1):
            timestamp, _, title = entry.partition(" ")
            if keep_segments:
                shifted = adjust_chapter_ts(timestamp, remove_segments)
                print(f"  Chapter {i} shifted: {timestamp} → {shifted}")
                timestamp = shifted
            lines.append(f"CHAPTER{i:02d}={timestamp}")
            lines.append(f"CHAPTER{i:02d}NAME={title}")
        generated_chapters.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"  Generated {len(chapters)} chapters")

    chapter_source = Path(args.chapters_file) if args.chapters_file else generated_chapters
    if chapter_source and chapter_source.is_file():
        mkvmerge_cmd += ["--chapters", chapter_source]
        print(f"  Chapters: {chapter_source}")

    # Cover images
    first_cover = True
    for cover in covers:
        cover_path = Path(cover)
        if not cover_path.is_file():
            print(f"  Warning: Cover image not found, skipping: {cover}")
            continue
        mime = "image/png" if cover_path.suffix == ".png" else "image/jpeg"
        if first_cover:
            name = f"cover{cover_path.suffix}"
            description = "Cover"
            first_cover = False
        else:
            name = cover_path.name
            description = cover_path.stem
        mkvmerge_cmd += [
            "--attachment-name", name,
            "--attachment-description", description,
            "--attachment-mime-type", mime,
            "--attach-file", cover_path,
        ]  # fmt: skip

    print(f"  Command: mkvmerge {shlex.join(str(a) for a in mkvmerge_cmd)}")
    mkvmerge_tolerant(mkvmerge_cmd)

    print()
    print("=" * 60)
    print(f"Encode complete: {final_mkv}")
    print()
    print_media_summary(final_mkv, 80)
    print("=" * 60)
    return 0
