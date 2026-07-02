"""Reusable encoding building blocks shared by the encode and upload commands.

Two profile registries live here so every encode setting exists exactly once:

  X265_PROFILES    final publish encodes (x265 CLI options, `vhs-tool encode`)
  FFMPEG_PROFILES  one-pass ffmpeg encodes (YouTube upscale, archive.org preview)
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from .common import run

# =============================================================================
# x265 publish profiles (settings from JET guide, tuned for VHS)
# =============================================================================
#
# HEVC Main 10 (4:2:0 10-bit) — x265 direkt statt über ffmpeg's libx265,
# weil der ffmpeg-Wrapper einige Parameter (no-sao, no-cutree, bframes)
# mit Preset-Defaults überschreibt.
#
# VapourSynth-Script muss finalen Output als YUV420P10 liefern.
# SAR kommt über y4m-Header von VapourSynth SetFrameProps.
#
# The *-youtube variants drop --sar (square pixels after Spline64 upscale
# to 2880x2160).


def _x265_opts(*, tskip: bool, aq_strength: str, deblock: str, sar: str | None) -> list[str]:
    opts = [
        "--preset", "slow", "--crf", "14", "--output-depth", "10",
        "--no-sao", "--no-cutree", "--bframes", "16",
    ]  # fmt: skip
    if tskip:
        opts.append("--tskip")
    opts += [
        "--aq-mode", "3", "--aq-strength", aq_strength,
        "--psy-rd", "2.00", "--psy-rdoq", "1.50",
        "--deblock", deblock, "--qcomp", "0.70", "--cbqpoffs", "-2",
        "--rc-lookahead", "150", "--merange", "32",
        "--keyint", "500", "--min-keyint", "50",
        "--colorprim", "bt470bg", "--transfer", "bt470bg", "--colormatrix", "bt470bg",
    ]  # fmt: skip
    if sar:
        opts += ["--sar", sar]
    return opts


X265_PROFILES: dict[str, list[str]] = {
    "anime": _x265_opts(tskip=True, aq_strength="0.65", deblock="-1:-1", sar="47:57"),
    "liveaction": _x265_opts(tskip=False, aq_strength="0.80", deblock="-2:-2", sar="47:57"),
    "anime-youtube": _x265_opts(tskip=True, aq_strength="0.65", deblock="-1:-1", sar=None),
    "liveaction-youtube": _x265_opts(tskip=False, aq_strength="0.80", deblock="-2:-2", sar=None),
}

# =============================================================================
# ffmpeg one-pass encodes
# =============================================================================


@dataclass(frozen=True)
class FfmpegProfile:
    """Settings for a one-pass ffmpeg encode (see ffmpeg_encode()).

    Use .with_options(crf=..., preset=..., scale=...) for ad-hoc variations.
    """

    codec: str
    crf: int
    preset: str
    scale: str | None = None  # "W:H" — lanczos scale + setsar=1
    pix_fmt: str | None = None
    faststart: bool = False  # -movflags +faststart (MP4 web playback)
    audio_codec: str = "copy"
    audio_bitrate: str | None = None
    map_streams: tuple[str, ...] = ()
    aspect: str | None = None

    def with_options(self, **overrides) -> FfmpegProfile:
        return replace(self, **overrides)

    def describe(self) -> str:
        parts = []
        if self.scale:
            parts.append(f"{self.scale.replace(':', 'x')} lanczos")
        parts.append(f"{self.codec} crf {self.crf}")
        return ", ".join(parts)


FFMPEG_PROFILES: dict[str, FfmpegProfile] = {
    # 4x upscale so YouTube serves VP9/AV1 instead of low-bitrate h264
    "youtube-upscale": FfmpegProfile(codec="libx265", crf=15, preset="faster", scale="2880:2160"),
    # archive.org inline player (no support for MKV with x265 and Opus)
    "archive-preview": FfmpegProfile(
        codec="libx264",
        crf=23,
        preset="medium",
        pix_fmt="yuv420p",
        faststart=True,
        audio_codec="aac",
        audio_bitrate="128k",
        map_streams=("0:v:0", "0:a"),
        aspect="4:3",
    ),
}


def ffmpeg_encode(
    input_file: Path | str,
    output_file: Path | str,
    profile: FfmpegProfile,
    *,
    loglevel: str | None = None,
) -> None:
    """Run a one-pass ffmpeg encode of input_file to output_file."""
    cmd: list = ["ffmpeg", "-hide_banner"]
    if loglevel:
        cmd += ["-loglevel", loglevel]
    cmd += ["-i", input_file]
    if profile.scale:
        cmd += ["-vf", f"scale={profile.scale}:flags=lanczos,setsar=1"]
    cmd += ["-c:v", profile.codec, "-crf", str(profile.crf), "-preset", profile.preset]
    if profile.pix_fmt:
        cmd += ["-pix_fmt", profile.pix_fmt]
    if profile.faststart:
        cmd += ["-movflags", "+faststart"]
    cmd += ["-c:a", profile.audio_codec]
    if profile.audio_bitrate:
        cmd += ["-b:a", profile.audio_bitrate]
    for stream in profile.map_streams:
        cmd += ["-map", stream]
    if profile.aspect:
        cmd += ["-aspect", profile.aspect]
    cmd.append(output_file)
    run(cmd)
