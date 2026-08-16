"""Shared FLAC knowledge: encode settings, STREAMINFO/tag access, command lines.

Three commands write FLAC capture files — `rf-compress` (raw → FLAC and
recompression), `rf-trim` (trimmed copies) and `rf-resample` (downsampled
copies) — and they must agree on how a capture is encoded. This module is that
agreement; each command only decides *which* files it writes, not *how*.

Two kinds of capture live side by side and must be treated differently:

  RF data (video/hifi/headswitch)   noise-like, mono; a huge blocksize of
                                    65535 compresses it best, which requires
                                    --lax because it exceeds the FLAC
                                    streaming subset.
  Linear audio                      an ordinary playable audio file (24-bit
                                    stereo); it stays a subset stream at
                                    flac's default blocksize — which on real
                                    audio also compresses better than the
                                    huge RF blocks.

`source_kind()` decides between them, `encode_settings()` turns that into
(blocksize, lax).

Level 8 is optimal for RF data; levels 9+ (--lax high-order) cause ~42% file
bloat due to rejected LPC predictions on RF signals.
See: https://github.com/harrypm/Scripts/issues/2
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import NamedTuple

from .common import ToolError, log, run

# -- Encode defaults -----------------------------------------------------------
FLAC_LEVEL = 8  # optimal for RF data
RF_BLOCKSIZE = 65535  # optimal for RF data (non-subset, needs --lax)
AUDIO_BLOCKSIZE = 4096  # flac's default; keeps audio re-encodes subset
SUBSET_MAX_BLOCKSIZE = 4608  # FLAC streaming-subset blocksize limit for rates ≤ 48 kHz
ENDIAN = "little"
CHUNK = 1 << 20  # pipe/hash read size
MAX_THREADS = 128  # flac's own upper limit for --threads

_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")

# Vorbis comment marking a file as verified for a compression level: either
# actually encoded at it, or measured to gain nothing worthwhile from it.
LEVEL_TAG = "VHS_TOOL_FLAC_LEVEL"

# Capture channel suffixes, by the kind of data they carry. These are the
# strongest signal available: a file named "<base>-video.flac" holds RF even
# when an earlier tool wrote it with an audio blocksize.
RF_DATA_CHANNELS = ("-video", "-hifi", "-headswitch")
AUDIO_CHANNELS = ("-linear",)


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

    @property
    def bytes_per_sample(self) -> int:
        """Bytes one sample of all channels occupies in decoded raw form."""
        if self.bps % 8:
            raise ToolError(f"unsupported bit depth for raw processing: {self.bps}")
        return self.channels * (self.bps // 8)


class FlacVersion(NamedTuple):
    """Parsed `flac --version` output."""

    text: str  # as printed, e.g. "1.5.0" ("unknown" if unparsable)
    major: int
    minor: int

    @property
    def supports_threads(self) -> bool:
        """flac gained multi-threaded encoding (--threads) in 1.5.0."""
        return (self.major, self.minor) >= (1, 5)


# =============================================================================
# Encoder threads
# =============================================================================


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


def detect_threads(requested: int | None = None, *, announce: bool = True) -> int:
    """Ask the installed flac for its version and resolve the thread count."""
    version = parse_flac_version(run(["flac", "--version"], capture=True, check=False).stdout)
    threads = resolve_threads(requested, version)
    if announce:
        if threads > 1:
            log(f"FLAC {version.text} — multi-threaded encoding with {threads} threads")
        else:
            log(f"FLAC {version.text} — single-threaded encoding")
    return threads


# =============================================================================
# Encode settings
# =============================================================================


def source_kind(file: Path, info: FlacInfo) -> str:
    """ "rf" or "audio" — which kind of capture data `file` holds.

    The channel suffix decides when the name carries one, so a file that lost
    its RF blocksize somewhere (e.g. an older sox-written trim) is still
    recognized as RF, and a linear capture is never turned into a non-subset
    stream. For anything else the blocksize is the fallback signal: only a
    deliberate --lax encode exceeds the subset limit, and guessing "audio" for
    the rest is the safe direction (it costs a little size, never playability).
    """
    name = file.name.lower()
    if any(f"{channel}." in name for channel in AUDIO_CHANNELS):
        return "audio"
    if any(f"{channel}." in name for channel in RF_DATA_CHANNELS):
        return "rf"
    return "rf" if info.max_blocksize > SUBSET_MAX_BLOCKSIZE else "audio"


def encode_settings(
    file: Path, info: FlacInfo, rf_blocksize: int = RF_BLOCKSIZE
) -> tuple[int, bool]:
    """(blocksize, lax) for re-encoding `file`, preserving its character."""
    if source_kind(file, info) == "rf":
        return rf_blocksize, True
    return AUDIO_BLOCKSIZE, False


# =============================================================================
# Command lines
# =============================================================================


def flac_encode_cmd(
    file: Path | str, out: Path | None, *, bps: int, sign: str, rate: int, channels: int,
    blocksize: int, level: int, threads: int = 0, lax: bool = True,
) -> list[str]:  # fmt: skip
    """flac command line encoding raw samples with the given settings.

    `file` may be "-" for stdin; `out=None` encodes to stdout. `lax` allows
    non-subset streams (needed for the RF blocksize of 65535) and must be off
    for subset audio files.
    """
    cmd = ["flac", "--silent", f"-{level}"]
    if threads:
        cmd.append(f"--threads={threads}")
    cmd.append(f"--blocksize={blocksize}")
    if lax:
        cmd.append("--lax")
    cmd += [
        f"--sample-rate={rate}", f"--channels={channels}", f"--bps={bps}",
        f"--sign={sign}", f"--endian={ENDIAN}",
        "-f",
    ]  # fmt: skip
    if out is None:
        cmd += ["--stdout", str(file)]
    else:
        cmd += [str(file), "-o", str(out)]
    return cmd


def flac_decode_raw_cmd(file: Path, *, sign: str = "signed") -> list[str]:
    """flac command decoding a FLAC to raw samples on stdout.

    Decoding also verifies each frame's CRC and — at end of stream — the
    STREAMINFO MD5, so a nonzero exit proves the file did not decode to what
    its header claims.
    """
    return [
        "flac", "--silent", "-d", "--force-raw-format",
        f"--sign={sign}", f"--endian={ENDIAN}", "--stdout", str(file),
    ]  # fmt: skip


# =============================================================================
# Metadata (metaflac)
# =============================================================================


def parse_flac_info(output: str) -> FlacInfo:
    """Parse the six lines of `metaflac --show-md5sum --show-min-blocksize ...`."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if len(lines) != 6 or not all(line.isdigit() for line in lines[1:]):
        raise ToolError(f"unexpected metaflac output: {output!r}")
    md5, *nums = lines
    return FlacInfo(md5.lower(), *(int(n) for n in nums))


