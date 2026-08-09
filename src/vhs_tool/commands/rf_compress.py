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

Existing `.flac` files are recompressed to the target level when that pays off.
FLAC does not record its compression level, so the decision is made by
measurement: a probe re-encodes the first `--probe-size` MiB of the file and
projects the size gain; only when it reaches `--min-gain` percent is the file
fully re-encoded (decode→encode pipe, no intermediate raw). Verification uses
the STREAMINFO MD5 (computed by the encoder over the unencoded samples): the
decode side checks it for the source, the new file must carry the same MD5,
and `flac -t` proves the file on disk decodes cleanly — only then is the
original replaced. The verified level is stored as a `VHS_TOOL_FLAC_LEVEL`
Vorbis comment so later runs skip the file without probing. Recompression also
repairs the total-sample count in STREAMINFO (0 in piped captures).

Before anything is touched, the command prints a plan — files already
verified, files to compress, files to probe — and asks for confirmation
(`--yes` skips the prompt).

Requires: flac + metaflac (≥1.4; ≥1.5 for multi-threaded encoding)

Ref: the wiki recommends the FLAC CLI (not FFmpeg) for RF data:
  https://github.com/oyvindln/vhs-decode/wiki/RF-Compression-&-Decompression-Guide

Note: FLAC level 8 is optimal for RF data. Levels 9+ (--lax high-order) cause
~42% file bloat due to rejected LPC predictions on RF signals.
See: https://github.com/harrypm/Scripts/issues/2
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import os
import re
import shlex
import subprocess
import sys
import threading
from pathlib import Path
from typing import NamedTuple

from ..common import ToolError, check_deps, confirm, log, run

# -- Defaults ------------------------------------------------------------------
FLAC_LEVEL = 8
BLOCKSIZE = 65535
RATE = 40000  # 40 MSPS → stored as 40000 Hz (FLAC-scale, 1000:1)
CHANNELS = 1
ENDIAN = "little"
MAX_THREADS = 128  # flac's own upper limit for --threads
CHUNK = 1 << 20  # MD5 read size

# -- Recompression defaults ----------------------------------------------------
LEVEL_TAG = "VHS_TOOL_FLAC_LEVEL"  # Vorbis comment marking a verified level
PROBE_MIB = 1024  # probe window read from the start of the compressed file
MIN_GAIN_PCT = 0.5  # recompress only when the probe projects at least this gain

# Raw capture extensions in scan order → (bits per sample, sign).
RAW_FORMATS: dict[str, tuple[int, str]] = {
    "u8": (8, "unsigned"),
    "r8": (8, "unsigned"),
    "u16": (16, "unsigned"),
    "s16": (16, "signed"),
    "r16": (16, "signed"),
}

_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")
_ANA_FRAME_RE = re.compile(r"^frame=\d+\toffset=(\d+)\tbits=(\d+)\tblocksize=(\d+)\t")
_ANA_ORDER_RE = re.compile(r"\ttype=(?:LPC|FIXED)\torder=(\d+)")


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


class FlacInfo(NamedTuple):
    """STREAMINFO fields of an existing FLAC (via metaflac)."""

    md5: str  # hex digest of the unencoded samples; all zeros if the encoder couldn't seek back
    min_blocksize: int
    max_blocksize: int
    sample_rate: int
    channels: int
    bps: int

    @property
    def has_md5(self) -> bool:
        return set(self.md5) != {"0"}


class AnaFrame(NamedTuple):
    """One frame line of `flac --analyze` output."""

    offset: int  # byte offset of the frame in the file
    bits: int  # compressed size of the frame in bits
    blocksize: int  # samples per channel in the frame


class ProbeResult(NamedTuple):
    """Outcome of a trial re-encode of the file's first frames."""

    orig_bytes: int  # compressed bytes those frames occupy in the original
    probe_bytes: int  # compressed bytes of the trial re-encode (metadata excluded)
    samples: int
    frames: int
    max_order: int  # highest predictor order seen (>12 ⇒ --lax high-order encode)

    @property
    def gain_pct(self) -> float:
        if self.orig_bytes <= 0:
            return 0.0
        return (1 - self.probe_bytes / self.orig_bytes) * 100


