from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import vhs_tool.commands.capture as capture
from vhs_tool.capture_server import UnixSocketConnection
from vhs_tool.commands.capture import (
    build_chain_specs,
    build_sidecar,
    clock_item_for_rate,
    date_suffix,
    diff_expected,
    diff_resources,
    extract_difference_pcts,
    ffmpeg_linear_args,
    flac_encode_args,
    flac_header_rate,
    format_buffer_stats,
    parse_amixer_selection,
    parse_duration,
    parse_meminfo_available,
    parse_start_response,
    plan_outputs,
    sox_rate,
    sox_resample_args,
    spawn_chain,
    start_path,
    validate_flac_level,
)
from vhs_tool.common import ToolError

GIB = 1 << 30


# -- Rates and levels ----------------------------------------------------------


def test_validate_flac_level_accepts_0_to_8():
    for level in range(9):
        assert validate_flac_level(level, "--x") == level


@pytest.mark.parametrize("level", [-1, 9, 11])
def test_validate_flac_level_rejects_out_of_range(level):
    # The script's default of -11 with --lax bloats RF FLACs; 0-8 only.
    with pytest.raises(ToolError, match="between 0 and 8"):
        validate_flac_level(level, "--compress-video-level")


def test_flac_header_rate_is_1000_to_1():
    assert flac_header_rate(40_000_000) == 40000
    assert flac_header_rate(10_000_000) == 10000
    # community convention truncates: stock-crystal 28.63 MSPS → 28636
    assert flac_header_rate(28_636_363) == 28636


@pytest.mark.parametrize("rate", [0, -1000, 999])
def test_flac_header_rate_rejects_too_low(rate):
    with pytest.raises(ToolError, match="at least 1000"):
        flac_header_rate(rate)


def test_sox_rate_is_100_to_1():
    assert sox_rate(40_000_000) == 400000
    assert sox_rate(10_000_000) == 100000


def test_clock_item_for_rate():
    assert clock_item_for_rate(40_000_000) == "CXADC-40MHz"
    assert clock_item_for_rate(28_636_363) == "CXADC-28.63MHz"
    with pytest.raises(ToolError, match="No clockgen mode known for a capture rate of 12345"):
        clock_item_for_rate(12345)


# -- Command builders (argv fidelity with local-capture.sh) --------------------


def test_flac_encode_args_match_script():
    assert flac_encode_args(8, 40000, Path("tape-video.flac")) == [
        "flac", "--silent", "-8", "--blocksize=65535", "--lax",
        "--sample-rate=40000", "--channels=1", "--bps=8",
        "--sign=unsigned", "--endian=little",
        "-f", "-", "-o", "tape-video.flac",
    ]  # fmt: skip


def test_flac_encode_args_threads_position_matches_script():
    # capture.sh: --silent -$LEVEL $FLAC_THREAD_ARG --blocksize=...
    argv = flac_encode_args(8, 40000, Path("t.flac"), threads=16)
    assert argv[:5] == ["flac", "--silent", "-8", "--threads=16", "--blocksize=65535"]
    # <= 1 thread adds nothing, exactly like the script's ((FLAC_THREADS > 1)) guard
    assert "--threads=1" not in flac_encode_args(8, 40000, Path("t.flac"), threads=1)
    assert "--threads=0" not in flac_encode_args(8, 40000, Path("t.flac"), threads=0)


def test_sox_resample_args_match_script():
    # 40 MSPS → 10 MSPS is the script's `-r 400000 ... rate -l 100000` (100:1)
    assert sox_resample_args(40_000_000, 10_000_000, None) == [
        "sox", "-D",
        "-t", "raw", "-r", "400000", "-b", "8", "-c", "1", "-L", "-e", "unsigned-integer", "-",
        "-t", "raw", "-b", "8", "-c", "1", "-L", "-e", "unsigned-integer", "-",
        "rate", "-l", "100000",
    ]  # fmt: skip


def test_sox_resample_args_with_file_output():
    argv = sox_resample_args(40_000_000, 10_000_000, Path("tape-hifi.u8"))
    assert "tape-hifi.u8" in argv
    assert argv[-3:] == ["rate", "-l", "100000"]


