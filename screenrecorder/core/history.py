"""Recording history (JSON list, newest first)."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class HistoryEntry:
    path: str
    created: str
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: int = 0
    size_bytes: int = 0
    encoder: str = ""
    hardware: bool = False
    source: str = ""

    @property
    def exists(self) -> bool:
        return Path(self.path).exists()

    @property
    def title(self) -> str:
        return Path(self.path).name

    def summary(self) -> str:
        mb = self.size_bytes / (1024 * 1024)
        hw = "HW" if self.hardware else "SW"
        return f"{self.width}x{self.height} @ {self.fps} fps · {self.duration:.1f} s · {mb:.1f} MB · {hw}"


class HistoryStore:
    MAX_ENTRIES = 200

    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries: list[HistoryEntry] = []
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._entries = []
            return
        except (OSError, ValueError) as exc:
            log.warning("Ignoring unreadable history %s: %s", self.path, exc)
            self._entries = []
            return
        names = {f.name for f in fields(HistoryEntry)}
        self._entries = [HistoryEntry(**{k: v for k, v in e.items() if k in names}) for e in raw if "path" in e]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(e) for e in self._entries], indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def entries(self) -> list[HistoryEntry]:
        return list(self._entries)

    def add(self, entry: HistoryEntry) -> None:
        self._entries = [e for e in self._entries if e.path != entry.path]
        self._entries.insert(0, entry)
        del self._entries[self.MAX_ENTRIES :]
        self.save()

    def remove(self, path: str) -> None:
        self._entries = [e for e in self._entries if e.path != path]
        self.save()

    def prune_missing(self) -> int:
        before = len(self._entries)
        self._entries = [e for e in self._entries if e.exists]
        if len(self._entries) != before:
            self.save()
        return before - len(self._entries)

    @staticmethod
    def make_entry(path: Path, info: dict, duration: float, source: str) -> HistoryEntry:
        return HistoryEntry(
            path=str(path),
            created=datetime.now().isoformat(timespec="seconds"),
            duration=round(duration, 2),
            width=int(info.get("width", 0)),
            height=int(info.get("height", 0)),
            fps=int(info.get("fps", 0)),
            size_bytes=path.stat().st_size if path.exists() else 0,
            encoder=str(info.get("encoder_name", "")),
            hardware=bool(info.get("hardware", False)),
            source=source,
        )
