import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

import vhs_tool.commands.rf_compress as rf_compress
from vhs_tool.commands.rf_compress import (
    AnaFrame,
    FlacInfo,
    FlacVersion,
    ProbeResult,
    compress_file,
    detect_format,
    find_flac_files,
    find_raw_files,
    flac_encode_cmd,
    flac_metadata_length,
    human_bytes,
    parse_ana_frame,
    parse_ana_order,
    parse_flac_info,
    parse_flac_version,
    parse_level_tag,
    recompress_file,
    resolve_threads,
)
from vhs_tool.common import ToolError


@pytest.mark.parametrize(
    ("ext", "expected"),
    [
        ("u8", (8, "unsigned")),
        (".u8", (8, "unsigned")),
        ("r8", (8, "unsigned")),
        ("u16", (16, "unsigned")),
        ("s16", (16, "signed")),
        ("r16", (16, "signed")),
        (".S16", (16, "signed")),
        ("flac", None),
        ("", None),
    ],
)
def test_detect_format(ext, expected):
    assert detect_format(ext) == expected


def test_human_bytes():
    assert human_bytes(1024) == "1 KiB"
    assert human_bytes(5 * 1048576) == "5.0 MiB"
    assert human_bytes(3 * 1073741824) == "3.00 GiB"


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("flac 1.5.0\nCopyright ...", FlacVersion("1.5.0", 1, 5)),
        ("flac 1.4.3", FlacVersion("1.4.3", 1, 4)),
        ("flac 2.0", FlacVersion("2.0", 2, 0)),
        ("", FlacVersion("unknown", 0, 0)),
        ("command not found", FlacVersion("unknown", 0, 0)),
    ],
)
def test_parse_flac_version(output, expected):
    assert parse_flac_version(output) == expected


def test_supports_threads():
    # --threads only exists from flac 1.5.0 on
    assert FlacVersion("1.5.0", 1, 5).supports_threads
    assert FlacVersion("2.0.0", 2, 0).supports_threads
    assert not FlacVersion("1.4.3", 1, 4).supports_threads
    assert not FlacVersion("unknown", 0, 0).supports_threads


def test_resolve_threads(monkeypatch):
    monkeypatch.setattr(rf_compress.os, "cpu_count", lambda: 16)
    modern, old = FlacVersion("1.5.0", 1, 5), FlacVersion("1.4.3", 1, 4)
    assert resolve_threads(None, modern) == 16  # auto → all cores
    assert resolve_threads(4, modern) == 4
    assert resolve_threads(0, modern) == 0
    assert resolve_threads(None, old) == 0  # flag does not exist yet
    assert resolve_threads(8, old) == 0  # requested → dropped (with a warning)


def test_resolve_threads_caps_at_flac_limit(monkeypatch):
    monkeypatch.setattr(rf_compress.os, "cpu_count", lambda: 512)
    assert resolve_threads(None, FlacVersion("1.5.0", 1, 5)) == rf_compress.MAX_THREADS


def test_flac_encode_cmd():
    cmd = flac_encode_cmd(
        Path("/c/tape-video.u8"),
        Path("/c/tape-video.flac.part"),
        bps=8, sign="unsigned", rate=40000, channels=1,
        blocksize=65535, level=8, threads=16,
    )  # fmt: skip
    assert cmd == [
        "flac", "--silent", "-8", "--threads=16",
        "--blocksize=65535", "--lax",
        "--sample-rate=40000", "--channels=1", "--bps=8",
        "--sign=unsigned", "--endian=little",
        "-f", "/c/tape-video.u8", "-o", "/c/tape-video.flac.part",
    ]  # fmt: skip


def test_flac_encode_cmd_without_threads():
    cmd = flac_encode_cmd(
        Path("in.s16"), Path("out.flac"),
        bps=16, sign="signed", rate=40000, channels=1,
        blocksize=65535, level=8, threads=0,
    )  # fmt: skip
    assert not any(c.startswith("--threads") for c in cmd)
    assert "--bps=16" in cmd and "--sign=signed" in cmd


def test_find_raw_files_is_not_recursive(tmp_path):
    for name in ("b-video.u8", "a-video.u8", "c-video.s16", "d.flac", "e.tbc"):
        (tmp_path / name).write_bytes(b"x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep-video.u8").write_bytes(b"x")

    found = [p.name for p in find_raw_files(tmp_path)]
    # grouped by extension in RAW_FORMATS order, sorted within a group
    assert found == ["a-video.u8", "b-video.u8", "c-video.s16"]


