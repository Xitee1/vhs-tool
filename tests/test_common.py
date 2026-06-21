import pytest

from vhs_tool.common import (
    ToolError,
    normalize_tz,
    seconds_to_ts,
    to_matroska_date,
    ts_to_seconds,
)


def test_ts_roundtrip():
    assert ts_to_seconds("01:02:03.500") == 3723.5
    assert seconds_to_ts(3723.5) == "01:02:03.500"


def test_ts_without_millis():
    assert ts_to_seconds("00:05:00") == 300.0


def test_negative_seconds_clamped():
    assert seconds_to_ts(-1.0) == "00:00:00.000"


@pytest.mark.parametrize("bad", ["1:2:3", "00:00", "abc", "00:00:00.1234"])
def test_invalid_timestamp(bad):
    with pytest.raises(ToolError):
        ts_to_seconds(bad)


@pytest.mark.parametrize(
    "value,tz,expected",
    [
        ("1998", "+01:00", "1998-01-01T00:00:00+01:00"),
        ("1998-05", "+01:00", "1998-05-01T00:00:00+01:00"),
        ("1998-05-08", "+01:00", "1998-05-08T00:00:00+01:00"),
        ("1998-05-08T18:11:32", "+01:00", "1998-05-08T18:11:32+01:00"),
        ("1998-05-08 18:11", "+02:00", "1998-05-08T18:11:00+02:00"),
        ("  1998-5-8  ", "+01:00", "1998-05-08T00:00:00+01:00"),  # trim + single-digit
        ("1998-05-08T18:11:32+02:00", "+01:00", "1998-05-08T18:11:32+02:00"),  # explicit tz wins
        ("1998-05-08T18:11:32Z", "+01:00", "1998-05-08T18:11:32Z"),
        ("1998-05-08", "Z", "1998-05-08T00:00:00Z"),
    ],
)
def test_to_matroska_date(value, tz, expected):
    assert to_matroska_date(value, tz) == expected


@pytest.mark.parametrize("bad", ["", "abc", "98", "1998-13-01", "1998-02-30", "1998-05-08T25:00"])
def test_to_matroska_date_invalid(bad):
    with pytest.raises(ToolError):
        to_matroska_date(bad)


def test_normalize_tz():
    assert normalize_tz("+0100") == "+01:00"
    assert normalize_tz("-05:00") == "-05:00"
    assert normalize_tz("Z") == "Z"
    with pytest.raises(ToolError):
        normalize_tz("+1:00")
