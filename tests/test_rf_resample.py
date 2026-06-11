import pytest

from vhs_tool.commands.rf_resample import PRESETS, default_suffix, derive_base


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("VHS_PAL_Tape_010", "VHS_PAL_Tape_010"),
        ("VHS_PAL_Tape_010-video.flac", "VHS_PAL_Tape_010"),
        ("VHS_PAL_Tape_010-hifi.flac", "VHS_PAL_Tape_010"),
        ("VHS_PAL_Tape_010-linear.flac", "VHS_PAL_Tape_010"),
        ("VHS_PAL_Tape_010-headswitch.u8", "VHS_PAL_Tape_010"),
        ("VHS_PAL_Tape_010-video", "VHS_PAL_Tape_010"),
        ("VHS_PAL_Tape_010.lds", "VHS_PAL_Tape_010"),
        ("VHS_PAL_Tape_010-video.r16", "VHS_PAL_Tape_010"),
    ],
)
def test_derive_base(name, expected):
    assert derive_base(name) == expected


def test_default_suffix():
    # FLAC-scale: 20000 Hz represents 20 MSPS → '.8bit.20msps' (matches the
    # naming that `vhs-tool upload` and the wiki convention expect)
    assert default_suffix(8, 20000) == ".8bit.20msps"
    assert default_suffix(8, 16000) == ".8bit.16msps"
    assert default_suffix(16, 24000) == ".16bit.24msps"


def test_presets():
    assert PRESETS["pal"] == (20000, "0-9600")
    assert PRESETS["pal-min"] == (18000, "0-8670")
    assert PRESETS["ntsc"] == (16000, "0-7650")
    assert PRESETS["svhs"] == (24000, "0-9400")
