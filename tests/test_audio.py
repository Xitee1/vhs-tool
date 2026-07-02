import pytest

from vhs_tool.cli import build_parser
from vhs_tool.commands.audio import derive_base, parse_peak_gain, validate_hifi
from vhs_tool.common import ToolError


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("VHS_PAL_Tape_010", "VHS_PAL_Tape_010"),
        ("VHS_PAL_Tape_010-hifi.flac", "VHS_PAL_Tape_010"),
        ("VHS_PAL_Tape_010-linear.flac", "VHS_PAL_Tape_010"),
        ("VHS_PAL_Tape_010-hifi.wav", "VHS_PAL_Tape_010"),
        ("VHS_PAL_Tape_010-linear", "VHS_PAL_Tape_010"),
    ],
)
def test_derive_base(name, expected):
    assert derive_base(name) == expected


def test_parse_peak_gain():
    assert parse_peak_gain("... Peak gain is 349.17%. ...") == 349.17
    assert parse_peak_gain("Decoding...\nPeak gain is 1.5%.\nDone") == 1.5
    assert parse_peak_gain("no gain reported") is None
    assert parse_peak_gain("") is None


def test_validate_hifi():
    assert validate_hifi(349.17, 5.0) is True
    assert validate_hifi(5.0, 5.0) is True  # at threshold = valid (bash: fails only if <)
    assert validate_hifi(1.5, 5.0) is False
    assert validate_hifi(None, 5.0) is True  # unparsable → pass through unvalidated


def test_hifi_output_overwrite_guard(tmp_path):
    # Regression: with input dir == output dir, hifi-decode would write over its own
    # RF input and the intermediate cleanup would then delete the original capture.
    (tmp_path / "Tape-video.tbc.json").touch()
    (tmp_path / "Tape-hifi.flac").touch()

    args = build_parser().parse_args(["audio", str(tmp_path / "Tape"), "--output", str(tmp_path)])
    with pytest.raises(ToolError, match="overwrite its RF input"):
        args.func(args)