def test_find_flac_files_skips_partials_and_subdirs(tmp_path):
    for name in ("b-video.flac", "a-hifi.flac", "c-video.flac.part", "d-video.u8"):
        (tmp_path / name).write_bytes(b"x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep.flac").write_bytes(b"x")

    assert [p.name for p in find_flac_files(tmp_path)] == ["a-hifi.flac", "b-video.flac"]


def test_flac_encode_cmd_stdout_variant():
    cmd = flac_encode_cmd(
        "-", None,
        bps=8, sign="signed", rate=40000, channels=1,
        blocksize=65535, level=8, threads=0,
    )  # fmt: skip
    assert cmd[-2:] == ["--stdout", "-"]
    assert "-o" not in cmd


# =============================================================================
# Recompression helpers
# =============================================================================


def test_parse_flac_info():
    info = parse_flac_info("4726149E88DEE89AA27B1424E993867C\n65535\n65535\n40000\n1\n8\n")
    assert info == FlacInfo("4726149e88dee89aa27b1424e993867c", 65535, 65535, 40000, 1, 8)
    assert info.has_md5


def test_parse_flac_info_zero_md5_means_absent():
    assert not parse_flac_info("0" * 32 + "\n4096\n4096\n40000\n1\n16\n").has_md5


@pytest.mark.parametrize("output", ["", "garbage", "abc\n1\n2\n3\n4\nnope\n"])
def test_parse_flac_info_rejects_unexpected_output(output):
    with pytest.raises(ToolError):
        parse_flac_info(output)


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("VHS_TOOL_FLAC_LEVEL=8\n", 8),
        ("vhs_tool_flac_level=5\n", 5),  # Vorbis comment names are case-insensitive
        ("VHS_TOOL_FLAC_LEVEL=abc\n", None),
        ("OTHER_TAG=8\n", None),
        ("", None),
    ],
)
def test_parse_level_tag(output, expected):
    assert parse_level_tag(output) == expected


def test_parse_ana_frame():
    line = (
        "frame=0\toffset=8592\tbits=217256\tblocksize=65535\tsample_rate=40000"
        "\tchannels=1\tchannel_assignment=INDEPENDENT\n"
    )
    assert parse_ana_frame(line) == AnaFrame(8592, 217256, 65535)
    assert parse_ana_frame("\tsubframe=0\twasted_bits=0\ttype=LPC\torder=12\n") is None


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("\tsubframe=0\twasted_bits=0\ttype=LPC\torder=12\tqlp_coeff_precision=6\n", 12),
        ("\tsubframe=0\twasted_bits=0\ttype=FIXED\torder=3\n", 3),
        ("\tsubframe=0\twasted_bits=0\ttype=CONSTANT\tvalue=0\n", None),
        ("frame=0\toffset=8592\tbits=217256\tblocksize=65535\t\n", None),
    ],
)
def test_parse_ana_order(line, expected):
    assert parse_ana_order(line) == expected


def _meta_block(block_type: int, length: int, *, last: bool) -> bytes:
    header = bytes([block_type | (0x80 if last else 0)]) + length.to_bytes(3, "big")
    return header + b"\x00" * length


def test_flac_metadata_length():
    head = b"fLaC" + _meta_block(0, 34, last=False) + _meta_block(1, 10, last=True) + b"frames..."
    assert flac_metadata_length(head) == 4 + (4 + 34) + (4 + 10)


def test_flac_metadata_length_rejects_non_flac():
    with pytest.raises(ToolError):
        flac_metadata_length(b"RIFF....")


def test_flac_metadata_length_rejects_truncated_head():
    head = b"fLaC" + _meta_block(0, 34, last=False)  # no terminating last-block
    with pytest.raises(ToolError):
        flac_metadata_length(head)


def test_probe_gain_pct():
    assert ProbeResult(1000, 900, 65535, 1, 12).gain_pct == pytest.approx(10.0)
    assert ProbeResult(0, 0, 0, 0, 0).gain_pct == 0.0


def test_build_plan_partitions_by_tag():
    raw = [Path("new.u8")]
    a, b, c = Path("a.flac"), Path("b.flac"), Path("c.flac")
    tags = {a: 8, b: 5, c: None}

    plan = rf_compress.build_plan(raw, [a, b, c], tags, level=8, force=False)

    assert plan.verified == [a]
    assert plan.raw_todo == raw
    assert plan.flac_todo == [(b, 5), (c, None)]


def test_build_plan_force_keeps_everything_on_the_worklist():
    a, b = Path("a.flac"), Path("b.flac")

    plan = rf_compress.build_plan([], [a, b], {a: 8, b: None}, level=8, force=True)

    assert plan.verified == []
    assert plan.flac_todo == [(a, 8), (b, None)]


