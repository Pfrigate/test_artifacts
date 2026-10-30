"""CaptureManager: enumerates capture sources and creates capture sessions.

Backends:
* ``wgc``  – Windows Graphics Capture (monitor, window, region). Frames stay on the GPU:
  WGC texture -> D3D11 copy -> video processor -> NV12 -> hardware H.264. Driven by the
  native ``CapturePipeline`` thread with high-resolution CFR pacing.
* ``dxcam`` – Desktop Duplication via DXcam (monitor, region). DXcam returns numpy
  arrays in system memory, so each frame is uploaded once to the GPU and then follows the
  same conversion/encoding path. Used as fallback when WGC is unavailable.
* ``synthetic`` – GPU-generated test pattern (self-test, CI, benchmarks).
"""

from __future__ import annotations

import importlib.util
import logging
import threading
import time
from dataclasses import replace
from typing import Protocol

from screenrecorder.core.clock import HNS_PER_SECOND
from screenrecorder.core.models import Backend, MonitorInfo, Rect, SourceKind, SourceSpec, WindowInfo
from screenrecorder.native import native, require_native
from screenrecorder.winapi import win32

log = logging.getLogger(__name__)


class CaptureSession(Protocol):
    def start(self) -> None: ...
    def pause(self) -> None: ...
    def resume(self) -> None: ...
    def stop(self) -> None: ...
    def stats(self) -> dict: ...


class CaptureError(RuntimeError):
    pass


# ---------------------------------------------------------------------------------------
# Sessions


class NativePipelineSession:
    """WGC (or synthetic) capture running entirely in the native pipeline thread."""

    def __init__(self, device, encoder, spec: SourceSpec, cursor: bool, border: bool, max_frames: int = 0):
        n = require_native()
        if spec.kind == SourceKind.SYNTHETIC:
            kind = "synthetic"
        elif spec.kind == SourceKind.WINDOW:
            kind = "window"
        else:
            kind = "monitor"
        crop = spec.region.as_tuple() if spec.kind == SourceKind.REGION and spec.region else None
        synthetic_size = spec.size if spec.kind == SourceKind.SYNTHETIC and spec.size[0] > 0 else None
        self._pipeline = n.CapturePipeline(
            device,
            encoder.native,
            kind,
            spec.handle,
            crop=crop,
            cursor=cursor,
            border=border,
            synthetic_size=synthetic_size,
            max_frames=max_frames,
        )

    def start(self) -> None:
        self._pipeline.start()

    def pause(self) -> None:
        self._pipeline.pause()

    def resume(self) -> None:
        self._pipeline.resume()

    def stop(self) -> None:
        self._pipeline.stop()

    def stats(self) -> dict:
        return dict(self._pipeline.stats())


