"""Shared helpers: process execution, ffprobe wrappers, timestamps, prompts."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime
from fractions import Fraction
from pathlib import Path

import questionary

_TS_RE = re.compile(r"^\d{1,2}:\d{2}:\d{2}(\.\d{1,3})?$")
_MK_DATE_RE = re.compile(
    r"^(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?"
    r"(?:[T ](\d{1,2}):(\d{2})(?::(\d{2}))?)?"
    r"\s*(Z|[+-]\d{2}:?\d{2})?$"
)
_TZ_RE = re.compile(r"^([+-])(\d{2}):?(\d{2})$")


class ToolError(Exception):
    """Fatal error — caught in cli.main() and printed as 'Error: ...'."""


def check_deps(*commands: str) -> None:
    """Abort if any of the given external commands is not on PATH."""
    missing = [cmd for cmd in commands if shutil.which(cmd) is None]
    if missing:
        raise ToolError(f"Missing required tools: {' '.join(missing)}")


def resolve_binary(configured: str | Path, fallback: str) -> str:
    """Return `configured` if it is an executable file, else `fallback` from PATH.

    Logs a warning when falling back; raises ToolError when neither resolves.
    """
    if os.access(configured, os.X_OK) and Path(configured).is_file():
        return str(configured)
    if shutil.which(fallback) is not None:
        print(
            f"WARN: configured binary not found, using {fallback} from PATH",
            file=sys.stderr,
        )
        return fallback
    raise ToolError(f"{fallback} not found: {configured} (and not on PATH)")


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
    sys.stdout.flush()  # keep our status lines ordered before subprocess output
    try:
        return subprocess.run(argv, check=check, capture_output=capture, text=True, env=env)
    except subprocess.CalledProcessError as exc:
        detail = f": {exc.stderr.strip()}" if capture and exc.stderr and exc.stderr.strip() else ""
        raise ToolError(f"{argv[0]} exited with code {exc.returncode}{detail}") from exc
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


def audio_track_count(file: Path | str) -> int:
    """Number of audio streams in the file."""
    value = _ffprobe(
        file, "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0"
    )
    return len(value.splitlines()) if value else 0


def video_duration(file: Path | str) -> float:
    """Container duration in seconds."""
    value = _ffprobe(file, "-show_entries", "format=duration", "-of", "csv=p=0")
    if not value:
        raise ToolError(f"Could not determine video duration via ffprobe: {file}")
    try:
        return float(value)
    except ValueError as exc:
        raise ToolError(
            f"Could not determine video duration of {file}: ffprobe returned {value!r}"
        ) from exc


def format_tag(file: Path | str, name: str) -> str:
    """First value of a container-level (format) tag, '' if unset."""
    value = _ffprobe(
        file,
        "-show_entries",
        f"format_tags={name}",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
    )
    return value.splitlines()[0] if value else ""


def frame_rate(file: Path | str) -> Fraction:
    """r_frame_rate of the first video stream (e.g. 25/1)."""
    value = _ffprobe(
        file, "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0"
    )
    if not value:
        raise ToolError(f"Could not determine frame rate via ffprobe: {file}")
    try:
        return Fraction(value)
    except (ValueError, ZeroDivisionError) as exc:
        raise ToolError(
            f"Could not determine frame rate of {file}: ffprobe returned {value!r}"
        ) from exc


# -- Timestamps ----------------------------------------------------------------


def ts_to_seconds(ts: str) -> float:
    """Parse 'HH:MM:SS[.mmm]' into seconds."""
    if not _TS_RE.match(ts):
        raise ToolError(f"Invalid timestamp '{ts}' (expected HH:MM:SS[.mmm])")
    hours, minutes, seconds = ts.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def seconds_to_ts(seconds: float) -> str:
    """Format seconds as 'HH:MM:SS.mmm' (rounding carries into minutes/hours)."""
    ms = round(max(seconds, 0.0) * 1000)
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    return f"{hours:02d}:{minutes:02d}:{rem // 1000:02d}.{rem % 1000:03d}"


def seconds_to_hms(seconds: float) -> str:
    """Format seconds as 'HH:MM:SS' (rounded)."""
    s = int(seconds + 0.5)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def seconds_to_yt_ts(seconds: float) -> str:
    """YouTube chapter timestamp: M:SS below one hour, H:MM:SS above."""
    s = int(seconds + 0.5)
    hours, minutes, secs = s // 3600, (s % 3600) // 60, s % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


# -- Dates ---------------------------------------------------------------------


def normalize_tz(tz: str) -> str:
    """Normalize a timezone offset to '+HH:MM' / '-HH:MM' (or 'Z' for UTC)."""
    tz = tz.strip()
    if tz in ("Z", "z"):
        return "Z"
    match = _TZ_RE.match(tz)
    if not match:
        raise ToolError(f"Invalid timezone offset '{tz}' (expected +HH:MM, -HH:MM, or Z)")
    sign, hours, minutes = match.groups()
    if int(hours) > 14 or int(minutes) > 59:  # real-world offsets span -12:00..+14:00
        raise ToolError(f"Timezone offset '{tz}' out of range (expected -12:00..+14:00)")
    return f"{sign}{hours}:{minutes}"


def to_matroska_date(value: str, tz: str = "+00:00") -> str:
    """Normalize a flexible date/year into Matroska's ISO 8601 Segment date.

    Accepts a year ('1998'), date ('1998-05-08') or datetime
    ('1998-05-08T18:11:32', space separator and a trailing offset/'Z' allowed).
    Missing fields default to Jan 1st, 00:00:00. A value that already carries a
    timezone keeps it; otherwise `tz` (e.g. '+01:00') is appended. The result
    is what mkvpropedit's `--set date=` expects.
    """
    match = _MK_DATE_RE.match(value.strip())
    if not match:
        raise ToolError(
            f"Cannot parse date '{value}' (expected a year, date or datetime, "
            "e.g. 1998, 1998-05-08, or 1998-05-08T18:11:32)"
        )
    year, month, day, hour, minute, second, offset = match.groups()
    parts = (
        int(year),
        int(month or 1),
        int(day or 1),
        int(hour or 0),
        int(minute or 0),
        int(second or 0),
    )
    try:
        datetime(*parts)  # validate field ranges (e.g. month 13, day 32)
    except ValueError as exc:
        raise ToolError(f"Invalid date '{value}': {exc}") from exc
    out_tz = normalize_tz(offset or tz)
    return "{:04d}-{:02d}-{:02d}T{:02d}:{:02d}:{:02d}{}".format(*parts, out_tz)


# -- Interactive prompts ---------------------------------------------------------
#
# questionary (prompt_toolkit) gives full line editing — arrow keys, Home/End,
# word jumps, history — for free, plus arrow-key select menus for fixed choices.
# We call `.unsafe_ask()` (not `.ask()`, which swallows Ctrl-C): Ctrl-C then
# propagates as KeyboardInterrupt and aborts the command in cli.main(), while a
# non-interactive stdin raises EOFError, which we map back to the default so
# piped/non-interactive runs keep working.

# Show the default as a dimmed "(default)" hint so it doesn't read as part of
# the question. questionary renders the `instruction` text in the `instruction`
# style class; the rest falls back to questionary's built-in defaults.
_PROMPT_STYLE = questionary.Style([("instruction", "fg:#888888")])


def ask(prompt: str, default: str = "", choices: list[str] | None = None) -> str:
    """Prompt for a string; plain Enter accepts the default.

    With `choices`, shows an arrow-key select menu (default pre-selected);
    otherwise a free-text field with line editing. The default is shown in
    parentheses but left out of the input buffer (so you can just type over it);
    an empty line accepts it. A non-interactive stdin also falls back to the
    default; Ctrl-C aborts the command.
    """
    try:
        if choices:
            answer = questionary.select(
                prompt,
                choices=choices,
                default=default if default in choices else None,
            ).unsafe_ask()
        elif default:
            answer = questionary.text(
                prompt, instruction=f"({default}) ", style=_PROMPT_STYLE
            ).unsafe_ask()
        else:
            answer = questionary.text(prompt).unsafe_ask()
    except EOFError:
        return default
    return answer if answer != "" else default


def confirm(prompt: str, default: bool = False) -> bool:
    """Yes/no prompt; a non-interactive stdin uses the default, Ctrl-C aborts."""
    try:
        return questionary.confirm(prompt, default=default).unsafe_ask()
    except EOFError:
        return default


# -- RF captures ---------------------------------------------------------------

# Extensions and channel suffixes used by vhs-decode RF capture files. A capture
# set for one tape shares a <base> name with a per-channel suffix and a format
# extension, e.g. "VHS_PAL_Tape_010-video.flac".
RF_EXTENSIONS = (".flac", ".wav", ".raw", ".ldf", ".lds", ".r8", ".u8", ".r16", ".u16")
RF_CHANNELS = ("-video", "-hifi", "-linear", "-headswitch")


def derive_base(name: str) -> str:
    """Strip a capture file's format extension and channel suffix to recover <base>.

    Any channel/format of a capture set can therefore be passed as the base path.
    """
    for ext in RF_EXTENSIONS:
        name = name.removesuffix(ext)
    for channel in RF_CHANNELS:
        name = name.removesuffix(channel)
    return name


def soxi(file: Path | str, flag: str) -> int:
    """Return an integer soxi field (e.g. -r rate, -s sample count, -b bit depth)."""
    return int(run(["soxi", flag, file], capture=True).stdout.strip())


def log(message: str = "") -> None:
    """Print a timestamped progress line to stderr."""
    print(f"[{time.strftime('%H:%M:%S')}] {message}", file=sys.stderr)


# -- Misc ----------------------------------------------------------------------


def human_size(file: Path | str) -> str:
    """File size as a human-readable string (du -h style)."""
    size = float(Path(file).stat().st_size)
    for unit in ("B", "K", "M", "G", "T"):
        if size < 1024 or unit == "T":
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}{unit}"
        size /= 1024
    return f"{size:.1f}T"  # pragma: no cover (unreachable)
