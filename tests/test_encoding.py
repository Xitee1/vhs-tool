import vhs_tool.encoding as encoding
from vhs_tool.encoding import FFMPEG_PROFILES, ffmpeg_encode


def _captured_cmd(monkeypatch):
    calls = []
    monkeypatch.setattr(encoding, "run", lambda cmd: calls.append([str(c) for c in cmd]))
    return calls


def test_ffmpeg_encode_youtube_upscale_command(monkeypatch):
    """Must match the former inline command in upload.py verbatim."""
    calls = _captured_cmd(monkeypatch)
    ffmpeg_encode("in.mkv", "out.mkv", FFMPEG_PROFILES["youtube-upscale"])
    assert calls == [
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            "in.mkv",
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
            "out.mkv",
        ]  # fmt: skip
    ]


def test_ffmpeg_encode_archive_preview_command(monkeypatch):
    """Must match the former inline command in upload.py verbatim."""
    calls = _captured_cmd(monkeypatch)
    ffmpeg_encode("in.mkv", "out.mp4", FFMPEG_PROFILES["archive-preview"], loglevel="warning")
    assert calls == [
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-i",
            "in.mkv",
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
            "out.mp4",
        ]  # fmt: skip
    ]


def test_with_options_overrides():
    profile = FFMPEG_PROFILES["youtube-upscale"].with_options(crf=18, preset="medium")
    assert (profile.crf, profile.preset) == (18, "medium")
    assert profile.codec == "libx265"  # untouched
    assert FFMPEG_PROFILES["youtube-upscale"].crf == 15  # original frozen