class Plan(NamedTuple):
    """What a run will do — shown to the user before anything is touched."""

    verified: list[Path]  # FLACs already tagged for the target level, left alone
    raw_todo: list[Path]  # raw captures to compress
    flac_todo: list[tuple[Path, int | None]]  # FLACs to probe/recompress, with their tag


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
    file: Path | str, out: Path | None, *, bps: int, sign: str, rate: int, channels: int,
    blocksize: int, level: int, threads: int,
) -> list[str]:  # fmt: skip
    """flac command line encoding one raw capture with RF-optimal settings.

    `file` may be "-" for stdin; `out=None` encodes to stdout.
    """
    cmd = ["flac", "--silent", f"-{level}"]
    if threads:
        cmd.append(f"--threads={threads}")
    cmd += [
        f"--blocksize={blocksize}", "--lax",
        f"--sample-rate={rate}", f"--channels={channels}", f"--bps={bps}",
        f"--sign={sign}", f"--endian={ENDIAN}",
        "-f",
    ]  # fmt: skip
    if out is None:
        cmd += ["--stdout", str(file)]
    else:
        cmd += [str(file), "-o", str(out)]
    return cmd


def flac_decode_raw_cmd(file: Path) -> list[str]:
    """flac command decoding a FLAC to canonical raw samples (signed/little) on stdout.

    Decoding also verifies each frame's CRC and — at end of stream — the
    STREAMINFO MD5, so a nonzero exit proves the source did not decode to
    what its header claims.
    """
    return [
        "flac", "--silent", "-d", "--force-raw-format",
        "--sign=signed", f"--endian={ENDIAN}", "--stdout", str(file),
    ]  # fmt: skip


def parse_flac_info(output: str) -> FlacInfo:
    """Parse the six lines of `metaflac --show-md5sum --show-min-blocksize ...`."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if len(lines) != 6 or not all(line.isdigit() for line in lines[1:]):
        raise ToolError(f"unexpected metaflac output: {output!r}")
    md5, *nums = lines
    return FlacInfo(md5.lower(), *(int(n) for n in nums))


def parse_level_tag(output: str) -> int | None:
    """Level from `metaflac --show-tag=...` output (``NAME=8``), or None."""
    for line in output.splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip().upper() == LEVEL_TAG and value.strip().isdigit():
            return int(value.strip())
    return None


def parse_ana_frame(line: str) -> AnaFrame | None:
    """Frame accounting from one `flac --analyze` line, or None for other lines."""
    match = _ANA_FRAME_RE.match(line)
    if not match:
        return None
    return AnaFrame(int(match.group(1)), int(match.group(2)), int(match.group(3)))


def parse_ana_order(line: str) -> int | None:
    """Predictor order from a `flac --analyze` subframe line, or None."""
    match = _ANA_ORDER_RE.search(line)
    return int(match.group(1)) if match else None


def flac_metadata_length(head: bytes) -> int:
    """Byte length of the metadata section at the start of a FLAC stream."""
    if head[:4] != b"fLaC":
        raise ToolError("not a FLAC stream")
    pos = 4
    while True:
        if pos + 4 > len(head):
            raise ToolError("FLAC metadata longer than the buffered stream head")
        header = head[pos]
        pos += 4 + int.from_bytes(head[pos + 1 : pos + 4], "big")
        if header & 0x80:  # last-metadata-block flag
            return pos


def build_plan(
    raw_files: list[Path],
    flac_files: list[Path],
    tags: dict[Path, int | None],
    level: int,
    force: bool,
) -> Plan:
    """Partition the scanned files by what will happen to them.

    Without `force`, FLACs already tagged for the target level are set aside
    as verified; everything else stays on the worklist with its tag (None =
    level unknown, decided by the probe).
    """
    verified = [] if force else [f for f in flac_files if tags.get(f) == level]
    flac_todo = [(f, tags.get(f)) for f in flac_files if force or tags.get(f) != level]
    return Plan(verified, list(raw_files), flac_todo)


def find_raw_files(directory: Path) -> list[Path]:
    """Raw capture files directly in `directory` (no recursion), grouped by extension."""
    files: list[Path] = []
    for ext in RAW_FORMATS:
        files.extend(sorted(p for p in directory.glob(f"*.{ext}") if p.is_file()))
    return files


def find_flac_files(directory: Path) -> list[Path]:
    """FLAC files directly in `directory` (no recursion; `.flac.part` never matches)."""
    return sorted(p for p in directory.glob("*.flac") if p.is_file())


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
# FLAC metadata (metaflac)
# =============================================================================


def read_flac_info(file: Path) -> FlacInfo:
    """STREAMINFO of an existing FLAC."""
    out = run(
        ["metaflac", "--show-md5sum", "--show-min-blocksize", "--show-max-blocksize",
         "--show-sample-rate", "--show-channels", "--show-bps", file],
        capture=True,
    ).stdout  # fmt: skip
    return parse_flac_info(out)


def read_level_tag(file: Path) -> int | None:
    """Verified level stored in the file's Vorbis comment, or None."""
    out = run(["metaflac", f"--show-tag={LEVEL_TAG}", file], capture=True).stdout
    return parse_level_tag(out)


