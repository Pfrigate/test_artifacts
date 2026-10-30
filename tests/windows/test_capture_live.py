"""Real screen capture through WGC and DXcam. Needs an interactive desktop: CI runners
without one report these as skipped (with the reason) instead of failing."""

import time

import pytest

from screenrecorder.core.capture_manager import CaptureError, CaptureManager, DxcamSession, NativePipelineSession
from screenrecorder.core.encoder import MediaFoundationEncoder
from screenrecorder.core.frame_processor import output_size
from screenrecorder.core.models import Rect
from screenrecorder.core.selftest import verify_mp4
from screenrecorder.native import native

pytestmark = pytest.mark.capture


@pytest.fixture(scope="module")
def monitor():
    monitors = CaptureManager().monitors()
    if not monitors:
        pytest.skip("no monitors enumerated (headless session)")
    return monitors[0]


def _record(session, seconds):
    try:
        session.start()
    except (native.NativeError, CaptureError) as exc:
        pytest.skip(f"capture unavailable in this session: {exc}")
    time.sleep(seconds)
    stats = session.stats()
    session.stop()
    return stats


@pytest.mark.parametrize("region", [False, True])
def test_wgc_monitor(device, output_dir, monitor, region):
    if not CaptureManager.wgc_available():
        pytest.skip("Windows Graphics Capture not supported")
    cm = CaptureManager()
    spec = cm.spec_for_region(monitor, Rect(0, 0, 800, 600)) if region else cm.spec_for_monitor(monitor)
    w, h = output_size("720p", *spec.capture_size)
    enc = MediaFoundationEncoder(device, output_dir / f"wgc_{'region' if region else 'monitor'}.mp4", w, h, 30, 4_000_000)
    stats = _record(NativePipelineSession(device, enc, spec, cursor=True, border=False), 1.5)
    path = enc.finalize()
    if stats["frames_written"] == 0:
        pytest.skip(f"WGC delivered no frames: {stats}")
    assert stats["state"] in ("running", "finished"), stats
    assert verify_mp4(path, w, h, 30)["ok"]


def test_dxcam_monitor(output_dir, monitor):
    if not CaptureManager.dxcam_available():
        pytest.skip("dxcam not installed")
    device = native.Device()
    enc = MediaFoundationEncoder(device, output_dir / "dxcam_monitor.mp4", 1280, 720, 30, 4_000_000)
    dev_idx, out_idx = CaptureManager.dxcam_indices(monitor.handle)
    try:
        session = DxcamSession(enc, 30, dev_idx, out_idx, None)
    except Exception as exc:  # noqa: BLE001 - dxcam raises on import without a desktop
        pytest.skip(f"dxcam unavailable: {exc}")
    stats = _record(session, 1.5)
    path = enc.finalize()
    if stats["frames_written"] == 0:
        pytest.skip(f"Desktop Duplication delivered no frames: {stats}")
    assert verify_mp4(path, 1280, 720, 30)["ok"]