class DxcamSession:
    """Desktop Duplication capture through DXcam on a Python worker thread.

    DXcam's ``video_mode`` repeats the last frame at ``target_fps``; we still derive each
    frame index from the QPC clock so the MP4 timeline follows wall-clock time even when a
    frame is late (late slots are skipped, never compressed)."""

    def __init__(
        self,
        encoder,
        fps: int,
        device_idx: int,
        output_idx: int,
        region: tuple[int, int, int, int] | None,
        max_frames: int = 0,
    ) -> None:
        import dxcam  # imported lazily: it initialises DXGI on import

        self._dxcam = dxcam
        self._encoder = encoder
        self._fps = fps
        self._device_idx = device_idx
        self._output_idx = output_idx
        self._region = region  # (left, top, right, bottom) relative to the output
        self._max_frames = max_frames
        self._camera = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._resumed = threading.Event()
        self._lock = threading.Lock()
        self._stats = {
            "state": "idle",
            "error_kind": "",
            "error": "",
            "frames_written": 0,
            "frames_captured": 0,
            "frames_duplicated": 0,
            "frames_dropped": 0,
            "avg_tick_ms": 0.0,
            "max_tick_ms": 0.0,
            "recorded_seconds": 0.0,
            "source_width": 0,
            "source_height": 0,
        }

    def start(self) -> None:
        try:
            # backend="dxgi": Desktop Duplication. processor_backend="numpy": dxcam's default
            # processor is OpenCV, which this project must not depend on.
            self._camera = self._dxcam.create(
                device_idx=self._device_idx,
                output_idx=self._output_idx,
                output_color="BGRA",
                max_buffer_len=8,
                backend="dxgi",
                processor_backend="numpy",
            )
            if self._camera is None:
                raise CaptureError("DXcam could not create a camera for this output")
            self._camera.start(region=self._region, target_fps=self._fps, video_mode=True)
        except Exception as exc:
            raise CaptureError(f"DXcam start failed: {exc}") from exc
        self._set(state="running")
        self._thread = threading.Thread(target=self._run, name="dxcam-capture", daemon=True)
        self._thread.start()

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._resumed.set()
        self._paused.clear()

    def stop(self) -> None:
        self._stop.set()
        if self._camera is not None:
            try:
                self._camera.stop()
            except Exception as exc:  # noqa: BLE001
                log.debug("dxcam stop: %s", exc)
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._camera is not None:
            try:
                self._camera.release()
            except Exception:  # noqa: BLE001
                pass
            self._camera = None

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def _set(self, **values) -> None:
        with self._lock:
            self._stats.update(values)

    def _run(self) -> None:
        from screenrecorder.native import DEVICE_LOST_HRESULTS, hresult_of

        fps = self._fps
        base_ns = time.perf_counter_ns()
        base_index = 0
        last_index = -1
        total_ms = 0.0
        written = 0
        try:
            while not self._stop.is_set():
                frame = self._camera.get_latest_frame()
                if self._stop.is_set():
                    break
                if frame is None:
                    continue
                if self._paused.is_set():
                    self._set(state="paused")
                    continue
                if self._resumed.is_set():
                    # Remove the paused interval from the timeline.
                    self._resumed.clear()
                    base_ns = time.perf_counter_ns()
                    base_index = last_index + 1
                    self._set(state="running")
                elapsed_hns = (time.perf_counter_ns() - base_ns) // 100
                index = base_index + elapsed_hns * fps // HNS_PER_SECOND
                if index <= last_index:
                    continue  # early duplicate from video_mode; slot already filled
                dropped = index - last_index - 1 if last_index >= 0 else 0
                t0 = time.perf_counter()
                self._encoder.write_bgra(frame, index)
                tick_ms = (time.perf_counter() - t0) * 1000.0
                total_ms += tick_ms
                written += 1
                last_index = index
                with self._lock:
                    s = self._stats
                    s["frames_written"] = written
                    s["frames_captured"] = written
                    s["frames_dropped"] += max(0, dropped)
                    s["avg_tick_ms"] = total_ms / written
                    s["max_tick_ms"] = max(s["max_tick_ms"], tick_ms)
                    s["recorded_seconds"] = (last_index + 1) / fps
                    s["source_height"], s["source_width"] = frame.shape[0], frame.shape[1]
                if self._max_frames and written >= self._max_frames:
                    break
        except Exception as exc:  # noqa: BLE001 - surfaced through stats
            hr = hresult_of(exc)
            kind = "device_lost" if hr in DEVICE_LOST_HRESULTS else "error"
            self._set(state="error", error_kind=kind, error=str(exc))
            return
        self._set(state="finished")


# ---------------------------------------------------------------------------------------
# Manager