def has_padding(file: Path) -> bool:
    out = run(["metaflac", "--list", "--block-type=PADDING", file], capture=True).stdout
    return bool(out.strip())


def set_level_tag(file: Path, level: int) -> bool:
    """Store the verified level as a Vorbis comment; best-effort, never raises.

    Requires an existing PADDING block: without one metaflac would rewrite the
    entire file just to fit the tag — not worth it on captures of hundreds of
    GB. An untagged file merely gets probed again on the next run.
    """
    try:
        if not has_padding(file):
            log("    → no PADDING block — not tagging (metaflac would rewrite the whole file)")
            return False
        run(["metaflac", f"--remove-tag={LEVEL_TAG}", f"--set-tag={LEVEL_TAG}={level}", file])
        return True
    except ToolError as exc:
        log(f"    → tagging failed ({exc})")
        return False


# =============================================================================
# Recompression probe
# =============================================================================


class _StreamCounter(threading.Thread):
    """Drains a pipe, counting its bytes and keeping the head for metadata parsing."""

    HEAD = 65536

    def __init__(self, stream):
        super().__init__(daemon=True)
        self._stream = stream
        self.total = 0
        self.head = b""

    def run(self) -> None:
        while chunk := self._stream.read(CHUNK):
            if len(self.head) < self.HEAD:
                self.head += chunk[: self.HEAD - len(self.head)]
            self.total += len(chunk)


