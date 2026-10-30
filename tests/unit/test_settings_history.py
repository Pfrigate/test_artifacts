from datetime import datetime

from screenrecorder.core.history import HistoryEntry, HistoryStore
from screenrecorder.core.settings import AppSettings


def test_settings_roundtrip(tmp_path):
    path = tmp_path / "settings.json"
    s = AppSettings()
    s.video.fps = 60
    s.video.resolution = "2160p"
    s.output_dir = str(tmp_path / "videos")
    s.save(path)
    loaded = AppSettings.load(path)
    assert loaded.video.fps == 60 and loaded.video.resolution == "2160p"
    assert loaded.output_dir == str(tmp_path / "videos")


def test_settings_tolerates_garbage(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    assert AppSettings.load(path).video.fps == 30
    path.write_text('{"video": {"fps": 15, "unknown": 1}, "bogus": true}', encoding="utf-8")
    assert AppSettings.load(path).video.fps == 15


def test_output_path_pattern(tmp_path):
    s = AppSettings(output_dir=str(tmp_path))
    p = s.output_path(datetime(2026, 1, 2, 3, 4, 5))
    assert p == tmp_path / "Grabacion_2026-01-02_03-04-05.mp4"


def test_history(tmp_path):
    store = HistoryStore(tmp_path / "history.json")
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(b"x")
    b.write_bytes(b"y")
    store.add(HistoryStore.make_entry(a, {"width": 1920, "height": 1080, "fps": 30}, 2.0, "Monitor 1"))
    store.add(HistoryStore.make_entry(b, {"width": 1280, "height": 720, "fps": 60, "hardware": True}, 1.0, "Ventana"))
    store.add(HistoryEntry(path=str(a), created="2026-01-01T00:00:00"))  # re-adding moves to top
    assert [e.path for e in HistoryStore(tmp_path / "history.json").entries()] == [str(a), str(b)]
    b.unlink()
    assert store.prune_missing() == 1
    assert [e.path for e in store.entries()] == [str(a)]
