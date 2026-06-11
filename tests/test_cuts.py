import pytest

from vhs_tool.common import ToolError
from vhs_tool.encode import Segment, adjust_chapter_ts, build_cut_segments


def test_single_cut_pair():
    keep, remove = build_cut_segments([("begin", "00:10:00"), ("end", "00:20:00")], duration=3600.0)
    assert remove == [Segment(600.0, 1200.0)]
    assert keep == [Segment(0.0, 600.0), Segment(1200.0, 3600.0)]


def test_leading_end_implies_begin_at_zero():
    keep, remove = build_cut_segments([("end", "00:01:00")], duration=100.0)
    assert remove == [Segment(0.0, 60.0)]
    assert keep == [Segment(60.0, 100.0)]


def test_trailing_begin_cuts_to_end():
    keep, remove = build_cut_segments([("begin", "00:01:00")], duration=100.0)
    assert remove == [Segment(60.0, 100.0)]
    assert keep == [Segment(0.0, 60.0)]


def test_consecutive_same_type_rejected():
    with pytest.raises(ToolError, match="alternate"):
        build_cut_segments([("begin", "00:01:00"), ("begin", "00:02:00")], duration=3600.0)


def test_non_increasing_rejected():
    with pytest.raises(ToolError, match="strictly increasing"):
        build_cut_segments([("begin", "00:02:00"), ("end", "00:01:00")], duration=3600.0)


def test_timestamp_beyond_duration_rejected():
    with pytest.raises(ToolError, match="exceeds video duration"):
        build_cut_segments([("begin", "01:00:00")], duration=60.0)


def test_cut_everything_rejected():
    with pytest.raises(ToolError, match="entire video"):
        build_cut_segments([("end", "00:01:40")], duration=100.0)


def test_chapter_shifting():
    _, remove = build_cut_segments([("begin", "00:10:00"), ("end", "00:20:00")], duration=3600.0)
    # Chapter after the cut shifts left by the removed 10 minutes
    assert adjust_chapter_ts("00:30:00", remove) == "00:20:00.000"
    # Chapter before the cut is unchanged
    assert adjust_chapter_ts("00:05:00", remove) == "00:05:00.000"


def test_chapter_inside_cut_rejected():
    _, remove = build_cut_segments([("begin", "00:10:00"), ("end", "00:20:00")], duration=3600.0)
    with pytest.raises(ToolError, match="falls inside removed segment"):
        adjust_chapter_ts("00:15:00", remove)
