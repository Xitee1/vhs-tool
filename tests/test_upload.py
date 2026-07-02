from pathlib import Path

import pytest

import vhs_tool.commands.upload as upload
from vhs_tool.commands.upload import (
    build_notes,
    build_youtube_description,
    copy_into,
    encode_atomic,
    find_rf,
    find_uncompressed_rf,
    ia_identifier,
    normalize_platform,
    parse_capture_date,
)
from vhs_tool.common import ToolError, seconds_to_hms, seconds_to_yt_ts

BASE = "VHS_PAL_Tape_0013_Matze-2026-06-05_17_17_14_02_00"


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("ia", "ia"),
        ("IA", "ia"),
        ("archive", "ia"),
        ("archive.org", "ia"),
        ("yt", "youtube"),
        ("YouTube", "youtube"),
    ],
)
def test_normalize_platform(platform, expected):
    assert normalize_platform(platform) == expected


def test_normalize_platform_unknown():
    with pytest.raises(ToolError, match="Unknown platform"):
        normalize_platform("vimeo")


def test_seconds_to_hms_rounds():
    assert seconds_to_hms(3723.5) == "01:02:04"
    assert seconds_to_hms(59.4) == "00:00:59"


def test_seconds_to_yt_ts():
    assert seconds_to_yt_ts(0) == "0:00"
    assert seconds_to_yt_ts(75) == "1:15"
    assert seconds_to_yt_ts(59.6) == "1:00"
    assert seconds_to_yt_ts(3600) == "1:00:00"
    assert seconds_to_yt_ts(3675) == "1:01:15"


def test_parse_capture_date():
    assert parse_capture_date(BASE) == ("2026-06-05", "05.06.2026")


def test_parse_capture_date_missing():
    assert parse_capture_date("Some_Tape_Without_Timestamp") == ("unknown", "unbekannt")


def test_find_rf(tmp_path):
    (tmp_path / "VHS_PAL_Tape_010-video.flac").touch()
    assert find_rf([str(tmp_path)], "VHS_PAL_Tape_010", "-video.flac") is not None
    assert find_rf([str(tmp_path)], "VHS_PAL_Tape_010", "-hifi.flac") is None


def test_find_uncompressed_rf_flags_non_flac(tmp_path):
    base = "VHS_PAL_Tape_010"
    # FLAC captures are accepted (not flagged).
    (tmp_path / f"{base}-video.flac").touch()
    (tmp_path / f"{base}-headswitch.flac").touch()
    # The 20 MSPS downsample and unrelated files must not be flagged.
    (tmp_path / f"{base}-video.8bit.20msps.flac").touch()
    (tmp_path / f"{base}-info.txt").touch()
    assert find_uncompressed_rf([str(tmp_path)], base) == []

    # Raw/other-format captures for known channels are flagged.
    (tmp_path / f"{base}-headswitch.u8").touch()
    (tmp_path / f"{base}-hifi.wav").touch()
    flagged = sorted(p.name for p in find_uncompressed_rf([str(tmp_path)], base))
    assert flagged == [f"{base}-headswitch.u8", f"{base}-hifi.wav"]


def test_ia_identifier():
    assert ia_identifier("VHS_PAL_Tape_0013") == "vhs-pal-tape-0013"
    # '_' → '-' and repeated dashes are squeezed
    assert ia_identifier("A__B--C") == "a-b-c"


def test_youtube_description_full():
    desc = build_youtube_description(
        base=BASE,
        recording_date="1998",
        capture_date_de="05.06.2026",
        ia_url="https://archive.org/details/x",
        teletext=True,
        extra_text="Extra line.",
        chapters=[(10.0, "Intro"), (3675.0, "Part 2")],
    )
    assert desc.startswith(f"{BASE}\n\nInfo:\nExtra line.\n\nDigitalisiert mit vhs-decode.\n")
    assert "Aufnahmedatum: 1998\n" in desc
    assert "Teletext: ja (im Internet Archive enthalten)\n" in desc
    # First chapter starts at 10s ≥ 0.5s → implicit start chapter prepended
    assert "Kapitel:\n0:00 Start\n0:10 Intro\n1:01:15 Part 2\n" in desc
    assert desc.endswith("Domesday86 Discord: https://discord.gg/pVVrrxd\n")


def test_youtube_description_minimal():
    desc = build_youtube_description(
        base=BASE,
        recording_date="",
        capture_date_de="unbekannt",
        ia_url="url",
        teletext=False,
        extra_text="",
        chapters=[(0.0, "Begin")],
    )
    assert "Aufnahmedatum:" not in desc
    assert "Teletext: nein\n" in desc
    # First chapter at 0s → no extra start chapter
    assert "Kapitel:\n0:00 Begin\n" in desc


