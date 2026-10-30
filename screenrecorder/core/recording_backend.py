"""Concrete RecordingBackend wiring CaptureManager, FrameProcessor and
MediaFoundationEncoder together for one output file (a "segment")."""

from __future__ import annotations

import logging
from pathlib import Path

from screenrecorder.core.capture_manager import CaptureManager, CaptureSession
from screenrecorder.core.encoder import MediaFoundationEncoder
from screenrecorder.core.frame_processor import FramePlan, build_plan
from screenrecorder.core.models import Backend, SourceKind
from screenrecorder.core.recording_manager import RecordingRequest, segment_path
from screenrecorder.native import require_native

log = logging.getLogger(__name__)


class NativeSegment:
    def __init__(
        self,
        device,
        encoder: MediaFoundationEncoder,
        session: CaptureSession,
        backend: Backend,
        plan: FramePlan,
    ) -> None:
        self.device = device
        self.encoder = encoder
        self.session = session
        self.backend = backend
        self.plan = plan
        self.output_path: Path = encoder.final_path

    def start(self) -> None:
        self.session.start()

    def pause(self) -> None:
        self.session.pause()

    def resume(self) -> None:
        self.session.resume()

    def stop(self) -> None:
        self.session.stop()

    def stats(self) -> dict:
        return self.session.stats()

    def finalize(self) -> Path | None:
        return self.encoder.finalize()

    def encoder_info(self) -> dict:
        info = self.encoder.info()
        info.update(
            backend=self.backend.value,
            adapter=self.device.adapter_name,
            gpu_hardware=self.device.is_hardware,
            data_path=self.plan.data_path,
            level=self.plan.level,
        )
        return info


class NativeRecordingBackend:
    def __init__(self, capture_manager: CaptureManager | None = None, converter: str = "auto") -> None:
        self.capture_manager = capture_manager or CaptureManager()
        self.converter = converter

    def open_segment(self, request: RecordingRequest, index: int) -> NativeSegment:
        native = require_native()
        cm = self.capture_manager
        spec = cm.refresh(request.source)
        settings = request.settings
        backend = cm.resolve_backend(settings.backend, spec.kind)

        adapter = -1
        if spec.kind != SourceKind.SYNTHETIC:
            adapter = cm.adapter_for_monitor(cm.monitor_handle_for(spec))
        device = native.Device(adapter)

        crop = spec.region if spec.kind == SourceKind.REGION else None
        plan = build_plan(
            spec.size,
            crop,
            settings.resolution,
            settings.fps,
            settings.quality,
            settings.aspect_mode,
            backend.value,
            device.video_processor_available,
        )
        log.info("Segment %d: %s via %s, plan %s", index, spec.description, backend.value, plan)
        width, height = plan.output_size
        encoder = MediaFoundationEncoder(
            device,
            segment_path(request.output_path, index),
            width,
            height,
            settings.fps,
            plan.bitrate,
            encoder_preference=settings.encoder,
            converter=self.converter,
        )
        try:
            session = cm.create_session(
                spec,
                backend,
                device,
                encoder,
                settings.fps,
                cursor=settings.cursor,
                border=settings.border,
                max_frames=request.max_frames,
            )
        except Exception:
            encoder.abort()
            raise
        return NativeSegment(device, encoder, session, backend, plan)
