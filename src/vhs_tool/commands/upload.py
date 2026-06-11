"""vhs-tool upload — build an Internet Archive / YouTube upload folder from a final encode.

Port of tools/8_upload.sh (Step 3 of the publish pipeline).

  IA:       upload/<Tape>_IA/
            ├── Capture_Data/   RF data (video downsampled to 20 MSPS 8-bit,
            │                   hifi, linear, headswitch)
            ├── Video/          final MKV + .preview.mp4 (archive.org player)
            ├── Pipeline/       vapoursynth_vhs.vpy, 6_export.sh, 7_encode.sh
            ├── [teletext/]     manually added for now (picked up if present)
            ├── Notes.txt       auto-filled from MKV metadata + prompts
            ├── _rules.conf     CAT.ALL
            └── archive.sha256  checksums of all payload files

  YouTube:  upload/<Tape>_YT/
            ├── <Tape>_youtube.mkv   2880x2160 upscale encode
            └── description.txt      metadata + chapters extracted from MKV
                                     (YouTube ignores MKV metadata/chapters)

Everything derivable from the folder structure / MKV metadata is pre-filled;
interactive prompts cover the rest. Re-runs are safe: existing heavy outputs
(RF downsample, preview, YouTube encode) are skipped, text files regenerated.

The actual archive.org upload (ia CLI) is a separate script — out of scope.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..common import (
    ToolError,
    check_deps,
    confirm,
    format_tag,
    human_size,
    prompt_default,
    run,
    video_duration,
)
from . import rf_resample

# -- Defaults ------------------------------------------------------------------
DEFAULT_UPLOAD_DIR = "./upload"
DEFAULT_DECODED_DIR = "./decoded"
DEFAULT_TOOLS_DIR = "./tools"
# Directories searched (depth 3) for RF capture files
DEFAULT_CAPTURE_DIRS = ("./captures", "./export_new")

# Encode profile suffixes from `vhs-tool encode` / YouTube encodes
PROFILE_SUFFIXES = ("_anime-youtube", "_liveaction-youtube", "_youtube", "_anime", "_liveaction")

# Pipeline scripts copied into the IA item for reproducibility
PIPELINE_FILES = ("vapoursynth_vhs.vpy", "6_export.sh", "7_encode.sh")

# Files excluded from archive.sha256 (metadata, not payload)
CHECKSUM_EXCLUDE = {"archive.sha256", "Notes.txt", "_rules.conf"}

# -- Static capture hardware description (Notes.txt) --------------------------
HW_VCR = "Panasonic NV-VP30"
HW_RF_CAPTURE = "CX25800 (Video) + CX23883 (HiFi), Clockgen 40 MSPS"
HW_RF_AMP = "ADA4857 (4S 18650 Li-Ion)"
HW_RF_AMP_VIDEO = "R11/R12 = 15kΩ, R13 = 120Ω, R14 = 560Ω"
HW_RF_AMP_HIFI = "R21/R22 = 20kΩ, R23 = 390Ω, R24 = 560Ω"
HW_AUDIO_ADC = "PCM1802 (Clockgen)"

# -- Static description links (YouTube) ----------------------------------------
YT_LINKS = (
    "VHS-Decode Wiki: https://github.com/oyvindln/vhs-decode/wiki/\n"
    "VHS-Decode Reddit: https://www.reddit.com/r/vhsdecode/\n"
    "Domesday86 Discord: https://discord.gg/pVVrrxd"
)

_CAPTURE_DATE_RE = re.compile(r"-(\d{4})-(\d{2})-(\d{2})_\d{2}_\d{2}_\d{2}")
_ATTACHMENT_RE = re.compile(r"^Attachment ID (\d+):.*file name '([^']+)'")


def hr() -> None:
    print("=" * 60)


# =============================================================================
# Pure helpers (testable)
# =============================================================================


def strip_profile_suffix(name: str) -> str:
    """Strip encode profile suffixes (`_anime`, `_youtube`, ...) from a base name."""
    for suffix in PROFILE_SUFFIXES:
        name = name.removesuffix(suffix)
    return name


def normalize_platform(platform: str) -> str:
    p = platform.lower()
    if p in ("ia", "archive", "archive.org"):
        return "ia"
    if p in ("yt", "youtube"):
        return "youtube"
    raise ToolError(f"Unknown platform '{platform}' (expected: ia, youtube)")


def seconds_to_hms(seconds: float) -> str:
    """Format seconds as 'HH:MM:SS' (rounded)."""
    s = int(seconds + 0.5)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def seconds_to_yt_ts(seconds: float) -> str:
    """YouTube chapter timestamp: M:SS below one hour, H:MM:SS above."""
    s = int(seconds + 0.5)
    hours, minutes, secs = s // 3600, (s % 3600) // 60, s % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def parse_capture_date(base: str) -> tuple[str, str]:
    """Capture date from the filename timestamp → (ISO, German) date strings."""
    match = _CAPTURE_DATE_RE.search(base)
    if not match:
        return "unknown", "unbekannt"
    year, month, day = match.groups()
    return f"{year}-{month}-{day}", f"{day}.{month}.{year}"


def detect_tv_system(base: str) -> str:
    return "NTSC" if "NTSC" in base.upper() else "PAL"


def detect_tape_format(base: str) -> str:
    upper = base.upper()
    return "S-VHS" if "SVHS" in upper or "S-VHS" in upper else "VHS"


def ia_identifier(base: str) -> str:
    """Default archive.org identifier derived from the base name."""
    return re.sub("-+", "-", base.lower().replace("_", "-"))


def build_youtube_description(
    *,
    base: str,
    recording_date: str,
    capture_date_de: str,
    ia_url: str,
    teletext: bool,
    extra_text: str,
    chapters: list[tuple[float, str]],
) -> str:
    lines = [base, "", "Digitalisiert mit vhs-decode."]
    if recording_date:
        lines.append(f"Aufnahmedatum: {recording_date}")
    lines.append(f"Digitalisierungsdatum: {capture_date_de}")
    lines.append(f"Internet Archive Link (originale Qualität + RF Capture): {ia_url}")
    lines.append("")
    if teletext:
        lines.append("Teletext: ja (im Internet Archive enthalten)")
    else:
        lines.append("Teletext: nein")
    if extra_text:
        lines += ["", extra_text]
    if chapters:
        lines += ["", "Kapitel:"]
        if chapters[0][0] >= 0.5:
            lines.append("0:00 Start")
        for start, title in chapters:
            lines.append(f"{seconds_to_yt_ts(start)} {title}")
    lines += ["", YT_LINKS]
    return "\n".join(lines) + "\n"


def build_notes(
    *,
    tape_notes: str,
    tag_title: str,
    tape_format: str,
    tape_speed: str,
    tv_system: str,
    colour: str,
    has_hifi_rf: bool,
    has_hifi: bool,
    has_linear: bool,
    teletext: bool,
    runtime: str,
    recording_date: str,
    capture_date_iso: str,
    vhs_decode_version: str,
    extra_params: str,
) -> str:
    def yes_no(value: bool) -> str:
        return "Yes" if value else "No"

    lines = ["=== Tape ===", "Notes:"]
    if tape_notes:
        lines.append(tape_notes)
    lines.append("")
    if tag_title:
        lines.append(f"Title: {tag_title}")
    lines += [
        f"Format: {tape_format} {tape_speed}",
        f"TV System: {tv_system}",
        f"Colour: {colour}",
        f"HiFi: {yes_no(has_hifi)}",
        f"Linear: {yes_no(has_linear)}",
        f"Teletext: {yes_no(teletext)}",
        "",
        "=== FM RF Archive ===",
        f"Format:               {tape_format} {tv_system} {tape_speed}",
        f"Runtime:              {runtime}",
        f"Date of Recording:    {recording_date}",
        f"Date of Capture:      {capture_date_iso}",
        "",
        "=== Capture Hardware ===",
        f"VCR:                    {HW_VCR}",
        f"RF Capture:             {HW_RF_CAPTURE}",
        f"RF Amplifier:           {HW_RF_AMP}",
        f"    Video:                  {HW_RF_AMP_VIDEO}",
        f"    HiFi:                   {HW_RF_AMP_HIFI}",
        f"Audio ADC:              {HW_AUDIO_ADC}",
        "",
        "=== RF Data ===",
        "Original Sample Rate:   40 MSPS 8-bit",
        "Video Sample Rate:      20 MSPS 8-bit (SoX sinc -n 2500 0-9600)",
    ]
    if has_hifi_rf:
        lines.append(
            "HiFi Sample Rate:       "
            "10 MSPS 8-bit (SoX rate -l via local-capture.sh --resample-hifi)"
        )
    lines += [
        "",
        "=== Decode & Processing ===",
        f"Software:               vhs-decode {vhs_decode_version}",
    ]
    if has_hifi:
        lines.append("HiFi Decode:            hifi-decode")
    lines += [
        f"Extra parameters:       {extra_params}",
        "Export:                 tbc-video-export → FFV1 10-bit 4:2:2 (intermediate)",
        "VapourSynth:            See Pipeline/ folder for .vpy",
        "Final Encode:           x265",
        "",
        "=== Notes ===",
        "- FFV1 intermediate not included (reproducible from RF)",
        "- TBC files not included (reproducible from RF)",
    ]
    if has_linear:
        lines.append(f"- Linear audio is genuine linear from {HW_VCR} audio head")
    if teletext:
        lines.append("- Teletext extracted from VBI, see teletext/ folder")
    lines.append(
        "- .preview.mp4 is only for preview on archive.org (doesn't support MKV with x265 and Opus)"
    )
    return "\n".join(lines) + "\n"


# =============================================================================
# MKV / filesystem probing
# =============================================================================


def mkv_audio_tracks(mkv: Path) -> list[tuple[int, str]]:
    """(channels, track name) for each audio stream in the MKV."""
    result = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=channels:stream_tags=title",
            "-print_format",
            "json",
            mkv,
        ],  # fmt: skip
        capture=True,
    )
    data = json.loads(result.stdout or "{}")
    return [
        (int(stream.get("channels", 0)), (stream.get("tags") or {}).get("title", ""))
        for stream in data.get("streams", [])
    ]


def mkv_chapters(mkv: Path) -> list[tuple[float, str]]:
    """(start seconds, title) for each titled chapter in the MKV."""
    result = run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_chapters", mkv],
        capture=True,
    )
    data = json.loads(result.stdout or "{}")
    return [
        (float(chapter.get("start_time", 0.0)), title)
        for chapter in data.get("chapters", [])
        if (title := (chapter.get("tags") or {}).get("title")) is not None
    ]


def mkv_attachments(mkv: Path) -> list[tuple[int, str]]:
    """(attachment ID, file name) for each attachment listed by mkvmerge -i."""
    result = run(["mkvmerge", "-i", mkv], capture=True, check=False)
    return [
        (int(match.group(1)), match.group(2))
        for line in result.stdout.splitlines()
        if (match := _ATTACHMENT_RE.match(line))
    ]


def find_rf(capture_dirs: list[str], base: str, suffix: str) -> Path | None:
    """Find an RF capture file '<base><suffix>' in the capture dirs (depth 3, symlinks ok)."""
    target = f"{base}{suffix}"
    for directory in capture_dirs:
        root = Path(directory)
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
            if len(Path(dirpath).relative_to(root).parts) > 2:
                dirnames.clear()
                continue
            if target in filenames:
                return Path(dirpath) / target
    return None


def detect_vhs_decode_version(decoded_dir: Path, base: str) -> str:
    """Detect the vhs-decode version from decode artifacts (.tbc.json, logs)."""
    version = ""
    json_file = decoded_dir / f"{base}-video.tbc.json"
    if json_file.is_file():
        size = json_file.stat().st_size
        with open(json_file, "rb") as f:
            chunk = f.read(65536)
            if size > 65536:
                f.seek(max(65536, size - 65536))
                chunk += f.read(65536)
        text = chunk.decode("utf-8", errors="replace")
        for key in ("vhsDecodeVersion", "buildString", "version", "gitBranch"):
            match = re.search(rf'"{key}"\s*:\s*"([^"]+)"', text)
            if match:
                version = match.group(1)
                break
    if not version:
        for log in sorted(decoded_dir.glob(f"{base}*.log")):
            text = log.read_text(encoding="utf-8", errors="replace")
            match = re.search(r'vhs-decode[ _-]*v?\d+\.\d+[^ ",)]*', text, re.IGNORECASE)
            if match:
                inner = re.search(r'v?\d+\.\d+[^ ",)]*', match.group(0))
                version = inner.group(0) if inner else ""
                if version:
                    break
    if version and version[0].isdigit():
        version = f"v{version}"
    return version


def copy_into(src: Path, dstdir: Path) -> None:
    """Copy with progress (rsync if available), skip if destination already exists."""
    dst = dstdir / src.name
    if dst.is_file():
        print(f"  exists, skipping: {dst}")
        return
    print(f"  copying: {src.name} ({human_size(src)})")
    if shutil.which("rsync"):
        run(["rsync", "-a", "--info=progress2", src, dst])
    else:
        shutil.copy(src, dst)


def walk_files(root: Path) -> list[tuple[str, int]]:
    """All files below root as sorted (relative path, size) tuples."""
    return sorted(
        (str(path.relative_to(root)), path.stat().st_size)
        for path in root.rglob("*")
        if path.is_file()
    )


def write_checksums(item_dir: Path) -> int:
    """Write archive.sha256 (sha256sum format) for all payload files. Returns file count."""
    entries = []
    for rel, _size in walk_files(item_dir):
        if Path(rel).name in CHECKSUM_EXCLUDE:
            continue
        with open(item_dir / rel, "rb") as f:
            digest = hashlib.file_digest(f, "sha256")
        entries.append(f"{digest.hexdigest()}  {rel}")
    (item_dir / "archive.sha256").write_text("\n".join(entries) + "\n", encoding="utf-8")
    return len(entries)


# =============================================================================
# Tape info — everything extractable without prompting
# =============================================================================


@dataclass
class TapeInfo:
    base: str
    input_mkv: Path
    mkv: Path
    runtime: str
    tag_title: str
    tag_source: str
    tag_date: str
    tag_comment: str
    capture_date_iso: str
    capture_date_de: str
    tv_system: str
    tape_format: str
    has_hifi_track: bool
    has_linear_track: bool
    audio_summary: str
    chapters: list[tuple[float, str]]
    rf_video: Path | None
    rf_video_20: Path | None
    rf_hifi: Path | None
    rf_linear: Path | None
    rf_headswitch: Path | None
    vhs_decode_version: str
    ia_identifier: str


def gather_info(
    input_mkv: Path, mkv: Path, base: str, capture_dirs: list[str], decoded_dir: Path
) -> TapeInfo:
    has_hifi_track = has_linear_track = False
    audio_summary = ""
    for channels, track_name in mkv_audio_tracks(mkv):
        mode = "mono" if channels == 1 else "stereo"
        audio_summary += f"    {track_name or 'unnamed'}: {mode}\n"
        if "hifi" in track_name.lower():
            has_hifi_track = True
        if "linear" in track_name.lower():
            has_linear_track = True

    capture_date_iso, capture_date_de = parse_capture_date(base)

    return TapeInfo(
        base=base,
        input_mkv=input_mkv,
        mkv=mkv,
        runtime=seconds_to_hms(video_duration(mkv)),
        tag_title=format_tag(mkv, "title"),
        tag_source=format_tag(mkv, "SOURCE"),
        tag_date=format_tag(mkv, "DATE_RELEASED"),
        tag_comment=format_tag(mkv, "COMMENT"),
        capture_date_iso=capture_date_iso,
        capture_date_de=capture_date_de,
        tv_system=detect_tv_system(base),
        tape_format=detect_tape_format(base),
        has_hifi_track=has_hifi_track,
        has_linear_track=has_linear_track,
        audio_summary=audio_summary,
        chapters=mkv_chapters(mkv),
        rf_video=find_rf(capture_dirs, base, "-video.flac"),
        rf_video_20=find_rf(capture_dirs, base, "-video.8bit.20msps.flac"),
        rf_hifi=find_rf(capture_dirs, base, "-hifi.flac"),
        rf_linear=find_rf(capture_dirs, base, "-linear.flac"),
        rf_headswitch=find_rf(capture_dirs, base, "-headswitch.u8"),
        vhs_decode_version=detect_vhs_decode_version(decoded_dir, base),
        ia_identifier=ia_identifier(base),
    )


def print_summary(info: TapeInfo, platform: str) -> None:
    hr()
    print(f"VHS Upload Preparation — {platform.upper()}")
    hr()
    print(f"Tape base:      {info.base}")
    print(f"Source MKV:     {info.mkv}")
    print(f"Runtime:        {info.runtime}")
    print(f"Capture date:   {info.capture_date_iso}")
    print(f"Title tag:      {info.tag_title or '—'}")
    print(f"Source tag:     {info.tag_source or '—'}")
    print(f"Date tag:       {info.tag_date or '—'}")
    print(f"Comment tag:    {info.tag_comment or '—'}")
    print(f"Chapters:       {len(info.chapters)}")
    hifi = "yes" if info.has_hifi_track else "no"
    linear = "yes" if info.has_linear_track else "no"
    print(f"Audio tracks:   HiFi={hifi} Linear={linear}")
    if info.audio_summary:
        print(info.audio_summary, end="")
    if platform == "ia":
        print(f"RF video:       {info.rf_video or 'NOT FOUND'}")
        print(f"RF video 20M:   {info.rf_video_20 or 'not yet resampled'}")
        print(f"RF hifi:        {info.rf_hifi or '—'}")
        print(f"RF linear:      {info.rf_linear or '—'}")
        print(f"RF headswitch:  {info.rf_headswitch or '—'}")
        print(f"vhs-decode:     {info.vhs_decode_version or 'could not detect'}")
    hr()
    print()


# =============================================================================
# Argument parsing
# =============================================================================


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "upload",
        help="Build an Internet Archive / YouTube upload folder from a final encode",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "input_mkv",
        help="Final encode from `vhs-tool encode` (./final/...)",
    )
    parser.add_argument(
        "platform",
        nargs="?",
        help="Target platform: ia | youtube (asked interactively if omitted)",
    )
    parser.add_argument(
        "--upload-dir",
        default=DEFAULT_UPLOAD_DIR,
        help=f"Upload folder root (default: {DEFAULT_UPLOAD_DIR})",
    )
    parser.add_argument(
        "--capture-dir",
        dest="capture_dirs",
        action="append",
        metavar="DIR",
        help="Directory searched (depth 3) for RF capture files (repeatable; "
        f"default: {' '.join(DEFAULT_CAPTURE_DIRS)})",
    )
    parser.add_argument(
        "--decoded-dir",
        default=DEFAULT_DECODED_DIR,
        help=f"Decode artifacts dir, for vhs-decode version detection "
        f"(default: {DEFAULT_DECODED_DIR})",
    )
    parser.add_argument(
        "--tools-dir",
        default=DEFAULT_TOOLS_DIR,
        help=f"Tools dir with the pipeline scripts (default: {DEFAULT_TOOLS_DIR})",
    )
    parser.set_defaults(func=cmd_upload)


# =============================================================================
# Command
# =============================================================================


def cmd_upload(args: argparse.Namespace) -> int:
    check_deps("ffmpeg", "ffprobe", "mkvmerge", "mkvextract")

    input_mkv = Path(args.input_mkv)
    if not input_mkv.is_file():
        raise ToolError(f"Input not found: {input_mkv}")

    platform = normalize_platform(args.platform or prompt_default("Platform [ia/youtube]: ", "ia"))

    # Derive tape base name + locate the canonical final MKV
    base = strip_profile_suffix(input_mkv.name.removesuffix(".mkv"))
    mkv = input_mkv
    canonical = input_mkv.parent / f"{base}.mkv"
    if input_mkv.name.removesuffix(".mkv") != base and canonical.is_file():
        mkv = canonical
        print(f"Note: using {mkv} as metadata source (input had a profile suffix)")

    info = gather_info(
        input_mkv,
        mkv,
        base,
        args.capture_dirs or list(DEFAULT_CAPTURE_DIRS),
        Path(args.decoded_dir),
    )
    print_summary(info, platform)

    if platform == "youtube":
        return _upload_youtube(info, Path(args.upload_dir))
    return _upload_ia(info, Path(args.upload_dir), Path(args.tools_dir))


# =============================================================================
# YouTube
# =============================================================================


def _upload_youtube(info: TapeInfo, upload_dir: Path) -> int:
    item_dir = upload_dir / f"{info.base}_YT"
    yt_mkv = item_dir / f"{info.base}_youtube.mkv"

    # -- Prompts ---------------------------------------------------------------
    ia_url = prompt_default(
        "Internet Archive link: ", f"https://archive.org/details/{info.ia_identifier}"
    )
    teletext = confirm("Teletext included? [y/N]:", "n")
    extra_text = prompt_default("Extra description text (optional): ", "")
    recording_date = prompt_default("Aufnahmedatum/-jahr (optional): ", info.tag_date)

    item_dir.mkdir(parents=True, exist_ok=True)

    # -- Encode ------------------------------------------------------------------
    if yt_mkv.is_file():
        print(f"YouTube encode exists, skipping: {yt_mkv}")
    elif info.input_mkv != info.mkv and info.input_mkv.is_file():
        # User passed an existing *_youtube.mkv → reuse it
        print(f"Using existing YouTube encode: {info.input_mkv}")
        copy_into(info.input_mkv, item_dir)
        if info.input_mkv.name != yt_mkv.name:
            (item_dir / info.input_mkv.name).rename(yt_mkv)
    else:
        print()
        print("YouTube encode (2880x2160 lanczos, x265 crf 15): this takes a while.")
        if confirm("Start encode now? [Y/n]:", "y"):
            run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-i",
                    info.mkv,
                    "-vf",
                    "scale=2880:2160:flags=lanczos,setsar=1",
                    "-c:v",
                    "libx265",
                    "-crf",
                    "15",
                    "-preset",
                    "faster",
                    "-c:a",
                    "copy",
                    yt_mkv,
                ]  # fmt: skip
            )
        else:
            print("Skipped. Re-run the script later to encode.")

    # -- description.txt ---------------------------------------------------------
    (item_dir / "description.txt").write_text(
        build_youtube_description(
            base=info.base,
            recording_date=recording_date,
            capture_date_de=info.capture_date_de,
            ia_url=ia_url,
            teletext=teletext,
            extra_text=extra_text,
            chapters=info.chapters,
        ),
        encoding="utf-8",
    )

    print()
    hr()
    print(f"YouTube upload folder ready: {item_dir}")
    print()
    for entry in sorted(item_dir.iterdir()):
        print(f"  {entry.name:<50} {human_size(entry)}")
    print()
    print(f"Suggested video title: {info.tag_title or info.base}")
    if not info.chapters:
        print("Note: no chapters in MKV — description has no chapter list.")
    elif len(info.chapters) < 3:
        print(f"Note: YouTube needs ≥3 chapters for the chapter bar (found {len(info.chapters)}).")
    print()
    print("Manual upload: video + paste description.txt, set title/visibility.")
    hr()
    return 0


# =============================================================================
# Internet Archive
# =============================================================================


def _upload_ia(info: TapeInfo, upload_dir: Path, tools_dir: Path) -> int:
    item_dir = upload_dir / f"{info.base}_IA"

    # -- Prompts (everything up front, heavy work after one confirm) ---------------
    print("Notes.txt details (Enter accepts the default):")
    tape_notes = prompt_default("Tape notes: ", "")
    tape_speed = prompt_default("Tape speed [SP/LP/EP]: ", "SP")
    colour = prompt_default("Colour [Yes/No]: ", "Yes")
    recording_date = prompt_default("Date of Recording: ", info.tag_date or "unknown")
    vhs_decode_version = prompt_default("vhs-decode version: ", info.vhs_decode_version or "v0.3.9")
    extra_params = prompt_default("Extra decode parameters: ", "--ire0_adjust")
    teletext_default = "y" if (item_dir / "teletext").is_dir() else "n"
    teletext = confirm("Teletext included (teletext/ folder)? [y/n]:", teletext_default)

    has_hifi_rf = info.rf_hifi is not None
    has_linear_rf = info.rf_linear is not None

    # Resample plan
    rf_video_20 = info.rf_video_20
    resample_target = item_dir / "Capture_Data" / f"{info.base}-video.8bit.20msps.flac"
    need_resample = False
    if resample_target.is_file():
        resample_note = "already in upload folder"
    elif rf_video_20:
        resample_note = f"already resampled: {rf_video_20}"
    elif info.rf_video:
        need_resample = True
        resample_note = f"will resample {info.rf_video}"
    else:
        resample_note = "NO RF VIDEO FOUND — Capture_Data will be incomplete!"

    print()
    print("Planned steps:")
    print(f"  1. Video RF → 20 MSPS 8-bit  ({resample_note})")
    print("  2. Copy RF files into Capture_Data/")
    print("  3. Copy final MKV + encode preview MP4 into Video/")
    print("  4. Copy pipeline scripts, extract MKV cover attachment (if any)")
    print("  5. Write Notes.txt + _rules.conf")
    print("  6. Generate archive.sha256")
    print()
    if not confirm("Continue? [Y/n]:", "y"):
        print("Aborted.")
        return 0

    for subdir in ("Capture_Data", "Video", "Pipeline"):
        (item_dir / subdir).mkdir(parents=True, exist_ok=True)

    # -- 1. Resample video RF to 20 MSPS -------------------------------------------
    print()
    print("Step 1: Video RF (20 MSPS 8-bit)...")
    if need_resample:
        rf_video_20 = rf_resample.resample_file(
            info.rf_video, rf_resample.VIDEO_RATE, rf_resample.VIDEO_CUTOFF, "video"
        )
        if not rf_video_20 or not rf_video_20.is_file():
            raise ToolError(f"Resample did not produce a 20 MSPS video RF from {info.rf_video}")

    # -- 2. Copy RF files -----------------------------------------------------------
    print()
    print("Step 2: Copying RF files into Capture_Data/...")
    for rf in (rf_video_20, info.rf_hifi, info.rf_linear, info.rf_headswitch):
        if rf:
            copy_into(rf, item_dir / "Capture_Data")
    if not rf_video_20 and not resample_target.is_file():
        print("  WARNING: no video RF in Capture_Data — add it manually and re-run.")

    # -- 3. Final MKV + preview ------------------------------------------------------
    print()
    print("Step 3: Video/...")
    item_mkv = item_dir / "Video" / f"{info.base}.mkv"
    if item_mkv.is_file():
        print(f"  exists, skipping: {item_mkv}")
    else:
        copy_into(info.mkv, item_dir / "Video")
        if info.mkv.name != item_mkv.name:
            (item_dir / "Video" / info.mkv.name).rename(item_mkv)

    preview_mp4 = item_dir / "Video" / f"{info.base}.preview.mp4"
    if preview_mp4.is_file():
        print(f"  preview exists, skipping: {preview_mp4}")
    else:
        print("  encoding preview MP4 (x264, for the archive.org player)...")
        run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "warning",
                "-i",
                item_mkv,
                "-c:v",
                "libx264",
                "-crf",
                "23",
                "-preset",
                "medium",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-map",
                "0:v:0",
                "-map",
                "0:a",
                "-aspect",
                "4:3",
                preview_mp4,
            ]  # fmt: skip
        )

    # -- 4. Pipeline + cover ---------------------------------------------------------
    print()
    print("Step 4: Pipeline/ + cover...")
    for name in PIPELINE_FILES:
        src = tools_dir / name
        if src.is_file():
            shutil.copy(src, item_dir / "Pipeline" / name)
            print(f"  Pipeline/{name}")
        else:
            print(f"  not found, skipping: {src}")

    # Extract MKV attachments (cover etc.) into item root
    for att_id, att_name in mkv_attachments(item_mkv):
        target = item_dir / att_name
        if target.is_file():
            print(f"  attachment exists, skipping: {att_name}")
        else:
            run(["mkvextract", item_mkv, "attachments", f"{att_id}:{target}"], capture=True)
            print(f"  extracted attachment: {att_name}")

    # -- 5. Notes.txt + _rules.conf ---------------------------------------------------
    print()
    print("Step 5: Notes.txt + _rules.conf...")
    (item_dir / "Notes.txt").write_text(
        build_notes(
            tape_notes=tape_notes,
            tag_title=info.tag_title,
            tape_format=info.tape_format,
            tape_speed=tape_speed,
            tv_system=info.tv_system,
            colour=colour,
            has_hifi_rf=has_hifi_rf,
            has_hifi=has_hifi_rf or info.has_hifi_track,
            has_linear=has_linear_rf or info.has_linear_track,
            teletext=teletext,
            runtime=info.runtime,
            recording_date=recording_date,
            capture_date_iso=info.capture_date_iso,
            vhs_decode_version=vhs_decode_version,
            extra_params=extra_params,
        ),
        encoding="utf-8",
    )
    print("  Notes.txt written")
    (item_dir / "_rules.conf").write_text("CAT.ALL\n", encoding="utf-8")
    print("  _rules.conf written")

    # -- 6. Checksums ------------------------------------------------------------------
    print()
    print("Step 6: archive.sha256...")
    if teletext and not (item_dir / "teletext").is_dir():
        print("  NOTE: Teletext = Yes but no teletext/ folder yet.")
        print("        Add it manually, then re-run this script to refresh checksums.")
    if confirm("Generate checksums now (may take a while)? [Y/n]:", "y"):
        count = write_checksums(item_dir)
        print(f"  archive.sha256 written ({count} files)")
    else:
        print("  skipped — re-run the script to generate.")

    # -- Summary -------------------------------------------------------------------------
    print()
    hr()
    print(f"IA upload folder ready: {item_dir}")
    print()
    for rel, size in walk_files(item_dir):
        print(f"  {rel:<70} {size / 1024 / 1024:8.1f} MB")
    print()
    print("Next steps:")
    print("  - Review Notes.txt")
    if teletext and not (item_dir / "teletext").is_dir():
        print("  - Add teletext/ folder, then re-run for fresh checksums")
    print("  - Upload via the separate IA upload script (ia CLI)")
    hr()
    return 0
