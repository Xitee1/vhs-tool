"""vhs-tool audio — end-to-end audio post-processing for VHS-Decode captures.

Port of tools/4_audio.sh.

  HiFi RF FLAC   →  hifi-decode   →  stream-align  →  hifi.aligned.flac   (48 kHz)
  Linear FLAC    →                   stream-align  →  linear.aligned.flac (48 kHz)

Input: a single <base> path (e.g. "./captures/VHS_PAL_Tape_010"). Resolves:

  <base>-hifi.flac    HiFi RF capture input
  <base>-linear.flac  Linear capture input
  <output>/<base>-video.tbc.json   TBC JSON from video decode (required)

  <output>/<base>-hifi.flac             hifi-decode intermediate
  <output>/<base>-hifi.aligned.flac     final HiFi
  <output>/<base>-linear.aligned.flac   final Linear

Everything downstream of `hifi-decode` runs through a pipe chain
(ffmpeg | aaa stream-align | ffmpeg) — zero .raw/.pcm files on disk.

Tapes without a HiFi track are detected from the hifi-decode peak gain: their
decode is kept on disk and reported as a warning at the end of the run, unless
--delete-empty-tracks asks for it to be removed.

Based on https://github.com/oyvindln/vhs-decode/wiki/Auto-Audio-Align
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ..common import ToolError, collapse_base_args, derive_base, log, resolve_binary
from ..config import get_config

_CFG = get_config()

# -- Defaults (config file values, override via env or CLI) -----------------------
OUT_DIR = os.environ.get("OUT_DIR", _CFG.paths.decoded)

AAA_BIN = os.environ.get("AAA_BIN", _CFG.binaries.aaa)
HIFI_DECODE_BIN = os.environ.get("HIFI_DECODE_BIN", _CFG.binaries.hifi_decode)
FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "ffmpeg")

TV_STANDARD = os.environ.get("TV_STANDARD", _CFG.defaults.tv_system)  # pal | ntsc
HIFI_THREADS = os.environ.get("HIFI_THREADS", "14")
HIFI_DECODE_FREQ = os.environ.get("HIFI_DECODE_FREQ", "10")  # hifi-decode --frequency (MHz)
HIFI_AUDIO_RATE = os.environ.get("HIFI_AUDIO_RATE", "48000")  # hifi-decode output rate
LINEAR_RATE = os.environ.get("LINEAR_RATE", "46875")  # clockgen baseband rate
RF_VIDEO_RATE = os.environ.get("RF_VIDEO_RATE", "40000000")  # video RF capture rate

# HiFi signal validation threshold
# hifi-decode reports "Peak gain is X%": real signal typically >10%, noise <2%
HIFI_PEAK_GAIN_MIN = os.environ.get("HIFI_PEAK_GAIN_MIN", "5")

_PEAK_GAIN_RE = re.compile(r"Peak gain is ([0-9.]+)")


# =============================================================================
# Pure helpers (testable)
# =============================================================================


def parse_peak_gain(text: str) -> float | None:
    """Extract the peak gain percentage from hifi-decode output (None if absent)."""
    match = _PEAK_GAIN_RE.search(text)
    return float(match.group(1)) if match else None


def validate_hifi(peak_gain: float | None, minimum: float) -> bool:
    """Decide from hifi-decode's reported peak gain whether a real HiFi signal exists.

    Real recordings produce peak gains well above 10%, while tapes without HiFi
    signal produce <2% (decoder noise floor). An unparsable gain passes through
    unvalidated (with a warning).
    """
    if peak_gain is None:
        log("  WARN: Could not parse peak gain from hifi-decode — passing through unvalidated")
        return True

    log(f"  Peak gain: {peak_gain}% (threshold: ≥{minimum}%)")
    if peak_gain < minimum:
        log(f"  → NO HiFi signal detected (peak gain {peak_gain}% < {minimum}%)")
        return False
    log(f"  → HiFi signal OK (peak gain {peak_gain}%)")
    return True


# =============================================================================
# Pipelines
# =============================================================================


def run_hifi_decode(args: argparse.Namespace, hifi_decode_bin: str, out: Path) -> float | None:
    """Run hifi-decode (RF FLAC → stereo PCM-in-FLAC), echoing its output live.

    Returns the reported peak gain percentage (None if not found / dry run).
    """
    cmd = [
        hifi_decode_bin,
        f"--{args.standard}",
        "--audio_rate", HIFI_AUDIO_RATE,
        "--frequency", HIFI_DECODE_FREQ,
        "--threads", args.threads,
        str(args.hifi_file),
        str(out),
    ]  # fmt: skip
    if args.verbose or args.dry_run:
        print(f">>> {shlex.join(cmd)}", file=sys.stderr)
    if args.dry_run:
        return None

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    output_lines = []
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stderr.write(line)
        output_lines.append(line)
    if proc.wait() != 0:
        raise ToolError(f"hifi-decode exited with code {proc.returncode}")
    return parse_peak_gain("".join(output_lines))


def stream_align(
    args: argparse.Namespace,
    src: Path,
    out: Path,
    stream_rate: str,
    tbc_json: Path,
    *,
    resample_to: str | None = None,
) -> None:
    """Piped alignment: ffmpeg → aaa stream-align → ffmpeg (no temp files)."""
    decode = [
        FFMPEG_BIN, "-hide_banner", "-loglevel", "warning", "-y",
        "-i", str(src),
        "-filter", "channelmap=map=FL-FL|FR-FR",
        "-f", "s24le", "-ac", "2", "-",
    ]  # fmt: skip
    align = [
        args.aaa_bin, "stream-align",
        "--sample-size-bytes", "6",
        "--stream-sample-rate-hz", stream_rate,
        "--json", str(tbc_json),
        "--rf-video-sample-rate-hz", RF_VIDEO_RATE,
    ]  # fmt: skip
    encode = [
        FFMPEG_BIN, "-hide_banner", "-loglevel", "warning", "-y",
        "-f", "s24le", "-ar", stream_rate, "-ac", "2", "-i", "-",
    ]  # fmt: skip
    if resample_to:
        encode += ["-af", f"aresample={resample_to}"]
    encode += ["-c:a", "flac", "-sample_fmt", "s32", str(out)]

    if args.verbose:
        print(
            f">>> {shlex.join(decode)} | {shlex.join(align)} | {shlex.join(encode)}",
            file=sys.stderr,
        )

    decoder = subprocess.Popen(decode, stdout=subprocess.PIPE)
    aligner = subprocess.Popen(align, stdin=decoder.stdout, stdout=subprocess.PIPE)
    decoder.stdout.close()
    encoder = subprocess.Popen(encode, stdin=aligner.stdout)
    aligner.stdout.close()
    returncodes = (
        ("ffmpeg (decode)", decoder.wait()),
        ("aaa stream-align", aligner.wait()),
        ("ffmpeg (encode)", encoder.wait()),
    )
    for name, returncode in returncodes:
        if returncode != 0:
            raise ToolError(f"{name} exited with code {returncode}")


def process_hifi(
    args: argparse.Namespace, hifi_decode_bin: str, paths: dict[str, Path]
) -> str | None:
    """Decode + align HiFi.

    Returns a warning message when the tape carries no HiFi signal (printed
    again at the end of the run), else None.
    """
    log("========== HiFi ==========")
    hifi_decoded = paths["hifi_decoded"]
    peak_gain: float | None = None

    # 1) hifi-decode (RF FLAC → stereo 48 kHz PCM-in-FLAC)
    if args.skip_hifi_decode:
        log("Skipping hifi-decode; treating HiFi input as already-decoded FLAC")
        hifi_decoded = Path(args.hifi_file)
    else:
        log(f"hifi-decode → {hifi_decoded}")
        peak_gain = run_hifi_decode(args, hifi_decode_bin, hifi_decoded)

    # 2) Validate: did hifi-decode find a real signal?
    if not args.skip_hifi_validate and not args.dry_run:
        log("Validating HiFi signal...")
        if not validate_hifi(peak_gain, float(HIFI_PEAK_GAIN_MIN)):
            log("Skipping HiFi alignment — no signal on this tape")
            gain = f"peak gain {peak_gain}%" if peak_gain is not None else "no peak gain reported"
            if args.skip_hifi_decode or not hifi_decoded.is_file():
                return f"No HiFi signal detected ({gain}) — nothing was aligned."
            if args.delete_empty_tracks:
                log(f"Removing noise-only decoded HiFi: {hifi_decoded}")
                hifi_decoded.unlink()
                return f"No HiFi signal detected ({gain}) — decoded HiFi deleted."
            log(f"Keeping noise-only decoded HiFi: {hifi_decoded}")
            return (
                f"No HiFi signal detected ({gain}) — kept {hifi_decoded} for inspection.\n"
                "  Pass --delete-empty-tracks to remove noise-only decodes automatically."
            )

    # 3) Piped alignment: ffmpeg → aaa stream-align → ffmpeg
    hifi_aligned = paths["hifi_aligned"]
    log(f"stream-align (piped) → {hifi_aligned}")
    if args.dry_run:
        log(
            f">>> [ffmpeg -i {hifi_decoded} ... - | {args.aaa_bin} stream-align ... "
            f"| ffmpeg ... {hifi_aligned}]"
        )
    else:
        stream_align(args, hifi_decoded, hifi_aligned, HIFI_AUDIO_RATE, paths["tbc_json"])

    if not args.keep_intermediate and not args.skip_hifi_decode:
        log(f"Removing intermediate {hifi_decoded}")
        if not args.dry_run:
            hifi_decoded.unlink(missing_ok=True)

    log(f"HiFi done → {hifi_aligned}")
    print(file=sys.stderr)
    return None


def process_linear(args: argparse.Namespace, paths: dict[str, Path]) -> None:
    log("========== Linear ==========")
    linear_aligned = paths["linear_aligned"]
    log(f"stream-align (piped, resample {LINEAR_RATE} → 48000) → {linear_aligned}")
    if args.dry_run:
        log(
            f">>> [ffmpeg -i {args.linear_file} ... - | {args.aaa_bin} stream-align ... "
            f"| ffmpeg -af aresample=48000 ... {linear_aligned}]"
        )
    else:
        stream_align(
            args,
            Path(args.linear_file),
            linear_aligned,
            LINEAR_RATE,
            paths["tbc_json"],
            resample_to="48000",
        )
    log(f"Linear done → {linear_aligned}")
    print(file=sys.stderr)


# =============================================================================
# Argument parsing
# =============================================================================


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "audio",
        help="Decode and align HiFi/Linear audio against the video TBC",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "base",
        nargs="+",
        help='Path-prefix to the tape (e.g. "./captures/VHS_PAL_Tape_010"); '
        "a full -hifi.flac / -linear.flac file name or a wildcard over the "
        "capture's files works too",
    )
    parser.add_argument(
        "--output", default=OUT_DIR, help=f"Output / TBC-JSON directory (default: {OUT_DIR})"
    )
    parser.add_argument("--tbc-json", help="Override TBC JSON path")
    parser.add_argument("--hifi-file", help="Override HiFi input path")
    parser.add_argument("--linear-file", help="Override Linear input path")
    parser.add_argument(
        "--threads", default=HIFI_THREADS, help=f"hifi-decode threads (default: {HIFI_THREADS})"
    )
    parser.add_argument(
        "--standard", default=TV_STANDARD, help=f"TV system (pal|ntsc) (default: {TV_STANDARD})"
    )
    parser.add_argument(
        "--aaa-bin", default=AAA_BIN, help=f"vhs-decode-aaa AppImage (default: {AAA_BIN})"
    )
    parser.add_argument(
        "--skip-hifi-decode",
        action="store_true",
        help="Treat HiFi input as already-decoded FLAC",
    )
    parser.add_argument(
        "--skip-hifi-validate",
        action="store_true",
        help="Skip automatic noise detection on decoded HiFi",
    )
    parser.add_argument("--skip-hifi", action="store_true", help="Skip HiFi processing entirely")
    parser.add_argument(
        "--skip-linear", action="store_true", help="Skip Linear processing entirely"
    )
    parser.add_argument(
        "--keep-intermediate",
        action="store_true",
        help="Keep decoded HiFi FLAC after alignment (default: delete)",
    )
    parser.add_argument(
        "--delete-empty-tracks",
        action="store_true",
        help="Delete the decoded HiFi FLAC when no HiFi signal was detected "
        "(default: keep it and warn at the end)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print commands without executing")
    parser.add_argument("-v", "--verbose", action="store_true", help="Echo every executed command")
    parser.set_defaults(func=cmd_audio)


# =============================================================================
# Command
# =============================================================================


def cmd_audio(args: argparse.Namespace) -> int:
    base_path = Path(collapse_base_args(args.base))
    input_dir = base_path.parent
    base = derive_base(base_path.name)
    if not base:
        raise ToolError("Could not derive base name from input")

    out_dir = Path(args.output)

    # Fill file paths if not overridden
    args.hifi_file = args.hifi_file or str(input_dir / f"{base}-hifi.flac")
    args.linear_file = args.linear_file or str(input_dir / f"{base}-linear.flac")
    # Default TBC JSON matches vhs-decode convention "<name>-video.tbc.json"
    # (fall back to the suffix-less form if only that exists).
    if args.tbc_json:
        tbc_json = Path(args.tbc_json)
    elif (out_dir / f"{base}.tbc.json").is_file() and not (
        out_dir / f"{base}-video.tbc.json"
    ).is_file():
        tbc_json = out_dir / f"{base}.tbc.json"
    else:
        tbc_json = out_dir / f"{base}-video.tbc.json"

    paths = {
        "tbc_json": tbc_json,
        "hifi_decoded": out_dir / f"{base}-hifi.flac",
        "hifi_aligned": out_dir / f"{base}-hifi.aligned.flac",
        "linear_aligned": out_dir / f"{base}-linear.aligned.flac",
    }

    # -- Validation ---------------------------------------------------------
    out_dir.mkdir(parents=True, exist_ok=True)

    if not tbc_json.is_file():
        raise ToolError(
            f"TBC JSON not found: {tbc_json}\n"
            "  (hint: run vhs-decode on the video RF first, or pass --tbc-json)"
        )

    # Auto-skip missing streams with a warning
    if not args.skip_hifi and not Path(args.hifi_file).is_file():
        log(f"WARN: HiFi input not found, skipping HiFi: {args.hifi_file}")
        args.skip_hifi = True
    if not args.skip_linear and not Path(args.linear_file).is_file():
        log(f"WARN: Linear input not found, skipping Linear: {args.linear_file}")
        args.skip_linear = True
    if args.skip_hifi and args.skip_linear:
        raise ToolError("Nothing to do - neither HiFi nor Linear input available.")

    # hifi-decode must not write over (and later delete) its own RF input
    if (
        not args.skip_hifi
        and not args.skip_hifi_decode
        and Path(args.hifi_file).resolve() == paths["hifi_decoded"].resolve()
    ):
        raise ToolError(
            f"hifi-decode output would overwrite its RF input: {args.hifi_file}\n"
            "  (the output directory equals the RF input location — pass a different --output)"
        )

    # -- Tool checks ----------------------------------------------------------
    if shutil.which(FFMPEG_BIN) is None:
        raise ToolError(f"ffmpeg not found: {FFMPEG_BIN}")
    hifi_decode_bin = ""
    if not (args.skip_hifi_decode or args.skip_hifi):
        hifi_decode_bin = resolve_binary(HIFI_DECODE_BIN, "hifi-decode")
    if not (Path(args.aaa_bin).is_file() and os.access(args.aaa_bin, os.X_OK)):
        raise ToolError(f"vhs-decode-aaa not executable: {args.aaa_bin}")

    # -- Summary --------------------------------------------------------------
    log(f"Base:          {base}")
    log(f"Output dir:    {out_dir}")
    log(f"TBC JSON:      {tbc_json}")
    if not args.skip_hifi:
        log(f"HiFi in → out: {args.hifi_file} → {paths['hifi_aligned']}")
    if not args.skip_linear:
        log(f"Lin  in → out: {args.linear_file} → {paths['linear_aligned']}")
    print(file=sys.stderr)

    # -- Run ------------------------------------------------------------------
    start_ts = time.time()
    hifi_warning = None
    if not args.skip_hifi:
        hifi_warning = process_hifi(args, hifi_decode_bin, paths)
    if not args.skip_linear:
        process_linear(args, paths)
    log(f"All done in {int(time.time() - start_ts)}s")
    if hifi_warning:
        log(f"WARNING: {hifi_warning}")
    return 0
