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
