"""Video configuration: resolution, frame rate, quality, encoder and capture backend."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QStandardItemModel
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QWidget,
)

from screenrecorder.core.encoder import EncoderCapabilities
from screenrecorder.core.frame_processor import RESOLUTION_PRESETS, build_plan
from screenrecorder.core.models import VideoSettings

RESOLUTIONS = [("720p (1280x720)", "720p"), ("1080p (1920x1080)", "1080p"), ("4K (3840x2160)", "2160p"), ("Nativa de la fuente", "native")]
FPS = [("15 fps", 15), ("30 fps", 30), ("60 fps", 60)]
QUALITY = [("Baja", "low"), ("Media", "medium"), ("Alta", "high")]
ENCODERS = [("Automático (hardware si está disponible)", "auto"), ("Hardware (GPU)", "hardware"), ("Software (CPU)", "software")]
BACKENDS = [("Automático (WGC → DXcam)", "auto"), ("Windows Graphics Capture", "wgc"), ("DXcam (Desktop Duplication)", "dxcam")]
ASPECT = [("Lienzo 16:9 con barras negras (PowerPoint)", "letterbox"), ("Mantener proporción de la fuente", "source")]


def _fill(combo: QComboBox, items: list[tuple[str, object]]) -> None:
    for label, value in items:
        combo.addItem(label, value)


def _select(combo: QComboBox, value: object) -> None:
    index = combo.findData(value)
    if index >= 0:
        combo.setCurrentIndex(index)


class SettingsPanel(QGroupBox):
    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Configuración de vídeo", parent)
        self.resolution = QComboBox()
        self.fps = QComboBox()
        self.quality = QComboBox()
        self.encoder = QComboBox()
        self.backend = QComboBox()
        self.aspect = QComboBox()
        _fill(self.resolution, RESOLUTIONS)
        _fill(self.fps, FPS)
        _fill(self.quality, QUALITY)
        _fill(self.encoder, ENCODERS)
        _fill(self.backend, BACKENDS)
        _fill(self.aspect, ASPECT)
        self.cursor = QCheckBox("Capturar cursor")
        self.border = QCheckBox("Mostrar borde de captura (WGC)")
        self.output_dir = QLineEdit()
        self.browse = QPushButton("…")
        self.browse.setFixedWidth(32)
        self.hide_self = QCheckBox("Ocultar esta ventana en la grabación")
        self.minimize = QCheckBox("Minimizar al empezar a grabar")
        self.plan_label = QLabel()
        self.plan_label.setWordWrap(True)
        self.caps_label = QLabel("Detectando codificadores…")
        self.caps_label.setWordWrap(True)

        out_row = QHBoxLayout()
        out_row.addWidget(self.output_dir, 1)
        out_row.addWidget(self.browse)

        form = QFormLayout(self)
        form.addRow("Resolución", self.resolution)
        form.addRow("Fotogramas", self.fps)
        form.addRow("Calidad", self.quality)
        form.addRow("Codificador", self.encoder)
        form.addRow("Captura", self.backend)
        form.addRow("Encuadre", self.aspect)
        form.addRow(self.cursor)
        form.addRow(self.border)
        form.addRow("Carpeta", out_row)
        form.addRow(self.hide_self)
        form.addRow(self.minimize)
        form.addRow(self.plan_label)
        form.addRow(self.caps_label)

        for combo in (self.resolution, self.fps, self.quality, self.encoder, self.backend, self.aspect):
            combo.currentIndexChanged.connect(self.changed.emit)
        for box in (self.cursor, self.border, self.hide_self, self.minimize):
            box.toggled.connect(self.changed.emit)
        self.browse.clicked.connect(self._browse)

        self._caps: EncoderCapabilities | None = None
        self._source_size: tuple[int, int] = (1920, 1080)

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Carpeta de salida", self.output_dir.text())
        if folder:
            self.output_dir.setText(folder)
            self.changed.emit()

    # -- values -------------------------------------------------------------------------

    def set_values(self, video: VideoSettings, output_dir: str, hide_self: bool, minimize: bool) -> None:
        _select(self.resolution, video.resolution)
        _select(self.fps, video.fps)
        _select(self.quality, video.quality)
        _select(self.encoder, video.encoder)
        _select(self.backend, video.backend)
        _select(self.aspect, video.aspect_mode)
        self.cursor.setChecked(video.cursor)
        self.border.setChecked(video.border)
        self.output_dir.setText(output_dir)
        self.hide_self.setChecked(hide_self)
        self.minimize.setChecked(minimize)

    def video_settings(self) -> VideoSettings:
        return VideoSettings(
            resolution=self.resolution.currentData(),
            fps=int(self.fps.currentData()),
            quality=self.quality.currentData(),
            encoder=self.encoder.currentData(),
            backend=self.backend.currentData(),
            aspect_mode=self.aspect.currentData(),
            cursor=self.cursor.isChecked(),
            border=self.border.isChecked(),
        )

    def set_wgc_capabilities(self, caps: dict[str, bool]) -> None:
        self.border.setEnabled(caps.get("border_toggle", False))
        self.cursor.setEnabled(caps.get("cursor_toggle", True))

    # -- capability-aware hints ---------------------------------------------------------

    def set_capabilities(self, caps: EncoderCapabilities) -> None:
        self._caps = caps
        if caps.error:
            self.caps_label.setText(f"No se pudieron detectar codificadores: {caps.error}")
        elif not caps.encoders:
            self.caps_label.setText("No hay codificadores H.264 de Media Foundation en este equipo.")
        else:
            self.caps_label.setText("Codificadores H.264:\n" + "\n".join(caps.describe()))
        self._mark_unsupported()
        self.update_plan(self._source_size)

    def _mark_unsupported(self) -> None:
        """Grey out fps choices the hardware encoder rejected for the chosen resolution
        (only when hardware-only encoding is requested)."""
        model = self.fps.model()
        if not isinstance(model, QStandardItemModel) or self._caps is None:
            return
        preset = self.resolution.currentData()
        only_hw = self.encoder.currentData() == "hardware"
        for row in range(self.fps.count()):
            item = model.item(row)
            fps = int(self.fps.itemData(row))
            ok = True
            if only_hw and preset in RESOLUTION_PRESETS:
                w, h = RESOLUTION_PRESETS[preset]
                ok = self._caps.supports(w, h, fps, hardware=True)
            item.setEnabled(ok)

    def update_plan(self, source_size: tuple[int, int]) -> None:
        self._source_size = source_size
        self._mark_unsupported()
        v = self.video_settings()
        plan = build_plan(source_size, None, v.resolution, v.fps, v.quality, v.aspect_mode)
        w, h = plan.output_size
        hw = ""
        if self._caps is not None and self._caps.encoders:
            hw = " · HW disponible" if self._caps.supports(w, h, v.fps, hardware=True) else " · se usará codificación por software"
        self.plan_label.setText(
            f"Salida: {w}x{h} @ {v.fps} fps · H.264 nivel {plan.level} · {plan.bitrate / 1e6:.1f} Mbps{hw}"
        )
