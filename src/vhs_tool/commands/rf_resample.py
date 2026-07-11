"""vhs-tool rf-resample — downsample vhs-decode RF captures to save space for archival.

Port of tools/rf-resample.sh.

Input: a single <base> path (e.g. "./captures/VHS_PAL_Tape_010"). Auto-discovers:

  <base>-video.flac          Video RF   (40 MSPS → target, e.g. 20 MSPS)
  <base>-hifi.flac           HiFi RF    (only with --with-hifi; usually already
                                         resampled at capture time)

Linear and headswitch files are not resampled (not RF data / too small to matter).

Output: <base>-video.8bit.20msps.flac  (etc.) — originals are never modified.

Uses flac -8 --blocksize=65535 --lax for optimal output compression.

Requires: sox (with FLAC support), flac (≥1.4)

Ref: https://github.com/oyvindln/vhs-decode/wiki/RF-Compression-&-Decompression-Guide
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from ..common import ToolError, check_deps, derive_base, log, soxi

# -- Defaults ------------------------------------------------------------------
VIDEO_RATE = 20000  # target video rate in FLAC-scale (20000 = 20 MSPS)
VIDEO_CUTOFF = "0-9600"  # sinc lowpass for video
HIFI_RATE = 10000  # target hifi rate in FLAC-scale (10000 = 10 MSPS)
HIFI_CUTOFF = "0-3050"  # sinc lowpass for hifi
SINC_TAPS = 2500  # sinc filter length
FLAC_LEVEL = 8
BLOCKSIZE = 65535

# Preset (video rate, video cutoff) pairs
PRESETS: dict[str, tuple[int, str]] = {
    "pal": (20000, "0-9600"),  # default, safe for PAL VHS
    "pal-min": (18000, "0-8670"),  # bare minimum PAL VHS
    "ntsc": (16000, "0-7650"),  # NTSC VHS
    "svhs": (24000, "0-9400"),  # SVHS/Umatic/SuperBeta
}


# =============================================================================
# Pure helpers (testable)
# =============================================================================


def default_suffix(bps: int, target_rate: int) -> str:
    """Output suffix '.<bps>bit.<rate>msps' (FLAC-scale rate 20000 → '20msps')."""
    return f".{bps}bit.{target_rate // 1000}msps"


# =============================================================================
# Resampling
# =============================================================================


def _pipeline(
    file: Path, out: Path, src_rate: int, bps: int, target_rate: int,
    taps: int, cutoff: str, flac_level: int,
) -> None:  # fmt: skip
    """Decode → resample → lowpass → encode with optimal FLAC settings.

    Encodes to '<out>.part' and renames onto ``out`` only after the whole
    pipeline succeeded, so the final name never carries an incomplete file
    (which the "output exists → skip" logic would otherwise trust on a re-run).
    """
    part = out.with_name(out.name + ".part")
    part.unlink(missing_ok=True)  # stale partial from a previous crash

    decode = [
        "flac", "--silent", "-d", "--force-raw-format",
        "--sign=unsigned", "--endian=little", "--stdout", str(file),
    ]  # fmt: skip
    resample = [
        "sox",
        "-r", str(src_rate), "-b", str(bps), "-c", "1", "-e", "unsigned", "-t", "raw", "-",
        "-b", str(bps), "-r", str(target_rate), "-c", "1", "-e", "unsigned", "-t", "raw", "-",
        "sinc", "-n", str(taps), cutoff,
    ]  # fmt: skip
    encode = [
        "flac", "--silent", f"-{flac_level}", f"--blocksize={BLOCKSIZE}", "--lax",
        f"--sample-rate={target_rate}", "--channels=1", f"--bps={bps}",
        "--sign=unsigned", "--endian=little",
        "-f", "-", "-o", str(part),
    ]  # fmt: skip

    try:
        decoder = subprocess.Popen(decode, stdout=subprocess.PIPE)
        resampler = subprocess.Popen(resample, stdin=decoder.stdout, stdout=subprocess.PIPE)
        decoder.stdout.close()
        encoder = subprocess.Popen(encode, stdin=resampler.stdout)
        resampler.stdout.close()
        returncodes = (
            ("flac (decode)", decoder.wait()),
            ("sox", resampler.wait()),
            ("flac (encode)", encoder.wait()),
        )
        for name, returncode in returncodes:
            if returncode != 0:
                raise ToolError(f"{name} exited with code {returncode}")
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    part.replace(out)


def resample_file(
    file: Path,
    target_rate: int,
    cutoff: str,
    channel: str,
    *,
    taps: int = SINC_TAPS,
    suffix: str = "",
    flac_level: int = FLAC_LEVEL,
    dry_run: bool = False,
    out_dir: Path | None = None,
) -> Path | None:
    """Resample one RF FLAC. Returns the output path, or None if nothing was produced.

    Skips (with a log line) when the file is missing, already at/below the target
    rate, or the output already exists (the existing output path is returned).

    The output is written next to the source by default; pass ``out_dir`` to write
    it elsewhere (e.g. straight into an upload folder, avoiding a copy afterwards).
    """
    check_deps("sox", "soxi", "flac")

    if not file.is_file():
        log(f"  {channel}: not found, skipping")
        return None

    src_rate = soxi(file, "-r")
    bps = soxi(file, "-b")
    samples = soxi(file, "-s")

    log(f"  {file.name}")
    if src_rate == target_rate:
        log(f"    already at {target_rate} Hz, skipping")
        return None
    if src_rate < target_rate:
        log(
            f"    source rate {src_rate} Hz < target {target_rate} Hz, "
            "skipping (upsampling not supported)"
        )
        return None

    out_suffix = suffix or default_suffix(bps, target_rate)
    out = (out_dir or file.parent) / f"{file.name.removesuffix('.flac')}{out_suffix}.flac"
    out_samples = samples * target_rate // src_rate

    log(f"    {src_rate} Hz → {target_rate} Hz ({target_rate / src_rate * 100:.1f}%)")
    log(f"    sinc -n {taps} {cutoff}")
    log(f"    samples: {samples} → ~{out_samples}")
    log(f"    → {out.name}")

    if out.is_file():
        log("    WARNING: output exists, skipping")
        return out
    if dry_run:
        log("    (dry-run)")
        return None

    _pipeline(file, out, src_rate, bps, target_rate, taps, cutoff, flac_level)

    out_size = out.stat().st_size
    src_size = file.stat().st_size
    saved_pct = (1 - out_size / src_size) * 100
    log(f"    ✓ done — {out_size / 1073741824:.1f} GiB ({saved_pct:.1f}% smaller)")
    return out


# =============================================================================
# Argument parsing
# =============================================================================


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "rf-resample",
        help="Downsample RF captures for space-efficient archival",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "base",
        help='Path-prefix to the tape (e.g. "./captures/VHS_PAL_Tape_010"); '
        "a full file name of any channel works too",
    )
    parser.add_argument(
        "--preset",
        choices=sorted(PRESETS),
        default="pal",
        help="Preset video rate and cutoff: pal 20 MSPS (default), pal-min 18, ntsc 16, svhs 24",
    )
    parser.add_argument(
        "--vrate",
        type=int,
        help="Video target rate, FLAC-scale (default: from preset)",
    )
    parser.add_argument("--vcutoff", help="Video sinc cutoff (default: from preset)")
    parser.add_argument(
        "--hrate",
        type=int,
        default=HIFI_RATE,
        help=f"HiFi target rate, FLAC-scale (default: {HIFI_RATE})",
    )
    parser.add_argument(
        "--hcutoff", default=HIFI_CUTOFF, help=f"HiFi sinc cutoff (default: {HIFI_CUTOFF})"
    )
    parser.add_argument(
        "--taps", type=int, default=SINC_TAPS, help=f"Sinc filter taps (default: {SINC_TAPS})"
    )
    parser.add_argument(
        "--with-hifi",
        action="store_true",
        help="Also resample HiFi (off by default — HiFi is normally already "
        "resampled at capture time)",
    )
    parser.add_argument("--hifi-only", action="store_true", help="Resample only HiFi (skip video)")
    parser.add_argument(
        "--suffix", default="", help="Output suffix (default: .<bps>bit.<rate>msps)"
    )
    parser.add_argument(
        "--flac-level",
        type=int,
        default=FLAC_LEVEL,
        help=f"FLAC compression level (default: {FLAC_LEVEL})",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print what would be done without executing"
    )
    parser.set_defaults(func=cmd_rf_resample)


# =============================================================================
# Command
# =============================================================================


def cmd_rf_resample(args: argparse.Namespace) -> int:
    check_deps("sox", "soxi", "flac")

    preset_rate, preset_cutoff = PRESETS[args.preset]
    video_rate = args.vrate if args.vrate is not None else preset_rate
    video_cutoff = args.vcutoff if args.vcutoff is not None else preset_cutoff
    with_video = not args.hifi_only
    with_hifi = args.with_hifi or args.hifi_only

    base_path = Path(args.base)
    input_dir = base_path.parent
    base = derive_base(base_path.name)
    if not base:
        raise ToolError("Could not derive base name from input")

    log(f"Base:    {base}")
    log(f"Dir:     {input_dir}")

    print(file=sys.stderr)
    log("════════════════════════════════════════════")
    log("  rf-resample — RF downsampler")
    log("════════════════════════════════════════════")
    log()
    if with_video:
        log(f"  Video:  {video_rate} Hz (sinc {video_cutoff})")
    if with_hifi:
        log(f"  HiFi:   {args.hrate} Hz (sinc {args.hcutoff})")
    log(f"  Output: flac -{args.flac_level} --blocksize={BLOCKSIZE} --lax")
    log()

    options = {
        "taps": args.taps,
        "suffix": args.suffix,
        "flac_level": args.flac_level,
        "dry_run": args.dry_run,
    }
    if with_video:
        resample_file(
            input_dir / f"{base}-video.flac", video_rate, video_cutoff, "video", **options
        )
    if with_hifi:
        resample_file(input_dir / f"{base}-hifi.flac", args.hrate, args.hcutoff, "hifi", **options)

    print(file=sys.stderr)
    log("Done.")
    return 0