def test_ffmpeg_linear_args_match_script():
    assert ffmpeg_linear_args(46875, Path("t-linear.flac"), Path("t-headswitch.u8")) == [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-ar", "46875", "-ac", "3", "-f", "s24le", "-i", "-",
        "-filter_complex",
        "[0:a]channelsplit=channel_layout=2.1[FL][FR][headswitch],"
        "[FL][FR]amerge=inputs=2[linear]",
        "-map", "[linear]", "-compression_level", "0", "t-linear.flac",
        "-map", "[headswitch]", "-f", "u8", "t-headswitch.u8",
    ]  # fmt: skip


def test_curl_cmd_unix_socket():
    conn = UnixSocketConnection("/tmp/x/server.sock")
    assert conn.curl_cmd("/cxadc?0", "-") == [
        "curl", "-s", "-X", "GET",
        "--unix-socket", "/tmp/x/server.sock",
        "--output", "-",
        "http://localhost/cxadc?0",
    ]  # fmt: skip


def test_start_path_parameter_order():
    # lrate first, then the cards (stream index = position), then lname
    assert start_path(46875, [0, 1], "") == "/start?lrate=46875&cxadc0&cxadc1"
    assert (
        start_path(46875, [2], "hw:CARD=CXADCADCClockGe")
        == "/start?lrate=46875&cxadc2&lname=hw:CARD=CXADCADCClockGe"
    )
    assert start_path(48000, [], "") == "/start?lrate=48000"


def test_parse_start_response():
    assert parse_start_response({"state": "Running", "linear_rate": 46875}) == (
        "Running", 46875, "",
    )  # fmt: skip
    assert parse_start_response({"state": "Failed", "fail_reason": "busy"}) == (
        "Failed", None, "busy",
    )  # fmt: skip


# -- Output planning -----------------------------------------------------------


def test_plan_outputs_default_pipeline():
    plan = plan_outputs(
        "cap/tape", video=True, hifi=True,
        compress_video=True, compress_hifi=True, convert_linear=True,
    )  # fmt: skip
    assert plan.video == Path("cap/tape-video.flac")
    assert plan.hifi == Path("cap/tape-hifi.flac")
    assert plan.linear == Path("cap/tape-linear.flac")
    assert plan.headswitch == Path("cap/tape-headswitch.u8")
    assert plan.sidecar == Path("cap/tape-capture.json")


def test_plan_outputs_raw_variants():
    plan = plan_outputs(
        "t", video=True, hifi=False,
        compress_video=False, compress_hifi=False, convert_linear=False,
    )  # fmt: skip
    assert plan.video == Path("t-video.u8")
    assert plan.hifi is None
    assert plan.linear == Path("t-linear.s24")
    assert plan.headswitch is None
    assert plan.paths() == [Path("t-video.u8"), Path("t-linear.s24"), Path("t-capture.json")]


def test_date_suffix_matches_script_sed():
    # date -Iseconds | sed 's/[T:\+]/_/g'
    now = datetime(2026, 8, 9, 14, 30, 5, tzinfo=timezone(timedelta(hours=2)))
    assert date_suffix(now) == "2026-08-09_14_30_05_02_00"


# -- Durations -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("5400", 5400),
        ("45s", 45),
        ("90m", 5400),
        ("2h", 7200),
        ("90:00", 5400),
        ("1:30:00", 5400),
        (" 30 ", 30),
    ],
)
def test_parse_duration(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "0", "abc", "1d", ":30", "1:2:3:4", "-5"])
def test_parse_duration_rejects(text):
    with pytest.raises(ToolError, match="Invalid --duration"):
        parse_duration(text)


# -- Pre-flight parsing and diffs ----------------------------------------------


def test_parse_amixer_selection():
    output = (
        "Simple mixer control 'CXADC-Clock 0 Select Playback Source',0\n"
        "  Capabilities: enum\n"
        "  Items: 'CXADC-20MHz' 'CXADC-28.63MHz' 'CXADC-40MHz' 'CXADC-50MHz'\n"
        "  Item0: 'CXADC-40MHz'\n"
    )
    assert parse_amixer_selection(output) == "CXADC-40MHz"
    assert parse_amixer_selection("no items here") is None