class CaptureManager:
    def monitors(self) -> list[MonitorInfo]:
        return win32.list_monitors()

    def windows(self) -> list[WindowInfo]:
        return win32.list_windows()

    @staticmethod
    def wgc_available() -> bool:
        """Checked through PyWinRT (GraphicsCaptureSession.IsSupported); falls back to the
        native module when the PyWinRT projection is not installed."""
        try:
            from winrt.windows.graphics.capture import GraphicsCaptureSession

            return bool(GraphicsCaptureSession.is_supported())
        except ImportError:
            return bool(native is not None and native.wgc_supported())
        except Exception as exc:  # noqa: BLE001
            log.warning("WGC support check failed: %s", exc)
            return False

    @staticmethod
    def wgc_capabilities() -> dict[str, bool]:
        caps = {"cursor_toggle": False, "border_toggle": False}
        try:
            from winrt.windows.foundation.metadata import ApiInformation

            cls = "Windows.Graphics.Capture.GraphicsCaptureSession"
            caps["cursor_toggle"] = bool(ApiInformation.is_property_present(cls, "IsCursorCaptureEnabled"))
            caps["border_toggle"] = bool(ApiInformation.is_property_present(cls, "IsBorderRequired"))
        except Exception as exc:  # noqa: BLE001
            log.debug("ApiInformation unavailable: %s", exc)
        return caps

    @staticmethod
    def dxcam_available() -> bool:
        return win32.IS_WINDOWS and importlib.util.find_spec("dxcam") is not None

    def resolve_backend(self, preference: str, kind: SourceKind) -> Backend:
        if kind == SourceKind.SYNTHETIC:
            return Backend.SYNTHETIC
        wgc = self.wgc_available()
        if preference == Backend.DXCAM.value:
            if kind == SourceKind.WINDOW:
                raise CaptureError("DXcam (Desktop Duplication) cannot capture individual windows; use WGC")
            if not self.dxcam_available():
                raise CaptureError("DXcam is not installed")
            return Backend.DXCAM
        if preference == Backend.WGC.value and not wgc:
            raise CaptureError("Windows Graphics Capture is not supported on this system")
        if wgc:
            return Backend.WGC
        if kind != SourceKind.WINDOW and self.dxcam_available():
            return Backend.DXCAM
        raise CaptureError("No capture backend available for this source")

    # -- source specs -------------------------------------------------------------------

    @staticmethod
    def spec_for_monitor(monitor: MonitorInfo) -> SourceSpec:
        return SourceSpec(
            kind=SourceKind.MONITOR,
            handle=monitor.handle,
            size=(monitor.rect.width, monitor.rect.height),
            monitor_index=monitor.index,
            description=monitor.label,
        )

    @staticmethod
    def spec_for_window(window: WindowInfo) -> SourceSpec:
        return SourceSpec(
            kind=SourceKind.WINDOW,
            handle=window.handle,
            size=(window.rect.width, window.rect.height),
            description=window.title,
        )

    @staticmethod
    def spec_for_region(monitor: MonitorInfo, region: Rect) -> SourceSpec:
        bounded = region.intersected(Rect(0, 0, monitor.rect.width, monitor.rect.height))
        bounded = Rect(bounded.x & ~1, bounded.y & ~1, max(2, bounded.width & ~1), max(2, bounded.height & ~1))
        return SourceSpec(
            kind=SourceKind.REGION,
            handle=monitor.handle,
            size=(monitor.rect.width, monitor.rect.height),
            region=bounded,
            monitor_index=monitor.index,
            description=f"Región {bounded.width}x{bounded.height} en {monitor.label}",
        )

    @staticmethod
    def spec_for_synthetic(width: int, height: int) -> SourceSpec:
        return SourceSpec(kind=SourceKind.SYNTHETIC, size=(width, height), description="Patrón sintético")

    def refresh(self, spec: SourceSpec) -> SourceSpec:
        """Re-read the live size of a window source (it may have been resized)."""
        if spec.kind == SourceKind.WINDOW:
            if not win32.is_window(spec.handle):
                raise CaptureError("The selected window no longer exists")
            rect = win32.window_rect(spec.handle)
            if rect is not None:
                return replace(spec, size=(rect.width, rect.height))
        return spec

    # -- adapters -----------------------------------------------------------------------

    @staticmethod
    def adapter_for_monitor(hmonitor: int) -> int:
        """DXGI adapter index driving ``hmonitor`` (-1 = default adapter). Capturing and
        encoding on that adapter avoids cross-adapter copies on hybrid-GPU laptops."""
        if native is None or not hmonitor:
            return -1
        try:
            for adapter in native.enumerate_adapters():
                if any(o["hmonitor"] == hmonitor for o in adapter["outputs"]):
                    return int(adapter["index"])
        except Exception as exc:  # noqa: BLE001
            log.debug("enumerate_adapters failed: %s", exc)
        return -1

    @staticmethod
    def dxcam_indices(hmonitor: int) -> tuple[int, int]:
        """DXcam numbers devices counting only adapters that have outputs."""
        if native is None:
            return (0, 0)
        device_idx = 0
        for adapter in native.enumerate_adapters():
            outputs = adapter["outputs"]
            if not outputs:
                continue
            for output_idx, output in enumerate(outputs):
                if output["hmonitor"] == hmonitor:
                    return (device_idx, output_idx)
            device_idx += 1
        return (0, 0)

    def monitor_handle_for(self, spec: SourceSpec) -> int:
        if spec.kind == SourceKind.WINDOW and win32.IS_WINDOWS:
            return int(win32.user32.MonitorFromWindow(spec.handle, 2) or 0)  # MONITOR_DEFAULTTONEAREST
        return spec.handle

    # -- sessions -----------------------------------------------------------------------

    def create_session(
        self,
        spec: SourceSpec,
        backend: Backend,
        device,
        encoder,
        fps: int,
        cursor: bool = True,
        border: bool = False,
        max_frames: int = 0,
    ) -> CaptureSession:
        if backend in (Backend.WGC, Backend.SYNTHETIC):
            return NativePipelineSession(device, encoder, spec, cursor, border, max_frames)
        if backend == Backend.DXCAM:
            device_idx, output_idx = self.dxcam_indices(spec.handle)
            region = None
            if spec.kind == SourceKind.REGION and spec.region is not None:
                r = spec.region
                region = (r.x, r.y, r.right, r.bottom)
            return DxcamSession(encoder, fps, device_idx, output_idx, region, max_frames)
        raise CaptureError(f"Unsupported backend {backend}")
