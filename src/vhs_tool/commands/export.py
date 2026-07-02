"""vhs-tool export — decoded TBC + aligned FLAC → lossless FFV1 video + Opus audio.

Port of tools/6_export.sh (Step 1 of the publish pipeline).

Output files:
  {output}.ffv1.mkv     Lossless FFV1 10-bit 4:2:2 video
  {output}.hifi.opus    HiFi Opus audio (if HiFi source exists)
  {output}.linear.opus  Linear Opus audio

Run this first, then `vhs-tool encode` for postprocessing and final encoding.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from ..common import ToolError, audio_channels, check_deps, human_size, run
from ..config import get_config

_CFG = get_config()

# -- Defaults ------------------------------------------------------------------
MONO_THRESHOLD_DB = -50.0
HIFI_BITRATE_STEREO = "160k"
HIFI_BITRATE_MONO = "80k"
LINEAR_BITRATE_STEREO = "96k"
LINEAR_BITRATE_MONO = "64k"

PARTS = ("linear", "hifi", "video")


def parse_only(value: str) -> list[str]:
    """Parse a comma-separated --only value into a validated list of parts."""
    parts = [p.strip() for p in value.split(",") if p.strip()]
    if not parts:
        raise argparse.ArgumentTypeError(f"expected at least one of: {', '.join(PARTS)}")
    invalid = [p for p in parts if p not in PARTS]
    if invalid:
        raise argparse.ArgumentTypeError(
            f"invalid part(s): {', '.join(invalid)} (choose from {', '.join(PARTS)})"
        )
    return parts


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "export",
        help="Export decoded TBC files to lossless FFV1 video + Opus audio",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "base_path",
        help="Decoded files base path (without suffix), e.g. "
        "./decoded/VHS_PAL_Tape__Name-2026-05-03_18_14_58_02_00",
    )
    parser.add_argument(
        "-o",
        "--output",
        help=f"Output base path without extension (default: {_CFG.paths.export}/<input-basename>)",
    )
    parser.add_argument(
        "--only",
        type=parse_only,
        metavar="{linear,hifi,video}[,...]",
        help="Export only specific parts, comma-separated (e.g. --only linear,video)",
    )
    parser.add_argument(
        "--force-mono",
        action="store_true",
        help="Force mono downmix (pan L+R) regardless of detection",
    )
    parser.add_argument(
        "--process-vbi",
        action="store_true",
        help="Process VBI data during FFV1 export (off by default)",
    )
    parser.add_argument(
        "--lang",
        default=_CFG.defaults.lang,
        help=f"Audio language code (default: {_CFG.defaults.lang})",
    )
    parser.add_argument(
        "--tbc-export",
        default=_CFG.binaries.tbc_video_export,
        help=f"Path to tbc-video-export (default: {_CFG.binaries.tbc_video_export})",
    )
    parser.add_argument(
        "--tbc-tools",
        default=_CFG.binaries.tbc_tools,
        help=f"Path to tbc-tools AppImage (default: {_CFG.binaries.tbc_tools})",
    )
    parser.add_argument(
        "--config-file",
        default=_CFG.binaries.tbc_export_config,
        help=f"Path to tbc-video-export.json (default: {_CFG.binaries.tbc_export_config})",
    )
    parser.set_defaults(func=cmd_export)


# -- Mono/Stereo detection -----------------------------------------------------


def detect_channels(file: Path, label: str) -> str:
    """Return 'mono' or 'stereo' for an audio file.

    Single-channel sources are mono by definition; for 2-channel sources the
    L-R difference RMS level decides (below MONO_THRESHOLD_DB → dual-mono).
    """
    if audio_channels(file) == 1:
        print(f"  {label}: mono (source is single channel)")
        return "mono"

    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(file),
            "-af",
            "pan=1c|c0=c0-c1,astats=metadata=1:reset=0",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    rms_lines = [
        line for line in (proc.stderr + proc.stdout).splitlines() if "RMS level dB" in line
    ]
    if proc.returncode != 0 or not rms_lines:
        tail = "\n".join(proc.stderr.strip().splitlines()[-5:])
        raise ToolError(
            f"Channel detection failed for {file} (ffmpeg exit {proc.returncode}): {tail}"
        )
    raw = rms_lines[-1].split("RMS level dB:")[-1].strip()
    try:
        diff_rms = float(raw)  # "-inf" (perfect dual-mono) parses fine
    except ValueError as exc:
        raise ToolError(
            f"Channel detection failed for {file}: unexpected RMS value {raw!r}"
        ) from exc

    if diff_rms < MONO_THRESHOLD_DB:
        print(f"  {label}: mono (L-R diff: {raw} dB)")
        return "mono"
    print(f"  {label}: stereo (L-R diff: {raw} dB)")
    return "stereo"


def transcode_opus(
    src: Path, dst: str, mode: str, bitrate_stereo: str, bitrate_mono: str, label: str
) -> None:
    bitrate = bitrate_mono if mode == "mono" else bitrate_stereo
    print(f"  {label}: Opus {bitrate} {mode} → {dst}")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y", "-i", src]
    # Downmix only multi-channel sources: on a 1-channel input the pan filter
    # would treat the missing c1 as silence and attenuate the output by 6 dB.
    if mode == "mono" and audio_channels(src) > 1:
        cmd += ["-af", "pan=1c|c0=0.5*c0+0.5*c1"]
    cmd += ["-c:a", "libopus", "-b:a", bitrate, "-vbr", "on", "-application", "audio", dst]
    run(cmd)


# -- Command -------------------------------------------------------------------


def cmd_export(args: argparse.Namespace) -> int:
    check_deps("ffmpeg", "ffprobe")

    only: list[str] = args.only or []

    def should_run(part: str) -> bool:
        return not only or part in only

    base_path = args.base_path
    output = args.output or str(Path(_CFG.paths.export) / Path(base_path).name)

    tbc_export = Path(args.tbc_export)
    tbc_tools = Path(args.tbc_tools)
    config_file = Path(args.config_file)

    # Dependency checks (only validate what's actually needed)
    if should_run("video"):
        if not tbc_export.is_file():
            raise ToolError(f"tbc-video-export not found: {tbc_export}")
        if not tbc_tools.is_file():
            raise ToolError(f"tbc-tools not found: {tbc_tools}")
        if not config_file.is_file():
            raise ToolError(f"Config file not found: {config_file}")

    # Discover files
    tbc_file = Path(f"{base_path}-video.tbc")
    hifi_flac = Path(f"{base_path}-hifi.aligned.flac")
    linear_flac = Path(f"{base_path}-linear.aligned.flac")

    if should_run("video") and not tbc_file.is_file():
        raise ToolError(f"TBC file not found: {tbc_file}")
    if should_run("linear") and not linear_flac.is_file():
        raise ToolError(f"Linear audio not found: {linear_flac}")

    has_hifi = hifi_flac.is_file()
    if should_run("hifi") and not has_hifi:
        raise ToolError(f"HiFi audio not found: {hifi_flac}")

    Path(output).parent.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("VHS Decode Export")
    print("=" * 60)
    print(f"Base path:  {base_path}")
    print(f"Output:     {output}")
    print(f"TBC file:   {tbc_file}")
    print(f"Linear:     {linear_flac}")
    print(f"HiFi:       {hifi_flac if has_hifi else 'not found'}")
    print(f"Only:       {' '.join(only) if only else 'all'}")
    print(f"Force mono: {args.force_mono}")
    print(f"Language:   {args.lang}")
    print("=" * 60)

    # Mono/Stereo detection
    print("\nDetecting channel layout...")
    linear_mode = hifi_mode = "mono"
    if args.force_mono:
        print("  Forced mono downmix (--force-mono)")
    else:
        if should_run("linear"):
            linear_mode = detect_channels(linear_flac, "Linear")
        if should_run("hifi") and has_hifi:
            hifi_mode = detect_channels(hifi_flac, "HiFi")

    # Transcode audio to Opus
    linear_opus = f"{output}.linear.opus"
    hifi_opus = f"{output}.hifi.opus"

    if should_run("linear"):
        print("\nTranscoding Linear audio...")
        transcode_opus(
            linear_flac,
            linear_opus,
            linear_mode,
            LINEAR_BITRATE_STEREO,
            LINEAR_BITRATE_MONO,
            "Linear",
        )
    else:
        print("\nSkipping Linear audio")

    if should_run("hifi") and has_hifi:
        print("\nTranscoding HiFi audio...")
        transcode_opus(
            hifi_flac, hifi_opus, hifi_mode, HIFI_BITRATE_STEREO, HIFI_BITRATE_MONO, "HiFi"
        )
    elif not has_hifi:
        print("\nNo HiFi source found")
    else:
        print("\nSkipping HiFi audio")

    # Export FFV1 from TBC
    ffv1_file = Path(f"{output}.ffv1.mkv")
    if should_run("video"):
        print("\nExporting FFV1...")
        export_cmd = [
            tbc_export,
            "--tbc-tools-appimage",
            tbc_tools,
            "--config-file",
            config_file,
        ]
        if args.process_vbi:
            export_cmd.append("--process-vbi")
        export_cmd += [
            "--export-metadata",
            "--profile",
            "ffv1",
            tbc_file,
            output,
        ]
        run(export_cmd)

        # tbc-video-export creates {base}.mkv — rename to {base}.ffv1.mkv
        tbc_output = Path(f"{output}.mkv")
        if tbc_output.is_file() and tbc_output != ffv1_file:
            tbc_output.rename(ffv1_file)

        if not ffv1_file.is_file():
            raise ToolError("FFV1 export failed")
    else:
        print("\nSkipping video export")

    print()
    print("=" * 60)
    print("Export complete!")
    print()
    for label, file in (
        ("Video", ffv1_file),
        ("Linear", Path(linear_opus)),
        ("HiFi", Path(hifi_opus)),
    ):
        if file.is_file():
            print(f"  {label}: {file} ({human_size(file)})")
    print()
    print("Next: Run `vhs-tool encode` to postprocess and encode.")
    print("=" * 60)
    return 0