def test_parse_meminfo_available():
    text = "MemTotal:       32000000 kB\nMemAvailable:   16000000 kB\n"
    assert parse_meminfo_available(text) == 16000000 * 1024
    with pytest.raises(ToolError, match="MemAvailable"):
        parse_meminfo_available("MemTotal: 1 kB\n")


def test_diff_expected_reports_deviations_only():
    expected = {"vmux": 0, "level": 0, "tenbit": 0}
    actual = {"vmux": 2, "level": 0}
    assert diff_expected("cxadc0 (video)", actual, expected) == [
        "cxadc0 (video) vmux: expected 0, got 2",
        "cxadc0 (video) tenbit: expected 0, got <missing>",
    ]
    assert diff_expected("cxadc0", {"vmux": 0, "level": 0, "tenbit": 0}, expected) == []


def test_diff_resources():
    # 2 cards → 2 GiB ring buffers + 1 GiB headroom
    ok = diff_resources(400 * GIB, 4 * GIB, 2, 350.0, 1.0, "./captures")
    assert ok == []

    failures = diff_resources(100 * GIB, 2 * GIB, 2, 350.0, 1.0, "./captures")
    assert len(failures) == 2
    assert "free disk space in ./captures: expected >= 350 GiB, got 100.0 GiB" in failures[0]
    assert "1 GiB ring buffer x 2 cards" in failures[1]


# -- Stats ---------------------------------------------------------------------


def test_format_buffer_stats_linear_first():
    stats = {
        "linear": {"difference_pct": 1},
        "cxadc": [{"difference_pct": 0}, {"difference_pct": 12}],
    }
    assert format_buffer_stats(stats) == "Buffers:  1%  0% 12%"


def test_extract_difference_pcts():
    stats = {"linear": {"difference_pct": 3}, "cxadc": [{"difference_pct": 7}]}
    assert extract_difference_pcts(12.34, stats) == {
        "elapsed": 12.3,
        "linear": 3,
        "cxadc": [7],
    }
    assert extract_difference_pcts(1.0, {}) == {"elapsed": 1.0, "linear": None, "cxadc": []}


# -- Chain specs ---------------------------------------------------------------


def _default_plan():
    return plan_outputs(
        "cap/t", video=True, hifi=True,
        compress_video=True, compress_hifi=True, convert_linear=True,
    )  # fmt: skip


def test_build_chain_specs_default_pipeline():
    specs = build_chain_specs(
        cards=[("video", 0), ("hifi", 1)], plan=_default_plan(),
        compress_video=True, video_level=8, compress_hifi=True, hifi_level=8,
        resample_hifi=True, capture_rate=40_000_000, resample_rate=10_000_000,
        convert_linear=True, linear_rate=46875, flac_threads=8,
    )  # fmt: skip
    assert [(s.name, s.path, s.curl_out) for s in specs] == [
        ("video", "/cxadc?0", "-"),
        ("hifi", "/cxadc?1", "-"),
        ("linear", "/linear", "-"),
    ]
    video, hifi, linear = specs
    assert [label for label, _ in video.stages] == ["flac"]
    assert "--sample-rate=40000" in video.stages[0][1]
    assert "--threads=8" in video.stages[0][1]
    assert "--threads=8" in hifi.stages[1][1]  # both flac stages get the threads
    # resampled hifi: sox to stdout, then flac with the *resample* header rate
    assert [label for label, _ in hifi.stages] == ["sox", "flac"]
    assert hifi.stages[0][1][-1] == "100000"  # sox target, 100:1 scale
    assert "-" in hifi.stages[0][1]
    assert "--sample-rate=10000" in hifi.stages[1][1]
    assert [label for label, _ in linear.stages] == ["ffmpeg"]
    assert linear.describe == "linear to cap/t-linear.flac, headswitch to cap/t-headswitch.u8"


def test_build_chain_specs_stream_index_follows_card_order():
    # hifi only → it gets stream index 0 regardless of its card number
    plan = plan_outputs(
        "t", video=False, hifi=True,
        compress_video=True, compress_hifi=False, convert_linear=False,
    )  # fmt: skip
    specs = build_chain_specs(
        cards=[("hifi", 1)], plan=plan,
        compress_video=True, video_level=8, compress_hifi=False, hifi_level=8,
        resample_hifi=False, capture_rate=40_000_000, resample_rate=10_000_000,
        convert_linear=False, linear_rate=46875,
    )  # fmt: skip
    assert [(s.name, s.path, s.curl_out) for s in specs] == [
        ("hifi", "/cxadc?0", "t-hifi.u8"),
        ("linear", "/linear", "t-linear.s24"),
    ]
    assert specs[0].stages == []


