"""Shared MKV metadata handling for `encode` and `set-props`.

Both commands expose the same set of metadata properties (title, source,
publisher, date, comment, audio language). `encode` writes them while muxing a
fresh file with mkvmerge; `set-props` patches them into an existing file with
mkvpropedit. The argument definitions, the arg→Matroska-tag mapping and the
global-tags XML writer/reader live here so the two commands cannot drift apart.
"""

from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from xml.sax.saxutils import escape

from .config import get_config

_CFG = get_config()

# argparse dest -> Matroska global tag name. `date` additionally drives the
# Segment 'date' element (each command handles that separately); here it is the
# DATE_RELEASED tag, taking the value verbatim (e.g. "1998").
TAG_FIELDS: tuple[tuple[str, str], ...] = (
    ("source", "SOURCE"),
    ("publisher", "PUBLISHER"),
    ("date", "DATE_RELEASED"),
    ("comment", "COMMENT"),
)


def add_metadata_args(parser: argparse.ArgumentParser, *, lang_default: str | None) -> None:
    """Add the metadata flags shared by `encode` and `set-props`.

    `lang_default=None` gives PATCH semantics (omitted ⇒ leave the value alone);
    `encode` passes the configured default because it always muxes a language.
    """
    parser.add_argument("--title", help="Segment title")
    parser.add_argument("--source", help='Source device (e.g. "Panasonic NV-VP30")')
    parser.add_argument("--publisher", help="Publisher name")
    parser.add_argument(
        "--date",
        help="Recording date/year → DATE_RELEASED tag + Matroska Segment date "
        "(the timeline date used by Immich etc.): 1998 | 1998-05-08 | 1998-05-08T18:11:32",
    )
    parser.add_argument(
        "--date-tz",
        default=_CFG.defaults.timezone,
        help=f"Timezone offset for --date when it has none (default: {_CFG.defaults.timezone})",
    )
    parser.add_argument("--comment", help="Comment")
    lang_help = "Audio language code"
    lang_help += f" (default: {lang_default})" if lang_default else " (default: leave unchanged)"
    parser.add_argument("--lang", default=lang_default, help=lang_help)


def build_global_tags_xml(pairs: Iterable[tuple[str, str]]) -> str:
    """Matroska global-tags XML for mkvmerge --global-tags / mkvpropedit --tags global:."""
    simples = "".join(
        f"<Simple><Name>{escape(name)}</Name><String>{escape(value)}</String></Simple>"
        for name, value in pairs
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f"<Tags><Tag><Targets></Targets>{simples}</Tag></Tags>\n"
    )


def parse_global_tags(xml: str) -> dict[str, str]:
    """Global (non-track) Simple Name→String pairs from `mkvextract tags` XML.

    Per-track tags (Targets with a TrackUID — e.g. the BPS/DURATION statistics)
    are skipped so that rewriting via `--tags global:` leaves them untouched.
    """
    text = xml.lstrip("\ufeff \t\r\n")
    if not text:
        return {}
    root = ET.fromstring(text.encode("utf-8"))
    result: dict[str, str] = {}
    for tag in root.findall("Tag"):
        targets = tag.find("Targets")
        if targets is not None and targets.find("TrackUID") is not None:
            continue  # track-specific tag — not global
        for simple in tag.findall("Simple"):
            name = simple.findtext("Name")
            if name:
                result[name] = simple.findtext("String") or ""
    return result
