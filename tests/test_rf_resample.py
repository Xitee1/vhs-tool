from pathlib import Path

import pytest

import vhs_tool.commands.rf_resample as rf_resample
from vhs_tool.commands.rf_resample import PRESETS, _pipeline, default_suffix, derive_base
from vhs_tool.common import ToolError


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("VHS_PAL_Tape_010", "VHS_PAL_Tape_010"),
        ("VHS_PAL_Tape_010-video.flac", "VHS_PAL_Tape_010"),
        ("VHS_PAL_Tape_010-hifi.flac", "VHS_PAL_Tape_010"),
        ("VHS_PAL_Tape_010-linear.flac", "VHS_PAL_Tape_010"),
        ("VHS_PAL_Tape_010-headswitch.u8", "VHS_PAL_Tape_010"),
        ("VHS_PAL_Tape_010-video", "VHS_PAL_Tape_010"),
        ("VHS_PAL_Tape_010.lds", "VHS_PAL_Tape_010"),
        ("VHS_PAL_Tape_010-video.r16", "VHS_PAL_Tape_010"),
    ],
)
def test_derive_base(name, expected):
    assert derive_base(name) == expected


def test_default_suffix():
    # FLAC-scale: 20000 Hz represents 20 MSPS → '.8bit.20msps' (matches the
    # naming that `vhs-tool upload` and the wiki convention expect)
    assert default_suffix(8, 20000) == ".8bit.20msps"
    assert default_suffix(8, 16000) == ".8bit.16msps"
    assert default_suffix(16, 24000) == ".16bit.24msps"


def test_presets():
    assert PRESETS["pal"] == (20000, "0-9600")
    assert PRESETS["pal-min"] == (18000, "0-8670")
    assert PRESETS["ntsc"] == (16000, "0-7650")
    assert PRESETS["svhs"] == (24000, "0-9400")


class _FakePipe:
    def close(self):
        pass


def _fake_popen(encode_rc: int):
    """Stand-in for subprocess.Popen: the flac-encode stage writes its -o target."""

    class FakeProc:
        def __init__(self, cmd, stdin=None, stdout=None):
            cmd = [str(c) for c in cmd]
            self.stdout = _FakePipe()
            self._rc = 0
            if "-o" in cmd:  # the flac encode stage
                Path(cmd[cmd.index("-o") + 1]).write_bytes(b"flac data")
                self._rc = encode_rc

        def wait(self):
            return self._rc

    return FakeProc


def _run_pipeline(src: Path, out: Path) -> None:
    _pipeline(src, out, 40000, 8, 20000, 2500, "0-9600", 8)


def test_pipeline_renames_onto_final_name_on_success(tmp_path, monkeypatch):
    monkeypatch.setattr(rf_resample.subprocess, "Popen", _fake_popen(encode_rc=0))
    src = tmp_path / "tape-video.flac"
    src.write_bytes(b"src")
    out = tmp_path / "tape-video.8bit.20msps.flac"
    stale = tmp_path / "tape-video.8bit.20msps.flac.part"
    stale.write_bytes(b"stale partial")  # from a previous crash — must not survive
    _run_pipeline(src, out)
    assert out.read_bytes() == b"flac data"
    assert not stale.exists()


def test_pipeline_failure_leaves_no_output(tmp_path, monkeypatch):
    monkeypatch.setattr(rf_resample.subprocess, "Popen", _fake_popen(encode_rc=1))
    src = tmp_path / "tape-video.flac"
    src.write_bytes(b"src")
    out = tmp_path / "tape-video.8bit.20msps.flac"
    with pytest.raises(ToolError, match=r"flac \(encode\) exited with code 1"):
        _run_pipeline(src, out)
    # Nothing lands on the final name (which resample_file would skip next run),
    # and the partial is cleaned up.
    assert not out.exists()
    assert not out.with_name(out.name + ".part").exists()
