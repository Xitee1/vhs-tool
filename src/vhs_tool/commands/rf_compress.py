"""vhs-tool rf-compress — losslessly compress raw RF captures to FLAC.

Port of captures/rf-compress.sh.

Input: a directory, scanned non-recursively for raw capture files, or a single
raw file. Format parameters are derived from the extension:

  .u8  / .r8     8-bit unsigned
  .u16          16-bit unsigned
  .s16 / .r16   16-bit signed

Each file is encoded to `<name>.flac` with the settings that are optimal for RF
data, the FLAC is decoded again and compared to the original via MD5, and only
after that verification is the raw file removed (`--keep-raw` keeps it).

Files that already have a `.flac` sibling are skipped, and the FLAC only takes
its final name once it is complete and verified — so re-runs are safe.

Requires: flac (≥1.4; ≥1.5 for multi-threaded encoding)

Ref: the wiki recommends the FLAC CLI (not FFmpeg) for RF data:
  https://github.com/oyvindln/vhs-decode/wiki/RF-Compression-&-Decompression-Guide

Note: FLAC level 8 is optimal for RF data. Levels 9+ (--lax high-order) cause
~42% file bloat due to rejected LPC predictions on RF signals.
See: https://github.com/harrypm/Scripts/issues/2
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

from ..common import ToolError, check_deps, log, run

# -- Defaults ------------------------------------------------------------------
FLAC_LEVEL = 8
BLOCKSIZE = 65535
RATE = 40000  # 40 MSPS → stored as 40000 Hz (FLAC-scale, 1000:1)
CHANNELS = 1
ENDIAN = "little"
MAX_THREADS = 128  # flac's own upper limit for --threads
CHUNK = 1 << 20  # MD5 read size

# Raw capture extensions in scan order → (bits per sample, sign).
RAW_FORMATS: dict[str, tuple[int, str]] = {
    "u8": (8, "unsigned"),
    "r8": (8, "unsigned"),
    "u16": (16, "unsigned"),
    "s16": (16, "signed"),
    "r16": (16, "signed"),
}

_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


class FlacVersion(NamedTuple):
    """Parsed `flac --version` output."""

    text: str  # as printed, e.g. "1.5.0" ("unknown" if unparsable)
    major: int
    minor: int

    @property
    def supports_threads(self) -> bool:
        """flac gained multi-threaded encoding (--threads) in 1.5.0."""
        return (self.major, self.minor) >= (1, 5)


class CompressResult(NamedTuple):
    status: str  # "compressed" | "skipped" | "error"
    raw_size: int = 0
    flac_size: int = 0


# =============================================================================
# Pure helpers (testable)
# =============================================================================


def human_bytes(size: int) -> str:
    """Byte count as a human-readable string (GiB/MiB/KiB)."""
    if size >= 1073741824:
        return f"{size / 1073741824:.2f} GiB"
    if size >= 1048576:
        return f"{size / 1048576:.1f} MiB"
    return f"{size / 1024:.0f} KiB"


def detect_format(ext: str) -> tuple[int, str] | None:
    """(bps, sign) for a raw capture extension, or None if unknown."""
    return RAW_FORMATS.get(ext.lstrip(".").lower())


def parse_flac_version(output: str) -> FlacVersion:
    """Parse the first line of `flac --version` (e.g. 'flac 1.5.0')."""
    first_line = output.splitlines()[0] if output.strip() else ""
    match = _VERSION_RE.search(first_line)
    if not match:
        return FlacVersion("unknown", 0, 0)
    return FlacVersion(match.group(0), int(match.group(1)), int(match.group(2)))


def resolve_threads(requested: int | None, version: FlacVersion) -> int:
    """FLAC encoder thread count; 0 means "don't pass --threads at all".

    Without an explicit request all cores are used (capped at flac's limit).
    On a flac older than 1.5.0 the flag does not exist, so it is dropped —
    with a warning when it was asked for explicitly.
    """
    if not version.supports_threads:
        if requested:
            log(
                f"WARNING: --threads requested but FLAC {version.text} < 1.5.0 — "
                "threading not available"
            )
        return 0
    if requested is not None:
        return max(requested, 0)
    return min(os.cpu_count() or 1, MAX_THREADS)


def flac_encode_cmd(
    file: Path, out: Path, *, bps: int, sign: str, rate: int, channels: int,
    blocksize: int, level: int, threads: int,
) -> list[str]:  # fmt: skip
    """flac command line encoding one raw capture with RF-optimal settings."""
    cmd = ["flac", "--silent", f"-{level}"]
    if threads:
        cmd.append(f"--threads={threads}")
    cmd += [
        f"--blocksize={blocksize}", "--lax",
        f"--sample-rate={rate}", f"--channels={channels}", f"--bps={bps}",
        f"--sign={sign}", f"--endian={ENDIAN}",
        "-f", str(file), "-o", str(out),
    ]  # fmt: skip
    return cmd


def find_raw_files(directory: Path) -> list[Path]:
    """Raw capture files directly in `directory` (no recursion), grouped by extension."""
    files: list[Path] = []
    for ext in RAW_FORMATS:
        files.extend(sorted(p for p in directory.glob(f"*.{ext}") if p.is_file()))
    return files


# =============================================================================
# MD5 verification
# =============================================================================


def md5_file(file: Path) -> str:
    """MD5 of a file's bytes."""
    digest = hashlib.md5()
    with open(file, "rb") as f:
        while chunk := f.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def md5_flac_raw(file: Path, *, sign: str) -> str:
    """MD5 of the raw sample data decoded back out of a FLAC.

    Decodes to stdout with --force-raw-format so the result is byte-comparable
    with the original capture (no WAV header, same sign/endianness).
    """
    cmd = [
        "flac", "--silent", "-d", "--force-raw-format",
        f"--sign={sign}", f"--endian={ENDIAN}", "--stdout", str(file),
    ]  # fmt: skip
    digest = hashlib.md5()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    try:
        while chunk := proc.stdout.read(CHUNK):
            digest.update(chunk)
    except BaseException:
        proc.kill()
        raise
    finally:
        proc.stdout.close()
        returncode = proc.wait()
    if returncode != 0:
        raise ToolError(f"flac (decode) exited with code {returncode}")
    return digest.hexdigest()


# =============================================================================
# Compression
# =============================================================================


def compress_file(
    file: Path,
    *,
    bps: int | None = None,
    sign: str | None = None,
    rate: int = RATE,
    channels: int = CHANNELS,
    blocksize: int = BLOCKSIZE,
    flac_level: int = FLAC_LEVEL,
    threads: int = 0,
    keep_raw: bool = False,
    dry_run: bool = False,
    verbose: bool = False,
) -> CompressResult:
    """Compress one raw capture to FLAC, verify it via MD5, drop the original.

    `bps`/`sign` override the values auto-detected from the extension (both are
    needed for an extension this tool does not know). The FLAC is written to a
    `.part` file and only renamed onto `<name>.flac` after the MD5 of the
    decoded data matched the original — a crashed or mismatching run therefore
    never leaves a file that the next run would skip as "already compressed".
    """
    out = file.with_suffix(".flac")
    log(f"── {file.name}")

    if out.is_file():
        log(f"    → FLAC already exists: {out.name}, skipping")
        return CompressResult("skipped")

    detected = detect_format(file.suffix)
    if bps is None or sign is None:
        if detected is None:
            log(f"    → cannot auto-detect format for {file.suffix}, skipping (use --bps/--sign)")
            return CompressResult("skipped")
        bps = detected[0] if bps is None else bps
        sign = detected[1] if sign is None else sign

    raw_size = file.stat().st_size
    log(f"    → raw size: {human_bytes(raw_size)}")
    log(f"    → format: {bps}-bit {sign}, {channels}ch, {rate} Hz")

    if dry_run:
        log(f"    → [dry run] would compress to {out.name}")
        return CompressResult("compressed")

    part = out.with_name(out.name + ".part")
    part.unlink(missing_ok=True)  # stale partial from a previous crash

    # -- 1. Compress raw → FLAC ------------------------------------------------
    cmd = flac_encode_cmd(
        file, part, bps=bps, sign=sign, rate=rate, channels=channels,
        blocksize=blocksize, level=flac_level, threads=threads,
    )  # fmt: skip
    log(f"    → compressing with flac -{flac_level}" + (f" --threads={threads}" if threads else ""))
    if verbose:
        print(f"  $ {shlex.join(cmd)}", file=sys.stderr)
    try:
        run(cmd, check=True)
    except ToolError as exc:
        log(f"    ✗ flac encoding failed ({exc}), skipping")
        part.unlink(missing_ok=True)
        return CompressResult("error")
    except BaseException:
        part.unlink(missing_ok=True)
        raise

    flac_size = part.stat().st_size
    ratio = flac_size / raw_size * 100 if raw_size else 0.0
    log(
        f"    → FLAC size: {human_bytes(flac_size)} ({ratio:.1f}% of raw, "
        f"saved {human_bytes(raw_size - flac_size)})"
    )

    # -- 2. Verify the decoded FLAC is bit-identical to the raw input -----------
    log("    → verifying MD5 ...")
    try:
        hash_orig = md5_file(file)
        hash_new = md5_flac_raw(part, sign=sign)
    except ToolError as exc:
        log(f"    ✗ verification failed ({exc}), keeping original")
        part.unlink(missing_ok=True)
        return CompressResult("error")
    except BaseException:
        part.unlink(missing_ok=True)
        raise

    log(f"      original: {hash_orig}")
    log(f"      decoded:  {hash_new}")
    if hash_orig != hash_new:
        log("    ✗ MD5 MISMATCH — keeping original, removing bad FLAC")
        part.unlink(missing_ok=True)
        return CompressResult("error")
    log("    ✓ MD5 verified")

    # -- 3. Finalize -----------------------------------------------------------
    part.replace(out)
    if keep_raw:
        log("    → keeping raw file (--keep-raw)")
    else:
        file.unlink()
        log("    → raw file removed")
    return CompressResult("compressed", raw_size, flac_size)


# =============================================================================
# Argument parsing
# =============================================================================


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "rf-compress",
        help="Losslessly compress raw RF captures to FLAC (MD5-verified)",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "path",
        help='Directory to scan (e.g. "./captures") or a single raw capture file',
    )
    parser.add_argument(
        "-l",
        "--level",
        type=int,
        default=FLAC_LEVEL,
        help=f"FLAC compression level (default: {FLAC_LEVEL} — optimal for RF)",
    )
    parser.add_argument(
        "-b",
        "--blocksize",
        type=int,
        default=BLOCKSIZE,
        help=f"FLAC blocksize (default: {BLOCKSIZE})",
    )
    parser.add_argument(
        "-r",
        "--rate",
        type=int,
        default=RATE,
        help=f"Sample rate in Hz, FLAC-scale (default: {RATE} = 40 MSPS)",
    )
    parser.add_argument("--bps", type=int, help="Bits per sample (default: auto from extension)")
    parser.add_argument(
        "--sign",
        choices=("unsigned", "signed"),
        help="Sample sign (default: auto from extension)",
    )
    parser.add_argument(
        "--channels", type=int, default=CHANNELS, help=f"Number of channels (default: {CHANNELS})"
    )
    parser.add_argument(
        "-t",
        "--threads",
        type=int,
        help="FLAC encoder threads (default: all cores, needs FLAC ≥ 1.5.0)",
    )
    parser.add_argument(
        "--keep-raw", action="store_true", help="Keep the original raw file after compression"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print what would be done without executing"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Echo every executed command")
    parser.set_defaults(func=cmd_rf_compress)


