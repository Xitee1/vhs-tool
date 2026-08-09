from pathlib import Path

import pytest

from vhs_tool.common import ToolError
from vhs_tool.flac import (
    AUDIO_BLOCKSIZE,
    RF_BLOCKSIZE,
    FlacInfo,
    encode_settings,
    flac_decode_raw_cmd,
    flac_encode_cmd,
    flac_metadata_length,
    parse_flac_info,
    parse_level_tag,
    source_kind,
)


def _info(*, blocksize=65535, rate=40000, channels=1, bps=8, md5="a" * 32) -> FlacInfo:
    return FlacInfo(md5, blocksize, blocksize, rate, channels, bps)


# =============================================================================
# Encode settings
# =============================================================================


@pytest.mark.parametrize(
    ("name", "blocksize", "expected"),
    [
        # The channel suffix decides, whatever blocksize the file happens to have.
        ("tape-video.flac", 65535, "rf"),
        ("tape-video.flac", 4096, "rf"),  # RF that lost its blocksize (older sox trim)
        ("tape-hifi.flac", 4096, "rf"),
        ("tape-headswitch.flac", 1152, "rf"),
        ("tape-linear.flac", 1152, "audio"),
        ("tape-linear.flac", 65535, "audio"),  # never turn linear into a non-subset stream
        # Without a known suffix the blocksize is the fallback signal.
        ("something.flac", 65535, "rf"),
        ("something.flac", 4608, "audio"),  # exactly the subset limit
        ("something.flac", 4096, "audio"),
    ],
)
def test_source_kind(name, blocksize, expected):
    assert source_kind(Path(name), _info(blocksize=blocksize)) == expected


def test_source_kind_matches_suffix_before_the_extension():
    """A trimmed temp name still carries its channel."""
    assert source_kind(Path("tape-video.trimmed.flac"), _info(blocksize=4096)) == "rf"


def test_encode_settings():
    assert encode_settings(Path("t-video.flac"), _info()) == (RF_BLOCKSIZE, True)
    assert encode_settings(Path("t-linear.flac"), _info(blocksize=1152, bps=24, channels=2)) == (
        AUDIO_BLOCKSIZE,
        False,
    )


def test_encode_settings_honors_a_custom_rf_blocksize():
    assert encode_settings(Path("t-video.flac"), _info(), 32768) == (32768, True)
    # audio is unaffected by the RF blocksize
    assert encode_settings(Path("t-linear.flac"), _info(blocksize=1152), 32768) == (
        AUDIO_BLOCKSIZE,
        False,
    )


# =============================================================================
# Command lines
# =============================================================================


def test_flac_encode_cmd():
    cmd = flac_encode_cmd(
        Path("/c/tape-video.u8"), Path("/c/tape-video.flac.part"),
        bps=8, sign="unsigned", rate=40000, channels=1,
        blocksize=65535, level=8, threads=16,
    )  # fmt: skip
    assert cmd == [
        "flac", "--silent", "-8", "--threads=16",
        "--blocksize=65535", "--lax",
        "--sample-rate=40000", "--channels=1", "--bps=8",
        "--sign=unsigned", "--endian=little",
        "-f", "/c/tape-video.u8", "-o", "/c/tape-video.flac.part",
    ]  # fmt: skip


def test_flac_encode_cmd_without_threads():
    cmd = flac_encode_cmd(
        Path("in.s16"), Path("out.flac"),
        bps=16, sign="signed", rate=40000, channels=1,
        blocksize=65535, level=8, threads=0,
    )  # fmt: skip
    assert not any(c.startswith("--threads") for c in cmd)
    assert "--bps=16" in cmd and "--sign=signed" in cmd


def test_flac_encode_cmd_stdout_variant():
    cmd = flac_encode_cmd(
        "-", None,
        bps=8, sign="signed", rate=40000, channels=1,
        blocksize=65535, level=8,
    )  # fmt: skip
    assert cmd[-2:] == ["--stdout", "-"]
    assert "-o" not in cmd


def test_flac_encode_cmd_without_lax():
    cmd = flac_encode_cmd(
        "-", None,
        bps=24, sign="signed", rate=46875, channels=2,
        blocksize=4096, level=8, lax=False,
    )  # fmt: skip
    assert "--lax" not in cmd
    assert "--blocksize=4096" in cmd


def test_flac_decode_raw_cmd():
    cmd = flac_decode_raw_cmd(Path("tape-video.flac"))
    assert cmd[-1] == "tape-video.flac"
    assert "--force-raw-format" in cmd and "--sign=signed" in cmd
    assert flac_decode_raw_cmd(Path("x.flac"), sign="unsigned")[-4] == "--sign=unsigned"


# =============================================================================
# Metadata parsing
# =============================================================================


def test_parse_flac_info():
    info = parse_flac_info("4726149E88DEE89AA27B1424E993867C\n65535\n65535\n40000\n1\n8\n")
    assert info == FlacInfo("4726149e88dee89aa27b1424e993867c", 65535, 65535, 40000, 1, 8)
    assert info.has_md5


def test_parse_flac_info_zero_md5_means_absent():
    assert not parse_flac_info("0" * 32 + "\n4096\n4096\n40000\n1\n16\n").has_md5


@pytest.mark.parametrize("output", ["", "garbage", "abc\n1\n2\n3\n4\nnope\n"])
def test_parse_flac_info_rejects_unexpected_output(output):
    with pytest.raises(ToolError):
        parse_flac_info(output)


@pytest.mark.parametrize(
    ("blocksize", "channels", "bps", "expected"),
    [(65535, 1, 8, 1), (1152, 2, 24, 6), (4096, 2, 16, 4)],
)
def test_bytes_per_sample(blocksize, channels, bps, expected):
    assert _info(blocksize=blocksize, channels=channels, bps=bps).bytes_per_sample == expected


def test_bytes_per_sample_rejects_unaligned_depth():
    with pytest.raises(ToolError):
        _ = _info(bps=20).bytes_per_sample


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("VHS_TOOL_FLAC_LEVEL=8\n", 8),
        ("vhs_tool_flac_level=5\n", 5),  # Vorbis comment names are case-insensitive
        ("VHS_TOOL_FLAC_LEVEL=abc\n", None),
        ("OTHER_TAG=8\n", None),
        ("", None),
    ],
)
def test_parse_level_tag(output, expected):
    assert parse_level_tag(output) == expected


def _meta_block(block_type: int, length: int, *, last: bool) -> bytes:
    header = bytes([block_type | (0x80 if last else 0)]) + length.to_bytes(3, "big")
    return header + b"\x00" * length


def test_flac_metadata_length():
    head = b"fLaC" + _meta_block(0, 34, last=False) + _meta_block(1, 10, last=True) + b"frames..."
    assert flac_metadata_length(head) == 4 + (4 + 34) + (4 + 10)


def test_flac_metadata_length_rejects_non_flac():
    with pytest.raises(ToolError):
        flac_metadata_length(b"RIFF....")


def test_flac_metadata_length_rejects_truncated_head():
    head = b"fLaC" + _meta_block(0, 34, last=False)  # no terminating last-block
    with pytest.raises(ToolError):
        flac_metadata_length(head)
