# vhs-tool — Agent Notes

Unified Python CLI for a VHS RF-capture decode pipeline. It incrementally
replaces the numbered bash scripts in the parent repo (`../*.sh`); each
subcommand is a faithful port of one script.

## Architecture

```
src/vhs_tool/
    cli.py          top-level argparse parser, subcommand dispatch, error handling
    common.py       shared helpers: run(), check_deps(), ffprobe wrappers, timestamps
    commands/       one module per subcommand, each exposing add_parser(subparsers)
        export.py   port of ../6_export.sh — TBC + FLAC → FFV1 + Opus
        encode.py   port of ../7_encode.sh — FFV1 + Opus → [VapourSynth] → x265 → MKV
tests/              pytest; covers pure logic (timestamps, cut segments)
```

Adding a command: create `commands/<name>.py` with `add_parser(subparsers)`
setting `func=cmd_<name>`, register it in `cli.py`. Raise `ToolError` for user
errors — `cli.main()` prints it as `Error: ...` and exits 1.

## Conventions

- **Stdlib only at runtime** (argparse + subprocess). Do not add runtime
  dependencies; the tool orchestrates external CLIs (ffmpeg, x265, mkvmerge,
  tbc-video-export, vspipe).
- **Port fidelity first**: when porting a bash script, preserve defaults,
  output filenames, console output, and exit behavior. The bash original
  stays in `../` until parity is verified on a real tape.
- Preserve the VapourSynth env contract used by `vapoursynth_vhs.vpy`:
  `VHS_INPUT`, `VHS_DEINTERLACE`, `ENCODE_PROFILE`, `VHS_KEEP_SEGMENTS`.
- mkvmerge exit code 1 means warnings, not failure — use `mkvmerge_tolerant()`.
- Default paths are relative (`./tools/...`, `./export`, `./final`): the tool
  is run from the parent repo root, not from this directory.

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
