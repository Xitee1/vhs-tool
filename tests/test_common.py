import pytest

import vhs_tool.common
from vhs_tool.common import (
    DECODED_SUFFIXES,
    ToolError,
    collapse_base_args,
    expand_wildcards,
    frame_rate,
    normalize_tz,
    seconds_to_ts,
    suffix_stripper,
    to_matroska_date,
    ts_to_seconds,
    video_duration,
)


def test_ts_roundtrip():
    assert ts_to_seconds("01:02:03.500") == 3723.5
    assert seconds_to_ts(3723.5) == "01:02:03.500"


def test_seconds_to_ts_rounding_carries():
    # rounding up .9995+ must carry into the next minute/hour, not emit ':60'
    assert seconds_to_ts(3599.9999) == "01:00:00.000"
    assert seconds_to_ts(59.9996) == "00:01:00.000"


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


def test_normalize_tz_out_of_range():
    with pytest.raises(ToolError, match="out of range"):
        normalize_tz("+99:99")


def test_video_duration_sentinel(monkeypatch):
    monkeypatch.setattr(vhs_tool.common, "_ffprobe", lambda file, *args: "N/A")
    with pytest.raises(ToolError, match=r"ffprobe returned 'N/A'"):
        video_duration("clip.mkv")


@pytest.mark.parametrize("bad", ["0/0", "N/A"])
def test_frame_rate_sentinel(monkeypatch, bad):
    monkeypatch.setattr(vhs_tool.common, "_ffprobe", lambda file, *args: bad)
    with pytest.raises(ToolError, match="ffprobe returned"):
        frame_rate("clip.mkv")


# -- Wildcard / multi-path arguments -------------------------------------------


def _touch(directory, *names):
    for name in names:
        (directory / name).write_bytes(b"")


def test_expand_wildcards_passthrough_and_dedupe(tmp_path):
    _touch(tmp_path, "a.flac", "b.flac")
    a = str(tmp_path / "a.flac")
    b = str(tmp_path / "b.flac")
    assert expand_wildcards([a, b, a]) == [a, b]


def test_expand_wildcards_expands_quoted_pattern(tmp_path):
    _touch(tmp_path, "X-video.flac", "X-hifi.flac", "other.txt")
    pattern = str(tmp_path / "X-*")
    assert expand_wildcards([pattern]) == sorted(
        [str(tmp_path / "X-hifi.flac"), str(tmp_path / "X-video.flac")]
    )


def test_expand_wildcards_no_match_is_an_error(tmp_path):
    with pytest.raises(ToolError, match="No files match"):
        expand_wildcards([str(tmp_path / "nope-*")])


def test_expand_wildcards_literal_file_with_glob_chars(tmp_path):
    _touch(tmp_path, "a[1].flac")
    literal = str(tmp_path / "a[1].flac")
    # glob would treat [1] as a character class and find nothing — the
    # existing file must win over the pattern interpretation
    assert expand_wildcards([literal]) == [literal]


def test_collapse_base_args_single_value_verbatim():
    assert collapse_base_args(["./captures/X"]) == "./captures/X"
    assert collapse_base_args(["./captures/X-video.flac"]) == "./captures/X-video.flac"


def test_collapse_base_args_capture_set(tmp_path):
    values = [
        str(tmp_path / "X-video.flac"),
        str(tmp_path / "X-hifi.flac"),
        str(tmp_path / "X-linear.flac.bak"),  # underivable sidecar: prefix check only
        str(tmp_path / "X-info.txt"),
    ]
    assert collapse_base_args(values) == str(tmp_path / "X")


def test_collapse_base_args_two_captures_is_an_error(tmp_path):
    values = [str(tmp_path / "X1-video.flac"), str(tmp_path / "X2-video.flac")]
    with pytest.raises(ToolError, match="more than one capture"):
        collapse_base_args(values)


def test_collapse_base_args_prefix_capture_is_an_error(tmp_path):
    # Tape_01* also matches Tape_010 — must not silently collapse to Tape_01
    values = [str(tmp_path / "Tape_01-video.flac"), str(tmp_path / "Tape_010-video.flac")]
    with pytest.raises(ToolError, match="more than one capture"):
        collapse_base_args(values)


def test_collapse_base_args_stray_file_is_an_error(tmp_path):
    values = [str(tmp_path / "X-video.flac"), str(tmp_path / "unrelated.txt")]
    with pytest.raises(ToolError, match="do not share the base"):
        collapse_base_args(values)


def test_collapse_base_args_multiple_directories_is_an_error():
    with pytest.raises(ToolError, match="span multiple directories"):
        collapse_base_args(["a/X-video.flac", "b/X-hifi.flac"])


def test_collapse_base_args_nothing_derivable_is_an_error():
    with pytest.raises(ToolError, match="Cannot derive"):
        collapse_base_args(["d/a.txt", "d/b.txt"])


def test_collapse_base_args_decoded_artifacts(tmp_path):
    strip = suffix_stripper(*DECODED_SUFFIXES)
    values = [
        str(tmp_path / "X-video.tbc"),
        str(tmp_path / "X-video.tbc.json"),
        str(tmp_path / "X-video_chroma.tbc"),
        str(tmp_path / "X-hifi.aligned.flac"),
        str(tmp_path / "X-linear.aligned.flac"),
    ]
    assert collapse_base_args(values, strip) == str(tmp_path / "X")


def test_suffix_stripper_strips_only_known_suffixes():
    strip = suffix_stripper(".ffv1.mkv", ".hifi.opus")
    assert strip("X.ffv1.mkv") == "X"
    assert strip("X.hifi.opus") == "X"
    assert strip("X.other") == "X.other"
