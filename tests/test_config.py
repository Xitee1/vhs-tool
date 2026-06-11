import pytest

from vhs_tool.common import ToolError
from vhs_tool.config import Config, load_config


def test_defaults_without_file():
    cfg = load_config(None)
    assert cfg == Config()
    assert cfg.paths.decoded == "./decoded"
    assert cfg.defaults.lang == "de"
    assert cfg.hardware.vcr == "Panasonic NV-VP30"


def test_partial_file_overrides_only_given_keys(tmp_path):
    file = tmp_path / "vhs-tool.toml"
    file.write_text(
        """
        [paths]
        decoded = "/mnt/tapes/decoded"
        captures = ["/mnt/tapes/rf"]

        [defaults]
        lang = "en"

        [hardware]
        vcr = "JVC HR-S7500"
        """,
        encoding="utf-8",
    )
    cfg = load_config(file)
    assert cfg.paths.decoded == "/mnt/tapes/decoded"
    assert cfg.paths.captures == ("/mnt/tapes/rf",)  # list → tuple
    assert cfg.paths.upload == "./upload"  # untouched default
    assert cfg.defaults.lang == "en"
    assert cfg.defaults.tv_system == "pal"
    assert cfg.hardware.vcr == "JVC HR-S7500"
    assert cfg.links == Config().links


def test_unknown_section_rejected(tmp_path):
    file = tmp_path / "vhs-tool.toml"
    file.write_text("[pathz]\ndecoded = './x'\n", encoding="utf-8")
    with pytest.raises(ToolError, match=r"Unknown section \[pathz\]"):
        load_config(file)


def test_unknown_key_rejected(tmp_path):
    file = tmp_path / "vhs-tool.toml"
    file.write_text("[paths]\ndecode_dir = './x'\n", encoding="utf-8")
    with pytest.raises(ToolError, match=r"Unknown key 'decode_dir' in \[paths\]"):
        load_config(file)


def test_invalid_toml_rejected(tmp_path):
    file = tmp_path / "vhs-tool.toml"
    file.write_text("[paths\n", encoding="utf-8")
    with pytest.raises(ToolError, match="Invalid TOML"):
        load_config(file)
