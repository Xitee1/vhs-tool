"""vhs-tool rf-trim — trim noise from the end of synchronized vhs-decode RF captures.

Port of tools/rf-trim.sh.

Input: a single <base> path (e.g. "./captures/VHS_PAL_Tape_010"). Auto-discovers:

  <base>-video.flac          Video RF   (required — used as sync reference)
  <base>-hifi.flac           HiFi RF    (optional)
  <base>-linear.flac         Linear     (optional)
  <base>-headswitch.flac|.u8 Headswitch (optional; .flac preferred, .u8 fallback)

Two modes (exactly one is required):
  --end 01:23:45    Keep content up to this timestamp (trim everything after)
  --trim 120        Trim 120 real seconds from the end

All files are trimmed proportionally by sample count so they stay in sync
regardless of differing sample rates (40 MSPS video, 10 MSPS hifi, 46875 Hz
linear ...).

Output: the trimmed result takes the original file name; the untouched original
is preserved alongside it as `<file>.bak`. Pass --delete-original to remove it.

Trimming a FLAC is "keep the first N samples", which in the decoded raw domain
is a plain byte cut — so the trimmed file is written by decoding the source,
cutting the stream and re-encoding it (see vhs_tool.flac), exactly like the
`.u8` path has always worked. That keeps RF captures at their RF-optimal
settings and linear audio a subset stream, and lets the result carry the same
verified-level tag `rf-compress` writes. Verification is free: the MD5 of the
bytes streamed into the encoder must match the STREAMINFO MD5 it wrote.

Requires: flac + metaflac (≥1.5 for multi-threaded encoding), sox/soxi
(sample-rate and fallback sample counting)

Ref: the wiki recommends the FLAC CLI (not FFmpeg) for RF data:
  https://github.com/oyvindln/vhs-decode/wiki/RF-Compression-&-Decompression-Guide
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from ..common import ToolError, check_deps, collapse_base_args, derive_base, log, run, soxi
from ..flac import (
    CHUNK,
    FLAC_LEVEL,
    FlacInfo,
    detect_threads,
    encode_settings,
    flac_decode_raw_cmd,
    flac_encode_cmd,
    read_flac_info,
    set_level_tag,
)

# -- Defaults ------------------------------------------------------------------
RF_SCALE = 1000  # FLAC kHz → real MHz multiplier (standard: 1000)
OFFSET = 4  # seconds added to --end to compensate vhs-decode lock-in

_NUM_RE = re.compile(r"^[0-9]*\.?[0-9]+$")

# Channel discovery: (channel, required, extensions-in-preference-order).
# .flac is preferred for headswitch, with .u8 (raw unsigned 8-bit) as fallback.
_DISCOVER = (
    ("video", True, ("flac",)),
    ("hifi", False, ("flac",)),
    ("linear", False, ("flac",)),
    ("headswitch", False, ("flac", "u8")),
)


# =============================================================================
# Pure helpers (testable)
# =============================================================================


def parse_timestamp(ts: str) -> float:
    """Parse HH:MM:SS[.x], MM:SS[.x], or plain seconds into a float of seconds."""
    if ":" in ts:
        parts = ts.split(":")
        try:
            if len(parts) == 2:  # MM:SS
                seconds = int(parts[0]) * 60 + float(parts[1])
            elif len(parts) == 3:  # HH:MM:SS
                seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
            else:
                raise ValueError
        except ValueError:
            raise ToolError(
                f"Invalid timestamp format: {ts} (use HH:MM:SS, MM:SS, or seconds)"
            ) from None
    else:
        if not _NUM_RE.match(ts):
            raise ToolError(f"Invalid time value: {ts} (must be a positive number)")
        seconds = float(ts)
    if seconds < 0:
        raise ToolError(f"Invalid time value: {ts} (must be a positive number)")
    return seconds


def fmt_timestamp(total: float) -> str:
    """Seconds → HH:MM:SS[.fff] for display (fractional part shown only if present)."""
    int_sec = int(total)
    frac = total - int_sec
    h, m, s = int_sec // 3600, (int_sec % 3600) // 60, int_sec % 60
    base = f"{h:02d}:{m:02d}:{s:02d}"
    if frac > 0:
        return base + f"{frac:.3f}".lstrip("0").rstrip("0")
    return base


def resolve_mode(end: str | None, trim: str | None) -> tuple[str, str]:
    """Resolve (mode, time_value) from the --end/--trim flags. Exactly one is required."""
    if end is not None and trim is not None:
        raise ToolError("Use only one of --end or --trim")
    if end is not None:
        return "end", end
    if trim is not None:
        return "trim", trim
    raise ToolError("A time value is required: use --end <timestamp> or --trim <seconds>")


def compute_trim(
    mode: str,
    time_seconds: float,
    ref_samples: int,
    ref_rate: int,
    *,
    rf_scale: int = RF_SCALE,
    offset: float = OFFSET,
) -> dict:
    """Compute the keep fraction (and friends) shared by every channel.

    Returns a dict with real_duration, keep_seconds, trim_seconds, keep_fraction.
    Raises ToolError when the request leaves nothing (or everything) to keep.
    """
    real_duration = ref_samples / (ref_rate * rf_scale)

    if mode == "end":
        # --end is decoded-video time; add OFFSET so the decoded video ends at
        # the requested timestamp (the RF capture extends a few seconds further).
        keep_seconds = time_seconds + offset
        trim_seconds = real_duration - keep_seconds
    else:
        # --trim works directly in RF time; no offset applied.
        trim_seconds = time_seconds
        keep_seconds = real_duration - trim_seconds

    if keep_seconds <= 0:
        if mode == "end":
            raise ToolError(
                f"End point {fmt_timestamp(time_seconds)} exceeds total duration "
                f"{fmt_timestamp(real_duration)}"
            )
        raise ToolError(
            f"Trim {time_seconds}s exceeds total duration {fmt_timestamp(real_duration)}"
        )
    if trim_seconds <= 0:
        raise ToolError(
            f"Nothing to trim — keep point {fmt_timestamp(keep_seconds)} is at or beyond "
            f"total duration {fmt_timestamp(real_duration)}"
        )

    keep_samples_ref = int(keep_seconds * ref_rate * rf_scale)
    return {
        "real_duration": real_duration,
        "keep_seconds": keep_seconds,
        "trim_seconds": trim_seconds,
        "keep_fraction": keep_samples_ref / ref_samples,
    }


# =============================================================================
# I/O helpers
# =============================================================================


def get_samples(file: Path) -> int:
    """Robust sample count for a FLAC capture.

    FLAC files written by local-capture.sh sometimes carry total_samples=0 in the
    STREAMINFO block, making `soxi -s` return 0. Fall back through ffprobe
    (duration × rate) and finally `sox … stat` (a full scan, slow but always
    correct) before giving up.

    Note: ffprobe only helps when the FLAC carries a duration in its metadata —
    for headerless captures (total_samples=0) it returns nothing, so the full
    scan is unavoidable. Re-encoding the FLAC with a correct header fixes it.
    """
    try:
        samples = soxi(file, "-s")
        if samples > 0:
            return samples
    except (ToolError, ValueError):
        pass

    if shutil.which("ffprobe") is not None:
        try:
            from ..common import video_duration

            rate = soxi(file, "-r")
            samples = int(video_duration(file) * rate)
            if samples > 0:
                return samples
        except (ToolError, ValueError):
            pass

    log(
        f"  {file.name}: sample count is missing from the FLAC header — counting "
        "it by reading the whole file (slow: minutes for a large RF capture). "
        "Re-encoding the FLAC with a correct header avoids this."
    )
    result = run(["sox", file, "-n", "stat"], capture=True, check=False)
    for line in result.stderr.splitlines():
        if line.startswith("Samples read"):
            samples = int(line.split()[-1])
            if samples > 0:
                return samples

    raise ToolError(
        f"Could not determine sample count for {file.name} — tried soxi -s, ffprobe, "
        "and sox stat (all returned 0/empty). The FLAC may be corrupt or unreadable."
    )


def _copy_head(src: Path, dst: Path, nbytes: int) -> None:
    """Copy the first nbytes bytes of src to dst (equivalent of `head -c`)."""
    remaining = nbytes
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while remaining > 0:
            chunk = fin.read(min(remaining, CHUNK))
            if not chunk:
                break
            fout.write(chunk)
            remaining -= len(chunk)


def encode_head(
    file: Path, out: Path, info: FlacInfo, keep_samples: int, *,
    level: int, blocksize: int, lax: bool, threads: int = 0,
    dry_run: bool = False, verbose: bool = False,
) -> None:  # fmt: skip
    """Write the first `keep_samples` of `file` to `out` as a fresh FLAC.

    Trimming is a byte cut in the decoded raw domain, so the file is decoded,
    the stream is cut and the head is re-encoded — which is what lets the
    result use settings chosen for the data instead of whatever the writing
    tool defaults to.

    Verification comes for free: every byte passes through here anyway, so the
    MD5 of what was fed to the encoder is compared against the STREAMINFO MD5
    the encoder computed, proving the trimmed file holds exactly those samples.
    """
    dec_cmd = flac_decode_raw_cmd(file)
    enc_cmd = flac_encode_cmd(
        "-", out, bps=info.bps, sign="signed", rate=info.sample_rate,
        channels=info.channels, blocksize=blocksize, level=level, threads=threads, lax=lax,
    )  # fmt: skip
    remaining = keep_samples * info.bytes_per_sample
    if verbose or dry_run:
        print(
            f"  $ {shlex.join(dec_cmd)} | head -c {remaining} | {shlex.join(enc_cmd)}",
            file=sys.stderr,
        )
    if dry_run:
        return

    digest = hashlib.md5()
    truncated = False
    decoder = subprocess.Popen(dec_cmd, stdout=subprocess.PIPE)
    try:
        encoder = subprocess.Popen(enc_cmd, stdin=subprocess.PIPE)
    except BaseException:
        decoder.kill()
        decoder.wait()
        raise
    try:
        while remaining > 0:
            chunk = decoder.stdout.read(min(CHUNK, remaining))
            if not chunk:
                truncated = True
                break
            digest.update(chunk)
            try:
                encoder.stdin.write(chunk)
            except BrokenPipeError:  # encoder died; its exit code reports why
                break
            remaining -= len(chunk)
    finally:
        with contextlib.suppress(OSError):
            encoder.stdin.close()
        # The decoder is stopped on purpose once the head is read, so its own
        # exit code carries no meaning here.
        decoder.terminate()
        decoder.stdout.close()
        decoder.wait()
        encoder_rc = encoder.wait()

    if encoder_rc != 0:
        raise ToolError(f"flac (encode) exited with code {encoder_rc}")
    if truncated:
        raise ToolError(
            f"source ended {remaining} byte(s) before the requested sample count — "
            "the trim point does not fit the file"
        )
    written_md5 = read_flac_info(out).md5
    if written_md5 != digest.hexdigest():
        raise ToolError("MD5 mismatch between the streamed samples and the written FLAC")


# =============================================================================
# Trimming
# =============================================================================


def work_paths(file: Path) -> tuple[Path, Path]:
    """(tmp, bak) working paths trim_file uses for `file`.

    The tmp name inserts ".trimmed" before the extension so sox infers the
    output format; the backup simply appends ".bak".
    """
    tmp = file.with_name(f"{file.stem}.trimmed{file.suffix}")
    bak = file.with_name(f"{file.name}.bak")
    return tmp, bak


def trim_file(
    file: Path,
    keep_fraction: float,
    *,
    known_samples: int | None = None,
    delete_original: bool = False,
    flac_level: int = FLAC_LEVEL,
    threads: int = 0,
    dry_run: bool = False,
    verbose: bool = False,
) -> bool:
    """Trim one capture file in-place to keep_fraction of its content.

    Returns True on success, False if the file was skipped (unsupported extension
    or a stale temp/backup file already present). Pass known_samples to reuse an
    already-computed FLAC sample count and skip a second (potentially slow) scan.
    """
    ext = file.suffix.lower()
    info: FlacInfo | None = None
    if ext == ".flac":
        info = read_flac_info(file)
        total = known_samples if known_samples is not None else get_samples(file)
        rate = info.sample_rate
        unit = "samples"
    elif ext == ".u8":
        total = file.stat().st_size  # raw unsigned 8-bit: 1 byte = 1 sample
        rate = None
        unit = "bytes"
    else:
        log(f"  WARNING: unsupported extension for {file.name} — skipping")
        return False

    # Same fraction → same real time, regardless of unit.
    keep = int(total * keep_fraction)

    tmp, bak = work_paths(file)

    log(f"  {file.name}")
    if rate is not None:
        log(f"    rate={rate} Hz  {unit}: {total} → {keep}")
    else:
        log(f"    {unit}: {total} → {keep}")

    # Defensive second layer — cmd_trim pre-flights the whole set before touching
    # anything, but trim_file may be called on its own.
    if tmp.exists():
        log(f"    WARNING: stale temp file exists ({tmp.name}) — skipping")
        return False
    if bak.exists():
        log(f"    WARNING: backup file already exists ({bak.name}) — skipping")
        return False

    # 1. Write trimmed output to a temp file (original untouched until success).
    #    A failed/interrupted write must not leave a partial tmp behind — it
    #    would poison the next run.
    if ext == ".flac":
        assert info is not None  # set together with ext == ".flac" above
        blocksize, lax = encode_settings(file, info)
        log(
            f"    {'RF' if lax else 'audio'}: flac -{flac_level} --blocksize={blocksize}"
            + (" --lax" if lax else "")
            + (f" --threads={threads}" if threads else "")
        )
        try:
            encode_head(
                file, tmp, info, keep,
                level=flac_level, blocksize=blocksize, lax=lax, threads=threads,
                dry_run=dry_run, verbose=verbose,
            )  # fmt: skip
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        if not dry_run:
            # The re-encode is a fresh file at the target level, so record that
            # the same way rf-compress does — no stale tag can survive here.
            set_level_tag(tmp, flac_level)
    else:  # .u8
        if verbose or dry_run:
            print(f"  $ head -c {keep} {file} > {tmp}", file=sys.stderr)
        if not dry_run:
            try:
                _copy_head(file, tmp, keep)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise

    if dry_run:
        log("    (dry-run)")
        return True

    # 2. Atomic swap: original → .bak, trimmed → original name.
    file.rename(bak)
    tmp.rename(file)

    # 3. The original stays as .bak. With --delete-original the caller removes
    #    the backups only after ALL files completed, so a partial run stays
    #    recoverable.
    if delete_original:
        log(f"    original kept for now: {bak.name} (deleted once all files succeed)")
    else:
        log(f"    original kept: {bak.name}")
    return True


# =============================================================================
# Argument parsing
# =============================================================================


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "rf-trim",
        help="Trim noise from the end of synchronized RF captures",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "base",
        nargs="+",
        help='Path-prefix to the tape (e.g. "./captures/VHS_PAL_Tape_010"); '
        "a full file name of any channel or a wildcard over the capture's files works too",
    )
    parser.add_argument(
        "--end",
        metavar="TIME",
        help="Keep content up to this end point (decoded-video time; HH:MM:SS, MM:SS, "
        f"or seconds). --offset ({OFFSET}s) is added to compensate vhs-decode lock-in.",
    )
    parser.add_argument(
        "--trim",
        metavar="SECS",
        help="Remove this many seconds from the end (raw RF time; --offset ignored).",
    )
    parser.add_argument(
        "--delete-original",
        action="store_true",
        help="Delete the original after trimming (default: kept as <file>.bak)",
    )
    parser.add_argument(
        "--offset",
        type=float,
        default=OFFSET,
        help=f"Seconds added to --end to compensate vhs-decode lock-in (default: {OFFSET}). "
        "Set to 0 to work in raw RF time.",
    )
    parser.add_argument(
        "--rf-scale",
        type=int,
        default=RF_SCALE,
        help="Multiplier from the FLAC-header sample rate to the real RF rate "
        f"(default: {RF_SCALE}). vhs-decode stores the RF rate in kHz in the FLAC "
        "header (e.g. 40000 Hz = 40 MSPS), so real samples/s = FLAC rate × this "
        "value. Used to convert your --end/--trim seconds into a sample count and "
        "to report the true capture duration; the standard captures need 1000.",
    )
    parser.add_argument(
        "-t",
        "--threads",
        type=int,
        help="FLAC encoder threads (default: all cores, needs FLAC ≥ 1.5.0)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print what would be done without executing"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Echo every executed command")
    parser.set_defaults(func=cmd_trim)


# =============================================================================
# Command
# =============================================================================


def cmd_trim(args: argparse.Namespace) -> int:
    check_deps("flac", "metaflac", "sox", "soxi")

    mode, time_value = resolve_mode(args.end, args.trim)
    time_seconds = parse_timestamp(time_value)

    base_path = Path(collapse_base_args(args.base))
    input_dir = base_path.parent
    base = derive_base(base_path.name)
    if not base:
        raise ToolError("Could not derive base name from input")

    # -- Discover files --------------------------------------------------------
    ref_file: Path | None = None
    trim_files: list[Path] = []
    for channel, required, exts in _DISCOVER:
        candidate = next(
            (c for ext in exts if (c := input_dir / f"{base}-{channel}.{ext}").is_file()),
            None,
        )
        if candidate is None:
            if required:
                joined = ",".join(exts)
                raise ToolError(
                    f"Required file not found: {input_dir / f'{base}-{channel}.{{{joined}}}'}"
                )
            continue
        trim_files.append(candidate)
        if channel == "video":
            ref_file = candidate

    assert ref_file is not None  # video is required, so always set above

    log(f"Base:    {base}")
    log(f"Dir:     {input_dir}")
    log(f"Found:   {len(trim_files)} file(s)")
    for f in trim_files:
        log(f"  · {f.name}")

    # -- Pre-flight: refuse to touch ANY file while stale temp/backup files exist.
    # Trimming only part of the set would silently desynchronize the channels.
    stale = [p for f in trim_files for p in work_paths(f) if p.exists()]
    if stale:
        listing = "\n".join(f"  {p}" for p in stale)
        raise ToolError(
            "Stale temp/backup file(s) from a previous run found — remove or restore "
            f"them first; no file was modified:\n{listing}"
        )

    # -- Calculate keep fraction from the reference ----------------------------
    ref_samples = get_samples(ref_file)
    ref_rate = soxi(ref_file, "-r")
    result = compute_trim(
        mode, time_seconds, ref_samples, ref_rate, rf_scale=args.rf_scale, offset=args.offset
    )

    real_rate = ref_rate * args.rf_scale
    print(file=sys.stderr)
    log("════════════════════════════════════════════")
    log("  rf-trim — synchronized capture trimmer")
    log("════════════════════════════════════════════")
    log()
    log(f"  Reference:      {ref_file.name}")
    log(f"  FLAC rate:      {ref_rate} Hz (× {args.rf_scale} = {real_rate} real samples/s)")
    log(f"  Total samples:  {ref_samples}")
    log(f"  Total duration: {fmt_timestamp(result['real_duration'])}")
    log()
    if mode == "end":
        log(f"  Mode:           --end {fmt_timestamp(time_seconds)} (decoded video time)")
        if args.offset > 0:
            log(f"  Offset:         +{args.offset}s (vhs-decode lock-in compensation)")
    else:
        log(f"  Mode:           --trim {result['trim_seconds']}s from end")
    log(
        f"  Keeping:        {fmt_timestamp(result['keep_seconds'])}  "
        f"({result['keep_fraction'] * 100}%)"
    )
    log(f"  Removing:       {fmt_timestamp(result['trim_seconds'])} from end")
    log()

    # -- Trim each file --------------------------------------------------------
    threads = detect_threads(args.threads)
    errors = 0
    for file in trim_files:
        try:
            ok = trim_file(
                file,
                result["keep_fraction"],
                # The reference (video) sample count is already known — reuse it so a
                # headerless FLAC is not scanned a second time.
                known_samples=ref_samples if file == ref_file else None,
                delete_original=args.delete_original,
                threads=threads,
                dry_run=args.dry_run,
                verbose=args.verbose,
            )
        except ToolError as exc:
            log(f"    ERROR: {exc}")
            ok = False
        if not ok:
            errors += 1

    print(file=sys.stderr)
    if errors == 0:
        # Delete the .bak originals only now that ALL files succeeded — a partial
        # run must stay recoverable from the backups.
        if args.delete_original and not args.dry_run:
            for file in trim_files:
                work_paths(file)[1].unlink(missing_ok=True)
            log(f"Originals deleted ({len(trim_files)} .bak file(s)).")
        log(f"Done. All {len(trim_files)} file(s) trimmed successfully.")
        return 0

    log(
        f"WARNING: {errors} of {len(trim_files)} file(s) were NOT trimmed — the capture "
        "set may be DESYNCHRONIZED. Restore the trimmed files from their .bak originals, "
        "or fix the problem and trim the remaining files."
    )
    if args.delete_original:
        log("  --delete-original was ignored; all .bak originals were kept.")
    return 1