# =============================================================================
# compress_file
# =============================================================================


class _FakeFlac:
    """Stand-in for the two flac invocations compress_file makes.

    ``run`` writes the encoded output; ``Popen`` replays the raw bytes the
    encoder was given (or ``corrupt`` instead, to simulate a bad round-trip).
    """

    def __init__(self, *, encode_fails: bool = False, corrupt: bytes | None = None,
                 decode_rc: int = 0):  # fmt: skip
        self.encode_fails = encode_fails
        self.corrupt = corrupt
        self.decode_rc = decode_rc
        self.payload = b""
        self.commands: list[list[str]] = []

    def run(self, cmd, **kwargs):
        cmd = [str(c) for c in cmd]
        self.commands.append(cmd)
        if cmd[0] == "metaflac":  # set_level_tag: padding query, then tag write
            out = "  type: 1 (PADDING)\n" if "--list" in cmd else ""
            return SimpleNamespace(stdout=out, returncode=0)
        if self.encode_fails:
            raise ToolError("flac exited with code 1")
        self.payload = Path(cmd[cmd.index("-f") + 1]).read_bytes()
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"FLAC" + self.payload)
        return None

    def popen(self, cmd, stdout=None):
        outer = self

        class FakeProc:
            def __init__(self):
                self.stdout = _FakeStdout(outer.corrupt if outer.corrupt is not None
                                          else outer.payload)  # fmt: skip

            def wait(self):
                return outer.decode_rc

            def kill(self):
                pass

        return FakeProc()


class _FakeStdout:
    def __init__(self, data: bytes):
        self._data = data

    def read(self, size):
        chunk, self._data = self._data[:size], self._data[size:]
        return chunk

    def close(self):
        pass


@pytest.fixture
def fake_flac(monkeypatch):
    def install(**kwargs):
        fake = _FakeFlac(**kwargs)
        monkeypatch.setattr(rf_compress, "run", fake.run)
        monkeypatch.setattr(rf_compress.subprocess, "Popen", fake.popen)
        return fake

    return install


def _raw(tmp_path: Path, name: str = "tape-video.u8", data: bytes = b"raw rf samples") -> Path:
    file = tmp_path / name
    file.write_bytes(data)
    return file


def test_compress_file_verifies_and_removes_the_raw(tmp_path, fake_flac):
    fake = fake_flac()
    raw = _raw(tmp_path)
    out = tmp_path / "tape-video.flac"

    result = compress_file(raw, threads=4)

    assert result.status == "compressed"
    assert result.raw_size == len(b"raw rf samples")
    assert out.read_bytes() == b"FLACraw rf samples"
    assert not raw.exists()  # removed only after the MD5 matched
    assert not out.with_name(out.name + ".part").exists()
    # format auto-detected from the .u8 extension
    assert "--bps=8" in fake.commands[0] and "--sign=unsigned" in fake.commands[0]


def test_compress_file_keep_raw(tmp_path, fake_flac):
    fake_flac()
    raw = _raw(tmp_path)
    assert compress_file(raw, keep_raw=True).status == "compressed"
    assert raw.exists()
    assert (tmp_path / "tape-video.flac").exists()


def test_compress_file_md5_mismatch_keeps_raw_and_drops_the_flac(tmp_path, fake_flac):
    fake_flac(corrupt=b"different bytes")
    raw = _raw(tmp_path)

    result = compress_file(raw)

    assert result.status == "error"
    assert raw.read_bytes() == b"raw rf samples"
    # nothing lands on the final name — the next run must not skip this file
    assert not (tmp_path / "tape-video.flac").exists()
    assert not (tmp_path / "tape-video.flac.part").exists()


def test_compress_file_encode_failure_leaves_everything_intact(tmp_path, fake_flac):
    fake_flac(encode_fails=True)
    raw = _raw(tmp_path)

    assert compress_file(raw).status == "error"
    assert raw.exists()
    assert not (tmp_path / "tape-video.flac").exists()
    assert not (tmp_path / "tape-video.flac.part").exists()


def test_compress_file_decode_failure_is_an_error(tmp_path, fake_flac):
    fake_flac(decode_rc=1)
    raw = _raw(tmp_path)

    assert compress_file(raw).status == "error"
    assert raw.exists()
    assert not (tmp_path / "tape-video.flac").exists()


def test_compress_file_clears_a_stale_partial(tmp_path, fake_flac):
    fake_flac()
    raw = _raw(tmp_path)
    (tmp_path / "tape-video.flac.part").write_bytes(b"stale partial")

    assert compress_file(raw).status == "compressed"
    assert (tmp_path / "tape-video.flac").read_bytes() == b"FLACraw rf samples"


