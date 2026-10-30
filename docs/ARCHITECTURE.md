# Arquitectura técnica

ScreenRecorder graba pantalla, monitores, ventanas y regiones en Windows 11 y produce
**MP4 / H.264 sin audio** compatible con PowerPoint. No usa FFmpeg, GStreamer ni OpenCV:
la captura usa Windows Graphics Capture (WGC) o Desktop Duplication (DXcam), el procesado
usa Direct3D 11 y la codificación usa Media Foundation (`IMFSinkWriter`).

## Capas

```
┌──────────────────────── PySide6 (hilo GUI) ────────────────────────┐
│ UserInterface: SourcePanel · SettingsPanel · MainWindow · History  │
│      │ QTimer 250 ms: poll()                                        │
├──────┼────────────────────── Python core ───────────────────────────┤
│ RecordingManager (máquina de estados, segmentos, device-lost)       │
│ CaptureManager (enumeración, backend, sesiones)                     │
│ FrameProcessor (geometría, letterbox, formato, bitrate, nivel)      │
│ MediaFoundationEncoder (wrapper, .recording.mp4 → rename seguro)    │
├───────────────────── _srnative (C++17/20, pybind11) ───────────────┤
│ CapturePipeline (std::thread, temporizador alta resolución, CFR)    │
│ WgcCapture ── FrameConverter ── SurfacePool ── VideoEncoder         │
│ D3DDevice · mf_caps (MFTEnumEx, SourceReader probe/thumbnail)       │
└─────────────────────────────────────────────────────────────────────┘
```

| Componente | Archivo(s) | Responsabilidad |
|---|---|---|
| CaptureManager | `screenrecorder/core/capture_manager.py`, `native/wgc_capture.*` | Enumerar monitores (EnumDisplayMonitors) y ventanas (EnumWindows + DWM cloaking), decidir backend (WGC → DXcam), crear sesiones, mapear HMONITOR ↔ adaptador DXGI ↔ índices DXcam, controlar FPS y detectar cierre de fuente / pérdida de dispositivo. |
| FrameProcessor | `screenrecorder/core/frame_processor.py`, `native/frame_converter.*` | Política (Python): tamaño de salida, dimensiones pares, límites H.264 nivel 5.2, letterbox, bitrate, nivel. Ejecución (GPU): recorte + escalado + BGRA→NV12 con `ID3D11VideoProcessor`, o shader + RGB32 si el dispositivo no tiene vídeo D3D11. |
| MediaFoundationEncoder | `screenrecorder/core/encoder.py`, `native/mf_encoder.*`, `native/mf_caps.*` | `MFStartup`, `IMFDXGIDeviceManager`, `IMFSinkWriter` MP4, tipo H.264 (High→Main), resolución/FPS, CodecAPI (VBR, GOP 2 s), `WriteSample` con timestamps CFR, `Finalize`, detección de MFT HW/SW. |
| RecordingManager | `screenrecorder/core/recording_manager.py`, `core/recording_backend.py` | Iniciar/pausar/reanudar/detener, transiciones validadas, errores, reinicio en nuevo segmento tras device-lost. |
| UserInterface | `screenrecorder/ui/*` | Selección de fuente (miniaturas, ventanas, overlay de región), configuración, cronómetro, indicador REC, bandeja, atajos F9/F10, historial con vista previa. |

## Flujo de un fotograma (ruta WGC, sin copias a CPU)

1. `Direct3D11CaptureFramePool` (free-threaded, 2 buffers) entrega una textura BGRA.
2. `CopySubresourceRegion` a una textura propia → el buffer vuelve a WGC enseguida.
3. `ID3D11VideoProcessorBlt`: recorte (región), escalado con letterbox, BGRA (0‑255) → NV12 BT.709 rango limitado.
4. La textura NV12 pertenece a un `SurfacePool` de `IMFSample` rastreados (`IMFTrackedSample`): sólo se reutiliza cuando el codificador libera la muestra.
5. `MFCreateDXGISurfaceBuffer` + `IMFSinkWriter::WriteSample` → MFT H.264 de hardware (NVENC/QSV/AMF) en el mismo `ID3D11Device` gracias a `MF_SINK_WRITER_D3D_MANAGER`.

