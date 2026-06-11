"""Shared helpers: process execution, ffprobe wrappers, timestamps."""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

_TS_RE = re.compile(r"^\d{1,2}:\d{2}:\d{2}(\.\d{1,3})?$")


class ToolError(Exception):
    """Fatal error — caught in cli.main() and printed as 'Error: ...'."""


def check_deps(*commands: str) -> None:
    """Abort if any of the given external commands is not on PATH."""
    missing = [cmd for cmd in commands if shutil.which(cmd) is None]
    if missing:
        raise ToolError(f"Missing required tools: {' '.join(missing)}")


def run(
    cmd: list,
    *,
    check: bool = True,
    capture: bool = False,
    echo: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run an external command. All cmd items are stringified (Path-friendly)."""
    argv = [str(c) for c in cmd]
    if echo:
        print(f"  $ {shlex.join(argv)}", file=sys.stderr)
    try:
        return subprocess.run(argv, check=check, capture_output=capture, text=True, env=env)
    except subprocess.CalledProcessError as exc:
        raise ToolError(f"{argv[0]} exited with code {exc.returncode}") from exc
    except FileNotFoundError as exc:
        raise ToolError(f"Command not found: {argv[0]}") from exc


# -- ffprobe wrappers ----------------------------------------------------------


def _ffprobe(file: Path | str, *args: str) -> str:
    result = run(["ffprobe", "-v", "error", *args, file], capture=True)
    return result.stdout.strip()


def audio_channels(file: Path | str) -> int:
    """Channel count of the first audio stream."""
    value = _ffprobe(
        file, "-select_streams", "a:0", "-show_entries", "stream=channels", "-of", "csv=p=0"
    )
    if not value:
        raise ToolError(f"Could not determine audio channels of {file}")
    return int(value)


def video_duration(file: Path | str) -> float:
    """Container duration in seconds."""
    value = _ffprobe(file, "-show_entries", "format=duration", "-of", "csv=p=0")
    if not value:
        raise ToolError(f"Could not determine video duration via ffprobe: {file}")
    return float(value)


def frame_rate(file: Path | str) -> Fraction:
    """r_frame_rate of the first video stream (e.g. 25/1)."""
    value = _ffprobe(
        file, "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0"
    )
    if not value:
        raise ToolError(f"Could not determine frame rate via ffprobe: {file}")
    return Fraction(value)


# -- Timestamps ----------------------------------------------------------------


def ts_to_seconds(ts: str) -> float:
    """Parse 'HH:MM:SS[.mmm]' into seconds."""
    if not _TS_RE.match(ts):
        raise ToolError(f"Invalid timestamp '{ts}' (expected HH:MM:SS[.mmm])")
    hours, minutes, seconds = ts.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def seconds_to_ts(seconds: float) -> str:
    """Format seconds as 'HH:MM:SS.mmm'."""
    seconds = max(seconds, 0.0)
    hours = int(seconds // 3600)
    minutes = int((seconds - hours * 3600) // 60)
    secs = seconds - hours * 3600 - minutes * 60
    return f"{hours:02d}:{minutes:02d}:{secs:06.3f}"


# -- Misc ----------------------------------------------------------------------


def human_size(file: Path | str) -> str:
    """File size as a human-readable string (du -h style)."""
    size = float(Path(file).stat().st_size)
    for unit in ("B", "K", "M", "G", "T"):
        if size < 1024 or unit == "T":
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}{unit}"
        size /= 1024
    return f"{size:.1f}T"  # pragma: no cover (unreachable)