# =============================================================================
# Command
# =============================================================================


def cmd_rf_compress(args: argparse.Namespace) -> int:
    check_deps("flac")

    path = Path(args.path)
    if path.is_dir():
        files = find_raw_files(path)
    elif path.is_file():
        files = [path]
    else:
        raise ToolError(f"Not a file or directory: {path}")

    version = parse_flac_version(run(["flac", "--version"], capture=True, check=False).stdout)
    threads = resolve_threads(args.threads, version)
    if threads > 1:
        log(f"FLAC {version.text} — multi-threaded encoding with {threads} threads")
    else:
        log(f"FLAC {version.text} — single-threaded encoding")

    if not files:
        log(f"No raw capture files found in: {path}")
        log("  (looked for: " + ", ".join(f"*.{ext}" for ext in RAW_FORMATS) + ")")
        return 0

    if path.is_dir():
        log(f"Found {len(files)} raw capture file(s) in: {path}")
    else:
        log(f"Compressing a single file: {path}")
    if args.dry_run:
        log("[DRY RUN — no files will be modified]")
    print(file=sys.stderr)

    counts = {"compressed": 0, "skipped": 0, "error": 0}
    total_raw = total_flac = 0
    for file in files:
        result = compress_file(
            file,
            bps=args.bps,
            sign=args.sign,
            rate=args.rate,
            channels=args.channels,
            blocksize=args.blocksize,
            flac_level=args.level,
            threads=threads,
            keep_raw=args.keep_raw,
            dry_run=args.dry_run,
            verbose=args.verbose,
        )
        counts[result.status] += 1
        total_raw += result.raw_size
        total_flac += result.flac_size

    print(file=sys.stderr)
    log(
        f"Done. {counts['compressed']} file(s) compressed, {counts['skipped']} skipped, "
        f"{counts['error']} error(s)."
    )
    if total_raw > 0:
        log(f"  Raw total:  {human_bytes(total_raw)}")
        log(f"  FLAC total: {human_bytes(total_flac)} ({total_flac / total_raw * 100:.1f}%)")
        log(f"  Saved:      {human_bytes(total_raw - total_flac)}")
    return 1 if counts["error"] else 0