def test_build_chain_specs_resample_without_compress():
    plan = plan_outputs(
        "t", video=False, hifi=True,
        compress_video=False, compress_hifi=False, convert_linear=True,
    )  # fmt: skip
    specs = build_chain_specs(
        cards=[("hifi", 1)], plan=plan,
        compress_video=False, video_level=8, compress_hifi=False, hifi_level=8,
        resample_hifi=True, capture_rate=40_000_000, resample_rate=10_000_000,
        convert_linear=True, linear_rate=46875,
    )  # fmt: skip
    hifi = specs[0]
    # sox writes the raw file itself; curl pipes into it
    assert hifi.curl_out == "-"
    assert [label for label, _ in hifi.stages] == ["sox"]
    assert "t-hifi.u8" in hifi.stages[0][1]


# -- Chain wiring --------------------------------------------------------------


class _FakePipe:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeProc:
    def __init__(self, argv, stdin=None, stdout=None, start_new_session=False):
        self.argv = [str(a) for a in argv]
        self.stdin = stdin
        self.stdout = _FakePipe() if stdout is not None else None
        self.start_new_session = start_new_session
        self.pid = 4711

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass


def test_spawn_chain_closes_parent_pipe_ends(monkeypatch):
    monkeypatch.setattr(capture.subprocess, "Popen", _FakeProc)
    chain = spawn_chain(
        "hifi",
        ["curl", "--output", "-", "url"],
        [("sox", ["sox", "-"]), ("flac", ["flac", "-o", "out.flac"])],
    )
    labels = [label for label, _ in chain.procs]
    assert labels == ["curl", "sox", "flac"]
    curl, sox_proc, flac_proc = (proc for _, proc in chain.procs)
    # every intermediate pipe's parent copy must be closed, or EOF never travels
    assert curl.stdout.closed
    assert sox_proc.stdout.closed
    assert sox_proc.stdin is curl.stdout
    assert flac_proc.stdin is sox_proc.stdout
    assert flac_proc.stdout is None
    # own sessions: a terminal Ctrl-C must never reach the encoders directly
    assert all(proc.start_new_session for _, proc in chain.procs)


def test_spawn_chain_direct_to_file(monkeypatch):
    monkeypatch.setattr(capture.subprocess, "Popen", _FakeProc)
    chain = spawn_chain("video", ["curl", "--output", "x.u8", "url"], [])
    assert [label for label, _ in chain.procs] == ["curl"]
    assert chain.procs[0][1].stdout is None


# -- Sidecar -------------------------------------------------------------------


def test_build_sidecar_shape():
    doc = build_sidecar(
        base="cap/tape",
        started_at="2026-08-09T14:00:00+02:00",
        stopped_at="2026-08-09T15:00:00+02:00",
        stop_reason="duration",
        elapsed=3600.04,
        settings={"capture_rate": 40_000_000},
        linear_rate=46875,
        overflows=0,
        preflight={"cxadc0": {"vmux": 0}},
        skipped_checks=["resources"],
        files={"video": Path("cap/tape-video.flac"), "headswitch": None},
        chain_returncodes={"video": [["curl", 0], ["flac", 0]]},
        stats_history=[{"elapsed": 0.0, "linear": 0, "cxadc": [0, 0]}],
        versions={"vhs_tool": "1.0"},
    )
    assert doc["elapsed_seconds"] == 3600.0
    assert doc["linear_rate"] == 46875
    assert doc["preflight"] == {"checked": {"cxadc0": {"vmux": 0}}, "skipped": ["resources"]}
    assert doc["files"] == {"video": "cap/tape-video.flac"}  # None entries dropped
    assert set(doc) == {
        "base", "started_at", "stopped_at", "stop_reason", "elapsed_seconds",
        "settings", "linear_rate", "overflows", "preflight", "files",
        "chain_returncodes", "stats_history", "versions",
    }  # fmt: skip
