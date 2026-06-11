"""Jinja2 rendering for generated text files (Notes.txt, YouTube description).

Default templates ship with the package (this directory). A file with the
same name in the user template directory (config: [paths] templates,
default ./templates) overrides the packaged one — copy a .j2 file there to
customize the layout without touching the package.
"""

from __future__ import annotations

from pathlib import Path

import jinja2

from ..common import seconds_to_yt_ts
from ..config import get_config

_PACKAGE_DIR = Path(__file__).parent

_env: jinja2.Environment | None = None


def _environment() -> jinja2.Environment:
    global _env
    if _env is None:
        _env = jinja2.Environment(
            loader=jinja2.ChoiceLoader(
                [
                    jinja2.FileSystemLoader(get_config().paths.templates),
                    jinja2.FileSystemLoader(_PACKAGE_DIR),
                ]
            ),
            undefined=jinja2.StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )
        _env.filters["yt_ts"] = seconds_to_yt_ts
    return _env


def render(template_name: str, **variables) -> str:
    return _environment().get_template(template_name).render(**variables)