La ruta DXcam sube cada `numpy` BGRA una vez (`UpdateSubresource`, admite stride > ancho·4) y
continúa en el paso 3.

## Tiempo y sincronización

* Línea temporal **CFR**: `ts(i) = i · 10⁷ / fps` (unidades de 100 ns), idéntica en C++ (`frame_time_hns`) y Python (`frame_timestamp`), estrictamente creciente; el codificador rechaza índices no crecientes.
* El hilo nativo despierta con `CreateWaitableTimerExW(CREATE_WAITABLE_TIMER_HIGH_RESOLUTION)` en `base + ts(tick)` medido con QPC.
* Si no llega un frame nuevo (WGC sólo emite cambios) se re-codifica el último → vídeo continuo.
* Si el pipeline se retrasa uno o más periodos, se saltan esas ranuras (`frames_dropped`): los timestamps siguen al reloj real, nunca se comprime el tiempo.
* Pausa: la línea temporal continúa en `last_index + 1` al reanudar → sin huecos ni saltos. Durante la pausa se sigue drenando WGC para que el primer frame tras reanudar sea actual.
* DXcam: el índice se deriva de `perf_counter_ns` (QPC) con la misma regla.

## Hilos y GIL

| Hilo | Trabajo |
|---|---|
| GUI (STA de Qt) | UI, `poll()` cada 250 ms, nunca bloquea en captura/codificación |
| `CapturePipeline` (nativo, MTA) | WGC, conversión, `WriteSample` |
| `dxcam-capture` (Python) | `get_latest_frame` + `write_bgra` (libera el GIL) |
| Work queues de MF | codificación asíncrona, callbacks de `IMFTrackedSample` |
| QThreadPool | detección de codificadores, miniaturas |

Todas las llamadas nativas largas usan `py::gil_scoped_release`. El contexto inmediato D3D11
está protegido (`ID3D11Multithread`) y las secuencias de llamadas se serializan con un
`recursive_mutex` del dispositivo.

## Memoria y buffers

* Pool fijo de 6 superficies NV12 por codificador (≈ 3 MB c/u a 1080p, 12 MB a 4K).
* 2 buffers en el frame pool de WGC + 1 textura copia; DXcam ring de 8 frames.
* `IMFSinkWriter` con *throttling* activado: si el codificador va lento, `WriteSample` frena el productor en vez de acumular memoria.

## Errores y robustez

* `HRESULT` → `NativeError(msg, hresult)` en Python.
* `DXGI_ERROR_DEVICE_REMOVED/RESET/HUNG` o `GetDeviceRemovedReason` → `error_kind = device_lost`: se finaliza el MP4 actual y se continúa en `<nombre>_parte2.mp4` con un dispositivo nuevo (máx. 3 veces).
* `GraphicsCaptureItem.Closed` (ventana cerrada, monitor desconectado) → `source_closed`: parada limpia.
* El MP4 se escribe como `*.recording.mp4` y se renombra sólo tras `Finalize` correcto. `closeEvent` y `shutdown()` finalizan siempre.

## Compatibilidad PowerPoint

H.264 High (o Main) progresivo, 4:2:0 NV12, PAR 1:1, BT.709 rango limitado, nivel ≤ 5.2,
dimensiones pares, sin pista de audio. Las pruebas y `--selftest` verifican todo esto con
`core/mp4info.py` y el Source Reader de Media Foundation, y además **decodifican un fotograma de
cada archivo**. `moov` queda al final del archivo (ver LIMITATIONS: la reubicación
`MF_MPEG4SINK_MOOV_BEFORE_MDAT` generaba archivos no decodificables); PowerPoint reproduce
archivos locales así sin problema.
