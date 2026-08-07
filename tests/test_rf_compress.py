import hashlib
from pathlib import Path

import pytest

import vhs_tool.commands.rf_compress as rf_compress
from vhs_tool.commands.rf_compress import (
    FlacVersion,
    compress_file,
    detect_format,
    find_raw_files,
    flac_encode_cmd,
    human_bytes,
    parse_flac_version,
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