def read_flac_info(file: Path) -> FlacInfo:
    """STREAMINFO of an existing FLAC."""
    out = run(
        ["metaflac", "--show-md5sum", "--show-min-blocksize", "--show-max-blocksize",
         "--show-sample-rate", "--show-channels", "--show-bps", file],
        capture=True,
    ).stdout  # fmt: skip
    return parse_flac_info(out)


def parse_level_tag(output: str) -> int | None:
    """Level from `metaflac --show-tag=...` output (``NAME=8``), or None."""
    for line in output.splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip().upper() == LEVEL_TAG and value.strip().isdigit():
            return int(value.strip())
    return None


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


# =============================================================================
# Exact sample count (frame-header scan)
# =============================================================================
# STREAMINFO's total_samples field is only 36 bits wide, and the encoder
# stores 0 ("unknown") when the real count does not fit — which every video RF
# capture beyond ~28 min (2^36 samples at 40 MSPS) hits. Counting by decoding
# (`sox … stat`) then reads the whole file. The frames themselves carry what
# is needed instead: they are byte-aligned, CRC-protected and numbered, so
# parsing the last frame header yields the exact total from a little tail I/O.

FRAME_SCAN_WINDOW = 4 << 20  # tail bytes scanned; dozens of worst-case RF frames
_HEAD_BUFFER = 1 << 20  # metadata must fit in this head read

