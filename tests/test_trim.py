import argparse
from pathlib import Path

import pytest

from vhs_tool.commands import trim
from vhs_tool.commands.trim import (
    compute_trim,
    derive_base,
    fmt_timestamp,
    parse_timestamp,
    resolve_mode,
    trim_file,
    work_paths,
)
from vhs_tool.common import ToolError
from vhs_tool.flac import FlacInfo


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


# =============================================================================
# trim_file / cmd_trim (filesystem behavior; sox/soxi are monkeypatched)
# =============================================================================

SAMPLES = 100_000_000  # with rate 10000 Hz × rf_scale 1000 → 10 s of RF
RF_INFO = FlacInfo("a" * 32, 65535, 65535, 10_000, 1, 8)


@pytest.fixture
def patched_io(monkeypatch):
    """Stub out the external tools the trim module drives."""
    monkeypatch.setattr(trim, "check_deps", lambda *cmds: None)
    monkeypatch.setattr(trim, "get_samples", lambda file: SAMPLES)
    monkeypatch.setattr(trim, "soxi", lambda file, flag: 10_000)
    monkeypatch.setattr(trim, "read_flac_info", lambda file: RF_INFO)
    monkeypatch.setattr(trim, "set_level_tag", lambda file, level: True)
    monkeypatch.setattr(trim, "detect_threads", lambda requested=None: 0)


def _encode_ok(file, out, info, keep_samples, **kwargs):
    """Successful encode stand-in: writes the tmp output file."""
    if kwargs.get("dry_run"):
        return
    Path(out).write_bytes(b"trimmed")


def _encode_fail(file, out, info, keep_samples, **kwargs):
    """Failing encode stand-in: leaves a partial tmp behind, then errors out."""
    Path(out).write_bytes(b"partial")
    raise ToolError("flac exited with code 2")


def _args(base, **overrides):
    ns = argparse.Namespace(
        base=str(base),
        end=None,
        trim="2",
        delete_original=False,
        offset=4.0,
        rf_scale=1000,
        threads=None,
        dry_run=False,
        verbose=False,
    )
    for key, value in overrides.items():
        setattr(ns, key, value)
    return ns


def test_trim_file_dry_run_flac_writes_nothing(tmp_path, monkeypatch, patched_io):
    calls = []
    monkeypatch.setattr(trim, "encode_head", lambda *a, **kw: calls.append(kw))
    file = tmp_path / "Tape-video.flac"
    file.write_bytes(b"original")

    assert trim_file(file, 0.8, known_samples=SAMPLES, dry_run=True) is True

    assert [c["dry_run"] for c in calls] == [True]  # nothing encoded for real
    tmp, bak = work_paths(file)
    assert not tmp.exists()
    assert not bak.exists()
    assert file.read_bytes() == b"original"


def test_trim_file_failed_encode_unlinks_tmp(tmp_path, monkeypatch, patched_io):
    monkeypatch.setattr(trim, "encode_head", _encode_fail)
    file = tmp_path / "Tape-video.flac"
    file.write_bytes(b"original")

    with pytest.raises(ToolError, match="exited with code 2"):
        trim_file(file, 0.8, known_samples=SAMPLES)

    tmp, bak = work_paths(file)
    assert not tmp.exists()  # partial output must not poison the next run
    assert not bak.exists()
    assert file.read_bytes() == b"original"


def test_cmd_trim_preflight_refuses_stale_files(tmp_path, monkeypatch, patched_io):
    calls = []
    monkeypatch.setattr(trim, "encode_head", lambda *a, **kw: calls.append(a))
    video = tmp_path / "Tape-video.flac"
    hifi = tmp_path / "Tape-hifi.flac"
    video.write_bytes(b"video")
    hifi.write_bytes(b"hifi")
    work_paths(hifi)[1].write_bytes(b"stale")  # leftover hifi backup

    with pytest.raises(ToolError, match="Stale temp/backup"):
        trim.cmd_trim(_args(tmp_path / "Tape"))

    assert calls == []  # nothing was touched, not even the clean video file
    assert video.read_bytes() == b"video"


def test_cmd_trim_delete_original_deferred_until_all_succeed(tmp_path, monkeypatch, patched_io):
    def fake_encode(file, out, *args, **kwargs):
        if "hifi" in Path(file).name:
            _encode_fail(file, out, *args, **kwargs)
        _encode_ok(file, out, *args, **kwargs)

    monkeypatch.setattr(trim, "encode_head", fake_encode)
    video = tmp_path / "Tape-video.flac"
    hifi = tmp_path / "Tape-hifi.flac"
    video.write_bytes(b"video")
    hifi.write_bytes(b"hifi")

    rc = trim.cmd_trim(_args(tmp_path / "Tape", delete_original=True))

    assert rc == 1  # a failed file must not exit 0
    # video was trimmed, but its backup survives because hifi failed
    assert video.read_bytes() == b"trimmed"
    assert work_paths(video)[1].read_bytes() == b"video"
    # the failed hifi is untouched and left no tmp behind
    assert hifi.read_bytes() == b"hifi"
    assert not work_paths(hifi)[0].exists()
    assert not work_paths(hifi)[1].exists()


def test_cmd_trim_delete_original_after_full_success(tmp_path, monkeypatch, patched_io):
    monkeypatch.setattr(trim, "encode_head", _encode_ok)
    video = tmp_path / "Tape-video.flac"
    hifi = tmp_path / "Tape-hifi.flac"
    video.write_bytes(b"video")
    hifi.write_bytes(b"hifi")

    rc = trim.cmd_trim(_args(tmp_path / "Tape", delete_original=True))

    assert rc == 0
    assert video.read_bytes() == b"trimmed"
    assert hifi.read_bytes() == b"trimmed"
    assert not work_paths(video)[1].exists()
    assert not work_paths(hifi)[1].exists()


def test_cmd_trim_errors_return_one(tmp_path, monkeypatch, patched_io):
    monkeypatch.setattr(trim, "encode_head", _encode_fail)
    video = tmp_path / "Tape-video.flac"
    video.write_bytes(b"video")

    rc = trim.cmd_trim(_args(tmp_path / "Tape"))

    assert rc == 1
    assert video.read_bytes() == b"video"
    assert not work_paths(video)[0].exists()


def test_cmd_trim_dry_run_leaves_no_files(tmp_path, monkeypatch, patched_io):
    calls = []
    monkeypatch.setattr(trim, "encode_head", lambda *a, **kw: calls.append(kw))
    video = tmp_path / "Tape-video.flac"
    hifi = tmp_path / "Tape-hifi.flac"
    video.write_bytes(b"video")
    hifi.write_bytes(b"hifi")

    rc = trim.cmd_trim(_args(tmp_path / "Tape", dry_run=True, delete_original=True))

    assert rc == 0
    assert all(c["dry_run"] for c in calls)
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "Tape-hifi.flac",
        "Tape-video.flac",
    ]
    assert video.read_bytes() == b"video"
    assert hifi.read_bytes() == b"hifi"
