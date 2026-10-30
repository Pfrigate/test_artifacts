"""Main window: wires the UserInterface to RecordingManager."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QFont, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from screenrecorder import APP_NAME, __version__
from screenrecorder.core.capture_manager import CaptureManager
from screenrecorder.core.clock import format_duration
from screenrecorder.core.encoder import EncoderCapabilities, detect_capabilities
from screenrecorder.core.history import HistoryStore
from screenrecorder.core.recording_backend import NativeRecordingBackend
from screenrecorder.core.recording_manager import (
    RecordingError,
    RecordingManager,
    RecordingRequest,
    RecordingResult,
    RecordingState,
)
from screenrecorder.core.settings import AppSettings, app_data_dir
from screenrecorder.native import NATIVE_IMPORT_ERROR, native
from screenrecorder.ui.common import app_icon, dot_icon, run_async
from screenrecorder.ui.history_panel import HistoryPanel
from screenrecorder.ui.settings_panel import SettingsPanel
from screenrecorder.ui.source_panel import SourcePanel
from screenrecorder.winapi import win32

log = logging.getLogger(__name__)

HOTKEY_TOGGLE = 1  # F9: start / stop
HOTKEY_PAUSE = 2  # F10: pause / resume

STATE_TEXT = {
    RecordingState.IDLE: "Listo",
    RecordingState.STARTING: "Iniciando…",
    RecordingState.RECORDING: "Grabando",
    RecordingState.PAUSED: "En pausa",
    RecordingState.STOPPING: "Finalizando MP4…",
    RecordingState.ERROR: "Error",
}


class MainWindow(QMainWindow):
    def __init__(self, settings: AppSettings | None = None) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.setWindowIcon(app_icon())
        self.resize(1100, 760)

        self.settings = settings or AppSettings.load()
        self.capture_manager = CaptureManager()
        self.history = HistoryStore(app_data_dir() / "history.json")
        self.manager = RecordingManager(
            NativeRecordingBackend(self.capture_manager),
            on_state=self._on_state,
            on_error=self._on_error,
            on_finished=self._on_finished,
            on_notice=self._on_notice,
        )
        self._current_source_label = ""
        self._hotkeys_registered = False
        self._blink = False

        # -- widgets ------------------------------------------------------------------
        self.source_panel = SourcePanel(self.capture_manager)
        self.settings_panel = SettingsPanel()
        self.settings_panel.set_values(
            self.settings.video,
            self.settings.output_dir,
            self.settings.hide_window_from_capture,
            self.settings.minimize_on_record,
        )
        self.history_panel = HistoryPanel(self.history)

        self.record_button = QPushButton("  Grabar (F9)")
        self.record_button.setIcon(dot_icon("#ef4444"))
        self.record_button.setMinimumHeight(48)
        self.pause_button = QPushButton("Pausar (F10)")
        self.pause_button.setMinimumHeight(48)
        self.pause_button.setEnabled(False)

        self.indicator = QLabel()
        self.indicator.setFixedSize(18, 18)
        self.timer_label = QLabel("00:00:00")
        font = QFont()
        font.setPointSize(26)
        font.setBold(True)
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.timer_label.setFont(font)
        self.state_label = QLabel(STATE_TEXT[RecordingState.IDLE])
        self.stats_label = QLabel()
        self.stats_label.setWordWrap(True)
        self.stats_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        controls = QHBoxLayout()
        controls.addWidget(self.record_button, 2)
        controls.addWidget(self.pause_button, 1)
        controls.addSpacing(16)
        controls.addWidget(self.indicator)
        controls.addWidget(self.timer_label)
        controls.addWidget(self.state_label, 1)

        top = QSplitter(Qt.Orientation.Horizontal)
        top.addWidget(self.source_panel)
        top.addWidget(self.settings_panel)
        top.setStretchFactor(0, 3)
        top.setStretchFactor(1, 2)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(top, 3)
        layout.addLayout(controls)
        layout.addWidget(self.stats_label)
        layout.addWidget(self.history_panel, 2)
        self.setCentralWidget(central)
        self._set_indicator(False)

        # -- tray ---------------------------------------------------------------------
        self.tray = QSystemTrayIcon(app_icon(), self)
        tray_menu = QMenu()
        self.tray_record = QAction("Grabar", self, triggered=self.toggle_recording)
        self.tray_pause = QAction("Pausar", self, triggered=self.toggle_pause, enabled=False)
        tray_menu.addAction(self.tray_record)
        tray_menu.addAction(self.tray_pause)
        tray_menu.addSeparator()
        tray_menu.addAction(QAction("Mostrar", self, triggered=self._show_from_tray))
        tray_menu.addAction(QAction("Salir", self, triggered=self.close))
        self.tray.setContextMenu(tray_menu)
        self.tray.setToolTip(APP_NAME)
        self.tray.activated.connect(lambda reason: self._show_from_tray() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

        # -- timers & signals -----------------------------------------------------------
        self.poll_timer = QTimer(self, interval=250, timeout=self._poll)
        self.blink_timer = QTimer(self, interval=500, timeout=self._toggle_blink)
        self.record_button.clicked.connect(self.toggle_recording)
        self.pause_button.clicked.connect(self.toggle_pause)
        self.source_panel.source_changed.connect(self._update_plan)
        self.settings_panel.changed.connect(self._settings_changed)

        self._detect_capabilities()
        self._update_plan()
        if native is None:
            self.stats_label.setText(f"<b>Extensión nativa no disponible:</b> {NATIVE_IMPORT_ERROR}")
            self.record_button.setEnabled(False)

    # -- startup ------------------------------------------------------------------------

    def _detect_capabilities(self) -> None:
        self.settings_panel.set_wgc_capabilities(self.capture_manager.wgc_capabilities())
        self._caps_worker = run_async(
            lambda: detect_capabilities(True),
            self.settings_panel.set_capabilities,
            lambda err: self.settings_panel.set_capabilities(EncoderCapabilities(error=err.splitlines()[0])),
        )

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        hwnd = int(self.winId())
        if not self._hotkeys_registered:
            ok1 = win32.register_hotkey(hwnd, HOTKEY_TOGGLE, 0, win32.VK_F9)
            ok2 = win32.register_hotkey(hwnd, HOTKEY_PAUSE, 0, win32.VK_F10)
            self._hotkeys_registered = ok1 or ok2
        win32.set_exclude_from_capture(hwnd, self.settings_panel.hide_self.isChecked())

    def nativeEvent(self, event_type, message):  # noqa: N802 - Qt API
        if bytes(event_type) == b"windows_generic_MSG":
            hotkey = win32.hotkey_id_from_message(int(message))
            if hotkey == HOTKEY_TOGGLE:
                self.toggle_recording()
                return True, 0
            if hotkey == HOTKEY_PAUSE:
                self.toggle_pause()
                return True, 0
        return super().nativeEvent(event_type, message)

    # -- settings -----------------------------------------------------------------------

    def _settings_changed(self) -> None:
        self._update_plan()
        win32.set_exclude_from_capture(int(self.winId()), self.settings_panel.hide_self.isChecked())

    def _update_plan(self) -> None:
        spec = self.source_panel.current_spec()
        size = spec.capture_size if spec is not None else (1920, 1080)
        self.settings_panel.update_plan(size)

    def _collect_settings(self) -> AppSettings:
        self.settings.video = self.settings_panel.video_settings()
        self.settings.output_dir = self.settings_panel.output_dir.text().strip() or self.settings.output_dir
        self.settings.hide_window_from_capture = self.settings_panel.hide_self.isChecked()
        self.settings.minimize_on_record = self.settings_panel.minimize.isChecked()
        return self.settings

    # -- recording ----------------------------------------------------------------------

    def toggle_recording(self) -> None:
        if self.manager.is_active:
            self.stop_recording()
        elif self.manager.state in (RecordingState.IDLE, RecordingState.ERROR):
            self.start_recording()

    def start_recording(self) -> None:
        spec = self.source_panel.current_spec()
        if spec is None:
            QMessageBox.warning(self, APP_NAME, "Selecciona una pantalla, ventana o región para grabar.")
            return
        settings = self._collect_settings()
        try:
            settings.save()
        except OSError as exc:
            log.warning("Could not save settings: %s", exc)
        request = RecordingRequest(source=spec, settings=settings.video, output_path=settings.output_path())
        self._current_source_label = spec.description
        self.manager.reset()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.manager.start(request)
        except RecordingError as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, APP_NAME, f"No se pudo iniciar la grabación:\n{exc}")
            return
        QApplication.restoreOverrideCursor()
        self.poll_timer.start()
        if settings.minimize_on_record:
            self.showMinimized()

    def stop_recording(self) -> None:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.manager.stop()
        except RecordingError as exc:
            QMessageBox.critical(self, APP_NAME, f"Error al finalizar el archivo:\n{exc}")
        finally:
            QApplication.restoreOverrideCursor()

    def toggle_pause(self) -> None:
        if self.manager.is_active:
            self.manager.toggle_pause()

    # -- manager callbacks ----------------------------------------------------------------

    def _on_state(self, state: RecordingState) -> None:
        self.state_label.setText(STATE_TEXT[state])
        active = state in (RecordingState.RECORDING, RecordingState.PAUSED)
        self.record_button.setText("  Detener (F9)" if active else "  Grabar (F9)")
        self.record_button.setIcon(dot_icon("#e5e7eb", square=True) if active else dot_icon("#ef4444"))
        self.pause_button.setEnabled(active)
        self.pause_button.setText("Reanudar (F10)" if state == RecordingState.PAUSED else "Pausar (F10)")
        self.tray_record.setText("Detener" if active else "Grabar")
        self.tray_pause.setEnabled(active)
        self.tray_pause.setText("Reanudar" if state == RecordingState.PAUSED else "Pausar")
        self.source_panel.setEnabled(not active)
        self.settings_panel.setEnabled(not active)
        if state == RecordingState.RECORDING:
            self.blink_timer.start()
        else:
            self.blink_timer.stop()
            self._set_indicator(state == RecordingState.PAUSED, color="#f59e0b")
        if not active:
            self.poll_timer.stop()
        self.tray.setToolTip(f"{APP_NAME} — {STATE_TEXT[state]}")

    def _on_error(self, message: str) -> None:
        self.poll_timer.stop()
        self.showNormal()
        QMessageBox.critical(self, APP_NAME, f"La grabación se detuvo por un error:\n{message}")
        self._record_history(self.manager.last_result)

    def _on_notice(self, message: str) -> None:
        self.tray.showMessage(APP_NAME, message, QSystemTrayIcon.MessageIcon.Warning)

    def _on_finished(self, result: RecordingResult) -> None:
        self._record_history(result)
        if result.paths:
            names = ", ".join(p.name for p in result.paths)
            self.tray.showMessage(APP_NAME, f"Grabación guardada: {names}", QSystemTrayIcon.MessageIcon.Information)
            self.stats_label.setText(
                f"Guardado en {result.paths[0].parent} · {result.frames} fotogramas · "
                f"{result.duration:.1f} s · {result.encoder.get('encoder_name', '')}"
                + ("<br>" + "<br>".join(result.warnings) if result.warnings else "")
            )
        else:
            self.stats_label.setText("No se grabó ningún fotograma." + " ".join(result.warnings))
        if self.isMinimized():
            self.showNormal()

    def _record_history(self, result: RecordingResult) -> None:
        if not result.paths:
            return
        per_file = result.duration / len(result.paths)
        for path in result.paths:
            self.history.add(HistoryStore.make_entry(Path(path), result.encoder, per_file, self._current_source_label))
        self.history_panel.refresh()

    # -- periodic UI update ---------------------------------------------------------------

    def _poll(self) -> None:
        stats = self.manager.poll()
        self.timer_label.setText(format_duration(self.manager.elapsed()))
        if self.manager.is_active:
            enc = ""
            info = self.manager.current_encoder_info()
            if info:
                enc = (
                    f"{info['width']}x{info['height']}@{info['fps']} · {info['encoder_name']}"
                    f" ({'HW' if info['hardware'] else 'SW'}) · {info['backend'].upper()}"
                    f" · {info['input_subtype']} via {info['converter']}"
                )
            self.stats_label.setText(
                f"{enc}<br>Fotogramas: {stats.frames_written} · nuevos {stats.frames_captured}"
                f" · repetidos {stats.frames_duplicated} · descartados {stats.frames_dropped}"
                f" · {stats.avg_frame_ms:.2f} ms/fotograma (máx {stats.max_frame_ms:.1f})"
            )

    def _toggle_blink(self) -> None:
        self._blink = not self._blink
        self._set_indicator(self._blink)

    def _set_indicator(self, on: bool, color: str = "#ef4444") -> None:
        fill = color if on else "transparent"
        self.indicator.setStyleSheet(f"border-radius:9px; background:{fill}; border:2px solid {color};")

    # -- window lifecycle ---------------------------------------------------------------

    def _show_from_tray(self) -> None:
        self.showNormal()
        self.activateWindow()
        self.raise_()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.manager.is_active:
            answer = QMessageBox.question(
                self, APP_NAME, "Hay una grabación en curso. ¿Detenerla y guardar el archivo antes de salir?"
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.stop_recording()
        self.manager.shutdown()
        try:
            self._collect_settings().save()
        except OSError:
            pass
        hwnd = int(self.winId())
        win32.unregister_hotkey(hwnd, HOTKEY_TOGGLE)
        win32.unregister_hotkey(hwnd, HOTKEY_PAUSE)
        self.tray.hide()
        super().closeEvent(event)
        QApplication.quit()


def run_gui(argv: list[str]) -> int:
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setWindowIcon(app_icon())
    app.setQuitOnLastWindowClosed(False)
    window = MainWindow()
    window.show()
    return app.exec()
