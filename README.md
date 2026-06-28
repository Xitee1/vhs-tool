# vhs-tool

Unified CLI for the VHS decode pipeline — a single Python tool covering every
step from RF capture to a publish-ready upload folder.

| Command                | Purpose                                              |
| ---------------------- | ---------------------------------------------------- |
| `vhs-tool decode`      | Video RF FLAC → TBC + JSON (vhs-decode wrapper)      |
| `vhs-tool audio`       | HiFi/Linear RF → decoded + aligned FLAC (48 kHz)     |
| `vhs-tool export`      | TBC + aligned FLAC → lossless FFV1 + Opus audio      |
| `vhs-tool encode`      | FFV1 + Opus → [VapourSynth] → x265 → final MKV       |
| `vhs-tool upload`      | Final MKV → Internet Archive / YouTube upload folder |
| `vhs-tool rf-resample` | Downsample RF captures for archival (40 → 20 MSPS)   |
| `vhs-tool rf-trim`     | Trim noise from the end of synchronized RF captures  |

## Requirements

- Python ≥ 3.11 (single runtime dependency: jinja2, installed automatically)
- External tools, depending on the command:
  - `decode`: vhs-decode (default: `./tools/vhs-decode/.venv/bin/vhs-decode`,
    falls back to PATH)
  - `audio`: ffmpeg, hifi-decode (default: `./tools/vhs-decode/.venv/bin/hifi-decode`,
    falls back to PATH), vhs-decode-aaa AppImage
  - `export`: ffmpeg, ffprobe, tbc-video-export AppImage, tbc-tools AppImage
  - `encode`: ffmpeg, ffprobe, mkvmerge, x265, optionally vspipe/vspreview
    (VapourSynth) and mediainfo
  - `upload`: ffmpeg, ffprobe, mkvmerge, mkvextract, optionally rsync
    (copy progress); sox + flac when the video RF still needs resampling
  - `rf-resample`: sox (with FLAC support), flac (≥1.4)
  - `rf-trim`: sox (with FLAC support); ffprobe optional (sample-count fallback)

## Install

```bash
# As a tool (recommended) — editable, so code changes apply immediately
uv tool install --editable ./tools/vhs-tool

# …or run it ad-hoc without installing
uv run --project ./tools/vhs-tool vhs-tool --help
```

Editable installs pick up *code* changes immediately, but **not new
dependencies** — if `vhs-tool` fails with `ModuleNotFoundError` after an
update (e.g. `No module named 'jinja2'`), re-resolve them with:

```bash
uv tool upgrade vhs-tool
```

> The default paths for the tbc-* AppImages and output directories
> (`./export`, `./final`) are relative — run `vhs-tool` from the
> repository root.

## Configuration

All setup-specific values (directory layout, binary paths, language,
TV system, capture hardware description for `Notes.txt`, YouTube
description links) live in an optional `vhs-tool.toml` at the pipeline
root — the directory you run `vhs-tool` from. `$VHS_TOOL_CONFIG` can
point to a different location.

Every key has a built-in default, so the file only needs what differs
on your setup. See [vhs-tool.example.toml](vhs-tool.example.toml) for
all keys; effective values always show up in `--help`. Precedence:
CLI flags > environment variables (`decode`/`audio`) > config file >
built-in defaults.

The generated text files (`Notes.txt`, YouTube `description.txt`) are
rendered from Jinja2 templates shipped with the package
(`src/vhs_tool/templates/*.j2`). To customize the layout, copy a
template into the directory configured as `[paths] templates`
(default `./templates`) and edit it — same-named files there override
the packaged ones.

Encode settings live in one place, `src/vhs_tool/encoding.py`: the
x265 publish profiles (`vhs-tool encode -p ...`) and the ffmpeg
profiles used by `vhs-tool upload` (YouTube 2880x2160 upscale,
archive.org preview MP4).

## Usage

```bash
vhs-tool --help
vhs-tool decode --help
vhs-tool audio --help
vhs-tool export --help
vhs-tool encode --help
vhs-tool upload --help
vhs-tool rf-resample --help
```

### Decode (step 1)

```bash
# PAL VHS, 40 MSPS capture (defaults) — reads ./captures/<name>-video.flac:
vhs-tool decode ./captures/VHS_PAL_Tape_010
# → decoded/<name>-video.{tbc,_chroma.tbc,tbc.json}

# Other formats / quick test decode of 500 frames:
vhs-tool decode ./captures/<name> --format svhs --chroma-trap
vhs-tool decode ./captures/<name> --start 100 --length 500

# Any unported vhs-decode flag can be passed through:
vhs-tool decode ./captures/<name> --extra "--cxadc --level_adjust 0.1"
```

### Audio (step 2)

```bash
# HiFi + Linear, aligned against the TBC JSON from step 1:
vhs-tool audio ./captures/VHS_PAL_Tape_010
# → decoded/<name>-{hifi,linear}.aligned.flac (48 kHz)
```

Missing HiFi or Linear inputs are auto-skipped with a warning. Decoded HiFi is
validated against the hifi-decode peak gain (noise-only tapes are detected and
skipped); the decoded intermediate is deleted after alignment unless
`--keep-intermediate` is given.

### Export (step 3)

```bash
vhs-tool export ./decoded/VHS_PAL_Tape__Name-2026-05-03_18_14_58_02_00
# → export/<name>.{ffv1.mkv,linear.opus,hifi.opus}

# Only re-export specific parts:
vhs-tool export --only video ./decoded/<name>
```

### Encode (step 4)

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

### Upload (step 5)

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

### RF trim

```bash
# Keep the first 1h23m45s of decoded video. The lock-in offset (+4s of RF) is
# added automatically so the decoded video ends at the requested timestamp:
vhs-tool rf-trim ./captures/VHS_PAL_Tape_010 --end 01:23:45

# Remove the last 120 seconds (raw RF time, no offset):
vhs-tool rf-trim ./captures/VHS_PAL_Tape_010 --trim 120

# --end accepts HH:MM:SS, MM:SS or plain seconds; other options:
vhs-tool rf-trim ./captures/<name> --end 2700         # keep up to 2700s
vhs-tool rf-trim ./captures/<name> --trim 120 --delete-original
vhs-tool rf-trim ./captures/<name> --end 45:30 --dry-run -v
```

Auto-discovers all channels (`-video` required as the sync reference, plus
`-hifi`, `-linear`, `-headswitch`) and trims them proportionally by sample
count so they stay in sync. The trimmed result takes the original file name and
the **untouched original is kept** alongside it as `<file>.bak` (pass
`--delete-original` to remove it instead).

## Development

```bash
cd tools/vhs-tool
uv sync          # create venv with dev dependencies
uv run pytest    # run tests
uv run ruff check src tests
uv run ruff format src tests
```
