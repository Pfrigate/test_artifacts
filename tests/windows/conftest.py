import os
import sys

import pytest

from screenrecorder.native import NATIVE_IMPORT_ERROR, native

if sys.platform == "win32" and native is None and os.environ.get("SR_REQUIRE_NATIVE") == "1":
    raise ImportError(f"SR_REQUIRE_NATIVE=1 but the native extension failed to load: {NATIVE_IMPORT_ERROR}")

if sys.platform != "win32" or native is None:
    # Not a failure: these tests need Windows plus the compiled _srnative extension.
    collect_ignore_glob = ["test_*.py"]
    print(f"skipping tests/windows: native extension unavailable ({NATIVE_IMPORT_ERROR})")


@pytest.fixture(scope="session")
def device():
    return native.Device()


@pytest.fixture(scope="session")
def capabilities():
    from screenrecorder.core.encoder import detect_capabilities

    return detect_capabilities(True)
