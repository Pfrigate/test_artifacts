"""Guard rail: the application must not depend on FFmpeg, GStreamer, OpenCV or PyAV."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN = re.compile(r"\b(ffmpeg|ffprobe|gstreamer|gst-launch|cv2|opencv|imageio_ffmpeg|moviepy|QtMultimedia)\b", re.I)
IMPORT = re.compile(r"^\s*(import|from)\s+(cv2|av|ffmpeg|imageio|moviepy|gi)\b", re.M)


def test_requirements_are_ffmpeg_free():
    for name in ("requirements.txt", "requirements-dev.txt", "pyproject.toml"):
        for line in (ROOT / name).read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("#") or "description" in line:
                continue
            assert not FORBIDDEN.search(line), f"{name}: {line}"


def test_sources_do_not_import_media_frameworks():
    for path in (ROOT / "screenrecorder").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not IMPORT.search(text), path
        assert "PySide6.QtMultimedia" not in text, path
    for path in (ROOT / "native").glob("*.*"):
        text = path.read_text(encoding="utf-8").lower()
        assert "avcodec" not in text and "gst_" not in text, path
