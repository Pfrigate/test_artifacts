"""Render the application icon to a multi-size .ico for the PyInstaller executable."""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtGui import QGuiApplication  # noqa: E402


def main() -> int:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "screenrecorder.ico")
    app = QGuiApplication([])  # noqa: F841 - required for QPixmap
    from screenrecorder.ui.common import app_icon

    image = app_icon().pixmap(256, 256).toImage()
    if not image.save(str(target), "ICO"):
        # The ICO image plugin is optional; fall back to PNG which PyInstaller also accepts.
        target = target.with_suffix(".png")
        image.save(str(target), "PNG")
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
