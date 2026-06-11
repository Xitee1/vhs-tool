import pytest

from vhs_tool.commands.upload import (
    build_notes,
    build_youtube_description,
    ia_identifier,
    normalize_platform,
    parse_capture_date,
)
from vhs_tool.common import ToolError, seconds_to_hms, seconds_to_yt_ts
from vhs_tool.encoding import strip_profile_suffix

BASE = "VHS_PAL_Tape_0013_Matze-2026-06-05_17_17_14_02_00"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (f"{BASE}_anime", BASE),
        (f"{BASE}_liveaction", BASE),
        (f"{BASE}_anime-youtube", BASE),
        (f"{BASE}_liveaction-youtube", BASE),
        (f"{BASE}_youtube", BASE),
        (BASE, BASE),
    ],
)
def test_strip_profile_suffix(name, expected):
    assert strip_profile_suffix(name) == expected


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
    assert desc.startswith(f"{BASE}\n\nDigitalisiert mit vhs-decode.\n")
    assert "Aufnahmedatum: 1998\n" in desc
    assert "Teletext: ja (im Internet Archive enthalten)\n" in desc
    assert "Extra line.\n" in desc
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
