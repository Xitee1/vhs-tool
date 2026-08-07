# vhs-tool — Agent Notes

Unified Python CLI for a VHS RF-capture decode pipeline. Each subcommand is
one stage of the pipeline, from decoding an RF capture through to preparing
an upload.

## Architecture

```
src/vhs_tool/
    cli.py          top-level argparse parser, subcommand dispatch, error handling
                    (command modules are imported inside build_parser() so config
                    errors hit main()'s ToolError handler)
    common.py       shared helpers: run(), check_deps(), ffprobe wrappers, timestamps, prompts
    config.py       optional vhs-tool.toml at the pipeline root ($VHS_TOOL_CONFIG overrides
                    the location); frozen dataclasses with built-in defaults for paths,
                    binaries, defaults (lang/tv_system/...), hardware, links, upload
    encoding.py     single home for all encode settings: X265_PROFILES (publish encodes),
                    FFMPEG_PROFILES + ffmpeg_encode() (YouTube upscale, archive.org preview)
    metadata.py     MKV metadata shared by encode + set-props: add_metadata_args()
                    (title/source/publisher/date/comment/lang flags), TAG_FIELDS
                    (arg→Matroska tag), build_global_tags_xml()/parse_global_tags()
    templates/      Jinja2 templates (notes.txt.j2, youtube_description.txt.j2) + render();
                    files in the user dir ([paths] templates, default ./templates) override
                    the packaged ones by name
    commands/       one module per subcommand, each exposing add_parser(subparsers)
        decode.py   video RF FLAC → TBC + JSON (vhs-decode wrapper)
        audio.py    HiFi/Linear RF → decoded + aligned FLAC
        export.py   TBC + FLAC → FFV1 + Opus
        encode.py   FFV1 + Opus → [VapourSynth] → x265 → MKV
        process_teletext.py  TBC VBI lines → t42 packet stream + HTML
                             (vhs-teletext wrapper; output feeds the IA item's
                             teletext/ folder)
        upload.py   final MKV → IA/YouTube upload folder (interactive)
        rf_compress.py  raw RF captures → FLAC (MD5-verified, deletes the raw)
        rf_resample.py  downsample RF captures, used by upload
        set_props.py    patch metadata on an existing MKV in place (mkvpropedit; PATCH)
        trim.py         trim noise from the end of synced RF captures
tests/              pytest; covers pure logic (timestamps, cut segments, upload text files,
                    config loading, encoding profiles/commands)
```

Adding a command: create `commands/<name>.py` with `add_parser(subparsers)`
setting `func=cmd_<name>`, register it in `cli.py`. Raise `ToolError` for user
errors — `cli.main()` prints it as `Error: ...` and exits 1.

## Conventions

- **Minimal runtime dependencies**: jinja2 (template rendering) and questionary
  (interactive prompts — line editing + select menus, via prompt_toolkit);
  everything else is stdlib (argparse + subprocess +
  tomllib). The tool orchestrates external CLIs (ffmpeg, x265, mkvmerge,
  tbc-video-export, vspipe). Don't add dependencies without a clear win.
- **Compatibility with the established pipeline**: existing captures and
  downstream steps rely on the current defaults, output filenames, console
  output, and exit behavior — don't change them without an explicit reason.
- **No new hardcoded setup values**: user-specific values (paths, binaries,
  hardware descriptions, static text) belong in `config.py` dataclasses
  (overridable via `vhs-tool.toml`); encode settings belong in `encoding.py`;
  generated-document layout belongs in `templates/*.j2`.
- Command modules read the config at import time into module constants /
  argparse defaults (`_CFG = get_config()`), so `--help` shows effective
  values. Precedence: CLI flags > env vars > config file > built-in defaults.
- Preserve the VapourSynth env contract used by `vapoursynth_vhs.vpy`:
  `VHS_INPUT`, `VHS_DEINTERLACE`, `ENCODE_PROFILE`, `VHS_KEEP_SEGMENTS`.
- mkvmerge exit code 1 means warnings, not failure — use `mkvmerge_tolerant()`.
- `decode` and `audio` honor env overrides (`OUT_DIR`, `VHS_DECODE_BIN`,
  `AAA_BIN`, `HIFI_DECODE_BIN`, ...) — they are read at module import as the
  argparse defaults, falling back to the config.
- Default paths are relative (`./tools/...`, `./export`, `./final`): the tool
  is run from the parent repo root, not from this directory. The Jinja
  templates in `templates/` must keep rendering byte-identical output for the
  existing tests in `tests/test_upload.py`.

## Commands

```bash
uv sync                          # venv with dev deps
uv run pytest -q                 # tests
uv run ruff check src tests     # lint
uv run ruff format src tests    # format
uv run vhs-tool --help           # smoke test
```

Run all of these before committing. ruff is configured in pyproject.toml
(line length 100, rules E/W/F/I/B/UP/C4/SIM). CI
(`.github/workflows/ci.yml`) runs the same checks on Python 3.11–3.13 for
every push and PR.

## Versioning

The version is derived from git tags via hatch-vcs — there is no version
string in the source. To release: `git tag vX.Y.Z && git push --tags`.
Untagged commits get a `.devN+g<hash>` suffix automatically. After tagging,
`uv sync --reinstall-package vhs-tool` refreshes the installed metadata.

## Repo

- GitHub: `git@github.com:Xitee1/vhs-tool.git` (use SSH).
- The parent directory (`/home/mato/vhs-decode`) is NOT a git repository;
  only this subfolder is.
