"""Monotonic timing helpers. ``time.perf_counter_ns`` is backed by QueryPerformanceCounter
on Windows, the same clock the native pipeline uses."""

from __future__ import annotations

import time
from typing import Callable

HNS_PER_SECOND = 10_000_000


def frame_timestamp(index: int, fps: int) -> int:
    """Presentation time (100 ns units) of CFR frame ``index`` — identical to the native
    ``frame_time_hns`` so Python and C++ agree on every sample time."""
    return index * HNS_PER_SECOND // fps


def frame_duration(index: int, fps: int) -> int:
    return frame_timestamp(index + 1, fps) - frame_timestamp(index, fps)


class RecordingClock:
    """Wall-clock recording time excluding pauses (drives the UI timer)."""

    def __init__(self, now_ns: Callable[[], int] = time.perf_counter_ns) -> None:
        self._now = now_ns
        self._started_at: int | None = None
        self._paused_at: int | None = None
        self._paused_total = 0
        self._stopped_at: int | None = None

    def start(self) -> None:
        self._started_at = self._now()
        self._paused_at = None
        self._paused_total = 0
        self._stopped_at = None

    def pause(self) -> None:
        if self._started_at is not None and self._paused_at is None:
            self._paused_at = self._now()

    def resume(self) -> None:
        if self._paused_at is not None:
            self._paused_total += self._now() - self._paused_at
            self._paused_at = None

    def stop(self) -> None:
        if self._started_at is not None and self._stopped_at is None:
            self.resume()
            self._stopped_at = self._now()

    @property
    def paused(self) -> bool:
        return self._paused_at is not None

    def elapsed(self) -> float:
        if self._started_at is None:
            return 0.0
        end = self._stopped_at or self._paused_at or self._now()
        return max(0, end - self._started_at - self._paused_total) / 1e9


def format_duration(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"
