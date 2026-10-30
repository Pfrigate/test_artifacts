"""Loader for the ``_srnative`` C++ extension (Direct3D 11 / WGC / Media Foundation).

The import is optional so pure-Python parts (UI layout, state machine, unit tests) still
load on machines where the extension has not been built.
"""

from __future__ import annotations

from types import ModuleType

native: ModuleType | None
try:
    from screenrecorder import _srnative as native  # type: ignore[attr-defined]

    NATIVE_IMPORT_ERROR: Exception | None = None
except ImportError as exc:  # pragma: no cover - depends on build
    native = None
    NATIVE_IMPORT_ERROR = exc


def require_native() -> ModuleType:
    if native is None:
        raise RuntimeError(
            "The native extension screenrecorder._srnative is not available "
            f"({NATIVE_IMPORT_ERROR}). Build it with build_native.ps1 on Windows."
        )
    return native


def hresult_of(exc: BaseException) -> int | None:
    """HRESULT carried by a NativeError (second constructor argument)."""
    if native is not None and isinstance(exc, native.NativeError) and len(exc.args) > 1:
        return int(exc.args[1])
    return None


DEVICE_LOST_HRESULTS = {
    0x887A0005,  # DXGI_ERROR_DEVICE_REMOVED
    0x887A0007,  # DXGI_ERROR_DEVICE_RESET
    0x887A0006,  # DXGI_ERROR_DEVICE_HUNG
    0x887A0020,  # DXGI_ERROR_DRIVER_INTERNAL_ERROR
    0x887A0026,  # DXGI_ERROR_ACCESS_LOST (Desktop Duplication)
}
