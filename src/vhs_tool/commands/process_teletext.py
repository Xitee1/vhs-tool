"""vhs-tool process-teletext — decoded TBC → teletext packet stream (t42) + HTML.

Extracts the teletext data that was broadcast in the VBI lines of a PAL
recording from the luma TBC produced by `vhs-tool decode`, using ali1234's
vhs-teletext.

Input: <base>-video.tbc   (luma TBC, full fields — its VBI lines carry teletext)

Output files (default: ./export/<base>.teletext/):
  <base>.t42          Raw packet stream, one 42-byte packet per VBI line
                      (--keep-empty inserts empty packets for lines that could
                      not be deconvolved, so packet N still maps back to the
                      TBC line it came from)
  <base>.squash.t42   Error-reduced page stream (frequency analysis over the
                      duplicates of each subpage) — this is the readable one
  pages.txt           Index of the pages found in the stream, with counts
  html/               Rendered pages, browsable offline

The output folder is what goes into an archive.org item as `teletext/`: copy it
there, then (re-)run `vhs-tool upload` so it lands in archive.sha256.

Requires: teletext (default: ./tools/vhs-decode/.venv/bin/teletext)
See https://github.com/oyvindln/vhs-decode/wiki/Teletext
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
import time
from pathlib import Path

from ..common import ToolError, human_size, log, resolve_binary, run
from ..config import get_config

_CFG = get_config()

# -- Defaults ------------------------------------------------------------------

# Capture "card" profiles that describe a ld-decode/vhs-decode TBC. Both are
# PAL 4fsc (17.73 MHz, 1135 samples/line); `tbc` reads full fields, `tbc-vbi`
# a TBC that only holds the VBI lines.
CARDS = ("tbc", "tbc-vbi")
# Sample sets vhs-teletext ships to deconvolve a given recording format.
TAPE_FORMATS = ("vhs", "betamax", "grundig_2x4")

PARTS = ("deconvolve", "squash", "pages", "html")

# A full teletext service is a few hundred pages — show a taste, not the file.
PAGES_PREVIEW_LINES = 8


def parse_only(value: str) -> list[str]:
    """Parse a comma-separated --only value into a validated list of parts."""
    parts = [p.strip() for p in value.split(",") if p.strip()]
    if not parts:
        raise argparse.ArgumentTypeError(f"expected at least one of: {', '.join(PARTS)}")
    invalid = [p for p in parts if p not in PARTS]
    if invalid:
        raise argparse.ArgumentTypeError(
            f"invalid part(s): {', '.join(invalid)} (choose from {', '.join(PARTS)})"
        )
    return parts


# =============================================================================
# Pure helpers (testable)
# =============================================================================


def resolve_input(base_path: str) -> tuple[Path, str]:
    """Map the command's argument to (luma TBC file, base name).

    Accepts a decoded base path ("./decoded/VHS_PAL_Tape_007-2026-...", the
    `-video.tbc` suffix is appended) as well as the .tbc file itself.
    """
    path = Path(base_path)
    if path.suffix == ".tbc":
        stem = path.stem
        if stem.endswith("_chroma"):
            raise ToolError(
                f"{path.name} is the chroma TBC — teletext lives in the luma TBC "
                f"({stem.removesuffix('_chroma')}.tbc)"
            )
        return path, stem.removesuffix("-video")
    return Path(f"{base_path}-video.tbc"), path.name


def default_output_dir(base: str) -> Path:
    """Where the teletext artifacts go if --output is not given."""
    return Path(_CFG.paths.export) / f"{base}.teletext"


def build_deconvolve_command(
    teletext_bin: str, args: argparse.Namespace, tbc_file: Path, output: Path
) -> list[str]:
    """Assemble `teletext deconvolve` — raw VBI samples → 42-byte packets."""
    cmd = [teletext_bin, "deconvolve", "--card", args.card, "--tape-format", args.tape_format]
    if args.keep_empty:
        cmd.append("--keep-empty")
    if args.force_cpu:
        cmd.append("--force-cpu")
    if args.threads:
        cmd += ["--threads", str(args.threads)]
    if args.limit:
        cmd += ["--limit", str(args.limit)]
    # `-o` takes a (format, file) pair; "bytes" is the raw t42 packet stream.
    cmd += ["--output", "bytes", str(output), str(tbc_file)]
    return cmd


def build_squash_command(
    teletext_bin: str, args: argparse.Namespace, source: Path, output: Path
) -> list[str]:
    """Assemble `teletext squash` — merge duplicate subpages to reduce errors."""
    cmd = [teletext_bin, "squash", "--min-duplicates", str(args.min_duplicates)]
    if args.ignore_empty:
        cmd.append("--ignore-empty")
    cmd += ["--output", "bytes", str(output), str(source)]
    return cmd


def has_packets(path: Path, chunk_size: int = 1 << 20) -> bool:
    """True if the t42 stream holds at least one non-padding packet.

    A packet is padding when all 42 of its bytes are zero, which is exactly what
    `deconvolve --keep-empty` writes for a line it could not read — so a stream
    with nothing but zeros carries no teletext at all. Worth checking before
    handing the file to `teletext list`/`html`, which choke on an empty stream.
    """
    try:
        with open(path, "rb") as handle:
            while chunk := handle.read(chunk_size):
                if any(chunk):
                    return True
    except OSError as exc:
        raise ToolError(f"Cannot read {path}: {exc}") from exc
    return False


def build_pages_command(teletext_bin: str, source: Path) -> list[str]:
    """Assemble `teletext list` — index of the pages in the stream (to stdout)."""
    return [teletext_bin, "list", "--count", str(source)]


def build_html_command(teletext_bin: str, source: Path, outdir: Path) -> list[str]:
    """Assemble `teletext html` — render the stream into browsable pages."""
    return [teletext_bin, "html", str(outdir), str(source)]


# =============================================================================
# Argument parsing
# =============================================================================


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "process-teletext",
        help="Extract teletext (t42) from a decoded TBC's VBI lines",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "base_path",
        help="Decoded files base path (without suffix), e.g. "
        "./decoded/VHS_PAL_Tape__Name-2026-05-03_18_14_58_02_00; "
        "the -video.tbc file itself works too",
    )
    parser.add_argument(
        "-o",
        "--output",
        help=f"Output directory (default: {_CFG.paths.export}/<input-basename>.teletext)",
    )
    parser.add_argument(
        "--only",
        type=parse_only,
        metavar="{deconvolve,squash,pages,html}[,...]",
        help="Run only specific steps, comma-separated (e.g. --only squash,html)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Redo steps whose output already exists (default: skip them)",
    )

    group = parser.add_argument_group("deconvolve settings")
    group.add_argument(
        "--card",
        choices=CARDS,
        default="tbc",
        help="TBC sample layout: tbc = full fields, tbc-vbi = VBI lines only (default: tbc)",
    )
    group.add_argument(
        "--tape-format",
        choices=TAPE_FORMATS,
        default="vhs",
        help="Source recording format for the sample data (default: vhs)",
    )
    group.add_argument(
        "--no-keep-empty",
        dest="keep_empty",
        action="store_false",
        help="Drop lines that could not be deconvolved instead of writing an empty "
        "packet (smaller file, but packets no longer map back to TBC lines)",
    )
    group.add_argument("--force-cpu", action="store_true", help="Disable GPU even if available")
    group.add_argument("--threads", type=int, help="Worker threads (default: all cores)")
    group.add_argument(
        "--limit", type=int, metavar="N", help="Stop after N VBI lines (quick trial run)"
    )

    group = parser.add_argument_group("squash settings")
    group.add_argument(
        "--min-duplicates",
        type=int,
        default=3,
        help="Only keep subpages seen at least N times (default: 3)",
    )
    group.add_argument(
        "--ignore-empty",
        action="store_true",
        help="Prefer the emptiest duplicate packets over the earliest ones",
    )

    group = parser.add_argument_group("paths")
    group.add_argument(
        "--teletext",
        dest="teletext_bin",
        default=_CFG.binaries.teletext,
        help=f"teletext binary path (default: {_CFG.binaries.teletext})",
    )

    group = parser.add_argument_group("general")
    group.add_argument("--dry-run", action="store_true", help="Print commands without executing")
    parser.set_defaults(func=cmd_process_teletext)


# =============================================================================
# Command
# =============================================================================


def run_to_file(cmd: list[str], output: Path, *, dry_run: bool) -> None:
    """Run a command with its stdout redirected into `output` (stderr stays live)."""
    log(f"$ {shlex.join(cmd)} > {output}")
    if dry_run:
        return
    sys.stdout.flush()
    try:
        with open(output, "w", encoding="utf-8") as handle:
            subprocess.run(cmd, stdout=handle, check=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise ToolError(f"{cmd[0]} exited with code {exc.returncode}") from exc
    except FileNotFoundError as exc:
        raise ToolError(f"Command not found: {cmd[0]}") from exc
    except OSError as exc:
        raise ToolError(f"Cannot write {output}: {exc}") from exc


def cmd_process_teletext(args: argparse.Namespace) -> int:
    tbc_file, base = resolve_input(args.base_path)
    if not base:
        raise ToolError("Could not derive base name from input")

    outdir = Path(args.output) if args.output else default_output_dir(base)
    raw_t42 = outdir / f"{base}.t42"
    squash_t42 = outdir / f"{base}.squash.t42"
    pages_txt = outdir / "pages.txt"
    html_dir = outdir / "html"

    only: list[str] = args.only or []

    def should_run(part: str) -> bool:
        return not only or part in only

    if should_run("deconvolve") and not tbc_file.is_file():
        raise ToolError(f"TBC file not found: {tbc_file}")

    teletext_bin = resolve_binary(args.teletext_bin, "teletext")

    # The TBC card profiles are PAL 4fsc; an NTSC TBC would be sliced at the
    # wrong line length and yield nothing but rejects.
    if "NTSC" in base.upper():
        log("WARN: this looks like an NTSC capture — the TBC card profiles are PAL 4fsc,")
        log("      teletext (WST) is a PAL/SECAM format. Expect no usable packets.")

    if not args.dry_run:
        outdir.mkdir(parents=True, exist_ok=True)

    log(f"Base:        {base}")
    log(f"TBC file:    {tbc_file}")
    log(f"Output dir:  {outdir}")
    log(f"Steps:       {', '.join(only) if only else 'all'}")
    log(
        f"Deconvolve:  card={args.card}  tape-format={args.tape_format}  "
        f"keep-empty={args.keep_empty}"
    )
    print(file=sys.stderr)

    start_ts = time.time()

    # -- 1. Deconvolve: VBI samples → packets ---------------------------------
    if not should_run("deconvolve"):
        log("Skipping deconvolve")
    elif raw_t42.is_file() and not args.overwrite:
        log(f"Deconvolve: {raw_t42.name} exists — skipping (use --overwrite to redo)")
    else:
        log("========== deconvolve ==========")
        cmd = build_deconvolve_command(teletext_bin, args, tbc_file, raw_t42)
        log(f"$ {shlex.join(cmd)}")
        if not args.dry_run:
            run(cmd)
            if not raw_t42.is_file():
                raise ToolError(f"Deconvolve produced no output: {raw_t42}")
        print(file=sys.stderr)

    # -- 2. Squash: reduce errors across duplicate subpages -------------------
    if not should_run("squash"):
        log("Skipping squash")
    elif squash_t42.is_file() and not args.overwrite:
        log(f"Squash: {squash_t42.name} exists — skipping (use --overwrite to redo)")
    else:
        if not raw_t42.is_file() and not args.dry_run:
            raise ToolError(f"No packet stream to squash: {raw_t42} (run the deconvolve step)")
        log("========== squash ==========")
        cmd = build_squash_command(teletext_bin, args, raw_t42, squash_t42)
        log(f"$ {shlex.join(cmd)}")
        if not args.dry_run:
            run(cmd)
        print(file=sys.stderr)

    # The squashed stream is the readable one; fall back to the raw packets when
    # squashing was skipped. Both reporting steps below choke on a stream without
    # a single real packet, so that is checked once and cached here.
    cached_source: list[Path | None] = []

    def report_source() -> Path | None:
        """The stream to report on, or None if there is no teletext in it."""
        if cached_source:
            return cached_source[0]
        if args.dry_run:
            return raw_t42
        for candidate in (squash_t42, raw_t42):
            if not candidate.is_file():
                continue
            if has_packets(candidate):
                cached_source.append(candidate)
                return candidate
            log(f"No teletext packets in {candidate.name}")
        if not squash_t42.is_file() and not raw_t42.is_file():
            raise ToolError(f"No packet stream found in {outdir} (run the deconvolve step first)")
        cached_source.append(None)
        return None

    # -- 3. Page index --------------------------------------------------------
    if not should_run("pages"):
        log("Skipping pages")
    elif pages_txt.is_file() and not args.overwrite:
        log(f"Pages: {pages_txt.name} exists — skipping (use --overwrite to redo)")
    elif (source := report_source()) is None:
        log("Pages: no teletext in this recording — skipping")
    else:
        log("========== pages ==========")
        run_to_file(build_pages_command(teletext_bin, source), pages_txt, dry_run=args.dry_run)
        print(file=sys.stderr)

    # -- 4. HTML rendering ----------------------------------------------------
    if not should_run("html"):
        log("Skipping html")
    elif html_dir.is_dir() and any(html_dir.iterdir()) and not args.overwrite:
        log(f"HTML: {html_dir.name}/ is not empty — skipping (use --overwrite to redo)")
    elif (source := report_source()) is None:
        log("HTML: no teletext in this recording — skipping")
    else:
        log("========== html ==========")
        cmd = build_html_command(teletext_bin, source, html_dir)
        log(f"$ {shlex.join(cmd)}")
        if not args.dry_run:
            html_dir.mkdir(parents=True, exist_ok=True)
            run(cmd)
        print(file=sys.stderr)

    elapsed = int(time.time() - start_ts)
    log(f"Teletext processing finished in {elapsed}s")

    if args.dry_run:
        return 0

    print(file=sys.stderr)
    log(f"Output: {outdir}")
    for file in (raw_t42, squash_t42, pages_txt):
        if file.is_file():
            log(f"  ✓ {file.name}  ({human_size(file)})")
    if html_dir.is_dir():
        count = sum(1 for _ in html_dir.glob("*.html"))
        log(f"  ✓ {html_dir.name}/  ({count} pages)")

    log("")
    if cached_source and cached_source[0] is None:
        log("No teletext recovered — this recording carries nothing in its VBI lines.")
        return 0

    pages = pages_txt.read_text(encoding="utf-8").strip() if pages_txt.is_file() else ""
    if pages:
        lines = pages.splitlines()
        log("Pages found (page/count):")
        for line in lines[:PAGES_PREVIEW_LINES]:
            log(f"  {line}")
        if len(lines) > PAGES_PREVIEW_LINES:
            log(f"  ... {len(lines) - PAGES_PREVIEW_LINES} more lines in {pages_txt.name}")
        log("")

    log("Next: copy the folder into the archive.org item as teletext/, e.g.")
    log(f"  cp -r {outdir} {_CFG.paths.upload}/{base}_IA/teletext")
    log("then (re-)run `vhs-tool upload` so it lands in archive.sha256.")
    return 0
