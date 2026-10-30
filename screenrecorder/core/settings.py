"""Persistent application settings (JSON in %LOCALAPPDATA%\\ScreenRecorder)."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path

from screenrecorder.core.models import VideoSettings

log = logging.getLogger(__name__)


def app_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / ".local" / "share"
    path = root / "ScreenRecorder"
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_output_dir() -> Path:
    return Path.home() / "Videos" / "ScreenRecorder"


@dataclass
class AppSettings:
    video: VideoSettings = field(default_factory=VideoSettings)
    output_dir: str = field(default_factory=lambda: str(default_output_dir()))
    filename_pattern: str = "Grabacion_%Y-%m-%d_%H-%M-%S"
    hide_window_from_capture: bool = True
    minimize_on_record: bool = False
    countdown_seconds: int = 0

    def output_path(self, now: datetime | None = None) -> Path:
        stamp = (now or datetime.now()).strftime(self.filename_pattern)
        return Path(self.output_dir) / f"{stamp}.mp4"

    @classmethod
    def load(cls, path: Path | None = None) -> "AppSettings":
        path = path or app_data_dir() / "settings.json"
        settings = cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return settings
        except (OSError, ValueError) as exc:
            log.warning("Ignoring unreadable settings %s: %s", path, exc)
            return settings
        video_fields = {f.name for f in fields(VideoSettings)}
        settings.video = VideoSettings(**{k: v for k, v in data.get("video", {}).items() if k in video_fields})
        for f in fields(cls):
            if f.name != "video" and f.name in data:
                setattr(settings, f.name, data[f.name])
        return settings

    def save(self, path: Path | None = None) -> None:
        path = path or app_data_dir() / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
