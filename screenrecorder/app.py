"""Command-line entry point: GUI by default, plus headless helpers for CI and support."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from screenrecorder import APP_NAME, __version__


def _setup_logging(level: str) -> None:
    from screenrecorder.core.settings import app_data_dir

    handlers: list[logging.Handler] = [logging.FileHandler(app_data_dir() / "screenrecorder.log", encoding="utf-8")]
    if sys.stderr is not None:  # windowed PyInstaller builds have no console
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


def _emit(data: dict, out: str | None) -> None:
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    if out:
        Path(out).write_text(text, encoding="utf-8")
    if sys.stdout is not None:
        print(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=APP_NAME, description="Grabador de pantalla H.264/MP4 sin FFmpeg")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    parser.add_argument("--selftest", action="store_true", help="grabar un patrón sintético y validar el MP4")
    parser.add_argument("--selftest-dir", default=None, help="carpeta para el MP4 del self-test")
    parser.add_argument("--selftest-seconds", type=float, default=2.0)
    parser.add_argument("--resolution", default="720p", choices=["720p", "1080p", "2160p"])
    parser.add_argument("--fps", type=int, default=30, choices=[15, 30, 60])
    parser.add_argument("--list-encoders", action="store_true", help="mostrar codificadores H.264 detectados")
    parser.add_argument("--out", default=None, help="escribir la salida JSON en este archivo")
    parser.add_argument("--log-level", default="INFO")
    args, qt_args = parser.parse_known_args(argv if argv is not None else sys.argv[1:])

    _setup_logging(args.log_level)
    from screenrecorder.winapi.win32 import enable_dpi_awareness

    enable_dpi_awareness()

    if args.list_encoders:
        from screenrecorder.core.capture_manager import CaptureManager
        from screenrecorder.core.encoder import detect_capabilities
        from screenrecorder.native import native

        caps = detect_capabilities(True)
        _emit(
            {
                "encoders": caps.encoders,
                "decoders": list(native.enumerate_decoders()) if native else [],
                "error": caps.error,
                "wgc_supported": CaptureManager.wgc_available(),
                "wgc_capabilities": CaptureManager.wgc_capabilities(),
                "dxcam_available": CaptureManager.dxcam_available(),
                "adapters": list(native.enumerate_adapters()) if native else [],
            },
            args.out,
        )
        return 0 if caps.encoders else 1

    if args.selftest:
        from screenrecorder.core.selftest import run_selftest

        out_dir = Path(args.selftest_dir) if args.selftest_dir else Path.cwd() / "selftest-output"
        try:
            report = run_selftest(out_dir, args.selftest_seconds, args.resolution, args.fps)
        except Exception as exc:  # noqa: BLE001
            report = {"ok": False, "problems": [f"{type(exc).__name__}: {exc}"]}
        _emit(report, args.out)
        return 0 if report.get("ok") else 1

    from screenrecorder.ui.main_window import run_gui

    return run_gui([sys.argv[0], *qt_args])


if __name__ == "__main__":
    raise SystemExit(main())
