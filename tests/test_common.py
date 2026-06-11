import pytest

from vhs_tool.common import ToolError, seconds_to_ts, ts_to_seconds


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