def test_compress_file_skips_when_the_flac_already_exists(tmp_path, fake_flac):
    fake = fake_flac()
    raw = _raw(tmp_path)
    (tmp_path / "tape-video.flac").write_bytes(b"earlier run")

    assert compress_file(raw).status == "skipped"
    assert raw.exists()
    assert fake.commands == []  # flac was never invoked


def test_compress_file_skips_an_undetectable_extension(tmp_path, fake_flac):
    fake = fake_flac()
    raw = _raw(tmp_path, "tape-video.bin")

    assert compress_file(raw).status == "skipped"
    assert fake.commands == []


def test_compress_file_explicit_format_overrides(tmp_path, fake_flac):
    fake = fake_flac()
    raw = _raw(tmp_path, "tape-video.bin")

    assert compress_file(raw, bps=16, sign="signed", rate=28636).status == "compressed"
    assert "--bps=16" in fake.commands[0]
    assert "--sign=signed" in fake.commands[0]
    assert "--sample-rate=28636" in fake.commands[0]


def test_compress_file_sign_override_alone(tmp_path, fake_flac):
    """--sign overrides the extension while bps stays auto-detected (and vice versa)."""
    fake = fake_flac()
    assert compress_file(_raw(tmp_path, "tape-video.u16"), sign="signed").status == "compressed"
    assert "--bps=16" in fake.commands[0]
    assert "--sign=signed" in fake.commands[0]


def test_compress_file_dry_run_touches_nothing(tmp_path, fake_flac):
    fake = fake_flac()
    raw = _raw(tmp_path)

    assert compress_file(raw, dry_run=True).status == "compressed"
    assert raw.exists()
    assert not (tmp_path / "tape-video.flac").exists()
    assert fake.commands == []


def test_md5_helpers_agree_on_identical_data(tmp_path, fake_flac):
    """The verification compares the raw file against the decoded FLAC bytes."""
    fake_flac()
    data = b"\x00\x01\x02" * 1000
    raw = _raw(tmp_path, "tape-video.u8", data)
    assert compress_file(raw, keep_raw=True).status == "compressed"
    assert rf_compress.md5_file(raw) == hashlib.md5(data).hexdigest()


def test_compress_file_tags_the_new_flac(tmp_path, fake_flac):
    fake = fake_flac()
    compress_file(_raw(tmp_path))
    tag_writes = [c for c in fake.commands if c[0] == "metaflac" and "--list" not in c]
    assert tag_writes and f"--set-tag={rf_compress.LEVEL_TAG}=8" in tag_writes[0]
    # tagged while still a .part file, before the atomic rename
    assert tag_writes[0][-1].endswith(".flac.part")


# =============================================================================
# recompress_file
# =============================================================================

ORIG_DATA = b"original flac bytes, reasonably long"


class _FakeRecompress:
    """Stubs the flac/metaflac machinery that recompress_file drives."""

    def __init__(
        self, monkeypatch, *, tag=None, gain=5.0, new_data=b"tiny", new_md5="a" * 32,
        source_md5="a" * 32, transcode_fails=False, test_fails=False,
    ):  # fmt: skip
        self.tagged: list[tuple[str, int]] = []
        self.probed = 0
        self.transcoded = 0
        self.tested = 0
        info = FlacInfo(source_md5, 65535, 65535, 40000, 1, 8)
        new_info = FlacInfo(new_md5, 65535, 65535, 40000, 1, 8)

        def read_flac_info(file):
            return new_info if file.name.endswith(".part") else info

        def probe_flac(file, **kwargs):
            self.probed += 1
            return ProbeResult(10000, round(10000 * (1 - gain / 100)), 65535, 1, 12)

        def transcode(file, part, **kwargs):
            if transcode_fails:
                raise ToolError("flac transcode failed (decode rc=1, encode rc=0)")
            self.transcoded += 1
            part.write_bytes(new_data)

        def set_level_tag(file, level):
            self.tagged.append((file.name, level))
            return True

        def fake_run(cmd, **kwargs):
            assert "-t" in [str(c) for c in cmd]
            self.tested += 1
            if test_fails:
                raise ToolError("flac exited with code 1")
            return None

        monkeypatch.setattr(rf_compress, "read_flac_info", read_flac_info)
        monkeypatch.setattr(rf_compress, "read_level_tag", lambda f: tag)
        monkeypatch.setattr(rf_compress, "probe_flac", probe_flac)
        monkeypatch.setattr(rf_compress, "transcode", transcode)
        monkeypatch.setattr(rf_compress, "set_level_tag", set_level_tag)
        monkeypatch.setattr(rf_compress, "run", fake_run)
        monkeypatch.setattr(rf_compress, "md5_flac_raw", lambda f, sign: "d" * 32)


