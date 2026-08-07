"""Tests for the pure helpers of `vhs-tool process-teletext`."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from vhs_tool.commands import process_teletext as pt
from vhs_tool.common import ToolError


def make_args(**overrides) -> argparse.Namespace:
    """A namespace with the argparse defaults of the deconvolve/squash groups."""
    defaults = {
        "card": "tbc",
        "tape_format": "vhs",
        "keep_empty": True,
        "force_cpu": False,
        "threads": None,
        "limit": None,
        "min_duplicates": 3,
        "ignore_empty": False,
    }
    return argparse.Namespace(**{**defaults, **overrides})


# -- parse_only ----------------------------------------------------------------


def test_parse_only_single_and_list():
    assert pt.parse_only("squash") == ["squash"]
    assert pt.parse_only("deconvolve, html") == ["deconvolve", "html"]


@pytest.mark.parametrize("value", ["", " , ", "encode", "squash,nope"])
def test_parse_only_rejects_invalid(value):
    with pytest.raises(argparse.ArgumentTypeError):
        pt.parse_only(value)


# -- resolve_input -------------------------------------------------------------


def test_resolve_input_from_base_path():
    tbc, base = pt.resolve_input("./decoded/VHS_PAL_Tape_0013-2026-05-09_13_52_37_02_00")
    assert tbc == Path("./decoded/VHS_PAL_Tape_0013-2026-05-09_13_52_37_02_00-video.tbc")
    assert base == "VHS_PAL_Tape_0013-2026-05-09_13_52_37_02_00"


def test_resolve_input_from_tbc_file():
    tbc, base = pt.resolve_input("./decoded/VHS_PAL_Tape_0013-video.tbc")
    assert tbc == Path("./decoded/VHS_PAL_Tape_0013-video.tbc")
    assert base == "VHS_PAL_Tape_0013"


def test_resolve_input_rejects_chroma_tbc():
    with pytest.raises(ToolError, match="chroma TBC"):
        pt.resolve_input("./decoded/VHS_PAL_Tape_0013-video_chroma.tbc")


def test_default_output_dir_is_a_folder_next_to_the_exports():
    outdir = pt.default_output_dir("VHS_PAL_Tape_0013")
    assert outdir.name == "VHS_PAL_Tape_0013.teletext"
    assert outdir.parent == Path("./export")


# -- Command builders ----------------------------------------------------------


def test_deconvolve_command_defaults():
    cmd = pt.build_deconvolve_command(
        "teletext", make_args(), Path("in-video.tbc"), Path("out/in.t42")
    )
    assert cmd == [
        "teletext",
        "deconvolve",
        "--card",
        "tbc",
        "--tape-format",
        "vhs",
        "--keep-empty",
        "--output",
        "bytes",
        "out/in.t42",
        "in-video.tbc",
    ]


def test_deconvolve_command_optional_flags():
    args = make_args(
        card="tbc-vbi",
        tape_format="betamax",
        keep_empty=False,
        force_cpu=True,
        threads=4,
        limit=100,
    )
    cmd = pt.build_deconvolve_command("teletext", args, Path("in.tbc"), Path("out.t42"))
    assert "--keep-empty" not in cmd
    assert cmd[:6] == ["teletext", "deconvolve", "--card", "tbc-vbi", "--tape-format", "betamax"]
    assert "--force-cpu" in cmd
    assert cmd[cmd.index("--threads") + 1] == "4"
    assert cmd[cmd.index("--limit") + 1] == "100"
    # The input file stays last, the output pair right before it
    assert cmd[-4:] == ["--output", "bytes", "out.t42", "in.tbc"]


def test_squash_command():
    cmd = pt.build_squash_command("teletext", make_args(), Path("in.t42"), Path("out.squash.t42"))
    assert cmd == [
        "teletext",
        "squash",
        "--min-duplicates",
        "3",
        "--output",
        "bytes",
        "out.squash.t42",
        "in.t42",
    ]


def test_squash_command_options():
    args = make_args(min_duplicates=5, ignore_empty=True)
    cmd = pt.build_squash_command("teletext", args, Path("in.t42"), Path("out.t42"))
    assert cmd[cmd.index("--min-duplicates") + 1] == "5"
    assert "--ignore-empty" in cmd


def test_has_packets_detects_an_all_padding_stream(tmp_path):
    # `deconvolve --keep-empty` writes 42 zero bytes per unreadable line
    empty = tmp_path / "empty.t42"
    empty.write_bytes(bytes(42 * 100))
    assert pt.has_packets(empty) is False

    real = tmp_path / "real.t42"
    real.write_bytes(bytes(42 * 100) + b"\x15" + bytes(41))
    assert pt.has_packets(real) is True


def test_has_packets_on_zero_length_file(tmp_path):
    stream = tmp_path / "nothing.t42"
    stream.write_bytes(b"")
    assert pt.has_packets(stream) is False


def test_has_packets_spanning_chunk_boundary(tmp_path):
    stream = tmp_path / "late.t42"
    stream.write_bytes(bytes(1024) + b"\x01")
    assert pt.has_packets(stream, chunk_size=64) is True


def test_pages_and_html_commands():
    assert pt.build_pages_command("teletext", Path("in.t42")) == [
        "teletext",
        "list",
        "--count",
        "in.t42",
    ]
    assert pt.build_html_command("teletext", Path("in.t42"), Path("out/html")) == [
        "teletext",
        "html",
        "out/html",
        "in.t42",
    ]


def test_html_command_with_language():
    assert pt.build_html_command("teletext", Path("in.t42"), Path("out/html"), "ger") == [
        "teletext",
        "html",
        "--localcodepage",
        "ger",
        "out/html",
        "in.t42",
    ]


def test_html_command_omits_an_empty_language():
    # Empty means "don't pass the option": which subsets exist depends on the
    # installed vhs-teletext, so vhs-tool must not invent a default.
    assert "--localcodepage" not in pt.build_html_command(
        "teletext", Path("in.t42"), Path("out/html"), ""
    )
