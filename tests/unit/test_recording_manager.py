from pathlib import Path

import pytest

from screenrecorder.core.models import SourceKind, SourceSpec, VideoSettings
from screenrecorder.core.recording_manager import (
    InvalidTransition,
    RecordingError,
    RecordingManager,
    RecordingRequest,
    RecordingState,
    segment_path,
)


class FakeSegment:
    def __init__(self, path: Path, fail_start=False):
        self.output_path = path
        self.fail_start = fail_start
        self.calls = []
        self.raw = {"state": "running", "frames_written": 0, "error_kind": "", "error": ""}

    def start(self):
        self.calls.append("start")
        if self.fail_start:
            raise RuntimeError("no capture")

    def pause(self):
        self.calls.append("pause")
        self.raw["state"] = "paused"

    def resume(self):
        self.calls.append("resume")
        self.raw["state"] = "running"

    def stop(self):
        self.calls.append("stop")
        if self.raw["state"] != "error":
            self.raw["state"] = "finished"

    def stats(self):
        return dict(self.raw)

    def finalize(self):
        self.calls.append("finalize")
        return self.output_path if self.raw["frames_written"] else None

    def encoder_info(self):
        return {"encoder_name": "fake", "hardware": False, "width": 1280, "height": 720, "fps": 30}


class FakeBackend:
    def __init__(self, fail_start=False):
        self.segments: list[FakeSegment] = []
        self.fail_start = fail_start
        self.fail_reopen = False

    def open_segment(self, request, index):
        if index > 0 and self.fail_reopen:
            raise RuntimeError("no GPU")
        seg = FakeSegment(segment_path(request.output_path, index), self.fail_start)
        self.segments.append(seg)
        return seg


def make_request(tmp_path):
    return RecordingRequest(
        source=SourceSpec(kind=SourceKind.SYNTHETIC, size=(1280, 720)),
        settings=VideoSettings(),
        output_path=tmp_path / "out.mp4",
    )


def test_full_lifecycle(tmp_path):
    states = []
    finished = []
    backend = FakeBackend()
    m = RecordingManager(backend, on_state=states.append, on_finished=finished.append)
    m.start(make_request(tmp_path))
    seg = backend.segments[0]
    seg.raw["frames_written"] = 10
    m.pause()
    m.resume()
    result = m.stop()
    assert states == [
        RecordingState.STARTING,
        RecordingState.RECORDING,
        RecordingState.PAUSED,
        RecordingState.RECORDING,
        RecordingState.STOPPING,
        RecordingState.IDLE,
    ]
    assert seg.calls == ["start", "pause", "resume", "stop", "finalize"]
    assert result.paths == [tmp_path / "out.mp4"] and result.frames == 10
    assert finished == [result]


def test_invalid_transitions(tmp_path):
    m = RecordingManager(FakeBackend())
    with pytest.raises(InvalidTransition):
        m.pause()
    with pytest.raises(InvalidTransition):
        m.stop()
    m.start(make_request(tmp_path))
    with pytest.raises(InvalidTransition):
        m.start(make_request(tmp_path))
    with pytest.raises(InvalidTransition):
        m.resume()


def test_start_failure_goes_to_error_and_can_retry(tmp_path):
    backend = FakeBackend(fail_start=True)
    m = RecordingManager(backend)
    with pytest.raises(RecordingError):
        m.start(make_request(tmp_path))
    assert m.state == RecordingState.ERROR
    assert backend.segments[0].calls == ["start", "finalize"]
    backend.fail_start = False
    m.start(make_request(tmp_path))
    assert m.state == RecordingState.RECORDING


def test_device_lost_continues_in_new_segment(tmp_path):
    notices = []
    backend = FakeBackend()
    m = RecordingManager(backend, on_notice=notices.append)
    m.start(make_request(tmp_path))
    first = backend.segments[0]
    first.raw.update(state="error", error_kind="device_lost", error="DXGI_ERROR_DEVICE_REMOVED", frames_written=30)
    m.poll()
    assert m.state == RecordingState.RECORDING
    assert len(backend.segments) == 2 and notices
    second = backend.segments[1]
    assert second.output_path == tmp_path / "out_parte2.mp4"
    second.raw["frames_written"] = 20
    assert m.poll().frames_written == 50
    result = m.stop()
    assert result.paths == [tmp_path / "out.mp4", tmp_path / "out_parte2.mp4"]
    assert result.frames == 50


def test_device_lost_restart_failure_is_error(tmp_path):
    errors = []
    backend = FakeBackend()
    m = RecordingManager(backend, on_error=errors.append)
    m.start(make_request(tmp_path))
    backend.fail_reopen = True
    backend.segments[0].raw.update(state="error", error_kind="device_lost", error="lost", frames_written=5)
    m.poll()
    assert m.state == RecordingState.ERROR
    assert errors and "reinicio fallido" in errors[0]
    assert m.last_result.paths == [tmp_path / "out.mp4"]  # what was recorded is kept


def test_source_closed_stops_cleanly(tmp_path):
    finished = []
    backend = FakeBackend()
    m = RecordingManager(backend, on_finished=finished.append)
    m.start(make_request(tmp_path))
    backend.segments[0].raw.update(state="error", error_kind="source_closed", error="closed", frames_written=3)
    m.poll()
    assert m.state == RecordingState.IDLE
    assert finished and finished[0].warnings


def test_generic_error(tmp_path):
    errors = []
    backend = FakeBackend()
    m = RecordingManager(backend, on_error=errors.append)
    m.start(make_request(tmp_path))
    backend.segments[0].raw.update(state="error", error_kind="error", error="boom")
    m.poll()
    assert m.state == RecordingState.ERROR and errors == ["boom"]
    m.reset()
    assert m.state == RecordingState.IDLE


def test_max_frames_finish(tmp_path):
    backend = FakeBackend()
    m = RecordingManager(backend)
    m.start(make_request(tmp_path))
    backend.segments[0].raw.update(state="finished", frames_written=60)
    m.poll()
    assert m.state == RecordingState.IDLE and m.last_result.frames == 60


def test_segment_path():
    assert segment_path(Path("a/b.mp4"), 0) == Path("a/b.mp4")
    assert segment_path(Path("a/b.mp4"), 2) == Path("a/b_parte3.mp4")
