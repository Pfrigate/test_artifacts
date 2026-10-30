"""Small Qt helpers shared by the widgets."""

from __future__ import annotations

import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPixmap


class _WorkerSignals(QObject):
    done = Signal(object)
    failed = Signal(str)


class Worker(QRunnable):
    """Runs ``fn`` on the global thread pool; results come back through Qt signals so
    slow native calls (encoder detection, thumbnail decode) never block the UI."""

    def __init__(self, fn: Callable[[], Any]) -> None:
        super().__init__()
        self.fn = fn
        self.signals = _WorkerSignals()

    def run(self) -> None:  # noqa: D401 - QRunnable API
        try:
            result = self.fn()
        except Exception as exc:  # noqa: BLE001
            self.signals.failed.emit(f"{exc}\n{traceback.format_exc()}")
            return
        self.signals.done.emit(result)


def run_async(fn: Callable[[], Any], on_done: Callable[[Any], None], on_failed: Callable[[str], None] | None = None) -> Worker:
    worker = Worker(fn)
    worker.signals.done.connect(on_done)
    if on_failed is not None:
        worker.signals.failed.connect(on_failed)
    QThreadPool.globalInstance().start(worker)
    return worker


def dot_icon(color: str, size: int = 64, square: bool = False) -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(color))
    painter.setPen(Qt.PenStyle.NoPen)
    margin = size // 8
    if square:
        painter.drawRoundedRect(margin, margin, size - 2 * margin, size - 2 * margin, size // 10, size // 10)
    else:
        painter.drawEllipse(margin, margin, size - 2 * margin, size - 2 * margin)
    painter.end()
    return QIcon(pixmap)


def app_icon() -> QIcon:
    size = 256
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#1f2937"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(8, 40, size - 16, size - 80, 28, 28)
    painter.setBrush(QColor("#ef4444"))
    painter.drawEllipse(size // 2 - 56, size // 2 - 56, 112, 112)
    painter.end()
    return QIcon(pixmap)


def bgra_to_qimage(data: bytes, width: int, height: int) -> QImage:
    # Format_ARGB32 is 0xAARRGGBB per pixel = B,G,R,A bytes on little-endian Windows.
    return QImage(data, width, height, width * 4, QImage.Format.Format_ARGB32).copy()


THUMB_SIZE = QSize(192, 108)
