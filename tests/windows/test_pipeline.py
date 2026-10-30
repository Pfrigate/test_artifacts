"""Native capture pipeline (CFR pacing thread) and the RecordingManager end to end."""

import time

import pytest

from screenrecorder.core.capture_manager import CaptureManager, NativePipelineSession
from screenrecorder.core.encoder import MediaFoundationEncoder
from screenrecorder.core.selftest import run_selftest, verify_mp4
from screenrecorder.native import native


def _wait_finished(session, timeout):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if session.stats()["state"] in ("finished", "error"):
            return session.stats()
        time.sleep(0.02)
    raise AssertionError(f"pipeline did not finish: {session.stats()}")


def test_paced_pipeline_runs_in_real_time(device, output_dir):
    enc = MediaFoundationEncoder(device, output_dir / "paced_720p30.mp4", 1280, 720, 30, 4_000_000)
    spec = CaptureManager.spec_for_synthetic(1280, 720)
    session = NativePipelineSession(device, enc, spec, cursor=False, border=False, max_frames=45)
    t0 = time.perf_counter()
    session.start()
    stats = _wait_finished(session, 15)
    elapsed = time.perf_counter() - t0
    session.stop()
    assert stats["state"] == "finished", stats
    assert stats["frames_written"] == 45
    assert elapsed >= 1.4  # paced at 30 fps, not as fast as possible
    report = verify_mp4(enc.finalize(), 1280, 720, 30)
    assert report["ok"], report["problems"]
    # Dropped slots (slow CI runner) leave gaps, never compress time.
    assert report["probe"]["last_pts"] == pytest.approx((44 + stats["frames_dropped"]) / 30, abs=0.04)


def test_pause_removes_paused_time(device, output_dir):
    enc = MediaFoundationEncoder(device, output_dir / "pause_resume.mp4", 1280, 720, 30, 4_000_000)
    session = NativePipelineSession(device, enc, CaptureManager.spec_for_synthetic(1280, 720), False, False)
    session.start()
    time.sleep(1.0)
    session.pause()
    time.sleep(0.2)
    assert session.stats()["state"] == "paused"
    time.sleep(1.0)
    session.resume()
    time.sleep(1.0)
    session.stop()
    report = verify_mp4(enc.finalize(), 1280, 720, 30)
    assert report["ok"], report["problems"]
    assert 1.6 <= report["probe"]["duration"] <= 2.5, report["probe"]


def test_selftest_end_to_end(output_dir):
    report = run_selftest(output_dir / "selftest", seconds=1.5, resolution="1080p", fps=60)
    print(report)
    assert report["ok"], report.get("problems")
    assert report["avc_profile"] in (77, 100)


def test_wgc_support_queries():
    cm = CaptureManager()
    assert isinstance(cm.wgc_available(), bool)
    assert isinstance(native.wgc_supported(), bool)
    caps = cm.wgc_capabilities()
    assert set(caps) == {"cursor_toggle", "border_toggle"}