# Frame-header field tables (FLAC format spec).
_BLOCKSIZE_CODES = {
    1: 192, 2: 576, 3: 1152, 4: 2304, 5: 4608, 8: 256, 9: 512, 10: 1024,
    11: 2048, 12: 4096, 13: 8192, 14: 16384, 15: 32768,
}  # fmt: skip
_RATE_CODES = {
    1: 88200, 2: 176400, 3: 192000, 4: 8000, 5: 16000, 6: 22050, 7: 24000,
    8: 32000, 9: 44100, 10: 48000, 11: 96000,
}  # fmt: skip
_BPS_CODES = {1: 8, 2: 12, 4: 16, 5: 20, 6: 24, 7: 32}


def _crc_table(poly: int, width: int) -> list[int]:
    top, mask = 1 << (width - 1), (1 << width) - 1
    table = []
    for byte in range(256):
        crc = byte << (width - 8)
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & mask if crc & top else (crc << 1) & mask
        table.append(crc)
    return table


_CRC8_TABLE = _crc_table(0x07, 8)  # frame-header CRC
_CRC16_TABLE = _crc_table(0x8005, 16)  # whole-frame CRC


def _crc8(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = _CRC8_TABLE[crc ^ b]
    return crc


def _crc16(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = _CRC16_TABLE[(crc >> 8) ^ b] ^ ((crc << 8) & 0xFFFF)
    return crc


class FrameHeader(NamedTuple):
    """One parsed and CRC-8-verified frame header inside a scanned buffer."""

    pos: int  # byte offset of the sync code in the buffer
    number: int  # frame number (fixed blocksize) or first-sample number (variable)
    variable: bool  # blocking-strategy bit
    blocksize: int  # this frame's own blocksize in samples


def _coded_number(buf: bytes, i: int) -> tuple[int, int] | None:
    """Decode FLAC's extended-UTF-8 frame/sample number; (value, length) or None."""
    b0 = buf[i]
    if b0 < 0x80:
        return b0, 1
    length, mask = 0, 0x80
    while mask and b0 & mask:
        length += 1
        mask >>= 1
    if not 2 <= length <= 7:
        return None
    value = b0 & (0xFF >> (length + 1)) if length < 7 else 0
    for k in range(1, length):
        b = buf[i + k]
        if b & 0xC0 != 0x80:
            return None
        value = (value << 6) | (b & 0x3F)
    return value, length


def parse_frame_header(buf: bytes, pos: int, info: FlacInfo) -> FrameHeader | None:
    """Parse a frame header at buf[pos], validating every field against `info`.

    Returns None unless the sync code, all fixed fields (rate, channels, bps,
    reserved bits), the coded frame/sample number and the header CRC-8 are all
    consistent — random data survives this with probability ~2^-40.
    """
    try:
        if buf[pos] != 0xFF or (buf[pos + 1] & 0xFE) != 0xF8:
            return None
        variable = bool(buf[pos + 1] & 1)
        bs_code, rate_code = buf[pos + 2] >> 4, buf[pos + 2] & 0xF
        ch_code, bps_code = buf[pos + 3] >> 4, (buf[pos + 3] >> 1) & 7
        if buf[pos + 3] & 1 or bs_code == 0 or rate_code == 15 or bps_code == 3:
            return None
        if ch_code <= 7:  # plain channel count; 8-10 are the 2-channel joint modes
            if ch_code + 1 != info.channels:
                return None
        elif ch_code <= 10:
            if info.channels != 2:
                return None
        else:
            return None
        if bps_code and _BPS_CODES[bps_code] != info.bps:
            return None
        num = _coded_number(buf, pos + 4)
        if num is None:
            return None
        number, num_len = num
        p = pos + 4 + num_len
        if bs_code == 6:  # 8/16-bit blocksize at end of header, stored as value-1
            blocksize = buf[p] + 1
            p += 1
        elif bs_code == 7:
            blocksize = (buf[p] << 8 | buf[p + 1]) + 1
            p += 2
        else:
            blocksize = _BLOCKSIZE_CODES[bs_code]
        if rate_code == 12:  # uncommon rate at end of header (kHz / Hz / daHz)
            rate = buf[p] * 1000
            p += 1
        elif rate_code == 13:
            rate = buf[p] << 8 | buf[p + 1]
            p += 2
        elif rate_code == 14:
            rate = (buf[p] << 8 | buf[p + 1]) * 10
            p += 2
        else:
            rate = info.sample_rate if rate_code == 0 else _RATE_CODES[rate_code]
        if rate != info.sample_rate or _crc8(buf[pos:p]) != buf[p]:
            return None
    except IndexError:
        return None
    return FrameHeader(pos, number, variable, blocksize)


def _chains_to(prev: FrameHeader, last: FrameHeader, info: FlacInfo) -> bool:
    """Whether `prev` is the immediate predecessor frame of `last`."""
    if prev.variable != last.variable:
        return False
    if last.variable:  # numbers are first-sample positions
        return prev.number + prev.blocksize == last.number
    # Fixed blocksize: numbers are consecutive and only the stream's last
    # frame may be shorter than the nominal blocksize.
    return prev.number + 1 == last.number and prev.blocksize == info.max_blocksize


def total_samples_from_tail(tail: bytes, info: FlacInfo, *, tail_at_data_start: bool) -> int | None:
    """Exact stream total from a buffer holding the end of the frame section.

    The count is taken from the last frame header whose frame passes the
    whole-frame CRC-16 against the end of the buffer AND whose number is
    confirmed by a chained predecessor header (or by being frame 0 at the
    start of the data section, for single-frame streams). Anything less
    proves nothing — then None is returned and the caller must count the
    hard way. A truncated or trailing-garbage file fails the CRC-16, so a
    partially written frame is never counted.
    """
    candidates = []
    i = 0
    while (j := tail.find(b"\xff", i)) != -1:
        header = parse_frame_header(tail, j, info)
        if header is not None:
            candidates.append(header)
        i = j + 1

    for idx in range(len(candidates) - 1, -1, -1):
        last = candidates[idx]
        if len(tail) - last.pos < 2:
            continue
        if _crc16(tail[last.pos : len(tail) - 2]) != int.from_bytes(tail[-2:], "big"):
            continue  # not a frame ending at EOF (false sync, truncation, junk)
        confirmed = any(_chains_to(c, last, info) for c in candidates[:idx]) or (
            tail_at_data_start and last.pos == 0 and last.number == 0
        )
        if not confirmed:
            continue
        if last.variable:
            return last.number + last.blocksize
        return last.number * info.max_blocksize + last.blocksize
    return None


def scan_total_samples(file: Path, info: FlacInfo) -> int | None:
    """Exact total sample count of a FLAC via its last frame header, or None.

    Reads only the metadata head and the last FRAME_SCAN_WINDOW bytes, so it
    is fast regardless of file size — unlike decoding the whole stream. None
    means the tail could not be verified (see total_samples_from_tail); it
    never guesses.
    """
    try:
        size = file.stat().st_size
        with open(file, "rb") as fh:
            metadata_end = flac_metadata_length(fh.read(_HEAD_BUFFER))
            if metadata_end >= size:
                return None  # no frames at all
            start = max(metadata_end, size - FRAME_SCAN_WINDOW)
            fh.seek(start)
            tail = fh.read()
    except (OSError, ToolError):
        return None
    return total_samples_from_tail(tail, info, tail_at_data_start=start == metadata_end)
