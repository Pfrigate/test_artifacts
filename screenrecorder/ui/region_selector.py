"""Full-screen translucent overlay to drag-select a capture region on one monitor."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QKeyEvent, QMouseEvent, QPainter, QPaintEvent, QPen, QScreen
from PySide6.QtWidgets import QWidget

from screenrecorder.core.models import MonitorInfo, Rect


def screen_for_monitor(monitor: MonitorInfo) -> QScreen | None:
    screens = QGuiApplication.screens()
    for screen in screens:
        if screen.name() == monitor.device_name:
            return screen
    # Fallback: match physical size (logical size x devicePixelRatio).
    for screen in screens:
        g = screen.geometry()
        dpr = screen.devicePixelRatio()
        if (round(g.width() * dpr), round(g.height() * dpr)) == (monitor.rect.width, monitor.rect.height):
            return screen
    return QGuiApplication.primaryScreen()


class RegionSelector(QWidget):
    """Emits the selection in *physical pixels relative to the monitor's top-left*, which is
    the coordinate space WGC/DXcam frames use."""

    region_selected = Signal(Rect)
    cancelled = Signal()

    def __init__(self, monitor: MonitorInfo) -> None:
        super().__init__(None)
        self.monitor = monitor
        self.screen_ = screen_for_monitor(monitor)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        self._origin: QPoint | None = None
        self._current: QPoint | None = None
        if self.screen_ is not None:
            self.setGeometry(self.screen_.geometry())

    def start(self) -> None:
        self.show()
        if self.screen_ is not None:
            self.windowHandle().setScreen(self.screen_)
            self.setGeometry(self.screen_.geometry())
        self.showFullScreen()
        self.activateWindow()
        self.raise_()

    def _selection(self) -> QRect:
        if self._origin is None or self._current is None:
            return QRect()
        return QRect(self._origin, self._current).normalized()

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 110))
        sel = self._selection()
        if not sel.isEmpty():
            # Alpha 1 instead of 0: fully transparent pixels of a layered window are
            # click-through on Windows, which would break dragging inside the selection.
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            painter.fillRect(sel, QColor(0, 0, 0, 1))
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            painter.setPen(QPen(QColor("#ef4444"), 2))
            painter.drawRect(sel)
            phys = self._to_physical(sel)
            painter.setPen(QColor("white"))
            painter.drawText(sel.topLeft() + QPoint(6, -6), f"{phys.width} x {phys.height}")
        painter.setPen(QColor("white"))
        painter.drawText(
            self.rect().adjusted(0, 24, 0, 0),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            "Arrastra para seleccionar la región · Enter/soltar para confirmar · Esc para cancelar",
        )

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._origin = event.position().toPoint()
            self._current = self._origin
            self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._origin is not None:
            self._current = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._origin is not None:
            self._current = event.position().toPoint()
            self._confirm()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
            self.close()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._confirm()

    def _to_physical(self, logical: QRect) -> Rect:
        dpr = self.screen_.devicePixelRatio() if self.screen_ is not None else 1.0
        x = int(round(logical.x() * dpr)) & ~1
        y = int(round(logical.y() * dpr)) & ~1
        w = max(2, int(round(logical.width() * dpr)) & ~1)
        h = max(2, int(round(logical.height() * dpr)) & ~1)
        return Rect(x, y, w, h)

    def _confirm(self) -> None:
        sel = self._selection()
        if sel.width() < 16 or sel.height() < 16:
            return
        self.region_selected.emit(self._to_physical(sel))
        self.close()
