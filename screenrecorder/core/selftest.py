"""Headless end-to-end check: synthetic GPU frames -> converter -> Media Foundation H.264
-> MP4, then verification with the MF Source Reader and the MP4 box parser."""

from __future__ import annotations

import time
from pathlib import Path

from screenrecorder.core.capture_manager import CaptureManager
from screenrecorder.core.models import VideoSettings
from screenrecorder.core.mp4info import read_mp4_info
from screenrecorder.core.recording_backend import NativeRecordingBackend
from screenrecorder.core.recording_manager import RecordingManager, RecordingRequest, RecordingState
from screenrecorder.native import require_native


def verify_mp4(path: Path, width: int, height: int, fps: int, frames: int | None = None) -> dict:
    native = require_native()
    probe = dict(native.probe(str(path)))
    info = read_mp4_info(path)
    problems = list(info.powerpoint_issues())
    if probe["codec"] != "h264":
        problems.append(f"codec {probe['codec']}")
    if (probe["width"], probe["height"]) != (width, height):
        problems.append(f"size {probe['width']}x{probe['height']} != {width}x{height}")
    if abs(probe["fps"] - fps) > 0.01:
        problems.append(f"fps {probe['fps']} != {fps}")
    if probe["has_audio"] or info.has_audio:
        problems.append("unexpected audio track")
    if not probe["timestamps_unique"]:
        problems.append("duplicate timestamps")
    if frames is not None and probe["frame_count"] != frames:
        problems.append(f"frame_count {probe['frame_count']} != {frames}")
    if not info.moov_before_mdat:
        problems.append("moov not before mdat")
    return {
        "path": str(path),
        "probe": probe,
        "avc_profile": info.video_tracks[0].avc_profile if info.video_tracks else 0,
        "avc_level": info.video_tracks[0].avc_level if info.video_tracks else 0,
        "moov_before_mdat": info.moov_before_mdat,
        "problems": problems,
        "ok": not problems,
    }


def run_selftest(output_dir: Path, seconds: float = 2.0, resolution: str = "720p", fps: int = 30) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    cm = CaptureManager()
    width, height = {"720p": (1280, 720), "1080p": (1920, 1080), "2160p": (3840, 2160)}[resolution]
    frames = int(seconds * fps)
    finished = []
    manager = RecordingManager(NativeRecordingBackend(cm), on_finished=finished.append)
    request = RecordingRequest(
        source=cm.spec_for_synthetic(width, height),
        settings=VideoSettings(resolution=resolution, fps=fps, backend="synthetic"),
        output_path=output_dir / f"selftest_{resolution}_{fps}.mp4",
        max_frames=frames,
    )
    started = time.perf_counter()
    manager.start(request)
    encoder_info = manager.current_encoder_info()
    deadline = started + seconds * 4 + 30
    while manager.state != RecordingState.IDLE and time.perf_counter() < deadline:
        manager.poll()
        if manager.state == RecordingState.ERROR:
            break
        time.sleep(0.05)
    if manager.is_active:
        manager.stop()
    result = manager.last_result
    report = {"encoder": encoder_info or result.encoder, "error": result.error, "warnings": result.warnings}
    if not result.paths:
        report.update(ok=False, problems=[result.error or "no output file"])
        return report
    report.update(verify_mp4(result.paths[0], width, height, fps, frames))
    return report
