import pytest

from vhs_tool.commands.trim import (
    compute_trim,
    derive_base,
    fmt_timestamp,
    parse_timestamp,
    resolve_mode,
)
from vhs_tool.common import ToolError


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("VHS_PAL_Tape_010", "VHS_PAL_Tape_010"),
        ("VHS_PAL_Tape_010-video.flac", "VHS_PAL_Tape_010"),
        ("VHS_PAL_Tape_010-hifi.flac", "VHS_PAL_Tape_010"),
        ("VHS_PAL_Tape_010-linear.flac", "VHS_PAL_Tape_010"),
        ("VHS_PAL_Tape_010-headswitch.u8", "VHS_PAL_Tape_010"),
        ("VHS_PAL_Tape_010-headswitch.flac", "VHS_PAL_Tape_010"),
        ("VHS_PAL_Tape_010-video", "VHS_PAL_Tape_010"),
    ],
)
def test_derive_base(name, expected):
    assert derive_base(name) == expected


@pytest.mark.parametrize(
    ("ts", "expected"),
    [
        ("01:23:45", 5025.0),
        ("45:30", 2730.0),
        ("90", 90.0),
        ("12.5", 12.5),
        ("00:00:01.500", 1.5),
    ],
)
def test_parse_timestamp(ts, expected):
    assert parse_timestamp(ts) == pytest.approx(expected)


@pytest.mark.parametrize("bad", ["01:02:03:04", "abc", "-5", "1:2:x"])
def test_parse_timestamp_invalid(bad):
    with pytest.raises(ToolError):
        parse_timestamp(bad)


def test_fmt_timestamp():
    assert fmt_timestamp(5025) == "01:23:45"
    assert fmt_timestamp(0) == "00:00:00"
    assert fmt_timestamp(1.5) == "00:00:01.5"


@pytest.mark.parametrize(
    ("end", "trim", "expected"),
    [
        ("01:00:00", None, ("end", "01:00:00")),
        (None, "60", ("trim", "60")),
    ],
)
def test_resolve_mode(end, trim, expected):
    assert resolve_mode(end, trim) == expected


def test_resolve_mode_errors():
    with pytest.raises(ToolError):
        resolve_mode("1:00", "60")  # both flags
    with pytest.raises(ToolError):
        resolve_mode(None, None)  # neither flag


def test_compute_trim_end_mode():
    # 10000 FLAC-Hz × 1000 scale = 10 MSPS; 100M samples = 10s of RF.
    result = compute_trim("end", 4.0, ref_samples=100_000_000, ref_rate=10_000)
    # keep = 4s + 4s offset = 8s of 10s → 80%
    assert result["real_duration"] == pytest.approx(10.0)
    assert result["keep_seconds"] == pytest.approx(8.0)
    assert result["trim_seconds"] == pytest.approx(2.0)
    assert result["keep_fraction"] == pytest.approx(0.8)


def test_compute_trim_trim_mode():
    # --trim works in raw RF time; offset is not applied.
    result = compute_trim("trim", 2.0, ref_samples=100_000_000, ref_rate=10_000)
    assert result["keep_seconds"] == pytest.approx(8.0)
    assert result["trim_seconds"] == pytest.approx(2.0)
    assert result["keep_fraction"] == pytest.approx(0.8)


def test_compute_trim_offset_zero():
    result = compute_trim("end", 8.0, ref_samples=100_000_000, ref_rate=10_000, offset=0)
    assert result["keep_seconds"] == pytest.approx(8.0)
    assert result["keep_fraction"] == pytest.approx(0.8)


def test_compute_trim_trim_exceeds_duration():
    # trim > total → keep_seconds goes negative → "exceeds total duration"
    with pytest.raises(ToolError, match="exceeds total duration"):
        compute_trim("trim", 20.0, ref_samples=100_000_000, ref_rate=10_000)


def test_compute_trim_end_exceeds_duration():
    # Faithful to the bash original: an end point past the tape leaves a negative
    # trim, surfacing as "Nothing to trim" (keep_seconds itself stays positive).
    with pytest.raises(ToolError, match="Nothing to trim"):
        compute_trim("end", 20.0, ref_samples=100_000_000, ref_rate=10_000)


def test_compute_trim_nothing_to_trim():
    # end == full duration (offset 0, keep == 10s) → nothing to remove
    with pytest.raises(ToolError, match="Nothing to trim"):
        compute_trim("end", 10.0, ref_samples=100_000_000, ref_rate=10_000, offset=0)
