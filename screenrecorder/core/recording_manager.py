"""RecordingManager: recording lifecycle and state machine.

    IDLE -> STARTING -> RECORDING <-> PAUSED -> STOPPING -> IDLE
                 \\-> ERROR <-------------/            (ERROR -> IDLE via reset())

The manager is UI-agnostic: the Qt layer calls ``poll()`` from a QTimer and receives
callbacks. Actual work happens in a ``Segment`` (device + encoder + capture session)
created by a ``RecordingBackend``; tests inject a fake backend.

Device loss (driver reset/TDR, GPU removed) finalizes the current file and continues
in a new segment file on a freshly created device, up to ``MAX_RESTARTS`` times.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol

from screenrecorder.core.clock import RecordingClock
from screenrecorder.core.models import RecordingStats, SourceSpec, VideoSettings

log = logging.getLogger(__name__)


class RecordingState(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    RECORDING = "recording"
    PAUSED = "paused"
    STOPPING = "stopping"
    ERROR = "error"


TRANSITIONS: dict[RecordingState, set[RecordingState]] = {
    RecordingState.IDLE: {RecordingState.STARTING},
    RecordingState.STARTING: {RecordingState.RECORDING, RecordingState.ERROR},
    RecordingState.RECORDING: {RecordingState.PAUSED, RecordingState.STOPPING, RecordingState.ERROR},
    RecordingState.PAUSED: {RecordingState.RECORDING, RecordingState.STOPPING, RecordingState.ERROR},
    RecordingState.STOPPING: {RecordingState.IDLE, RecordingState.ERROR},
    RecordingState.ERROR: {RecordingState.IDLE, RecordingState.STARTING},
}


class InvalidTransition(RuntimeError):
    pass


class RecordingError(RuntimeError):
    pass


@dataclass
class RecordingRequest:
    source: SourceSpec
    settings: VideoSettings
    output_path: Path
    max_frames: int = 0  # 0 = until stopped (used by self-test / benchmarks)


@dataclass
class RecordingResult:
    paths: list[Path] = field(default_factory=list)
    duration: float = 0.0
    frames: int = 0
    encoder: dict = field(default_factory=dict)
    stats: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str = ""


class Segment(Protocol):
    output_path: Path

    def start(self) -> None: ...
    def pause(self) -> None: ...
    def resume(self) -> None: ...
    def stop(self) -> None: ...
    def stats(self) -> dict: ...
    def finalize(self) -> Path | None: ...
    def encoder_info(self) -> dict: ...


class RecordingBackend(Protocol):
    def open_segment(self, request: RecordingRequest, index: int) -> Segment: ...


def segment_path(base: Path, index: int) -> Path:
    if index == 0:
        return base
    return base.with_name(f"{base.stem}_parte{index + 1}{base.suffix}")


class RecordingManager:
    MAX_RESTARTS = 3

    def __init__(
        self,
        backend: RecordingBackend,
        on_state: Callable[[RecordingState], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_finished: Callable[[RecordingResult], None] | None = None,
        on_notice: Callable[[str], None] | None = None,
        clock: RecordingClock | None = None,
    ) -> None:
        self._backend = backend
        self._on_state = on_state
        self._on_error = on_error
        self._on_finished = on_finished
        self._on_notice = on_notice
        self._clock = clock or RecordingClock()
        self._state = RecordingState.IDLE
        self._request: RecordingRequest | None = None
        self._segment: Segment | None = None
        self._segment_index = 0
        self._restarts = 0
        self._result = RecordingResult()
        self._frames_before_segment = 0

    # -- state ---------------------------------------------------------------------------

    @property
    def state(self) -> RecordingState:
        return self._state

    @property
    def is_active(self) -> bool:
        return self._state in (RecordingState.RECORDING, RecordingState.PAUSED)

    def elapsed(self) -> float:
        return self._clock.elapsed()

    @property
    def last_result(self) -> RecordingResult:
        return self._result

    def current_encoder_info(self) -> dict:
        return self._segment.encoder_info() if self._segment is not None else {}

    def _transition(self, target: RecordingState) -> None:
        if target not in TRANSITIONS[self._state]:
            raise InvalidTransition(f"{self._state.value} -> {target.value}")
        self._state = target
        if self._on_state:
            self._on_state(target)

    # -- commands ------------------------------------------------------------------------

    def start(self, request: RecordingRequest) -> None:
        self._transition(RecordingState.STARTING)
        self._request = request
        self._segment_index = 0
        self._restarts = 0
        self._frames_before_segment = 0
        self._result = RecordingResult()
        try:
            self._segment = self._open_and_start(0)
        except Exception as exc:
            self._segment = None
            self._result.error = str(exc)
            self._transition(RecordingState.ERROR)
            raise RecordingError(str(exc)) from exc
        self._clock.start()
        self._transition(RecordingState.RECORDING)

    def pause(self) -> None:
        if self._state != RecordingState.RECORDING:
            raise InvalidTransition(f"cannot pause while {self._state.value}")
        assert self._segment is not None
        self._segment.pause()
        self._clock.pause()
        self._transition(RecordingState.PAUSED)

    def resume(self) -> None:
        if self._state != RecordingState.PAUSED:
            raise InvalidTransition(f"cannot resume while {self._state.value}")
        assert self._segment is not None
        self._segment.resume()
        self._clock.resume()
        self._transition(RecordingState.RECORDING)

    def toggle_pause(self) -> None:
        if self._state == RecordingState.RECORDING:
            self.pause()
        elif self._state == RecordingState.PAUSED:
            self.resume()

    def stop(self) -> RecordingResult:
        if not self.is_active:
            raise InvalidTransition(f"cannot stop while {self._state.value}")
        self._transition(RecordingState.STOPPING)
        self._clock.stop()
        try:
            self._close_segment()
        except Exception as exc:
            self._result.error = str(exc)
            self._result.duration = self._clock.elapsed()
            self._transition(RecordingState.ERROR)
            if self._on_error:
                self._on_error(str(exc))
            raise RecordingError(str(exc)) from exc
        self._result.duration = self._clock.elapsed()
        self._transition(RecordingState.IDLE)
        if self._on_finished:
            self._on_finished(self._result)
        return self._result

    def reset(self) -> None:
        if self._state == RecordingState.ERROR:
            self._transition(RecordingState.IDLE)

    def shutdown(self) -> None:
        """Best-effort safe finalization on application exit."""
        if self.is_active:
            try:
                self.stop()
            except Exception as exc:  # noqa: BLE001
                log.error("Stop during shutdown failed: %s", exc)

    # -- polling -------------------------------------------------------------------------

    def poll(self) -> RecordingStats:
        """Called periodically by the UI. Detects pipeline termination and reacts."""
        if self._segment is None:
            return RecordingStats(state=self._state.value)
        raw = self._segment.stats()
        stats = self._to_stats(raw)
        if self.is_active and raw.get("state") in ("error", "finished"):
            self._handle_segment_end(raw)
            stats.state = self._state.value
        return stats

    def _to_stats(self, raw: dict) -> RecordingStats:
        return RecordingStats(
            frames_written=self._frames_before_segment + int(raw.get("frames_written", 0)),
            frames_captured=int(raw.get("frames_captured", 0)),
            frames_duplicated=int(raw.get("frames_duplicated", 0)),
            frames_dropped=int(raw.get("frames_dropped", 0)),
            avg_frame_ms=float(raw.get("avg_tick_ms", 0.0)),
            max_frame_ms=float(raw.get("max_tick_ms", 0.0)),
            recorded_seconds=float(raw.get("recorded_seconds", 0.0)),
            state=self._state.value,
            error_kind=str(raw.get("error_kind", "")),
            error=str(raw.get("error", "")),
            extra={"source_width": raw.get("source_width", 0), "source_height": raw.get("source_height", 0)},
        )

    def _handle_segment_end(self, raw: dict) -> None:
        kind = raw.get("error_kind", "")
        message = raw.get("error", "")
        if raw.get("state") == "finished" or kind == "source_closed":
            if kind == "source_closed":
                self._result.warnings.append("La fuente de captura se cerró; grabación finalizada.")
            self.stop()
            return
        if kind == "device_lost" and self._restarts < self.MAX_RESTARTS:
            self._restarts += 1
            notice = f"Dispositivo GPU perdido ({message}). Continuando en un nuevo archivo."
            self._result.warnings.append(notice)
            log.warning(notice)
            try:
                self._close_segment()
            except Exception as exc:  # noqa: BLE001 - the old device is gone; keep going
                self._result.warnings.append(f"No se pudo finalizar el segmento anterior: {exc}")
            try:
                self._segment_index += 1
                self._segment = self._open_and_start(self._segment_index)
                if self._state == RecordingState.PAUSED:
                    self._segment.pause()
                if self._on_notice:
                    self._on_notice(notice)
                return
            except Exception as exc:  # noqa: BLE001
                message = f"{message}; reinicio fallido: {exc}"
        self._fail(message or "Error desconocido en la captura")

    def _fail(self, message: str) -> None:
        try:
            self._close_segment()
        except Exception as exc:  # noqa: BLE001
            self._result.warnings.append(f"Finalize falló: {exc}")
        self._clock.stop()
        self._result.duration = self._clock.elapsed()
        self._result.error = message
        self._transition(RecordingState.ERROR)
        if self._on_error:
            self._on_error(message)

    # -- segments ------------------------------------------------------------------------

    def _open_and_start(self, index: int) -> Segment:
        assert self._request is not None
        segment = self._backend.open_segment(self._request, index)
        try:
            segment.start()
        except Exception:
            try:
                segment.finalize()
            except Exception:  # noqa: BLE001
                pass
            raise
        return segment

    def _close_segment(self) -> None:
        segment = self._segment
        if segment is None:
            return
        self._segment = None
        try:
            segment.stop()
        finally:
            raw = segment.stats()
            self._result.stats = raw
            self._frames_before_segment += int(raw.get("frames_written", 0))
            self._result.frames = self._frames_before_segment
            self._result.encoder = segment.encoder_info()
            path = segment.finalize()
            if path is not None:
                self._result.paths.append(path)
