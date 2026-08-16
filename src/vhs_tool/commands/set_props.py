"""vhs-tool set-props — patch metadata on an existing MKV (PATCH semantics).

Updates only the properties you pass; anything you omit is left untouched. It
covers the same metadata `vhs-tool encode` can set:

  --title --source --publisher --date --comment --lang

  * --title / --date  → Matroska Segment info (title, date). --date also writes
                        the DATE_RELEASED tag, matching `encode`.
  * --source / --publisher / --comment / --date → global tags, MERGED with the
                        existing ones (other tags, incl. per-track statistics,
                        are preserved).
  * --lang            → language of every audio track.

Edits are applied in place with mkvpropedit — no remux. mkvmerge stamps the
Segment date with the muxing time, which media servers like Immich use as the
timeline date; --date overwrites it with the real recording date.

  vhs-tool set-props tape.mkv --date 1998
  vhs-tool set-props tape.mkv --title "Familie Bartusch — Festle" --source "Panasonic NV-VP30"
  vhs-tool set-props tape.mkv --lang de --comment ""        # clear the comment
"""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

from ..common import (
    ToolError,
    audio_track_count,
    check_deps,
    expand_wildcards,
    run,
    to_matroska_date,
)
from ..metadata import TAG_FIELDS, add_metadata_args, build_global_tags_xml, parse_global_tags


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "set-props",
        help="Patch metadata on an existing MKV in place (title, date, tags, language)",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "mkv",
        nargs="+",
        help="MKV file to edit in place; a wildcard works as long as it matches "
        "exactly one .mkv (non-MKV matches like .llc sidecars are ignored)",
    )
    add_metadata_args(parser, lang_default=None)
    parser.set_defaults(func=cmd_set_props)


def _read_global_tags(mkv: Path) -> dict[str, str]:
    result = run(["mkvextract", mkv, "tags"], capture=True, check=False)
    if result.returncode not in (0, 1):
        detail = (result.stderr or "").strip()
        raise ToolError(f"mkvextract tags exited with code {result.returncode}: {detail}")
    return parse_global_tags(result.stdout or "")


def cmd_set_props(args: argparse.Namespace) -> int:
    check_deps("mkvpropedit")

    values = expand_wildcards(args.mkv)
    if len(values) > 1:  # wildcard expansion — keep the MKVs, drop sidecars (.llc, ...)
        values = [v for v in values if v.lower().endswith(".mkv")]
        if not values:
            raise ToolError("None of the matched files is an MKV")
        if len(values) > 1:
            raise ToolError(
                "More than one MKV matched — name one: " + ", ".join(Path(v).name for v in values)
            )
    mkv = Path(values[0])
    if not mkv.is_file():
        raise ToolError(f"MKV not found: {mkv}")

    edits: list = []
    changes: list[str] = []

    # -- Segment info: title + date --------------------------------------------
    info_sets: list = []
    if args.title is not None:
        info_sets += ["--set", f"title={args.title}"]
        changes.append(f"  Segment title  → {args.title!r}")
    if args.date is not None:
        seg_date = to_matroska_date(args.date, args.date_tz)
        info_sets += ["--set", f"date={seg_date}"]
        changes.append(f"  Segment date   → {seg_date}")
    if info_sets:
        edits += ["--edit", "info", *info_sets]

    # -- Audio track language --------------------------------------------------
    if args.lang is not None:
        check_deps("ffprobe")
        count = audio_track_count(mkv)
        if count == 0:
            raise ToolError(f"--lang given but no audio tracks found in {mkv}")
        for i in range(1, count + 1):
            edits += ["--edit", f"track:a{i}", "--set", f"language={args.lang}"]
        changes.append(f"  Audio language → {args.lang} ({count} track{'s' if count != 1 else ''})")

    # -- Global tags (PATCH: merge updates into the existing tag set) -----------
    tag_updates = [
        (tag, getattr(args, dest)) for dest, tag in TAG_FIELDS if getattr(args, dest) is not None
    ]
    tags_file: Path | None = None
    if tag_updates:
        check_deps("mkvextract")
        merged = _read_global_tags(mkv)
        for name, value in tag_updates:
            merged[name] = value
            changes.append(f"  Tag {name:<13} → {value!r}")
        fd, tmp_name = tempfile.mkstemp(prefix=".vhs-tags-", suffix=".xml")
        os.close(fd)
        tags_file = Path(tmp_name)
        tags_file.write_text(build_global_tags_xml(merged.items()), encoding="utf-8")
        edits += ["--tags", f"global:{tags_file}"]

    if not edits:
        raise ToolError(
            "Nothing to update — pass at least one of "
            "--title / --source / --publisher / --date / --comment / --lang"
        )

    try:
        # mkvpropedit exit codes: 0=ok, 1=warning (change applied), 2=error.
        result = run(["mkvpropedit", mkv, *edits], capture=True, check=False)
    finally:
        if tags_file is not None:
            tags_file.unlink(missing_ok=True)
    if result.returncode not in (0, 1):
        detail = (result.stderr or result.stdout or "").strip()
        raise ToolError(f"mkvpropedit exited with code {result.returncode}: {detail}")

    print(f"Updated {mkv}:")
    print("\n".join(changes))
    return 0