@pytest.fixture
def fake_recompress(monkeypatch):
    def install(**kwargs):
        return _FakeRecompress(monkeypatch, **kwargs)

    return install


def _flac(tmp_path: Path, name: str = "tape-video.flac", data: bytes = ORIG_DATA) -> Path:
    file = tmp_path / name
    file.write_bytes(data)
    return file


def test_recompress_skips_a_file_tagged_at_the_target_level(tmp_path, fake_recompress):
    fake = fake_recompress(tag=8)
    file = _flac(tmp_path)

    assert recompress_file(file).status == "skipped"
    assert fake.probed == 0 and fake.transcoded == 0
    assert file.read_bytes() == ORIG_DATA


def test_recompress_probe_below_threshold_tags_and_keeps(tmp_path, fake_recompress):
    fake = fake_recompress(gain=0.2)
    file = _flac(tmp_path)

    assert recompress_file(file, min_gain_pct=0.5).status == "skipped"
    assert fake.probed == 1 and fake.transcoded == 0
    assert fake.tagged == [("tape-video.flac", 8)]
    assert file.read_bytes() == ORIG_DATA


def test_recompress_full_run_replaces_and_tags(tmp_path, fake_recompress):
    fake = fake_recompress(gain=5.0, new_data=b"tiny")
    file = _flac(tmp_path)

    result = recompress_file(file)

    assert result == ("compressed", len(ORIG_DATA), len(b"tiny"))
    assert file.read_bytes() == b"tiny"
    assert not file.with_name(file.name + ".part").exists()
    assert fake.tested == 1
    # tagged while still a .part file, before the atomic rename
    assert fake.tagged == [("tape-video.flac.part", 8)]


def test_recompress_md5_mismatch_keeps_the_original(tmp_path, fake_recompress):
    fake_recompress(new_md5="b" * 32)
    file = _flac(tmp_path)

    assert recompress_file(file).status == "error"
    assert file.read_bytes() == ORIG_DATA
    assert not file.with_name(file.name + ".part").exists()


def test_recompress_flac_t_failure_keeps_the_original(tmp_path, fake_recompress):
    fake_recompress(test_fails=True)
    file = _flac(tmp_path)

    assert recompress_file(file).status == "error"
    assert file.read_bytes() == ORIG_DATA
    assert not file.with_name(file.name + ".part").exists()


def test_recompress_transcode_failure_keeps_the_original(tmp_path, fake_recompress):
    fake_recompress(transcode_fails=True)
    file = _flac(tmp_path)

    assert recompress_file(file).status == "error"
    assert file.read_bytes() == ORIG_DATA
    assert not file.with_name(file.name + ".part").exists()


def test_recompress_without_improvement_tags_the_original(tmp_path, fake_recompress):
    fake = fake_recompress(new_data=b"x" * (len(ORIG_DATA) + 10))
    file = _flac(tmp_path)

    assert recompress_file(file).status == "skipped"
    assert file.read_bytes() == ORIG_DATA
    assert not file.with_name(file.name + ".part").exists()
    assert fake.tagged == [("tape-video.flac", 8)]


def test_recompress_force_skips_tag_check_and_probe(tmp_path, fake_recompress):
    fake = fake_recompress(tag=8)
    file = _flac(tmp_path)

    assert recompress_file(file, force=True).status == "compressed"
    assert fake.probed == 0 and fake.transcoded == 1


def test_recompress_dry_run_touches_nothing(tmp_path, fake_recompress):
    fake = fake_recompress(gain=5.0)
    file = _flac(tmp_path)

    assert recompress_file(file, dry_run=True).status == "compressed"
    assert fake.probed == 1 and fake.transcoded == 0
    assert fake.tagged == []
    assert file.read_bytes() == ORIG_DATA


def test_recompress_dry_run_below_threshold_does_not_tag(tmp_path, fake_recompress):
    fake = fake_recompress(gain=0.1)
    file = _flac(tmp_path)

    assert recompress_file(file, dry_run=True).status == "skipped"
    assert fake.tagged == []


def test_recompress_source_without_streaminfo_md5_compares_decodes(tmp_path, fake_recompress):
    fake = fake_recompress(source_md5="0" * 32)
    file = _flac(tmp_path)

    # header MD5 unusable → falls back to comparing both decoded streams
    assert recompress_file(file).status == "compressed"
    assert fake.tested == 1
