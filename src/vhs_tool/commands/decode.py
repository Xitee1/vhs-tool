"""vhs-tool decode — wrapper for vhs-decode: RF FLAC → TBC + JSON.

Port of tools/3_decode.sh.

Input: a single <base> path (e.g. "./captures/VHS_PAL_Tape_010"). Resolves:

  <base>-video.flac                    Video RF capture input

  <output>/<base>-video.tbc            Luma TBC
  <output>/<base>-video_chroma.tbc     Chroma TBC
  <output>/<base>-video.tbc.json       Decode metadata (needed by audio pipeline)

Requires: vhs-decode (default: ./tools/vhs-decode/.venv/bin/vhs-decode)

Based on https://github.com/oyvindln/vhs-decode/wiki/Command-List
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
import time
from pathlib import Path

from ..common import ToolError, collapse_base_args, derive_base, log, resolve_binary, run
from ..config import get_config

_CFG = get_config()

# -- Defaults (config file values, override via env or CLI) -----------------------
OUT_DIR = os.environ.get("OUT_DIR", _CFG.paths.decoded)
VHS_DECODE_BIN = os.environ.get("VHS_DECODE_BIN", _CFG.binaries.vhs_decode)

TV_SYSTEM = os.environ.get("TV_SYSTEM", _CFG.defaults.tv_system)
TAPE_FORMAT = os.environ.get("TAPE_FORMAT", _CFG.defaults.tape_format)
TAPE_SPEED = os.environ.get("TAPE_SPEED", "")  # sp | lp | ep/slp (empty = default/SP)
SAMPLE_FREQ = os.environ.get("SAMPLE_FREQ", "40")  # MHz — 40 for CX+clockgen
THREADS = os.environ.get("THREADS", "4")

# =============================================================================
# Pure helpers (testable)
# =============================================================================


def build_command(
    args: argparse.Namespace, vhs_decode_bin: str, input_file: Path, output_base: Path
) -> list[str]:
    """Assemble the vhs-decode invocation (flag order matches 3_decode.sh)."""
    cmd = [
        vhs_decode_bin,
        "--system", args.system,
        "--tape_format", args.format,
        "-f", args.freq,
        "--threads", args.threads,
    ]  # fmt: skip
    if args.speed:
        cmd += ["--tape_speed", args.speed]

    if not args.no_debug:
        cmd.append("--debug")
    if not args.no_ire0_adjust:
        cmd.append("--ire0_adjust")
    if args.chroma_trap:
        cmd.append("--ct")
    if args.nld:
        cmd.append("--nld")
    if args.sub_deemph:
        cmd.append("--sd")
    if args.dctp:
        cmd.append("--dctp")
    if args.use_saved_levels:
        cmd.append("--use_saved_levels")
    if args.overwrite:
        cmd.append("--overwrite")

    if args.y_comb is not None:
        cmd.append("--y_comb")
        if args.y_comb:
            cmd.append(args.y_comb)

    if args.sharpness:
        cmd += ["--sl", args.sharpness]

    if args.start:
        cmd += ["-s", args.start]
    if args.length:
        cmd += ["-l", args.length]
    if args.start_fileloc:
        cmd += ["--start_fileloc", args.start_fileloc]

    if args.extra:
        try:
            cmd += shlex.split(args.extra)
        except ValueError as exc:
            raise ToolError(f"Cannot parse --extra: {exc}") from exc

    cmd += [str(input_file), str(output_base)]
    return cmd


# =============================================================================
# Argument parsing
# =============================================================================


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "decode",
        help="Decode video RF capture to TBC + JSON (vhs-decode wrapper)",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "base",
        nargs="+",
        help='Path-prefix to the tape (e.g. "./captures/VHS_PAL_Tape_010"); '
        "a full -video.flac file name or a wildcard over the capture's files works too",
    )

    group = parser.add_argument_group("decode settings")
    group.add_argument(
        "--system",
        default=TV_SYSTEM,
        help=f"TV system (pal|ntsc|pal-m|ntsc-j|mesecam) (default: {TV_SYSTEM})",
    )
    group.add_argument(
        "--format",
        default=TAPE_FORMAT,
        help=f"Tape format (vhs|svhs|vhshq|umatic|...) (default: {TAPE_FORMAT})",
    )
    group.add_argument(
        "--speed", default=TAPE_SPEED, help="Tape speed (sp|lp|ep|slp|vp) (default: auto/SP)"
    )
    group.add_argument(
        "--freq", default=SAMPLE_FREQ, help=f"RF sample rate in MHz (default: {SAMPLE_FREQ})"
    )
    group.add_argument(
        "--threads", default=THREADS, help=f"Processing threads (default: {THREADS})"
    )
    group.add_argument("--sharpness", help="Output sharpness level 0-100 (default: decoder 0)")

    group = parser.add_argument_group("TBC / image control")
    group.add_argument("--no-debug", action="store_true", help="Disable --debug logging")
    group.add_argument("--no-ire0-adjust", action="store_true", help="Disable --ire0_adjust")
    group.add_argument("--chroma-trap", action="store_true", help="Enable chroma trap (--ct)")
    group.add_argument(
        "--y-comb",
        nargs="?",
        const="",
        metavar="IRE",
        help="Enable Y comb filter, optional IRE limit",
    )
    group.add_argument("--nld", action="store_true", help="Enable non-linear deemphasis")
    group.add_argument("--sub-deemph", action="store_true", help="Enable sub deemphasis")
    group.add_argument(
        "--dctp",
        action="store_true",
        help="Detect/correct chroma track phase changes (multi-session tapes)",
    )
    group.add_argument(
        "--use-saved-levels",
        action="store_true",
        help="Skip per-frame level detect (single-recording tapes)",
    )
    group.add_argument("--overwrite", action="store_true", help="Overwrite existing TBC files")

    group = parser.add_argument_group("time control")
    group.add_argument("-s", "--start", help="Start at frame number")
    group.add_argument("-l", "--length", help="Decode only N frames")
    group.add_argument("--start-fileloc", help="Start at sample number")

    group = parser.add_argument_group("paths")
    group.add_argument("--output", default=OUT_DIR, help=f"Output directory (default: {OUT_DIR})")
    group.add_argument("--input", help="Override input file path")
    group.add_argument(
        "--vhs-decode",
        dest="vhs_decode",
        default=VHS_DECODE_BIN,
        help=f"vhs-decode binary path (default: {VHS_DECODE_BIN})",
    )

    group = parser.add_argument_group("general")
    group.add_argument(
        "--extra", default="", help="Extra vhs-decode flags (quoted string, shell-style splitting)"
    )
    group.add_argument("--dry-run", action="store_true", help="Print commands without executing")
    group.add_argument("-v", "--verbose", action="store_true", help="Echo every executed command")
    parser.set_defaults(func=cmd_decode)


# =============================================================================
# Command
# =============================================================================


def cmd_decode(args: argparse.Namespace) -> int:
    base_path = Path(collapse_base_args(args.base))
    input_dir = base_path.parent
    base = derive_base(base_path.name)
    if not base:
        raise ToolError("Could not derive base name from input")

    input_file = Path(args.input) if args.input else input_dir / f"{base}-video.flac"
    # Output base (vhs-decode appends .tbc, _chroma.tbc, .tbc.json itself)
    output_base = Path(args.output) / f"{base}-video"

    # -- Validation ---------------------------------------------------------
    Path(args.output).mkdir(parents=True, exist_ok=True)

    if not input_file.is_file():
        raise ToolError(f"Video RF input not found: {input_file}")

    tbc_file = Path(f"{output_base}.tbc")
    if not args.overwrite and tbc_file.is_file():
        raise ToolError(f"Output already exists: {tbc_file} (use --overwrite to replace)")

    vhs_decode_bin = resolve_binary(args.vhs_decode, "vhs-decode")

    cmd = build_command(args, vhs_decode_bin, input_file, output_base)

    # -- Summary --------------------------------------------------------------
    log(f"Base:          {base}")
    log(f"Input:         {input_file}")
    log(f"Output base:   {output_base}")
    log(
        f"System:        {args.system}  |  Format: {args.format}  |  "
        f"Speed: {args.speed or 'SP (default)'}"
    )
    log(f"Sample rate:   {args.freq} MHz  |  Threads: {args.threads}")

    flags = []
    if not args.no_debug:
        flags.append("debug")
    if not args.no_ire0_adjust:
        flags.append("ire0_adjust")
    if args.chroma_trap:
        flags.append("chroma_trap")
    if args.y_comb is not None:
        flags.append(f"y_comb={args.y_comb}" if args.y_comb else "y_comb")
    if args.nld:
        flags.append("nld")
    if args.sub_deemph:
        flags.append("sub_deemph")
    if args.use_saved_levels:
        flags.append("saved_levels")
    if args.dctp:
        flags.append("dctp")
    if args.overwrite:
        flags.append("overwrite")
    if args.sharpness:
        flags.append(f"sharpness={args.sharpness}")
    if flags:
        log(f"Flags:         {' '.join(flags)}")

    if args.start or args.length or args.start_fileloc:
        log(
            f"Range:         start={args.start or 0}  length={args.length or '(full)'}  "
            f"fileloc={args.start_fileloc or '(none)'}"
        )

    if args.extra:
        log(f"Extra args:    {args.extra}")
    print(file=sys.stderr)

    # -- Run ------------------------------------------------------------------
    log("========== vhs-decode ==========")
    log(f"Command: {shlex.join(cmd)}")
    print(file=sys.stderr)

    start_ts = time.time()
    if args.dry_run or args.verbose:
        print(f">>> {shlex.join(cmd)}", file=sys.stderr)
    if not args.dry_run:
        run(cmd)
    elapsed = int(time.time() - start_ts)

    print(file=sys.stderr)
    log(f"Decode finished in {elapsed}s")

    # Quick sanity check on output
    if not args.dry_run:
        for file in (tbc_file, Path(f"{output_base}.tbc.json")):
            if file.is_file():
                log(f"  ✓ {file.name}  ({file.stat().st_size} bytes)")
            else:
                log(f"  ✗ {file.name} — MISSING (decode may have failed)")
        chroma_file = Path(f"{output_base}_chroma.tbc")
        if chroma_file.is_file():
            log(f"  ✓ {chroma_file.name}  ({chroma_file.stat().st_size} bytes)")

    log("All done.")
    return 0
