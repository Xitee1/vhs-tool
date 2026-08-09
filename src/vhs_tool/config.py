"""Pipeline configuration — optional `vhs-tool.toml` at the pipeline root.

Every value has a built-in default matching the original bash scripts, so the
config file is optional and only needs the keys that differ. Search order:

  1. $VHS_TOOL_CONFIG  (explicit path, must exist)
  2. ./vhs-tool.toml   (the directory vhs-tool is run from — the pipeline root)

Command modules read the config at import time into their argparse defaults,
so `--help` always shows the effective values. CLI flags override env vars
(where a command supports them), which override the config file, which
overrides the built-in defaults.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from .common import ToolError

CONFIG_ENV = "VHS_TOOL_CONFIG"
CONFIG_NAME = "vhs-tool.toml"


@dataclass(frozen=True)
class Paths:
    """Pipeline directory layout, relative to the directory vhs-tool runs from."""

    captures: tuple[str, ...] = ("./captures", "./export_new")  # searched for RF captures
    decoded: str = "./decoded"
    export: str = "./export"
    final: str = "./final"
    upload: str = "./upload"
    tools: str = "./tools"
    templates: str = "./templates"  # user overrides for the packaged Jinja2 templates


@dataclass(frozen=True)
class Binaries:
    """External tools that are not expected on PATH."""

    vhs_decode: str = "./tools/vhs-decode/.venv/bin/vhs-decode"
    hifi_decode: str = "./tools/vhs-decode/.venv/bin/hifi-decode"
    aaa: str = "./tools/vhs-decode-aaa-1.0.2-x86_64.appimage"
    tbc_video_export: str = "./tools/tbc-video-export.AppImage"
    tbc_tools: str = "./tools/tbc-tools/tbc-tools-x86_64.AppImage"
    tbc_export_config: str = "./tools/tbc-video-export.json"
    teletext: str = "./tools/vhs-decode/.venv/bin/teletext"  # ali1234/vhs-teletext
    # namazso/cxadc_vhs_server — used by `vhs-tool capture`
    cxadc_server: str = "./tools/Scripts/clockgen scripts/cxadc_vhs_server"


@dataclass(frozen=True)
class Defaults:
    lang: str = "de"  # audio language code for muxing
    timezone: str = "+00:00"  # offset for Matroska Segment dates lacking one (UTC)
    tv_system: str = "pal"  # pal | ntsc | pal-m | ntsc-j | mesecam
    tape_format: str = "vhs"  # vhs | vhshq | svhs | umatic | ...
    # G0 national subset for rendering teletext (a `teletext` charset key, e.g.
    # "ger"). Empty leaves the choice to vhs-teletext, which assumes English.
    teletext_language: str = ""
    extra_decode_params: str = "--ire0_adjust"  # Notes.txt "Extra parameters" default
    vhs_decode_version: str = "v0.4.0"  # Notes.txt fallback when not detectable


@dataclass(frozen=True)
class Capture:
    """`vhs-tool capture` defaults and pre-flight target values.

    `capture_rate` is the single source of truth for the CX card sample rate:
    it drives the FLAC header rate (1000:1 FLAC-scale), the clockgen pre-flight
    check, and the HiFi resample ratio. The `cxadc_*` values are what the
    pre-flight expects in /sys/class/cxadc/cxadc<N>/device/parameters.
    """

    video_card: int = 0  # CX card number for the video RF stream
    hifi_card: int = 1  # CX card number for the HiFi RF stream
    linear_device: str = ""  # ALSA device for linear ("" = server default)
    linear_rate: int = 46875  # clockgen PCM1802 baseband rate (Hz)
    capture_rate: int = 40_000_000  # CX card sample rate in Hz (clockgen 40 MSPS)
    hifi_resample_rate: int = 10_000_000  # HiFi target rate for --resample-hifi (Hz)
    flac_level: int = 8  # 0-8; 8 is optimal for RF (see rf-compress)
    flac_threads: int = 0  # FLAC encoder threads; 0 = auto (all cores, needs flac >= 1.5)
    # Pre-flight: expected cxadc parameters (both cards)
    cxadc_vmux: int = 0
    cxadc_level: int = 0  # 0 = min gain (external amp does the amplification)
    cxadc_sixdb: int = 0
    cxadc_tenxfsc: int = 0
    cxadc_tenbit: int = 0
    # Pre-flight: clockgen ALSA device (clock outputs are checked via amixer)
    clockgen_device: str = "hw:CARD=CXADCADCClockGe"
    clockgen_video_out: int = 0  # clockgen output wired to the video card
    clockgen_hifi_out: int = 1  # clockgen output wired to the hifi card
    # Pre-flight: resources (the server allocates a 1 GiB ring buffer per card)
    min_free_disk_gib: float = 350.0
    ram_headroom_gib: float = 1.0  # required free RAM beyond the ring buffers


@dataclass(frozen=True)
class Hardware:
    """Capture hardware description — verbatim text for Notes.txt."""

    vcr: str = "Panasonic NV-VP30"
    rf_capture: str = "CX25800 (Video) + CX23883 (HiFi), Clockgen 40 MSPS"
    rf_amp: str = "ADA4857 (4S 18650 Li-Ion)"
    rf_amp_video: str = "R11/R12 = 15kΩ, R13 = 120Ω, R14 = 560Ω"
    rf_amp_hifi: str = "R21/R22 = 20kΩ, R23 = 390Ω, R24 = 560Ω"
    audio_adc: str = "PCM1802 (Clockgen)"


@dataclass(frozen=True)
class Links:
    """Static footer links appended to the YouTube description."""

    youtube: str = (
        "VHS-Decode Wiki: https://github.com/oyvindln/vhs-decode/wiki/\n"
        "VHS-Decode Reddit: https://www.reddit.com/r/vhsdecode/\n"
        "Domesday86 Discord: https://discord.gg/pVVrrxd"
    )


@dataclass(frozen=True)
class Upload:
    # VapourSynth filter scripts copied into the IA item for reproducibility
    # (the .vpy drives the picture, unlike the wrapper scripts which aren't shipped)
    pipeline_files: tuple[str, ...] = ("vapoursynth_vhs.vpy",)


@dataclass(frozen=True)
class Config:
    paths: Paths = field(default_factory=Paths)
    binaries: Binaries = field(default_factory=Binaries)
    defaults: Defaults = field(default_factory=Defaults)
    capture: Capture = field(default_factory=Capture)
    hardware: Hardware = field(default_factory=Hardware)
    links: Links = field(default_factory=Links)
    upload: Upload = field(default_factory=Upload)


_SECTION_TYPES = {f.name: f.default_factory for f in fields(Config)}


def _build_section(name: str, data: dict, source: Path):
    cls = _SECTION_TYPES[name]
    defaults = {f.name: f.default for f in fields(cls)}
    kwargs = {}
    for key, value in data.items():
        if key not in defaults:
            raise ToolError(
                f"Unknown key '{key}' in [{name}] of {source} "
                f"(known: {', '.join(sorted(defaults))})"
            )
        # `from __future__ import annotations` makes field types strings, but every
        # field has a literal default (str, or tuple for list-valued keys) to check against.
        expected = type(defaults[key])
        if expected is tuple:
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ToolError(f"'{key}' in [{name}] of {source} must be a list of strings")
            value = tuple(value)
        elif expected is float and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)  # TOML "350" for a float-typed key
        elif expected is int and isinstance(value, bool):
            raise ToolError(f"'{key}' in [{name}] of {source} must be a int, got bool")
        elif not isinstance(value, expected):
            raise ToolError(
                f"'{key}' in [{name}] of {source} must be a {expected.__name__}, "
                f"got {type(value).__name__}"
            )
        kwargs[key] = value
    return cls(**kwargs)


def load_config(path: Path | None) -> Config:
    """Parse a config file into a Config; None → all built-in defaults."""
    if path is None:
        return Config()
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ToolError(f"Invalid TOML in {path}: {exc}") from exc
    except OSError as exc:
        raise ToolError(f"Cannot read config file {path}: {exc}") from exc

    sections = {}
    for name, value in data.items():
        if name not in _SECTION_TYPES:
            raise ToolError(
                f"Unknown section [{name}] in {path} (known: {', '.join(sorted(_SECTION_TYPES))})"
            )
        if not isinstance(value, dict):
            raise ToolError(f"'{name}' in {path} must be a [{name}] table, not a plain value")
        sections[name] = _build_section(name, value, path)
    return Config(**sections)


def find_config() -> Path | None:
    """Resolve the config file path ($VHS_TOOL_CONFIG, else ./vhs-tool.toml), if any."""
    env = os.environ.get(CONFIG_ENV)
    if env:
        path = Path(env)
        if not path.is_file():
            raise ToolError(f"${CONFIG_ENV} points to a missing file: {path}")
        return path
    path = Path(CONFIG_NAME)
    return path if path.is_file() else None


_config: Config | None = None


def get_config() -> Config:
    """The process-wide config, loaded once on first use."""
    global _config
    if _config is None:
        _config = load_config(find_config())
    return _config
