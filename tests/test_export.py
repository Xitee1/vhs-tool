import argparse

import pytest

from vhs_tool.commands.export import parse_only


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
