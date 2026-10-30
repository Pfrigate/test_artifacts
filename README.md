# ScreenRecorder

Grabador de pantalla para **Windows 11** que genera **MP4 H.264 sin audio, compatible con
PowerPoint**, sin FFmpeg, GStreamer ni OpenCV.

* Captura: **Windows Graphics Capture** (monitor, ventana, región) con texturas Direct3D 11; **DXcam** (Desktop Duplication) como alternativa.
* Procesado: **Direct3D 11** `ID3D11VideoProcessor` (recorte + escalado con barras + BGRA→NV12) en GPU.
* Codificación: **Media Foundation** `IMFSinkWriter` → H.264 por **hardware** (NVENC / Quick Sync / AMF) cuando existe, software de Microsoft si no.
* Interfaz: **PySide6** con selección de fuente, región por arrastre, configuración, cronómetro, indicador REC, bandeja, atajos globales (F9 grabar/detener, F10 pausar) e historial con vista previa.
* Resoluciones 720p / 1080p / 4K / nativa · 15 / 30 / 60 fps · timestamps CFR monotónicos · finalización segura del MP4.

Documentación: [arquitectura](docs/ARCHITECTURE.md) · [limitaciones de los bindings](docs/LIMITATIONS.md).

## Requisitos

* Windows 10 2004+ (recomendado Windows 11), Python **3.12** x64.
* Para compilar la extensión: Visual Studio 2022+ con *Desarrollo para el escritorio con C++* y Windows SDK 10.0.22621+, CMake ≥ 3.20.

## Instalación desde el código

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
./build_native.ps1          # compila screenrecorder/_srnative.pyd
python -m screenrecorder    # abre la interfaz
```

Dependencias de ejecución (`requirements.txt`): `PySide6-Essentials` (sin QtMultimedia, que
incluiría FFmpeg), `numpy`, `dxcam`, `winrt-runtime`, `winrt-Windows.Graphics.Capture`,
`winrt-Windows.Foundation`, `winrt-Windows.Foundation.Metadata`.
Compilación/pruebas (`requirements-dev.txt`): `pybind11`, `cmake`, `pytest`, `psutil`, `pyinstaller`.

## Uso desde línea de comandos

```powershell
python -m screenrecorder --list-encoders                 # codificadores H.264, soporte WGC, adaptadores
python -m screenrecorder --selftest --resolution 1080p --fps 60   # grabación sintética + validación del MP4
```

## Pruebas

```powershell
$env:QT_QPA_PLATFORM="offscreen"; python -m pytest -v -rs
```

* `tests/unit`: lógica pura (geometría, timestamps, máquina de estados, parser MP4, ajustes, UI offscreen); también en Linux.
* `tests/windows`: extensión nativa — los 9 modos (720p/1080p/4K × 15/30/60), ambos convertidores, codificador software, subida desde CPU con stride, huecos de timeline, pausa/reanudación, self-test completo, miniaturas y captura real WGC/DXcam. Cada MP4 se valida con el Source Reader de Media Foundation y `core/mp4info.py` (H.264, perfil/nivel, sin audio, `moov` antes de `mdat`, nº de frames, timestamps únicos).

## Benchmark de CPU y GPU

```powershell
python bench/benchmark.py --duration 10 --backends synthetic,wgc,dxcam --out bench-results
```

Genera `bench-results/benchmark.json` y `benchmark.md` con fps efectivos, frames descartados/
repetidos, ms por frame, CPU % del proceso, RSS máximo, uso por motor de GPU (contadores PDH
`GPU Engine`: 3D, VideoEncode, VideoProcessing…) y bitrate resultante.

## Ejecutable de Windows

```powershell
./build_exe.ps1     # dist/ScreenRecorder/ScreenRecorder.exe y dist/ScreenRecorder.zip
```

PyInstaller (*onedir*, sin consola). El script falla si detecta binarios de FFmpeg/GStreamer/OpenCV
en el paquete. El workflow `.github/workflows/windows.yml` compila, prueba, ejecuta el benchmark,
construye el `.exe`, ejecuta su `--selftest` y publica el ZIP como artefacto.
