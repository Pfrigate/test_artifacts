# Limitaciones de los bindings Python y justificación de la extensión C++

La extensión nativa `_srnative` (≈1.500 líneas C++) existe sólo porque estas piezas no son
alcanzables desde Python sin perder el camino GPU o sin reescribir COM a mano:

| Necesidad | Python disponible | Problema | Solución |
|---|---|---|---|
| Crear `GraphicsCaptureItem` desde HMONITOR/HWND | PyWinRT | Requiere `IGraphicsCaptureItemInterop` (COM clásico, no WinRT); PyWinRT sólo ofrece `GraphicsCapturePicker`, que necesita `IInitializeWithWindow`. | `IGraphicsCaptureItemInterop::CreateForMonitor/CreateForWindow` en C++/WinRT. |
| Obtener `ID3D11Texture2D` de un `IDirect3DSurface` | PyWinRT | `IDirect3DDxgiInterfaceAccess` no está proyectado; sólo se puede copiar a `SoftwareBitmap` (GPU→CPU). | `IDirect3DDxgiInterfaceAccess::GetInterface` en C++. |
| Crear el `IDirect3DDevice` WinRT desde D3D11 | PyWinRT | `CreateDirect3D11DeviceFromDXGIDevice` es una función Win32. | C++. |
| `IMFSinkWriter`, `IMFDXGIDeviceManager`, `IMFTrackedSample`, CodecAPI | comtypes | Media Foundation no tiene type library; habría que declarar a mano decenas de vtables y GUIDs, y cada `WriteSample` pasaría por ctypes con el GIL. | C++ con los headers del Windows SDK. |
| `ID3D11VideoProcessor` | ninguno | Sin bindings Python mantenidos para D3D11 video. | C++. |
| Captura con DXcam | DXcam | Devuelve `numpy` en memoria de sistema: copia GPU→CPU (Desktop Duplication → staging) inevitable; no expone superficies GPU. | Se usa como alternativa; el frame se sube una vez a GPU y sigue la ruta NV12/HW. |

PyWinRT se usa donde funciona bien: `GraphicsCaptureSession.is_supported()` y
`ApiInformation.is_property_present()` para detectar `IsBorderRequired` / `IsCursorCaptureEnabled`.

## Limitaciones de la captura

* **WGC**: ventanas minimizadas no generan frames (se repite el último); contenido protegido (DRM) sale negro; en Windows 10 el borde amarillo no puede desactivarse; el cursor de WGC se puede ocultar sólo en 2004+.
* **DXcam / Desktop Duplication**: no captura ventanas individuales; no dibuja el cursor; falla en sesiones sin escritorio interactivo (servicios, algunos runners de CI); el modo pantalla completa exclusiva puede invalidar la duplicación (`DXGI_ERROR_ACCESS_LOST`).
* Una región se define dentro de un único monitor.
* Al redimensionar una ventana, el tamaño de salida se mantiene y el contenido se re-encuadra con letterbox.

## Limitaciones de la codificación

* La disponibilidad de 4K60 por hardware depende de la GPU/driver; la detección (`MFTEnumEx` + `SetOutputType/SetInputType`) es orientativa: algunos MFT aceptan el tipo y fallan después. Si `BeginWriting` falla, se cae automáticamente a Main y luego al codificador software de Microsoft.
* El codificador software a 4K60 no alcanza tiempo real en CPUs modestas; el pipeline descarta ranuras (timestamps correctos, vídeo con saltos).
* Sin audio por diseño.
* `moov` se escribe al final (sin *fast start*). Con `MF_MPEG4SINK_MOOV_BEFORE_MDAT` el sink de
  Media Foundation reubicaba `moov` en `Finalize`, pero los offsets de las muestras quedaban
  apuntando a bytes incorrectos y el decodificador H.264 descartaba todo; se desactivó y ahora cada
  prueba decodifica un fotograma para detectarlo. No afecta a PowerPoint con archivos locales.
* Ediciones Windows "N" requieren el *Media Feature Pack*; Windows Server requiere la característica *Server-Media-Foundation*.

## Pruebas en CI

Los runners de GitHub Actions no tienen GPU ni escritorio interactivo: se ejecutan con el
adaptador básico de Microsoft / WARP y el codificador H.264 software. Las pruebas de captura
real (`tests/windows/test_capture_live.py`) se marcan como *skipped* con el motivo si la sesión
no permite captura; deben ejecutarse en un equipo Windows 11 real.