def test_youtube_description_no_chapters():
    desc = build_youtube_description(
        base=BASE,
        recording_date="",
        capture_date_de="unbekannt",
        ia_url="url",
        teletext=False,
        extra_text="",
        chapters=[],
    )
    assert "Kapitel:" not in desc


def _notes(**overrides):
    kwargs = {
        "tape_notes": "",
        "tag_title": "",
        "tape_format": "VHS",
        "tape_speed": "SP",
        "tv_system": "PAL",
        "colour": "Yes",
        "has_hifi_rf": False,
        "has_hifi": False,
        "has_linear": False,
        "teletext": False,
        "runtime": "01:30:00",
        "recording_date": "unknown",
        "capture_date_iso": "2026-06-05",
        "vhs_decode_version": "v0.3.9",
        "extra_params": "--ire0_adjust",
    }
    kwargs.update(overrides)
    return build_notes(**kwargs)


def test_notes_minimal():
    notes = _notes()
    assert "Format: VHS SP\n" in notes
    assert "HiFi: No\n" in notes
    assert "Title:" not in notes
    assert "HiFi Sample Rate" not in notes
    assert "HiFi Decode" not in notes
    assert "Linear audio is genuine linear" not in notes
    assert "Teletext extracted from VBI" not in notes


def test_notes_full():
    notes = _notes(
        tape_notes="Tape slightly damaged.",
        tag_title="My Tape",
        has_hifi_rf=True,
        has_hifi=True,
        has_linear=True,
        teletext=True,
    )
    assert "Notes:\nTape slightly damaged.\n" in notes
    assert "Title: My Tape\n" in notes
    assert "HiFi: Yes\n" in notes
    assert "HiFi Sample Rate:       10 MSPS 8-bit" in notes
    assert "HiFi Decode:            hifi-decode\n" in notes
    assert "- Linear audio is genuine linear from Panasonic NV-VP30 audio head\n" in notes
    assert "- Teletext extracted from VBI, see teletext/ folder\n" in notes


def test_copy_into_fallback_copies_via_temp_name(tmp_path, monkeypatch):
    monkeypatch.setattr(upload.shutil, "which", lambda name: None)  # force shutil fallback
    src = tmp_path / "src.bin"
    src.write_bytes(b"payload")
    dstdir = tmp_path / "dst"
    dstdir.mkdir()
    copy_into(src, dstdir)
    assert (dstdir / "src.bin").read_bytes() == b"payload"
    assert not (dstdir / "src.bin.part").exists()


def test_copy_into_fallback_failure_leaves_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(upload.shutil, "which", lambda name: None)

    def broken_copy(src, dst):
        Path(dst).write_bytes(b"half-writ")
        raise OSError("disk full")

    monkeypatch.setattr(upload.shutil, "copy", broken_copy)
    src = tmp_path / "src.bin"
    src.write_bytes(b"payload")
    dstdir = tmp_path / "dst"
    dstdir.mkdir()
    with pytest.raises(OSError, match="disk full"):
        copy_into(src, dstdir)
    # Neither a partial on the final name nor a stray temp file is left behind.
    assert list(dstdir.iterdir()) == []


def test_encode_atomic_replaces_on_success(tmp_path, monkeypatch):
    dst = tmp_path / "tape_youtube.mkv"
    dst.write_text("old encode")
    stale = tmp_path / "tape_youtube.part.mkv"
    stale.write_text("stale partial")  # from a previous crash — must not survive

    def fake_encode(src, out, profile, *, loglevel=None):
        assert Path(out).suffix == ".mkv"  # muxer chosen by extension
        Path(out).write_text("new encode")

    monkeypatch.setattr(upload, "ffmpeg_encode", fake_encode)
    encode_atomic(tmp_path / "in.mkv", dst, None)
    assert dst.read_text() == "new encode"
    assert not stale.exists()


def test_encode_atomic_failure_keeps_old_file(tmp_path, monkeypatch):
    dst = tmp_path / "tape.preview.mp4"
    dst.write_text("old encode")

    def broken_encode(src, out, profile, *, loglevel=None):
        Path(out).write_text("half-writ")
        raise ToolError("ffmpeg exited with code 1")

    monkeypatch.setattr(upload, "ffmpeg_encode", broken_encode)
    with pytest.raises(ToolError):
        encode_atomic(tmp_path / "in.mkv", dst, None)
    assert dst.read_text() == "old encode"
    assert list(tmp_path.iterdir()) == [dst]  # no .part left behind
