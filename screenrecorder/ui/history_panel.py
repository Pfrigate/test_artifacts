"""Recording history with first-frame previews decoded by Media Foundation (no QtMultimedia,
which would pull in Qt's FFmpeg backend)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QImage, QPixmap
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from screenrecorder.core.history import HistoryEntry, HistoryStore
from screenrecorder.native import native
from screenrecorder.ui.common import THUMB_SIZE, bgra_to_qimage, run_async
from screenrecorder.winapi import win32

_ROLE = Qt.ItemDataRole.UserRole


class HistoryPanel(QGroupBox):
    def __init__(self, store: HistoryStore, parent: QWidget | None = None) -> None:
        super().__init__("Historial", parent)
        self.store = store
        self._workers = []
        self._thumbs: dict[str, QImage] = {}

        self.list = QListWidget()
        self.list.setIconSize(THUMB_SIZE)
        self.preview = QLabel("Selecciona una grabación")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(320, 180)
        self.preview.setStyleSheet("background:#111; color:#aaa; border-radius:6px;")
        self.details = QLabel()
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.play = QPushButton("Reproducir")
        self.reveal = QPushButton("Mostrar en carpeta")
        self.remove = QPushButton("Quitar")
        self.delete = QPushButton("Eliminar archivo")
        buttons = QHBoxLayout()
        for b in (self.play, self.reveal, self.remove, self.delete):
            buttons.addWidget(b)

        right = QVBoxLayout()
        right.addWidget(self.preview, 1)
        right.addWidget(self.details)
        right.addLayout(buttons)

        layout = QHBoxLayout(self)
        layout.addWidget(self.list, 1)
        layout.addLayout(right, 1)

        self.list.currentItemChanged.connect(self._on_select)
        self.list.itemDoubleClicked.connect(lambda _i: self._play())
        self.play.clicked.connect(self._play)
        self.reveal.clicked.connect(self._reveal)
        self.remove.clicked.connect(self._remove)
        self.delete.clicked.connect(self._delete)
        self.refresh()

    def refresh(self) -> None:
        self.store.prune_missing()
        self.list.clear()
        for entry in self.store.entries():
            item = QListWidgetItem(f"{entry.title}\n{entry.summary()}")
            item.setData(_ROLE, entry)
            if entry.path in self._thumbs:
                item.setIcon(QIcon(QPixmap.fromImage(self._thumbs[entry.path])))
            self.list.addItem(item)
            self._load_thumbnail(entry)
        if self.list.count():
            self.list.setCurrentRow(0)
        else:
            self.preview.setText("Aún no hay grabaciones")
            self.details.clear()

    def _load_thumbnail(self, entry: HistoryEntry) -> None:
        if native is None or entry.path in self._thumbs:
            return
        path = entry.path

        def decode():
            data, w, h = native.thumbnail(path, min(0.5, entry.duration / 2))
            return path, bgra_to_qimage(data, w, h)

        self._workers.append(run_async(decode, self._thumbnail_ready, lambda _err: None))

    def _thumbnail_ready(self, result) -> None:
        path, image = result
        self._thumbs[path] = image
        for row in range(self.list.count()):
            item = self.list.item(row)
            if item.data(_ROLE).path == path:
                item.setIcon(QIcon(QPixmap.fromImage(image)))
                if item is self.list.currentItem():
                    self._show_preview(path)

    def _current(self) -> HistoryEntry | None:
        item = self.list.currentItem()
        return item.data(_ROLE) if item is not None else None

    def _on_select(self, *_args) -> None:
        entry = self._current()
        if entry is None:
            return
        self._show_preview(entry.path)
        self.details.setText(
            f"<b>{entry.title}</b><br>{entry.summary()}<br>Codificador: {entry.encoder}<br>"
            f"Fuente: {entry.source}<br>{entry.created}"
        )

    def _show_preview(self, path: str) -> None:
        image = self._thumbs.get(path)
        if image is None:
            self.preview.setText("Generando vista previa…")
            return
        self.preview.setPixmap(
            QPixmap.fromImage(image).scaled(
                self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
        )

    def _play(self) -> None:
        entry = self._current()
        if entry is not None:
            win32.open_with_default_app(Path(entry.path))

    def _reveal(self) -> None:
        entry = self._current()
        if entry is not None:
            win32.reveal_in_explorer(Path(entry.path))

    def _remove(self) -> None:
        entry = self._current()
        if entry is not None:
            self.store.remove(entry.path)
            self.refresh()

    def _delete(self) -> None:
        entry = self._current()
        if entry is None:
            return
        answer = QMessageBox.question(self, "Eliminar", f"¿Eliminar definitivamente {entry.title}?")
        if answer == QMessageBox.StandardButton.Yes:
            Path(entry.path).unlink(missing_ok=True)
            self._thumbs.pop(entry.path, None)
            self.store.remove(entry.path)
            self.refresh()