def probe_flac(
    file: Path, *, info: FlacInfo, level: int, blocksize: int, threads: int, probe_bytes: int
) -> ProbeResult:
    """Measure on the file's first `probe_bytes` how much a re-encode at `level` saves.

    FLAC does not store its compression level, so the question "is this file
    already optimal?" is answered by measurement: pass 1 walks the frames
    within the window via `flac --analyze` (their compressed sizes, blocksizes
    and predictor orders); pass 2 decodes exactly those samples and re-encodes
    them with the target settings, counting the output bytes. Both passes stop
    at the window — the bulk of the file is never read. `--until`/`--skip`
    are no help here: piped captures have a total-sample count of 0.
    """
    # -- Pass 1: frame accounting of the original encode -----------------------
    frames = samples = bits = max_order = 0
    ana_cmd = ["flac", "--silent", "-a", "--stdout", str(file)]
    proc = subprocess.Popen(
        ana_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, errors="replace"
    )
    try:
        for line in proc.stdout:
            frame = parse_ana_frame(line)
            if frame is not None:
                if frame.offset > probe_bytes:
                    break
                frames += 1
                samples += frame.blocksize
                bits += frame.bits
                continue
            order = parse_ana_order(line)
            if order is not None:
                max_order = max(max_order, order)
    finally:
        proc.terminate()
        proc.stdout.close()
        proc.wait()
    if frames == 0:
        raise ToolError("flac --analyze produced no frames")

    # -- Pass 2: trial re-encode of exactly those samples -----------------------
    enc_cmd = flac_encode_cmd(
        "-", None, bps=info.bps, sign="signed", rate=info.sample_rate,
        channels=info.channels, blocksize=blocksize, level=level, threads=threads,
    )  # fmt: skip
    dec = subprocess.Popen(
        flac_decode_raw_cmd(file), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    enc = subprocess.Popen(
        enc_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    counter = _StreamCounter(enc.stdout)
    counter.start()
    try:
        remaining = samples * info.channels * (info.bps // 8)
        while remaining > 0:
            chunk = dec.stdout.read(min(CHUNK, remaining))
            if not chunk:
                break
            enc.stdin.write(chunk)
            remaining -= len(chunk)
    finally:
        with contextlib.suppress(OSError):
            enc.stdin.close()
        dec.terminate()
        dec.stdout.close()
        dec.wait()
        counter.join()
        returncode = enc.wait()
    if returncode != 0:
        raise ToolError(f"probe encode exited with code {returncode}")
    probe_size = counter.total - flac_metadata_length(counter.head)
    return ProbeResult(bits // 8, probe_size, samples, frames, max_order)


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
    set_level_tag(part, flac_level)  # lets later recompress runs skip it without probing
    part.replace(out)
    if keep_raw:
        log("    → keeping raw file (--keep-raw)")
    else:
        file.unlink()
        log("    → raw file removed")
    return CompressResult("compressed", raw_size, flac_size)


# =============================================================================
# Recompression (existing FLACs)
# =============================================================================


def transcode(
    file: Path, part: Path, *, info: FlacInfo, level: int, blocksize: int,
    threads: int, verbose: bool,
) -> None:  # fmt: skip
    """Re-encode `file` into `part` via a decode→encode pipe (no intermediate raw).

    The decode side verifies the source (frame CRCs + STREAMINFO MD5) as a side
    effect and fails the pipe on any mismatch.
    """
    dec_cmd = flac_decode_raw_cmd(file)
    enc_cmd = flac_encode_cmd(
        "-", part, bps=info.bps, sign="signed", rate=info.sample_rate,
        channels=info.channels, blocksize=blocksize, level=level, threads=threads,
    )  # fmt: skip
    if verbose:
        print(f"  $ {shlex.join(dec_cmd)} | {shlex.join(enc_cmd)}", file=sys.stderr)
    dec = subprocess.Popen(dec_cmd, stdout=subprocess.PIPE)
    try:
        enc = subprocess.Popen(enc_cmd, stdin=dec.stdout)
    except BaseException:
        dec.kill()
        raise
    finally:
        dec.stdout.close()
    enc_rc = enc.wait()
    dec_rc = dec.wait()
    if dec_rc != 0 or enc_rc != 0:
        raise ToolError(f"flac transcode failed (decode rc={dec_rc}, encode rc={enc_rc})")


def recompress_file(
    file: Path,
    *,
    flac_level: int = FLAC_LEVEL,
    blocksize: int = BLOCKSIZE,
    threads: int = 0,
    min_gain_pct: float = MIN_GAIN_PCT,
    probe_bytes: int = PROBE_MIB * 1048576,
    force: bool = False,
    dry_run: bool = False,
    verbose: bool = False,
) -> CompressResult:
    """Re-encode an existing FLAC at `flac_level` when a probe projects enough gain.

    The `VHS_TOOL_FLAC_LEVEL` tag marks a file as verified for a level — either
    actually encoded at it, or measured to gain less than the threshold from a
    re-encode. Tagged files are skipped instantly; untagged files are probed
    (see probe_flac) and only fully re-encoded when the projected gain reaches
    `min_gain_pct` percent. `force` recompresses unconditionally.

    Verification chain before the original is replaced: the decode side of the
    transcode checks the source against its own STREAMINFO MD5, the new file
    must carry the identical MD5, and `flac -t` proves that what landed on disk
    decodes cleanly to exactly those samples.
    """
    log(f"── {file.name}")
    orig_size = file.stat().st_size

    try:
        info = read_flac_info(file)
        tag = read_level_tag(file)
    except ToolError as exc:
        log(f"    ✗ cannot read FLAC metadata ({exc}), skipping")
        return CompressResult("error")
    log(
        f"    → FLAC size: {human_bytes(orig_size)} "
        f"({info.bps}-bit, {info.channels}ch, {info.sample_rate} Hz)"
    )

    if tag == flac_level and not force:
        log(f"    → already verified for level {flac_level} ({LEVEL_TAG} tag), skipping")
        return CompressResult("skipped")

    if not force:
        try:
            probe = probe_flac(
                file, info=info, level=flac_level, blocksize=blocksize,
                threads=threads, probe_bytes=probe_bytes,
            )  # fmt: skip
        except ToolError as exc:
            log(f"    ✗ probe failed ({exc}), skipping")
            return CompressResult("error")
        log(
            f"    → probe: {probe.frames} frames ({human_bytes(probe.orig_bytes)}), "
            f"max predictor order {probe.max_order}, projected gain {probe.gain_pct:.2f}%"
        )
        if probe.gain_pct < min_gain_pct:
            if dry_run:
                log(f"    → [dry run] below {min_gain_pct}% — would tag as verified and keep")
                return CompressResult("skipped")
            log(f"    → below {min_gain_pct}% — keeping file, tagging as verified")
            set_level_tag(file, flac_level)
            return CompressResult("skipped")

    if dry_run:
        log(f"    → [dry run] would recompress at level {flac_level}")
        return CompressResult("compressed")

    part = file.with_name(file.name + ".part")
    part.unlink(missing_ok=True)  # stale partial from a previous crash

    # -- 1. Transcode old FLAC → new FLAC ---------------------------------------
    log(
        f"    → recompressing with flac -{flac_level}"
        + (f" --threads={threads}" if threads else "")
    )
    try:
        transcode(
            file, part, info=info, level=flac_level, blocksize=blocksize,
            threads=threads, verbose=verbose,
        )  # fmt: skip
    except ToolError as exc:
        log(f"    ✗ {exc}, keeping original")
        part.unlink(missing_ok=True)
        return CompressResult("error")
    except BaseException:
        part.unlink(missing_ok=True)
        raise

    new_size = part.stat().st_size
    ratio = new_size / orig_size * 100 if orig_size else 0.0
    log(f"    → new size: {human_bytes(new_size)} ({ratio:.1f}% of original)")

    # -- 2. Verify: same samples (STREAMINFO MD5), intact on disk (flac -t) -----
    try:
        if info.has_md5:
            new_md5 = read_flac_info(part).md5
            old_md5 = info.md5
        else:  # source predates MD5-capable encoding — compare full decodes
            log("    → source has no STREAMINFO MD5, comparing decoded streams ...")
            old_md5 = md5_flac_raw(file, sign="signed")
            new_md5 = md5_flac_raw(part, sign="signed")
        if old_md5 != new_md5:
            log(f"      original: {old_md5}")
            log(f"      new:      {new_md5}")
            raise ToolError("MD5 mismatch")
        log("    → verifying new file with flac -t ...")
        run(["flac", "--silent", "-t", part])
    except ToolError as exc:
        log(f"    ✗ {exc} — keeping original, removing new FLAC")
        part.unlink(missing_ok=True)
        return CompressResult("error")
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    log("    ✓ verified (STREAMINFO MD5 + flac -t)")

    # -- 3. Finalize -----------------------------------------------------------
    if new_size >= orig_size:
        log("    → no improvement — keeping original, tagging as verified")
        part.unlink()
        set_level_tag(file, flac_level)
        return CompressResult("skipped")
    set_level_tag(part, flac_level)
    part.replace(file)
    log(f"    ✓ saved {human_bytes(orig_size - new_size)}")
    return CompressResult("compressed", orig_size, new_size)


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
        help='Directory to scan (e.g. "./captures") or a single raw capture / FLAC file',
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
        "--min-gain",
        type=float,
        default=MIN_GAIN_PCT,
        metavar="PCT",
        help="Recompress a FLAC only when the probe projects at least this size gain "
        f"in percent (default: {MIN_GAIN_PCT})",
    )
    parser.add_argument(
        "--probe-size",
        type=int,
        default=PROBE_MIB,
        metavar="MIB",
        help=f"Probe window in MiB read from the start of a FLAC (default: {PROBE_MIB})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Recompress FLAC files unconditionally (skip the tag check and the probe)",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Start without the confirmation prompt after the plan overview",
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
    check_deps("flac", "metaflac")

    path = Path(args.path)
    if path.is_dir():
        raw_files = find_raw_files(path)
        flac_files = find_flac_files(path)
    elif path.is_file():
        if path.suffix.lower() == ".flac":
            raw_files, flac_files = [], [path]
        else:
            raw_files, flac_files = [path], []
    else:
        raise ToolError(f"Not a file or directory: {path}")

    if not raw_files and not flac_files:
        log(f"No capture files found in: {path}")
        log("  (looked for: " + ", ".join(f"*.{ext}" for ext in RAW_FORMATS) + ", *.flac)")
        return 0

    if path.is_dir():
        log(f"Found {len(raw_files)} raw and {len(flac_files)} FLAC capture file(s) in: {path}")

    # -- Plan overview: nothing is touched before the confirmation --------------
    tags: dict[Path, int | None] = {}
    for file in flac_files:
        try:
            tags[file] = read_level_tag(file)
        except ToolError:
            tags[file] = None
    plan = build_plan(raw_files, flac_files, tags, args.level, args.force)

    if plan.verified:
        log(
            f"{len(plan.verified)} FLAC file(s) already verified for level {args.level} "
            f"({LEVEL_TAG} tag)"
        )
    if not plan.raw_todo and not plan.flac_todo:
        log("Nothing to do.")
        return 0
    log(f"To process (target: FLAC level {args.level}):")
    for file in plan.raw_todo:
        log(f"  · {file.name} ({human_bytes(file.stat().st_size)}) — raw, will be compressed")
    for file, tag in plan.flac_todo:
        size = human_bytes(file.stat().st_size)
        if args.force:
            what = "will be recompressed (--force)"
        elif tag is not None:
            what = f"verified for level {tag}, will be probed"
        else:
            what = "level unknown, will be probed"
        log(f"  · {file.name} ({size}) — {what}")
    print(file=sys.stderr)

    if args.dry_run:
        log("[DRY RUN — no files will be modified]")
    elif not args.yes:
        todo = len(plan.raw_todo) + len(plan.flac_todo)
        if not confirm(f"Process {todo} file(s)?", default=True):
            log("Aborted.")
            return 1

    version = parse_flac_version(run(["flac", "--version"], capture=True, check=False).stdout)
    threads = resolve_threads(args.threads, version)
    if threads > 1:
        log(f"FLAC {version.text} — multi-threaded encoding with {threads} threads")
    else:
        log(f"FLAC {version.text} — single-threaded encoding")
    print(file=sys.stderr)

    counts = {"compressed": 0, "skipped": len(plan.verified), "error": 0}
    total_raw = total_flac = 0
    for file in plan.raw_todo:
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
    for file, _tag in plan.flac_todo:
        result = recompress_file(
            file,
            flac_level=args.level,
            blocksize=args.blocksize,
            threads=threads,
            min_gain_pct=args.min_gain,
            probe_bytes=args.probe_size * 1048576,
            force=args.force,
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
        log(f"  Input total:  {human_bytes(total_raw)}")
        log(f"  Output total: {human_bytes(total_flac)} ({total_flac / total_raw * 100:.1f}%)")
        log(f"  Saved:        {human_bytes(total_raw - total_flac)}")
    return 1 if counts["error"] else 0
