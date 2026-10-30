"""MediaFoundationEncoder: Python face of the native IMFSinkWriter H.264/MP4 encoder.

Writes to ``<name>.recording.mp4`` and renames to the final name only after
``IMFSinkWriter::Finalize`` succeeded, so an interrupted recording never leaves a file
that looks complete but has no ``moov`` box.
"""

from __future__ import annotations

import functools
import logging
from dataclasses import dataclass, field
from pathlib import Path

from screenrecorder.native import require_native

log = logging.getLogger(__name__)

VENDORS = {"VEN_10DE": "NVIDIA", "VEN_8086": "Intel", "VEN_1002": "AMD", "VEN_1022": "AMD", "VEN_1414": "Microsoft"}


@dataclass
class EncoderCapabilities:
    encoders: list[dict] = field(default_factory=list)
    error: str = ""

    @property
    def hardware_encoders(self) -> list[dict]:
        return [e for e in self.encoders if e.get("hardware")]

    @property
    def hardware_available(self) -> bool:
        return bool(self.hardware_encoders)

    def supports(self, width: int, height: int, fps: int, hardware: bool | None = None) -> bool:
        """Whether some encoder accepted this mode during detection. Modes that were not
        probed (e.g. native sizes) are assumed supported if any encoder exists."""
        candidates = self.encoders
        if hardware is not None:
            candidates = [e for e in candidates if bool(e.get("hardware")) == hardware]
        if not candidates:
            return False
        for encoder in candidates:
            modes = encoder.get("modes") or []
            match = [m for m in modes if (m["width"], m["height"], m["fps"]) == (width, height, fps)]
            if not match or match[0]["supported"]:
                return True
        return False

    def describe(self) -> list[str]:
        lines = []
        for e in self.encoders:
            kind = "HW" if e.get("hardware") else "SW"
            vendor = VENDORS.get(e.get("vendor_id", "").upper(), e.get("vendor_id", ""))
            lines.append(f"[{kind}] {e.get('name') or e.get('clsid')} {vendor}".strip())
        return lines


@functools.lru_cache(maxsize=1)
def detect_capabilities(test_modes: bool = True) -> EncoderCapabilities:
    try:
        native = require_native()
        return EncoderCapabilities(encoders=list(native.enumerate_encoders(test_modes)))
    except Exception as exc:  # noqa: BLE001 - reported in the UI
        log.warning("Encoder detection failed: %s", exc)
        return EncoderCapabilities(error=str(exc))


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for n in range(2, 10_000):
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(path)


class MediaFoundationEncoder:
    def __init__(
        self,
        device,
        path: Path,
        width: int,
        height: int,
        fps: int,
        bitrate: int,
        encoder_preference: str = "auto",
        profile: str = "high",
        converter: str = "auto",
        gop_seconds: int = 2,
    ) -> None:
        native = require_native()
        self.final_path = Path(path)
        self.final_path.parent.mkdir(parents=True, exist_ok=True)
        self.part_path = self.final_path.with_name(self.final_path.stem + ".recording.mp4")
        self.part_path.unlink(missing_ok=True)
        self._native = native.Encoder(
            device,
            str(self.part_path),
            width,
            height,
            fps,
            bitrate,
            prefer_hardware=encoder_preference != "software",
            profile=profile,
            gop_seconds=gop_seconds,
            converter=converter,
        )
        self.encoder_preference = encoder_preference
        if encoder_preference == "hardware" and not self._native.hardware:
            log.warning("Hardware encoding requested but %s was selected", self._native.encoder_name)

    @property
    def native(self):
        return self._native

    def info(self) -> dict:
        n = self._native
        return {
            "encoder_name": n.encoder_name,
            "hardware": n.hardware,
            "converter": n.converter,
            "input_subtype": n.input_subtype,
            "profile": n.profile,
            "width": n.width,
            "height": n.height,
            "fps": n.fps,
            "bitrate": n.bitrate,
        }

    @property
    def frames_written(self) -> int:
        return self._native.frames_written

    def write_bgra(self, frame, index: int = -1) -> int:
        return self._native.write_bgra(frame, index)

    def write_synthetic(self) -> int:
        return self._native.write_synthetic()

    def finalize(self) -> Path | None:
        """Finalize the MP4. Returns the final path, or None when no frame was written."""
        try:
            self._native.finalize()
        finally:
            if self._native.frames_written == 0:
                self.part_path.unlink(missing_ok=True)
        if self._native.frames_written == 0:
            return None
        target = unique_path(self.final_path)
        self.part_path.replace(target)
        return target

    def abort(self) -> Path | None:
        try:
            return self.finalize()
        except Exception as exc:  # noqa: BLE001
            log.error("Finalize failed, partial file kept at %s: %s", self.part_path, exc)
            return None
