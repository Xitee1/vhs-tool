"""Top-level argument parser and entry point for vhs-tool."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .commands import encode, export, rf_resample, upload
from .common import ToolError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vhs-tool",
        description="Unified CLI for the VHS decode pipeline.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command", required=True, metavar="<command>")
    export.add_parser(subparsers)
    encode.add_parser(subparsers)
    upload.add_parser(subparsers)
    rf_resample.add_parser(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ToolError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
        return 130
