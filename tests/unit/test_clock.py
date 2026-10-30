import pytest

from screenrecorder.core.clock import RecordingClock, format_duration, frame_duration, frame_timestamp


@pytest.mark.parametrize("fps", [15, 30, 60])
def test_frame_timestamps_monotonic_and_exact(fps):
    stamps = [frame_timestamp(i, fps) for i in range(fps * 600 + 1)]
    assert all(b > a for a, b in zip(stamps, stamps[1:]))
    assert stamps[fps] == 10_000_000  # exactly one second after `fps` frames
    assert stamps[-1] == 600 * 10_000_000  # no drift after 10 minutes
    durations = {frame_duration(i, fps) for i in range(fps)}
    assert max(durations) - min(durations) <= 1


class FakeTime:
    def __init__(self):
        self.ns = 0

    def __call__(self):
        return self.ns

    def advance(self, seconds):
        self.ns += int(seconds * 1e9)


def test_clock_excludes_pauses():
    t = FakeTime()
    clock = RecordingClock(t)
    clock.start()
    t.advance(2)
    clock.pause()
    t.advance(5)
    assert clock.elapsed() == pytest.approx(2)
    clock.resume()
    t.advance(1.5)
    assert clock.elapsed() == pytest.approx(3.5)
    clock.stop()
    t.advance(10)
    assert clock.elapsed() == pytest.approx(3.5)


def test_clock_stop_while_paused():
    t = FakeTime()
    clock = RecordingClock(t)
    clock.start()
    t.advance(1)
    clock.pause()
    t.advance(3)
    clock.stop()
    assert clock.elapsed() == pytest.approx(1)


def test_format_duration():
    assert format_duration(0) == "00:00:00"
    assert format_duration(3725.9) == "01:02:05"
