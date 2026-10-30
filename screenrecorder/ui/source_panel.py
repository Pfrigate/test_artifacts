"""Source selection: monitor, window or custom region."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from screenrecorder.core.capture_manager import CaptureManager
from screenrecorder.core.models import MonitorInfo, Rect, SourceSpec, WindowInfo
from screenrecorder.ui.common import THUMB_SIZE
from screenrecorder.ui.region_selector import RegionSelector, screen_for_monitor

_ROLE = Qt.ItemDataRole.UserRole


class SourcePanel(QGroupBox):
    source_changed = Signal()

    def __init__(self, capture_manager: CaptureManager, parent: QWidget | None = None) -> None:
        super().__init__("Fuente de captura", parent)
        self.cm = capture_manager
        self._monitors: list[MonitorInfo] = []
        self._region: Rect | None = None
        self._selector: RegionSelector | None = None

        self.radio_monitor = QRadioButton("Pantalla completa")
        self.radio_window = QRadioButton("Ventana")
        self.radio_region = QRadioButton("Región")
        self.radio_monitor.setChecked(True)
        self.kind_group = QButtonGroup(self)
        for i, radio in enumerate((self.radio_monitor, self.radio_window, self.radio_region)):
            self.kind_group.addButton(radio, i)

        self.monitor_list = QListWidget()
        self.monitor_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.monitor_list.setIconSize(THUMB_SIZE)
        self.monitor_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.monitor_list.setMovement(QListWidget.Movement.Static)
        self.monitor_list.setWordWrap(True)

        self.window_list = QListWidget()
        self.window_list.setAlternatingRowColors(True)

        region_page = QWidget()
        self.region_monitor = QComboBox()
        self.region_button = QPushButton("Seleccionar región…")
        self.region_label = QLabel("Ninguna región seleccionada")
        self.region_label.setWordWrap(True)
        rl = QVBoxLayout(region_page)
        rl.addWidget(QLabel("Monitor:"))
        rl.addWidget(self.region_monitor)
        rl.addWidget(self.region_button)
        rl.addWidget(self.region_label)
        rl.addStretch(1)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.monitor_list)
        self.stack.addWidget(self.window_list)
        self.stack.addWidget(region_page)

        self.refresh_button = QPushButton("Actualizar")
        top = QHBoxLayout()
        top.addWidget(self.radio_monitor)
        top.addWidget(self.radio_window)
        top.addWidget(self.radio_region)
        top.addStretch(1)
        top.addWidget(self.refresh_button)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.stack, 1)

        self.kind_group.idToggled.connect(self._on_kind)
        self.refresh_button.clicked.connect(self.refresh)
        self.region_button.clicked.connect(self._select_region)
        self.monitor_list.currentRowChanged.connect(lambda _row: self.source_changed.emit())
        self.window_list.currentRowChanged.connect(lambda _row: self.source_changed.emit())
        self.region_monitor.currentIndexChanged.connect(self._on_region_monitor)
        self.refresh()

    # -- population ---------------------------------------------------------------------

    def refresh(self) -> None:
        self._monitors = self.cm.monitors()
        self.monitor_list.clear()
        self.region_monitor.blockSignals(True)
        self.region_monitor.clear()
        for monitor in self._monitors:
            item = QListWidgetItem(self._monitor_icon(monitor), monitor.label)
            item.setData(_ROLE, monitor.index)
            self.monitor_list.addItem(item)
            self.region_monitor.addItem(monitor.label, monitor.index)
        self.region_monitor.blockSignals(False)
        if self._monitors:
            self.monitor_list.setCurrentRow(0)

        selected = self._selected_window_handle()
        self.window_list.clear()
        for window in self.cm.windows():
            item = QListWidgetItem(window.label)
            item.setData(_ROLE, window)
            item.setToolTip(f"{window.title}\n{window.rect.width}x{window.rect.height}")
            self.window_list.addItem(item)
            if window.handle == selected:
                self.window_list.setCurrentItem(item)
        self.source_changed.emit()

    @staticmethod
    def _monitor_icon(monitor: MonitorInfo) -> QIcon:
        screen = screen_for_monitor(monitor)
        if screen is None:
            return QIcon()
        pixmap = screen.grabWindow(0)
        if pixmap.isNull():
            return QIcon()
        return QIcon(pixmap.scaled(THUMB_SIZE, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def _selected_window_handle(self) -> int:
        item = self.window_list.currentItem()
        return item.data(_ROLE).handle if item is not None else 0

    # -- interaction --------------------------------------------------------------------

    def _on_kind(self, button_id: int, checked: bool) -> None:
        if checked:
            self.stack.setCurrentIndex(button_id)
            if button_id == 1:
                self.refresh_windows_only()
            self.source_changed.emit()

    def refresh_windows_only(self) -> None:
        selected = self._selected_window_handle()
        self.window_list.clear()
        for window in self.cm.windows():
            item = QListWidgetItem(window.label)
            item.setData(_ROLE, window)
            self.window_list.addItem(item)
            if window.handle == selected:
                self.window_list.setCurrentItem(item)

    def _on_region_monitor(self, _index: int) -> None:
        self._region = None
        self.region_label.setText("Ninguna región seleccionada")
        self.source_changed.emit()

    def _region_monitor(self) -> MonitorInfo | None:
        index = self.region_monitor.currentData()
        for monitor in self._monitors:
            if monitor.index == index:
                return monitor
        return None

    def _select_region(self) -> None:
        monitor = self._region_monitor()
        if monitor is None:
            return
        window = self.window()
        window.hide()
        QGuiApplication.processEvents()
        self._selector = RegionSelector(monitor)

        def done(region: Rect) -> None:
            self._region = region
            self.region_label.setText(f"x={region.x}, y={region.y}, {region.width}x{region.height} px")
            window.show()
            self.source_changed.emit()

        self._selector.region_selected.connect(done)
        self._selector.cancelled.connect(window.show)
        self._selector.start()

    # -- result -------------------------------------------------------------------------

    def current_spec(self) -> SourceSpec | None:
        kind = self.kind_group.checkedId()
        if kind == 0:
            item = self.monitor_list.currentItem()
            if item is None:
                return None
            index = item.data(_ROLE)
            monitor = next((m for m in self._monitors if m.index == index), None)
            return self.cm.spec_for_monitor(monitor) if monitor else None
        if kind == 1:
            item = self.window_list.currentItem()
            if item is None:
                return None
            window: WindowInfo = item.data(_ROLE)
            return self.cm.spec_for_window(window)
        monitor = self._region_monitor()
        if monitor is None or self._region is None:
            return None
        return self.cm.spec_for_region(monitor, self._region)
