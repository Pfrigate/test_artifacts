"""Constructs the main window offscreen to catch wiring errors without a display."""

import os

import pytest

pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def test_main_window_builds(qapp, tmp_path, monkeypatch):
    from screenrecorder.core.settings import AppSettings
    from screenrecorder.ui.main_window import MainWindow

    settings = AppSettings(output_dir=str(tmp_path / "out"))
    window = MainWindow(settings)
    window.show()
    qapp.processEvents()
    video = window.settings_panel.video_settings()
    assert video.fps in (15, 30, 60)
    assert "Salida:" in window.settings_panel.plan_label.text()
    from PySide6.QtCore import QThreadPool

    QThreadPool.globalInstance().waitForDone(60_000)  # background encoder detection
    qapp.processEvents()
    window.manager.shutdown()
    window.tray.hide()
    window.hide()


def test_region_selector_maps_to_physical(qapp):
    from PySide6.QtCore import QRect

    from screenrecorder.core.models import MonitorInfo, Rect
    from screenrecorder.ui.region_selector import RegionSelector

    monitor = MonitorInfo(handle=1, device_name="none", rect=Rect(0, 0, 1920, 1080), primary=True, index=0)
    selector = RegionSelector(monitor)
    dpr = selector.screen_.devicePixelRatio() if selector.screen_ else 1.0
    r = selector._to_physical(QRect(11, 21, 301, 201))
    assert r.x % 2 == 0 and r.y % 2 == 0 and r.width % 2 == 0 and r.height % 2 == 0
    assert abs(r.width - 301 * dpr) <= 2
    selector.close()
