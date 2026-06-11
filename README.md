# vhs-tool

Unified CLI for the VHS decode pipeline. Replaces the numbered shell scripts
(`tools/*.sh`) step by step with a single Python tool.

Currently ported:

| Command           | Replaces      | Purpose                                              |
| ----------------- | ------------- | ---------------------------------------------------- |
| `vhs-tool export` | `6_export.sh` | TBC + aligned FLAC → lossless FFV1 + Opus audio      |
| `vhs-tool encode` | `7_encode.sh` | FFV1 + Opus → [VapourSynth] → x265 → final MKV       |
| `vhs-tool upload` | `8_upload.sh` | Final MKV → Internet Archive / YouTube upload folder |
| `vhs-tool rf-resample` | `rf-resample.sh` | Downsample RF captures for archival (40 → 20 MSPS) |

## Requirements

- Python ≥ 3.11 (no runtime Python dependencies)
- External tools, depending on the command:
  - `export`: ffmpeg, ffprobe, tbc-video-export AppImage, tbc-tools AppImage
  - `encode`: ffmpeg, ffprobe, mkvmerge, x265, optionally vspipe/vspreview
    (VapourSynth) and mediainfo
  - `upload`: ffmpeg, ffprobe, mkvmerge, mkvextract, optionally rsync
    (copy progress); sox + flac when the video RF still needs resampling
  - `rf-resample`: sox (with FLAC support), flac (≥1.4)

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
vhs-tool upload --help
vhs-tool rf-resample --help
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

### Upload (step 3)

```bash
# Internet Archive folder (RF capture data, video + preview, notes, checksums):
vhs-tool upload ./final/<name>_anime.mkv ia

# YouTube folder (2880x2160 upscale encode + description.txt):
vhs-tool upload ./final/<name>_anime.mkv youtube
```

Interactive: metadata, chapters, runtime and capture date are extracted from
the MKV and the folder structure; the rest (tape notes, recording date,
teletext, ...) is prompted. Re-runs are safe — existing heavy outputs (RF
downsample, preview, YouTube encode) are skipped, text files are regenerated.
RF capture files are searched in `./captures` and `./export_new` by default
(override with `--capture-dir`). If the video RF is not yet downsampled, it is
resampled in place (same as `vhs-tool rf-resample` with defaults).

### RF resample (standalone)

```bash
# PAL VHS video RF → 20 MSPS 8-bit (default preset):
vhs-tool rf-resample ./captures/VHS_PAL_Tape_010

# Other presets / custom rates:
vhs-tool rf-resample ./captures/<name> --preset ntsc      # 16 MSPS
vhs-tool rf-resample ./captures/<name> --preset svhs      # 24 MSPS
vhs-tool rf-resample ./captures/<name> --vrate 18000 --vcutoff 0-8670

# Also resample HiFi (normally already resampled at capture time):
vhs-tool rf-resample ./captures/<name> --with-hifi

# Preview without writing anything:
vhs-tool rf-resample ./captures/<name> --dry-run
```

Outputs `<name>-video.8bit.20msps.flac` next to the source; originals are
never modified. Already-converted files are skipped, so re-runs are safe.

## Development

```bash
cd tools/vhs-tool
uv sync          # create venv with dev dependencies
uv run pytest    # run tests
uv run ruff check src tests
uv run ruff format src tests
```
