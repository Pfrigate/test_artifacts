"""Plain data types shared by the core components and the UI."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.width, self.height)

    def intersected(self, other: "Rect") -> "Rect":
        x = max(self.x, other.x)
        y = max(self.y, other.y)
        r = min(self.right, other.right)
        b = min(self.bottom, other.bottom)
        return Rect(x, y, max(0, r - x), max(0, b - y))

    def is_empty(self) -> bool:
        return self.width <= 0 or self.height <= 0


@dataclass(frozen=True)
class MonitorInfo:
    handle: int
    device_name: str
    rect: Rect  # physical pixels, virtual-desktop coordinates
    primary: bool
    index: int
    scale: float = 1.0

    @property
    def label(self) -> str:
        primary = " (principal)" if self.primary else ""
        return f"Monitor {self.index + 1}: {self.rect.width}x{self.rect.height}{primary}"


@dataclass(frozen=True)
class WindowInfo:
    handle: int
    title: str
    process_name: str
    pid: int
    rect: Rect  # physical pixels (DWM extended frame bounds)
    minimized: bool = False

    @property
    def label(self) -> str:
        suffix = " [minimizada]" if self.minimized else ""
        proc = f" — {self.process_name}" if self.process_name else ""
        return f"{self.title}{proc}{suffix}"


class SourceKind(str, Enum):
    MONITOR = "monitor"
    WINDOW = "window"
    REGION = "region"
    SYNTHETIC = "synthetic"


@dataclass(frozen=True)
class SourceSpec:
    """What to capture.

    ``region`` is relative to the monitor's top-left corner in physical pixels and is only
    used with ``SourceKind.REGION`` (a region is always captured from a monitor).
    """

    kind: SourceKind
    handle: int = 0
    size: tuple[int, int] = (0, 0)  # source size in physical pixels
    region: Rect | None = None
    monitor_index: int = 0
    description: str = ""

    @property
    def capture_size(self) -> tuple[int, int]:
        if self.kind == SourceKind.REGION and self.region is not None:
            return (self.region.width, self.region.height)
        return self.size


class Backend(str, Enum):
    AUTO = "auto"
    WGC = "wgc"
    DXCAM = "dxcam"
    SYNTHETIC = "synthetic"


@dataclass
class VideoSettings:
    resolution: str = "1080p"  # 720p | 1080p | 2160p | native
    fps: int = 30  # 15 | 30 | 60
    quality: str = "medium"  # low | medium | high
    encoder: str = "auto"  # auto | hardware | software
    backend: str = Backend.AUTO.value
    aspect_mode: str = "letterbox"  # letterbox (fixed 16:9 canvas) | source (keep source aspect)
    cursor: bool = True
    border: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RecordingStats:
    frames_written: int = 0
    frames_captured: int = 0
    frames_duplicated: int = 0
    frames_dropped: int = 0
    avg_frame_ms: float = 0.0
    max_frame_ms: float = 0.0
    recorded_seconds: float = 0.0
    state: str = "idle"
    error_kind: str = ""
    error: str = ""
    extra: dict = field(default_factory=dict)
