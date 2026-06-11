# vhs-tool

Unified CLI for the VHS decode pipeline. Replaces the numbered shell scripts
(`tools/*.sh`) step by step with a single Python tool.

Currently ported:

| Command           | Replaces      | Purpose                                              |
| ----------------- | ------------- | ---------------------------------------------------- |
| `vhs-tool export` | `6_export.sh` | TBC + aligned FLAC → lossless FFV1 + Opus audio      |
| `vhs-tool encode` | `7_encode.sh` | FFV1 + Opus → [VapourSynth] → x265 → final MKV       |

## Requirements

- Python ≥ 3.11 (no runtime Python dependencies)
- External tools, depending on the command:
  - `export`: ffmpeg, ffprobe, tbc-video-export AppImage, tbc-tools AppImage
  - `encode`: ffmpeg, ffprobe, mkvmerge, x265, optionally vspipe/vspreview
    (VapourSynth) and mediainfo

## Install

```bash
# As a tool (recommended) — editable, so code changes apply immediately
uv tool install --editable ./tools/vhs-tool

# …or run it ad-hoc without installing
uv run --project ./tools/vhs-tool vhs-tool --help
```

> The default paths for the tbc-* AppImages and output directories
> (`./export`, `./final`) are relative — run `vhs-tool` from the
> repository root, like the shell scripts.

## Usage

```bash
vhs-tool --help
vhs-tool export --help
vhs-tool encode --help
```

### Export (step 1)

```bash
vhs-tool export ./decoded/VHS_PAL_Tape__Name-2026-05-03_18_14_58_02_00
# → export/<name>.{ffv1.mkv,linear.opus,hifi.opus}

# Only re-export specific parts:
vhs-tool export --only video ./decoded/<name>
```

### Encode (step 2)

```bash
# Interactive VapourSynth tuning first:
vhs-tool encode --vpy ./tools/vapoursynth_vhs.vpy --vspreview ./export/<name>

# Test encode a 30s segment:
vhs-tool encode -p anime --vpy ./tools/vapoursynth_vhs.vpy \
    --test 00:05:00 00:00:30 ./export/<name>

# Full encode with metadata, chapters and cuts:
vhs-tool encode -p anime --vpy ./tools/vapoursynth_vhs.vpy \
    --title "Tape Title" --source "Panasonic NV-VP30" \
    --chapter "00:00:00.000 Intro" --chapter "00:12:34.000 Part 2" \
    --cut-begin 00:42:00 --cut-end 00:45:30 \
    ./export/<name>
```

## Development

```bash
cd tools/vhs-tool
uv sync          # create venv with dev dependencies
uv run pytest    # run tests
uv run ruff check src tests
uv run ruff format src tests
```
