"""vhs-tool capture — record RF captures via cxadc_vhs_server.

Port of "tools/Scripts/clockgen scripts/local-capture.sh" (harrypm/Scripts).

Spawns cxadc_vhs_server on a Unix socket in a private temp directory, arms the
capture through the JSON control API (/start), and streams every channel
through a curl → encoder pipe chain that Python never touches:

  video   curl | flac                <base>-video.flac      (--no-compress-video: .u8)
  hifi    curl | sox | flac          <base>-hifi.flac       (resampled to 10 MSPS;
                                                             raw/unresampled variants too)
  linear  curl | ffmpeg              <base>-linear.flac + <base>-headswitch.u8
                                                            (--no-convert-linear: .s24)

The configured capture rate is the single source of truth for the FLAC header
rate (1000:1 FLAC-scale), the clockgen pre-flight check, and the HiFi resample
ratio (sox, 100:1 scale) — the three can never drift apart.

Pre-flight (any deviation aborts with a diff; each group has an --ignore flag):

  cxadc      /sys/class/cxadc/<card>/device/parameters vs the configured values
  clock      clockgen output rate via amixer vs the capture rate
  resources  free disk space and free RAM (the server allocates a 1 GiB ring
             buffer per CX card)

Stopping — 'q', --duration, SIGINT and SIGTERM all take the same path:
/stop → curl streams run out to EOF → wait for all encoders → terminate the
server. Nothing in the data path is ever killed.

A sidecar <base>-capture.json records the pre-flight values, effective rates,
the linear rate reported by /start, the buffer-fill history, overflows and
tool versions.

Exit codes: 0 ok; 1 error (pre-flight, dead pipe stage, failed encoder);
2 capture completed but the server reported overflows (samples were lost).

Requires: curl, cxadc_vhs_server; flac/sox/ffmpeg depending on options.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import termios
import threading
import time
import tty
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from .. import __version__
from ..capture_server import LocalServer, ServerConnection
from ..common import ToolError, check_deps, log, resolve_binary, run
from ..config import get_config
from .rf_compress import BLOCKSIZE, flac_encode_cmd, parse_flac_version, resolve_threads

_CFG = get_config()
_CAP = _CFG.capture

# -- Defaults (config file values, override via env or CLI) -----------------------
CXADC_SERVER_BIN = os.environ.get("CXADC_SERVER_BIN", _CFG.binaries.cxadc_server)

GIB = 1 << 30
STATUS_INTERVAL = 5.0  # seconds between status lines / stats polls

# cxadc module parameters checked by the pre-flight, in sysfs order
CXADC_PARAMS = ("vmux", "level", "sixdb", "tenxfsc", "tenbit")

# Capture rate → clockgen mixer item (cxadc-clock-generator-audio-adc firmware)
CLOCK_ITEMS = {
    20_000_000: "CXADC-20MHz",
    28_636_363: "CXADC-28.63MHz",
    40_000_000: "CXADC-40MHz",
    50_000_000: "CXADC-50MHz",
}

_DURATION_RE = re.compile(r"^(\d+)\s*([smh]?)$")


# =============================================================================
# Pure helpers (testable)
# =============================================================================


def validate_flac_level(level: int, flag: str) -> int:
    """FLAC compression level, restricted to 0-8.

    Levels 9+ need --lax high-order LPC and bloat RF FLACs by ~40% (see
    rf_compress / harrypm/Scripts#2), so they are rejected instead of passed on.
    """
    if not 0 <= level <= 8:
        raise ToolError(f"{flag} must be between 0 and 8, got {level} (8 is optimal for RF)")
    return level


def flac_header_rate(rate: int) -> int:
    """FLAC-scale header rate, 1000:1 truncated (40 MSPS → 40000, 28636363 → 28636).

    FLAC headers cannot hold MSPS-range rates, so RF captures store the rate
    divided by 1000 — the community convention rf-compress/rf-resample and the
    decode steps already rely on, which truncates non-multiples (stock-crystal
    28.63 MSPS → 28636, as in `rf-compress --rate 28636`).
    """
    if rate < 1000:
        raise ToolError(f"Capture rate must be at least 1000 Hz, got {rate}")
    return rate // 1000


def sox_rate(rate: int) -> int:
    """sox-scale rate, 100:1 truncated (40 MSPS → 400000). Only the in:out ratio matters."""
    if rate < 100:
        raise ToolError(f"Capture rate must be at least 100 Hz, got {rate}")
    return rate // 100


def clock_item_for_rate(rate: int) -> str:
    """The clockgen mixer item ('CXADC-40MHz') the pre-flight expects for `rate`."""
    try:
        return CLOCK_ITEMS[rate]
    except KeyError:
        known = ", ".join(str(r) for r in CLOCK_ITEMS)
        raise ToolError(
            f"No clockgen mode known for a capture rate of {rate} Hz (known: {known}); "
            "use --ignore-clock-checks to capture at this rate anyway"
        ) from None


def start_path(linear_rate: int, cards: list[int], linear_device: str) -> str:
    """/start query — parameter order as in local-capture.sh (lrate, cxadcN..., lname).

    The stream index of each card on /cxadc?N is its position in this list.
    The ALSA device name is passed raw (unencoded), exactly like the script did.
    """
    parts = [f"lrate={linear_rate}"] + [f"cxadc{card}" for card in cards]
    if linear_device:
        parts.append(f"lname={linear_device}")
    return "/start?" + "&".join(parts)


def parse_start_response(data: dict) -> tuple[str, int | None, str]:
    """(state, linear_rate, fail_reason) from the /start JSON response."""
    linear_rate = data.get("linear_rate")
    return (
        str(data.get("state", "")),
        int(linear_rate) if linear_rate is not None else None,
        str(data.get("fail_reason") or ""),
    )


def flac_encode_args(level: int, header_rate: int, out: Path, threads: int = 0) -> list[str]:
    """flac stdin → file, parameters exactly as in capture.sh.

    Delegates to rf-compress's builder so the on-disk RF FLAC format has a
    single source of truth. `threads` > 1 inserts --threads=N right after the
    level, matching the script's $FLAC_THREAD_ARG position (needs flac >=
    1.5.0 — the caller resolves that; <= 1 adds nothing, like the script's
    ((FLAC_THREADS > 1)) guard).
    """
    return flac_encode_cmd(
        Path("-"), out, bps=8, sign="unsigned", rate=header_rate, channels=1,
        blocksize=BLOCKSIZE, level=level, threads=threads if threads > 1 else 0,
    )  # fmt: skip


def sox_resample_args(capture_rate: int, target_rate: int, out: Path | None) -> list[str]:
    """sox raw-u8 resampler, parameters exactly as in local-capture.sh.

    Rates are passed 100:1 (sox-scale); `out` None writes to stdout for a
    following flac stage.
    """
    return [
        "sox", "-D",
        "-t", "raw", "-r", str(sox_rate(capture_rate)),
        "-b", "8", "-c", "1", "-L", "-e", "unsigned-integer", "-",
        "-t", "raw",
        "-b", "8", "-c", "1", "-L", "-e", "unsigned-integer",
        str(out) if out is not None else "-",
        "rate", "-l", str(sox_rate(target_rate)),
    ]  # fmt: skip


def ffmpeg_linear_args(linear_rate: int, linear_out: Path, headswitch_out: Path) -> list[str]:
    """ffmpeg splitting the s24le 2.1 linear stream into linear FLAC + headswitch u8.

    `linear_rate` must be the rate the /start response reported, not the
    requested one.
    """
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-ar", str(linear_rate), "-ac", "3", "-f", "s24le", "-i", "-",
        "-filter_complex",
        "[0:a]channelsplit=channel_layout=2.1[FL][FR][headswitch],"
        "[FL][FR]amerge=inputs=2[linear]",
        "-map", "[linear]", "-compression_level", "0", str(linear_out),
        "-map", "[headswitch]", "-f", "u8", str(headswitch_out),
    ]  # fmt: skip


class OutputPlan(NamedTuple):
    """Planned output files; None = channel disabled / not produced."""

    video: Path | None
    hifi: Path | None
    linear: Path
    headswitch: Path | None
    sidecar: Path

    def paths(self) -> list[Path]:
        return [p for p in self if p is not None]


def plan_outputs(
    base: str, *, video: bool, hifi: bool,
    compress_video: bool, compress_hifi: bool, convert_linear: bool,
) -> OutputPlan:  # fmt: skip
    """Output file names for a capture — suffix conventions from local-capture.sh."""
    return OutputPlan(
        video=Path(f"{base}-video.{'flac' if compress_video else 'u8'}") if video else None,
        hifi=Path(f"{base}-hifi.{'flac' if compress_hifi else 'u8'}") if hifi else None,
        linear=Path(f"{base}-linear.{'flac' if convert_linear else 's24'}"),
        headswitch=Path(f"{base}-headswitch.u8") if convert_linear else None,
        sidecar=Path(f"{base}-capture.json"),
    )


class ChainSpec(NamedTuple):
    """One planned curl → encoder pipe chain (built pure, spawned later)."""

    name: str
    path: str  # server endpoint (e.g. "/cxadc?0")
    curl_out: str  # "-" pipes into the stages; otherwise curl writes the file itself
    stages: list[tuple[str, list[str]]]
    describe: str  # for the "PID N is capturing ..." log line


def build_chain_specs(
    *, cards: list[tuple[str, int]], plan: OutputPlan,
    compress_video: bool, video_level: int, compress_hifi: bool, hifi_level: int,
    resample_hifi: bool, capture_rate: int, resample_rate: int,
    convert_linear: bool, linear_rate: int, flac_threads: int = 0,
) -> list[ChainSpec]:  # fmt: skip
    """All pipe chains for a capture — the encoder variants of capture.sh.

    The stream index in /cxadc?N is the card's position in the /start URL,
    i.e. its position in `cards`. `linear_rate` must be the rate the /start
    response reported. `flac_threads` must already be resolved against the
    installed flac version (0/1 = single-threaded).
    """
    header_rate = flac_header_rate(capture_rate)
    specs = []
    for index, (label, _card) in enumerate(cards):
        if label == "video":
            stages = (
                [("flac", flac_encode_args(video_level, header_rate, plan.video, flac_threads))]
                if compress_video
                else []
            )
            specs.append(ChainSpec(
                "video", f"/cxadc?{index}", "-" if stages else str(plan.video),
                stages, f"video to {plan.video}",
            ))  # fmt: skip
            continue
        if resample_hifi and compress_hifi:
            stages = [
                ("sox", sox_resample_args(capture_rate, resample_rate, None)),
                (
                    "flac",
                    flac_encode_args(
                        hifi_level, flac_header_rate(resample_rate), plan.hifi, flac_threads
                    ),
                ),
            ]
        elif resample_hifi:
            stages = [("sox", sox_resample_args(capture_rate, resample_rate, plan.hifi))]
        elif compress_hifi:
            stages = [("flac", flac_encode_args(hifi_level, header_rate, plan.hifi, flac_threads))]
        else:
            stages = []
        specs.append(ChainSpec(
            "hifi", f"/cxadc?{index}", "-" if stages else str(plan.hifi),
            stages, f"hifi to {plan.hifi}",
        ))  # fmt: skip

    if convert_linear:
        specs.append(ChainSpec(
            "linear", "/linear", "-",
            [("ffmpeg", ffmpeg_linear_args(linear_rate, plan.linear, plan.headswitch))],
            f"linear to {plan.linear}, headswitch to {plan.headswitch}",
        ))  # fmt: skip
    else:
        specs.append(ChainSpec(
            "linear", "/linear", str(plan.linear),
            [], f"linear+headswitch to {plan.linear}",
        ))  # fmt: skip
    return specs


def date_suffix(now: datetime) -> str:
    """`date -Iseconds | sed 's/[T:\\+]/_/g'` — the --add-date suffix of the script."""
    return re.sub(r"[T:+]", "_", now.isoformat(timespec="seconds"))


def parse_duration(text: str) -> int:
    """--duration value in seconds: plain seconds, '90m'/'2h'/'45s', or [HH:]MM:SS."""
    text = text.strip()
    if ":" in text:
        parts = text.split(":")
        if len(parts) in (2, 3) and all(p.isdigit() for p in parts):
            hours, minutes, seconds = ([0, 0, 0] + [int(p) for p in parts])[-3:]
            value = hours * 3600 + minutes * 60 + seconds
        else:
            value = 0
    else:
        match = _DURATION_RE.match(text)
        value = int(match.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[match.group(2)] \
            if match else 0  # fmt: skip
    if value <= 0:
        raise ToolError(
            f"Invalid --duration '{text}' (expected seconds, e.g. 5400, '90m', '2h', or HH:MM:SS)"
        )
    return value


def parse_amixer_selection(output: str) -> str | None:
    """Current item from `amixer sget` output (the \"Item0: 'CXADC-40MHz'\" line)."""
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("Item0:"):
            match = re.search(r"'([^']*)'", line)
            return match.group(1) if match else line.split(":", 1)[1].strip()
    return None


def parse_meminfo_available(text: str) -> int:
    """MemAvailable from /proc/meminfo, in bytes."""
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    raise ToolError("Cannot find MemAvailable in /proc/meminfo")


def diff_expected(label: str, actual: dict, expected: dict) -> list[str]:
    """Human-readable deviations of `actual` from `expected` (missing counts too)."""
    return [
        f"{label} {key}: expected {expected[key]}, got {actual.get(key, '<missing>')}"
        for key in expected
        if actual.get(key) != expected[key]
    ]


def diff_resources(
    free_disk: int, avail_ram: int, n_cards: int,
    min_disk_gib: float, headroom_gib: float, where: str,
) -> list[str]:  # fmt: skip
    """Deviations for the resource pre-flight (disk for the capture, RAM for the server)."""
    failures = []
    if free_disk < min_disk_gib * GIB:
        failures.append(
            f"free disk space in {where}: expected >= {min_disk_gib:g} GiB, "
            f"got {free_disk / GIB:.1f} GiB"
        )
    need_ram = (n_cards + headroom_gib) * GIB
    if avail_ram < need_ram:
        failures.append(
            f"free RAM: expected >= {need_ram / GIB:.1f} GiB "
            f"(1 GiB ring buffer x {n_cards} cards + {headroom_gib:g} GiB headroom), "
            f"got {avail_ram / GIB:.1f} GiB"
        )
    return failures


def format_buffer_stats(stats: dict) -> str:
    """Status-line fragment from /stats — linear first, then the CX cards."""
    pcts = []
    linear = stats.get("linear")
    if isinstance(linear, dict) and "difference_pct" in linear:
        pcts.append(linear["difference_pct"])
    for entry in stats.get("cxadc") or []:
        if isinstance(entry, dict):
            pcts.append(entry.get("difference_pct"))
    return "Buffers: " + " ".join(f"{p if p is not None else '?':>2}%" for p in pcts)


def extract_difference_pcts(elapsed: float, stats: dict) -> dict:
    """Compact stats-history entry for the sidecar."""
    linear = stats.get("linear")
    return {
        "elapsed": round(elapsed, 1),
        "linear": linear.get("difference_pct") if isinstance(linear, dict) else None,
        "cxadc": [
            entry.get("difference_pct") if isinstance(entry, dict) else None
            for entry in stats.get("cxadc") or []
        ],
    }


def build_sidecar(
    *, base: str, started_at: str, stopped_at: str, stop_reason: str, elapsed: float,
    settings: dict, linear_rate: int | None, overflows: int | None,
    preflight: dict, skipped_checks: list[str], files: dict,
    chain_returncodes: dict, stats_history: list[dict], versions: dict,
) -> dict:  # fmt: skip
    """Assemble the <base>-capture.json sidecar document."""
    return {
        "base": base,
        "started_at": started_at,
        "stopped_at": stopped_at,
        "stop_reason": stop_reason,
        "elapsed_seconds": round(elapsed, 1),
        "settings": settings,
        "linear_rate": linear_rate,  # actual rate reported by /start
        "overflows": overflows,
        "preflight": {"checked": preflight, "skipped": skipped_checks},
        "files": {name: str(path) for name, path in files.items() if path is not None},
        "chain_returncodes": chain_returncodes,
        "stats_history": stats_history,
        "versions": versions,
    }


# =============================================================================
# Pre-flight (IO wrappers around the pure diff helpers)
# =============================================================================


def read_cxadc_params(card: int) -> dict[str, int]:
    """Current cxadc module parameters for one card from sysfs."""
    params_dir = Path(f"/sys/class/cxadc/cxadc{card}/device/parameters")
    if not params_dir.is_dir():
        raise ToolError(f"cxadc{card}: {params_dir} not found (card present? driver loaded?)")
    values = {}
    for name in CXADC_PARAMS:
        try:
            values[name] = int((params_dir / name).read_text().strip())
        except (OSError, ValueError) as exc:
            raise ToolError(f"cxadc{card}: cannot read parameter {name}: {exc}") from exc
    return values


def read_clock_selection(device: str, output_no: int) -> str:
    """Selected item of one clockgen output via amixer (e.g. 'CXADC-40MHz')."""
    result = run(
        ["amixer", "-D", device, "sget", f"CXADC-Clock {output_no} Select Playback Source,0"],
        capture=True,
    )
    item = parse_amixer_selection(result.stdout)
    if item is None:
        raise ToolError(f"Cannot parse amixer output for clockgen output {output_no}")
    return item


def run_preflight(
    args: argparse.Namespace, cards: list[tuple[str, int]], out_dir: Path
) -> tuple[dict, list[str]]:
    """All enabled check groups. Returns (checked values, skipped groups).

    Any deviation from the configured target values raises a ToolError listing
    every diff — deviations abort, they are never downgraded to warnings.
    """
    values: dict = {}
    failures: list[str] = []
    skipped: list[str] = []

    if args.ignore_cxadc_checks:
        skipped.append("cxadc")
    else:
        expected = {
            "vmux": _CAP.cxadc_vmux,
            "level": _CAP.cxadc_level,
            "sixdb": _CAP.cxadc_sixdb,
            "tenxfsc": _CAP.cxadc_tenxfsc,
            "tenbit": _CAP.cxadc_tenbit,
        }
        for label, card in cards:
            try:
                actual = read_cxadc_params(card)
                values[f"cxadc{card}"] = actual
                failures += diff_expected(f"cxadc{card} ({label})", actual, expected)
            except ToolError as exc:
                failures.append(str(exc))

    if args.ignore_clock_checks:
        skipped.append("clock")
    else:
        expected_item = clock_item_for_rate(args.rate)
        # Which clockgen output feeds which channel is wiring, not card
        # numbering — configured via clockgen_video_out / clockgen_hifi_out.
        outputs = {"video": _CAP.clockgen_video_out, "hifi": _CAP.clockgen_hifi_out}
        for label, _card in cards:
            output_no = outputs[label]
            try:
                item = read_clock_selection(_CAP.clockgen_device, output_no)
                values[f"clock{output_no}"] = item
                if item != expected_item:
                    failures.append(
                        f"clockgen output {output_no} ({label}): "
                        f"expected '{expected_item}' for {args.rate} Hz, got '{item}'"
                    )
            except ToolError as exc:
                failures.append(str(exc))

    if args.ignore_resource_checks:
        skipped.append("resources")
    else:
        free_disk = shutil.disk_usage(out_dir).free
        avail_ram = parse_meminfo_available(Path("/proc/meminfo").read_text())
        values["free_disk_gib"] = round(free_disk / GIB, 1)
        values["free_ram_gib"] = round(avail_ram / GIB, 1)
        failures += diff_resources(
            free_disk, avail_ram, len(cards),
            _CAP.min_free_disk_gib, _CAP.ram_headroom_gib, str(out_dir),
        )  # fmt: skip

    if failures:
        raise ToolError(
            "Pre-flight failed:\n  - "
            + "\n  - ".join(failures)
            + "\nFix the deviations or skip a group with --ignore-{cxadc,clock,resource}-checks."
        )
    return values, skipped


# =============================================================================
# Stream chains (curl | encoder... — Python never touches the data)
# =============================================================================


class Chain:
    """One running curl → encoder pipe chain for a single stream endpoint."""

    def __init__(self, name: str, procs: list[tuple[str, subprocess.Popen]]):
        self.name = name
        self.procs = procs

    def poll_exited(self) -> list[tuple[str, int]]:
        """(label, returncode) of stages that have already exited."""
        return [(label, proc.returncode) for label, proc in self.procs if proc.poll() is not None]

    def wait(self) -> list[tuple[str, int]]:
        """Wait for curl to run out to EOF, then for each encoder, in pipe order."""
        return [(label, proc.wait()) for label, proc in self.procs]

    def abort(self) -> None:
        """Emergency stop: terminate curl only — the encoders see EOF and finalize."""
        _, curl = self.procs[0]
        if curl.poll() is None:
            curl.terminate()


def spawn_chain(
    name: str,
    curl_argv: list[str],
    stages: list[tuple[str, list[str]]],
    echo: bool = False,
) -> Chain:
    """Start curl plus encoder stages, connected stdout→stdin.

    The parent's copy of every pipe write-end is closed right after the
    downstream process inherits it — otherwise the encoders would never see
    EOF when curl exits.

    Every process gets its own session: a terminal Ctrl-C must reach only
    vhs-tool (whose handler stops gracefully via /stop) — if it also hit the
    encoders directly, they would die mid-write and leave truncated files.
    """
    if echo:
        pretty = " | ".join(shlex.join(argv) for _, argv in [("curl", curl_argv), *stages])
        print(f"  $ {pretty}", file=sys.stderr)
    procs: list[tuple[str, subprocess.Popen]] = []
    try:
        curl = subprocess.Popen(
            curl_argv, stdout=subprocess.PIPE if stages else None, start_new_session=True
        )
        procs.append(("curl", curl))
        upstream = curl
        for index, (label, argv) in enumerate(stages):
            is_last = index == len(stages) - 1
            proc = subprocess.Popen(
                argv,
                stdin=upstream.stdout,
                stdout=None if is_last else subprocess.PIPE,
                start_new_session=True,
            )
            upstream.stdout.close()  # parent copy — the downstream process owns it now
            procs.append((label, proc))
            upstream = proc
    except OSError as exc:
        for _, proc in procs:
            proc.terminate()
        for _, proc in procs:
            proc.wait()
        raise ToolError(f"Cannot start {name} chain: {exc}") from exc
    return Chain(name, procs)


def drain_chains(chains: list[Chain], timeout: float = 30.0) -> None:
    """Emergency cleanup: stop the curls, give encoders `timeout` to finalize."""
    for chain in chains:
        chain.abort()
    deadline = time.monotonic() + timeout
    for chain in chains:
        for _, proc in chain.procs:
            try:
                proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


# =============================================================================
# Status loop ('q' / --duration / signals — one shared stop path)
# =============================================================================


def run_capture_loop(
    connection: ServerConnection,
    chains: list[Chain],
    duration: int | None,
    stop_event: threading.Event,
) -> tuple[str, float, list[dict]]:
    """Print status/stats until a stop condition; returns (reason, elapsed, history).

    Reasons: 'q' (keyboard), 'duration', 'signal' (SIGINT/SIGTERM), or
    'stream-died' (a pipe stage exited mid-capture). All of them lead to the
    same /stop → drain → terminate shutdown in cmd_capture.
    """
    history: list[dict] = []
    start = time.monotonic()
    reason = None
    is_tty = sys.stdin.isatty()

    # A socketpair as signal wakeup fd interrupts the select() below immediately
    # instead of waiting out the 5 s tick (PEP 475 would otherwise retry it).
    wake_r, wake_w = socket.socketpair()
    wake_r.setblocking(False)
    wake_w.setblocking(False)
    old_wakeup = signal.set_wakeup_fd(wake_w.fileno(), warn_on_full_buffer=False)
    saved_tty = None
    if is_tty:
        saved_tty = termios.tcgetattr(sys.stdin.fileno())
        tty.setcbreak(sys.stdin.fileno())

    if is_tty:
        log("Capture running... Press 'q' to stop the capture.")
    else:
        log("Capture running (no TTY — stop via --duration, SIGINT, or SIGTERM).")

    try:
        while reason is None:
            elapsed = time.monotonic() - start
            try:
                stats = connection.get_json("/stats")
                history.append(extract_difference_pcts(elapsed, stats))
                stats_msg = format_buffer_stats(stats)
            except ToolError:
                stats_msg = "Failed to get stats."
            log(f"Capturing for {int(elapsed) // 60}m {int(elapsed) % 60}s... {stats_msg}")

            dead = [
                f"{chain.name}/{label} exited with code {code}"
                for chain in chains
                for label, code in chain.poll_exited()
            ]
            if dead:
                for message in dead:
                    log(f"ERROR: {message}")
                reason = "stream-died"
                break
            if stop_event.is_set():
                reason = "signal"
                break
            timeout = STATUS_INTERVAL
            if duration is not None:
                remaining = duration - (time.monotonic() - start)
                if remaining <= 0:
                    reason = "duration"
                    break
                timeout = min(timeout, remaining)

            rlist: list = [wake_r]
            if is_tty:
                rlist.append(sys.stdin)
            ready, _, _ = select.select(rlist, [], [], timeout)
            if wake_r in ready:
                with contextlib.suppress(BlockingIOError):
                    wake_r.recv(4096)
            if is_tty and sys.stdin in ready:
                key = os.read(sys.stdin.fileno(), 1)
                if key in (b"q", b"Q"):
                    print(file=sys.stderr)
                    log("Stopping capture")
                    reason = "q"
                else:
                    print(file=sys.stderr)
                    log("Press 'q' to stop the capture.")
    finally:
        signal.set_wakeup_fd(old_wakeup)
        wake_r.close()
        wake_w.close()
        if saved_tty is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved_tty)

    return reason, time.monotonic() - start, history


