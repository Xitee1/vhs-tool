import argparse
import os

import pytest

from vhs_tool.commands.export import needs_compat_shim, parse_only, write_compat_shim


def test_single_part():
    assert parse_only("video") == ["video"]


def test_comma_separated_list():
    assert parse_only("linear,video") == ["linear", "video"]
    assert parse_only("linear,hifi,video") == ["linear", "hifi", "video"]


def test_whitespace_and_empty_items_ignored():
    assert parse_only(" linear , video ") == ["linear", "video"]
    assert parse_only("linear,,video,") == ["linear", "video"]


def test_invalid_part_rejected():
    with pytest.raises(argparse.ArgumentTypeError, match="invalid part"):
        parse_only("linear,audio")


def test_empty_value_rejected():
    with pytest.raises(argparse.ArgumentTypeError, match="at least one"):
        parse_only(",")


# -- tbc-tools compat shim ------------------------------------------------------


def test_needs_compat_shim_new_build():
    # Only new builds answer to the new tool name
    assert needs_compat_shim("tbc-export-metadata ld-decode-tools - Branch: nix / Commit: abc\n")


def test_needs_compat_shim_old_build():
    # Old builds fall back to ld-analyse for unknown tool names
    assert not needs_compat_shim(
        "Debug: Version - Git branch: vhs_decode\nld-analyse Branch: vhs_decode\n"
    )


def test_needs_compat_shim_failed_probe():
    assert not needs_compat_shim("")


def test_write_compat_shim(tmp_path):
    appimage = tmp_path / "tbc-tools-x86_64.AppImage"
    appimage.touch()
    shim = write_compat_shim(appimage, tmp_path)
    assert shim.is_file()
    assert os.access(shim, os.X_OK)
    content = shim.read_text()
    assert str(appimage.resolve()) in content
    assert 'tool="tbc-export-metadata"' in content
