from pathlib import Path

import pytest

from vhs_tool.cli import build_parser
from vhs_tool.commands.decode import build_command, derive_base
from vhs_tool.common import ToolError


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("VHS_PAL_Tape_010", "VHS_PAL_Tape_010"),
        ("VHS_PAL_Tape_010-video.flac", "VHS_PAL_Tape_010"),
        ("VHS_PAL_Tape_010-video", "VHS_PAL_Tape_010"),
        ("VHS_PAL_Tape_010-video.r16", "VHS_PAL_Tape_010"),
        ("VHS_PAL_Tape_010.lds", "VHS_PAL_Tape_010"),
    ],
)
def test_derive_base(name, expected):
    assert derive_base(name) == expected


def _build(*argv: str) -> list[str]:
    args = build_parser().parse_args(["decode", "tape", *argv])
    return build_command(args, "vhs-decode", Path("in.flac"), Path("out/tape-video"))


def test_build_command_defaults():
    assert _build() == [
        "vhs-decode",
        "--system", "pal",
        "--tape_format", "vhs",
        "-f", "40",
        "--threads", "4",
        "--debug",
        "--ire0_adjust",
        "in.flac",
        "out/tape-video",
    ]  # fmt: skip


def test_build_command_disable_defaults():
    cmd = _build("--no-debug", "--no-ire0-adjust")
    assert "--debug" not in cmd
    assert "--ire0_adjust" not in cmd


def test_build_command_flags():
    cmd = _build(
        "--speed", "lp", "--chroma-trap", "--nld", "--sub-deemph", "--recheck-phase",
        "--dctp", "--use-saved-levels", "--overwrite", "--sharpness", "50",
    )  # fmt: skip
    for flag in (
        "--ct",
        "--nld",
        "--sd",
        "--recheck_phase",
        "-dctp",
        "--use_saved_levels",
        "--overwrite",
    ):
        assert flag in cmd
    assert cmd[cmd.index("--tape_speed") + 1] == "lp"
    assert cmd[cmd.index("--sl") + 1] == "50"


def test_build_command_y_comb():
    assert "--y_comb" not in _build()
    cmd = _build("--y-comb")
    assert "--y_comb" in cmd
    assert cmd[cmd.index("--y_comb") + 1] == "in.flac"  # no IRE value follows
    cmd = _build("--y-comb", "10")
    assert cmd[cmd.index("--y_comb") + 1] == "10"


def test_build_command_range_and_extra():
    cmd = _build("--start", "100", "--length", "500", "--start-fileloc", "1234")
    assert cmd[cmd.index("-s") + 1] == "100"
    assert cmd[cmd.index("-l") + 1] == "500"
    assert cmd[cmd.index("--start_fileloc") + 1] == "1234"

    cmd = _build("--extra", "--foo bar --baz")
    assert cmd[-5:] == ["--foo", "bar", "--baz", "in.flac", "out/tape-video"]


def test_build_command_extra_quoted():
    cmd = _build("--extra", "--note 'two words'")
    assert cmd[-4:] == ["--note", "two words", "in.flac", "out/tape-video"]

    with pytest.raises(ToolError, match="Cannot parse --extra"):
        _build("--extra", "--note 'unbalanced")


def test_overwrite_guard_with_dotted_base(tmp_path):
    # Regression: with_suffix() mangled dotted base names (Tape.2024-video → Tape.tbc),
    # so the guard checked the wrong path and existing output was silently overwritten.
    base = tmp_path / "Tape.2024"
    (tmp_path / "Tape.2024-video.flac").touch()
    out_dir = tmp_path / "decoded"
    out_dir.mkdir()
    (out_dir / "Tape.2024-video.tbc").touch()

    args = build_parser().parse_args(["decode", str(base), "--output", str(out_dir)])
    with pytest.raises(ToolError, match=r"Output already exists: .*Tape\.2024-video\.tbc"):
        args.func(args)