# =============================================================================
# Versions (for the sidecar)
# =============================================================================


def _tool_version(argv: list[str]) -> str:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
    except OSError:
        return "unknown"
    lines = (result.stdout or result.stderr).strip().splitlines()
    return lines[0].strip() if lines else "unknown"


# =============================================================================
# Argument parsing
# =============================================================================


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "capture",
        help="Record RF captures via cxadc_vhs_server (video/hifi/linear)",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "base",
        help='Output base path (e.g. "./captures/VHS_PAL_Tape_010"); '
        "channel suffixes and extensions are appended",
    )

    channels = parser.add_argument_group("channels")
    channels.add_argument(
        "--video", type=int, default=_CAP.video_card, metavar="N",
        help=f"CX card number for video RF (default: {_CAP.video_card})",
    )  # fmt: skip
    channels.add_argument("--no-video", action="store_true", help="Disable the video RF capture")
    channels.add_argument(
        "--hifi", type=int, default=_CAP.hifi_card, metavar="N",
        help=f"CX card number for HiFi RF (default: {_CAP.hifi_card})",
    )  # fmt: skip
    channels.add_argument("--no-hifi", action="store_true", help="Disable the HiFi RF capture")
    channels.add_argument(
        "--linear", default=_CAP.linear_device, metavar="DEVICE",
        help="ALSA device for the linear+headswitch capture "
        f"(default: {_CAP.linear_device!r}; empty = server default)",
    )  # fmt: skip

    rates = parser.add_argument_group(
        "rates (the capture rate drives FLAC headers, clock check and resample ratio)"
    )
    rates.add_argument(
        "--rate", type=int, default=_CAP.capture_rate,
        help=f"CX card capture rate in Hz (default: {_CAP.capture_rate})",
    )  # fmt: skip
    rates.add_argument(
        "--resample-rate", type=int, default=_CAP.hifi_resample_rate,
        help=f"HiFi target rate in Hz for --resample-hifi (default: {_CAP.hifi_resample_rate})",
    )  # fmt: skip
    rates.add_argument(
        "--linear-rate", type=int, default=_CAP.linear_rate,
        help=f"Linear sample rate in Hz (default: {_CAP.linear_rate})",
    )  # fmt: skip

    encoding = parser.add_argument_group("encoding")
    encoding.add_argument(
        "--compress-video", action=argparse.BooleanOptionalAction, default=True,
        help="Compress video RF to FLAC (default: on; off writes .u8)",
    )  # fmt: skip
    encoding.add_argument(
        "--compress-video-level", type=int, default=_CAP.flac_level, metavar="0-8",
        help=f"Video FLAC compression level (default: {_CAP.flac_level})",
    )  # fmt: skip
    encoding.add_argument(
        "--compress-hifi", action=argparse.BooleanOptionalAction, default=True,
        help="Compress HiFi RF to FLAC (default: on; off writes .u8)",
    )  # fmt: skip
    encoding.add_argument(
        "--compress-hifi-level", type=int, default=_CAP.flac_level, metavar="0-8",
        help=f"HiFi FLAC compression level (default: {_CAP.flac_level})",
    )  # fmt: skip
    encoding.add_argument(
        "--flac-threads", type=int, default=_CAP.flac_threads, metavar="N",
        help="FLAC encoder threads (default: "
        f"{_CAP.flac_threads or 'auto = all cores'}; needs flac >= 1.5.0)",
    )  # fmt: skip
    encoding.add_argument(
        "--resample-hifi", action=argparse.BooleanOptionalAction, default=True,
        help="Resample HiFi to the target rate via sox (default: on)",
    )  # fmt: skip
    encoding.add_argument(
        "--convert-linear", action=argparse.BooleanOptionalAction, default=True,
        help="Split linear into FLAC + headswitch u8 via ffmpeg (default: on; off writes .s24)",
    )  # fmt: skip

    parser.add_argument(
        "--add-date", action="store_true", help="Append the current date/time to the base name"
    )
    parser.add_argument(
        "--duration", metavar="TIME",
        help="Stop automatically after TIME (seconds, '90m', '2h', or HH:MM:SS) "
        "for unattended captures",
    )  # fmt: skip

    checks = parser.add_argument_group("pre-flight checks")
    checks.add_argument(
        "--ignore-cxadc-checks", action="store_true",
        help="Skip the cxadc sysfs parameter checks",
    )  # fmt: skip
    checks.add_argument(
        "--ignore-clock-checks", action="store_true",
        help="Skip the clockgen rate check (amixer)",
    )  # fmt: skip
    checks.add_argument(
        "--ignore-resource-checks", action="store_true",
        help="Skip the free disk space / free RAM checks",
    )  # fmt: skip
    checks.add_argument("--ignore-checks", action="store_true", help="Skip all pre-flight checks")
    parser.add_argument("-v", "--verbose", action="store_true", help="Echo the pipe chains")
    parser.set_defaults(func=cmd_capture)


