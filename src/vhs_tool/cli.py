"""Top-level argument parser and entry point for vhs-tool."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .common import ToolError


def build_parser() -> argparse.ArgumentParser:
    # Imported here so config-file errors surface in main()'s ToolError handler
    # (command modules read the config at import time for their argparse defaults).
    from .commands import audio, decode, encode, export, rf_resample, set_props, upload

    parser = argparse.ArgumentParser(
        prog="vhs-tool",
        description="Unified CLI for the VHS decode pipeline.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command", required=True, metavar="<command>")
    decode.add_parser(subparsers)
    audio.add_parser(subparsers)
    export.add_parser(subparsers)
    encode.add_parser(subparsers)
    upload.add_parser(subparsers)
    rf_resample.add_parser(subparsers)
    set_props.add_parser(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return args.func(args)
    except ToolError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
        return 130
