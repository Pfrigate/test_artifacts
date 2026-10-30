import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolated_app_data(tmp_path, monkeypatch):
    """Keep settings/history/logs of the test run out of the real profile."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    yield


@pytest.fixture
def output_dir():
    """MP4s are written under test-output/ so CI can upload them as artifacts."""
    path = ROOT / "test-output"
    path.mkdir(exist_ok=True)
    return path


def pytest_report_header(config):
    lines = [f"platform: {sys.platform}"]
    try:
        from screenrecorder.native import NATIVE_IMPORT_ERROR, native

        lines.append(f"native extension: {'loaded' if native else NATIVE_IMPORT_ERROR}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"native extension: {exc}")
    lines.append(f"QT_QPA_PLATFORM={os.environ.get('QT_QPA_PLATFORM', '')}")
    return lines