# =============================================================================
# Command
# =============================================================================


def cmd_capture(args: argparse.Namespace) -> int:
    if args.ignore_checks:
        args.ignore_cxadc_checks = True
        args.ignore_clock_checks = True
        args.ignore_resource_checks = True

    # -- Validate settings (one source of truth for every rate) -----------------
    with_video = not args.no_video
    with_hifi = not args.no_hifi
    video_level = validate_flac_level(args.compress_video_level, "--compress-video-level")
    hifi_level = validate_flac_level(args.compress_hifi_level, "--compress-hifi-level")
    capture_header_rate = flac_header_rate(args.rate)
    if with_hifi and args.resample_hifi:  # the resample rate is unused otherwise
        flac_header_rate(args.resample_rate)  # validate representability early
        if args.resample_rate >= args.rate:
            raise ToolError(
                f"--resample-rate ({args.resample_rate}) must be below "
                f"the capture rate ({args.rate})"
            )
    duration = parse_duration(args.duration) if args.duration else None

    if with_video and with_hifi and args.video == args.hifi:
        raise ToolError(f"--video and --hifi use the same CX card ({args.video})")
    cards = [
        (label, card)
        for label, card, enabled in (
            ("video", args.video, with_video),
            ("hifi", args.hifi, with_hifi),
        )
        if enabled
    ]
    for label, card in cards:
        if card < 0:
            raise ToolError(f"--{label} card number must be >= 0, got {card}")

    base = args.base
    if args.add_date:
        base = f"{base}-{date_suffix(datetime.now().astimezone())}"

    plan = plan_outputs(
        base,
        video=with_video,
        hifi=with_hifi,
        compress_video=args.compress_video,
        compress_hifi=args.compress_hifi,
        convert_linear=args.convert_linear,
    )
    existing = [path for path in plan.paths() if path.exists()]
    if existing:
        raise ToolError(
            "Output files already exist: "
            + ", ".join(str(p) for p in existing)
            + " — remove them, change the base path, or use --add-date"
        )
    out_dir = Path(base).parent
    if not out_dir.exists():
        out_dir.mkdir(parents=True)
        log(f"Created output directory {out_dir}")

    # -- Dependencies ------------------------------------------------------------
    deps = ["curl"]
    if (with_video and args.compress_video) or (with_hifi and args.compress_hifi):
        deps.append("flac")
    if with_hifi and args.resample_hifi:
        deps.append("sox")
    if args.convert_linear:
        deps.append("ffmpeg")
    if not args.ignore_clock_checks and cards:
        deps.append("amixer")
    check_deps(*dict.fromkeys(deps))
    server_bin = resolve_binary(CXADC_SERVER_BIN, "cxadc_vhs_server")

    # -- FLAC threading (capture.sh: auto = nproc, needs flac >= 1.5.0) ----------
    if args.flac_threads < 0:
        raise ToolError(f"--flac-threads must be >= 0, got {args.flac_threads}")
    flac_threads = 0
    flac_version_output = ""
    if "flac" in deps:
        flac_version_output = run(["flac", "--version"], capture=True, check=False).stdout
        flac_threads = resolve_threads(
            args.flac_threads or None, parse_flac_version(flac_version_output)
        )
        if flac_threads > 1:
            log(f"FLAC threading: {flac_threads} threads")

    # -- Pre-flight --------------------------------------------------------------
    preflight_values, skipped_checks = run_preflight(args, cards, out_dir)
    if preflight_values:
        log("Pre-flight OK: " + json.dumps(preflight_values))

    server = LocalServer(server_bin)
    versions = {
        "vhs_tool": __version__,
        "cxadc_vhs_server": server.version(),
        "curl": _tool_version(["curl", "--version"]),
    }
    if "flac" in deps:
        versions["flac"] = (flac_version_output.strip().splitlines() or ["unknown"])[0]
    if "sox" in deps:
        versions["sox"] = _tool_version(["sox", "--version"])
    if "ffmpeg" in deps:
        versions["ffmpeg"] = _tool_version(["ffmpeg", "-version"])

    settings = {
        "capture_rate": args.rate,
        "flac_header_rate": capture_header_rate,
        "hifi_resample_rate": args.resample_rate if (with_hifi and args.resample_hifi) else None,
        "linear_rate_requested": args.linear_rate,
        "linear_device": args.linear or None,
        "video_card": args.video if with_video else None,
        "hifi_card": args.hifi if with_hifi else None,
        "compress_video": args.compress_video,
        "compress_video_level": video_level,
        "compress_hifi": args.compress_hifi,
        "compress_hifi_level": hifi_level,
        "flac_threads": flac_threads,
        "resample_hifi": args.resample_hifi,
        "convert_linear": args.convert_linear,
        "duration": duration,
    }

    # -- Capture -----------------------------------------------------------------
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    stop_event = threading.Event()
    received_signal: list[str] = []

    def request_stop(signum, frame):
        if stop_event.is_set():
            print("\n(shutdown already in progress)", file=sys.stderr)
        else:
            received_signal.append(signal.Signals(signum).name)
            stop_event.set()

    exit_code = 0
    overflows: int | None = None
    linear_rate_actual: int | None = None
    chain_returncodes: dict[str, list] = {}
    stats_history: list[dict] = []
    stop_reason = "error"
    elapsed = 0.0
    chains: list[Chain] = []

    with server as connection:
        log(f"Server started (PID {server.pid}, {connection.describe()})")
        old_int = signal.signal(signal.SIGINT, request_stop)
        old_term = signal.signal(signal.SIGTERM, request_stop)
        try:
            try:
                start_data = connection.get_json(
                    start_path(args.linear_rate, [card for _, card in cards], args.linear)
                )
                state, linear_rate_actual, fail_reason = parse_start_response(start_data)
                if state != "Running":
                    raise ToolError(f"Server failed to start capture: {fail_reason or start_data}")
                log(f"Capture armed (linear rate {linear_rate_actual} Hz)")

                specs = build_chain_specs(
                    cards=cards,
                    plan=plan,
                    compress_video=args.compress_video,
                    video_level=video_level,
                    compress_hifi=args.compress_hifi,
                    hifi_level=hifi_level,
                    resample_hifi=args.resample_hifi,
                    capture_rate=args.rate,
                    resample_rate=args.resample_rate,
                    convert_linear=args.convert_linear,
                    linear_rate=linear_rate_actual,
                    flac_threads=flac_threads,
                )
                for spec in specs:
                    chain = spawn_chain(
                        spec.name,
                        connection.curl_cmd(spec.path, spec.curl_out),
                        spec.stages,
                        echo=args.verbose,
                    )
                    chains.append(chain)
                    log(f"PID {chain.procs[0][1].pid} is capturing {spec.describe}")

                stop_reason, elapsed, stats_history = run_capture_loop(
                    connection, chains, duration, stop_event
                )
                if stop_reason == "signal":
                    name = received_signal[0] if received_signal else "signal"
                    log(f"Stopping capture ({name})")
                elif stop_reason == "duration":
                    log(f"Stopping capture (--duration {args.duration} reached)")
                if stop_reason == "stream-died":
                    exit_code = 1

                # -- Shutdown: /stop → curl EOF → encoders → server ----------------
                stopped_cleanly = False
                try:
                    stop_data = connection.get_json("/stop")
                    stopped_cleanly = stop_data.get("state") == "Idle"
                    if not stopped_cleanly:
                        log(f"ERROR: server failed to stop capture: {stop_data}")
                        exit_code = 1
                    if "overflows" in stop_data:
                        overflows = int(stop_data["overflows"])
                        log(f"Encountered {overflows} overflows during capture")
                    else:
                        log("WARNING: can't find overflow information in /stop response")
                        exit_code = 1
                except ToolError as exc:
                    log(f"ERROR: cannot send stop request to server: {exc}")
                    exit_code = 1
                if not stopped_cleanly:
                    # The streams may never see EOF now — stop the curls so the
                    # encoders can finalize instead of blocking forever below.
                    log("Server did not stop cleanly — closing the stream readers")
                    drain_chains(chains)

                log("Waiting for writes to finish...")
                for chain in chains:
                    results = chain.wait()
                    chain_returncodes[chain.name] = [list(item) for item in results]
                    for label, code in results:
                        # curl ends nonzero when the server closes mid-transfer;
                        # a nonzero *encoder* means the output cannot be trusted.
                        if code != 0 and label != "curl":
                            log(f"ERROR: {chain.name}/{label} exited with code {code}")
                            exit_code = 1
                        elif code != 0:
                            log(f"WARNING: {chain.name}/curl exited with code {code}")
            except BaseException:
                drain_chains(chains)
                raise
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)
        server.terminate()
        log("Server stopped")

    stopped_at = datetime.now().astimezone().isoformat(timespec="seconds")

    sidecar = build_sidecar(
        base=base,
        started_at=started_at,
        stopped_at=stopped_at,
        stop_reason=stop_reason,
        elapsed=elapsed,
        settings=settings,
        linear_rate=linear_rate_actual,
        overflows=overflows,
        preflight=preflight_values,
        skipped_checks=skipped_checks,
        files={
            "video": plan.video,
            "hifi": plan.hifi,
            "linear": plan.linear,
            "headswitch": plan.headswitch,
        },
        chain_returncodes=chain_returncodes,
        stats_history=stats_history,
        versions=versions,
    )
    plan.sidecar.write_text(json.dumps(sidecar, indent=2) + "\n")
    log(f"Sidecar written to {plan.sidecar}")

    if exit_code == 0 and overflows:
        log(f"ERROR: {overflows} overflows — samples were lost during capture")
        exit_code = 2
    log("Finished!" if exit_code == 0 else f"Finished with problems (exit {exit_code})")
    return exit_code
