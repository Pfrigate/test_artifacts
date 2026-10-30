# Benchmark de referencia (GitHub Actions)

Ejecutado por el workflow `build-and-test` (run 12) con `bench/benchmark.py --duration 3`.

**Entorno**: Windows Server 2025, AMD EPYC 4 vCPU, **sin GPU** (adaptador básico de Microsoft /
WARP, sin vídeo D3D11 → convertidor *shader* + RGB32, codificador **software** `H264 Encoder MFT`).
Son por tanto cifras de peor caso: en un equipo con GPU se usan `ID3D11VideoProcessor` (NV12) y el
MFT de hardware (NVENC / Quick Sync / AMF), y el coste de CPU baja drásticamente.

CPU % es del proceso con 1 núcleo = 100 (4 núcleos = 400). "dup" = ranuras en las que WGC no entregó
un frame nuevo (pantalla estática) y se repitió el último. "dropped" = ranuras saltadas por ir con
retraso (los timestamps siguen al reloj real).

| backend | mode | encoder | fps eff. | dropped | dup | ms/frame | CPU % (1 core=100) | GPU engines % | RSS MB | Mbps |
|---|---|---|---|---|---|---|---|---|---|---|
| synthetic | 720p@15 | Microsoft H.264 Encoder MFT (software) (SW, shader) | 15.26 | 0 | 0 | 0.264 | 24.9 | 3D 4.7 | 185.7 | 0.36 |
| synthetic | 720p@30 | Microsoft H.264 Encoder MFT (software) (SW, shader) | 30.19 | 0 | 0 | 0.216 | 57.5 | 3D 8.9 | 181.8 | 0.67 |
| synthetic | 720p@60 | Microsoft H.264 Encoder MFT (software) (SW, shader) | 59.99 | 0 | 0 | 0.194 | 110.9 | 3D 16.3 | 185.2 | 1.27 |
| synthetic | 1080p@15 | Microsoft H.264 Encoder MFT (software) (SW, shader) | 15.24 | 0 | 0 | 0.282 | 59.8 | 3D 10.3 | 320.1 | 0.79 |
| synthetic | 1080p@30 | Microsoft H.264 Encoder MFT (software) (SW, shader) | 30.14 | 0 | 0 | 0.28 | 114.4 | 3D 19.9 | 328.0 | 1.49 |
| synthetic | 1080p@60 | Microsoft H.264 Encoder MFT (software) (SW, shader) | 60.04 | 0 | 0 | 0.344 | 250.5 | 3D 49.1 | 338.7 | 2.81 |
| synthetic | 2160p@15 | Microsoft H.264 Encoder MFT (software) (SW, shader) | 15.0 | 0 | 0 | 0.309 | 246.4 | 3D 41.0 | 1102.7 | 3.18 |
| synthetic | 2160p@30 | Microsoft H.264 Encoder MFT (software) (SW, shader) | 17.47 | 36 | 0 | 48.672 | 370.1 | 3D 88.9 | 1220.4 | 8.05 |
| synthetic | 2160p@60 | Microsoft H.264 Encoder MFT (software) (SW, shader) | 9.72 | 146 | 0 | 93.096 | 366.8 | 3D 65.5 | 1234.8 | 30.1 |
| wgc | 720p@15 | Microsoft H.264 Encoder MFT (software) (SW, shader) | 14.63 | 0 | 39 | 0.214 | 26.9 | 3D 5.7 | 398.7 | 1.13 |
| wgc | 720p@30 | Microsoft H.264 Encoder MFT (software) (SW, shader) | 29.34 | 0 | 83 | 0.152 | 55.6 | 3D 10.1 | 240.4 | 1.53 |
| wgc | 720p@60 | Microsoft H.264 Encoder MFT (software) (SW, shader) | 58.37 | 0 | 173 | 0.127 | 100.0 | 3D 21.7 | 208.9 | 1.74 |
| wgc | 1080p@15 | Microsoft H.264 Encoder MFT (software) (SW, shader) | 14.68 | 0 | 39 | 0.212 | 49.7 | 3D 12.6 | 337.5 | 1.89 |
| wgc | 1080p@30 | Microsoft H.264 Encoder MFT (software) (SW, shader) | 29.3 | 0 | 84 | 0.165 | 119.8 | 3D 23.7 | 337.8 | 2.39 |
| wgc | 1080p@60 | Microsoft H.264 Encoder MFT (software) (SW, shader) | 58.32 | 0 | 173 | 0.211 | 232.0 | 3D 52.2 | 335.8 | 2.71 |
| wgc | 2160p@15 | Microsoft H.264 Encoder MFT (software) (SW, shader) | 14.53 | 0 | 38 | 0.396 | 228.3 | 3D 56.2 | 1089.0 | 4.15 |
| wgc | 2160p@30 | Microsoft H.264 Encoder MFT (software) (SW, shader) | 14.23 | 43 | 37 | 59.366 | 306.7 | 3D 86.0 | 1167.4 | 11.32 |
| wgc | 2160p@60 | Microsoft H.264 Encoder MFT (software) (SW, shader) | 8.15 | 143 | 20 | 107.963 | 312.2 | 3D 77.3 | 1175.8 | 43.6 |
| dxcam | 720p@15 | Microsoft H.264 Encoder MFT (software) (SW, shader) | 14.1 | 0 | 0 | 0.354 | 28.2 | 3D 3.1 | 405.4 | 1.1 |
| dxcam | 720p@30 | Microsoft H.264 Encoder MFT (software) (SW, shader) | 28.86 | 0 | 0 | 0.312 | 46.6 | 3D 6.9 | 264.7 | 1.55 |
| dxcam | 720p@60 | Microsoft H.264 Encoder MFT (software) (SW, shader) | 57.72 | 0 | 0 | 0.303 | 105.2 | 3D 13.0 | 272.5 | 1.75 |
| dxcam | 1080p@15 | Microsoft H.264 Encoder MFT (software) (SW, shader) | 14.25 | 0 | 0 | 0.354 | 55.4 | 3D 7.7 | 402.9 | 1.8 |
| dxcam | 1080p@30 | Microsoft H.264 Encoder MFT (software) (SW, shader) | 28.81 | 0 | 0 | 0.346 | 109.8 | 3D 15.4 | 401.2 | 2.45 |
| dxcam | 1080p@60 | Microsoft H.264 Encoder MFT (software) (SW, shader) | 57.94 | 0 | 0 | 0.352 | 211.9 | 3D 36.8 | 383.1 | 2.75 |
| dxcam | 2160p@15 | Microsoft H.264 Encoder MFT (software) (SW, shader) | 14.4 | 0 | 0 | 0.391 | 193.8 | 3D 30.9 | 1129.7 | 4.15 |
| dxcam | 2160p@30 | Microsoft H.264 Encoder MFT (software) (SW, shader) | 20.19 | 23 | 0 | 39.816 | 354.4 | 3D 70.5 | 1250.3 | 7.96 |
| dxcam | 2160p@60 | Microsoft H.264 Encoder MFT (software) (SW, shader) | 10.26 | 111 | 0 | 80.789 | 342.8 | 3D 56.8 | 1247.0 | 34.04 |

Lectura:

* 720p y 1080p a 15/30/60 fps se mantienen en tiempo real con codificación por software, con
  < 0,4 ms de trabajo propio por frame (captura + conversión + `WriteSample`).
* 4K a 30/60 fps no es alcanzable con el codificador software en 4 vCPU (se descartan ranuras;
  el vídeo sigue sincronizado). Es el caso para el que se prioriza el MFT de hardware.
* El RSS a 4K (~1,1 GB) lo domina el codificador software y la superficie WARP; con GPU las
  superficies viven en memoria de vídeo.
* Para medir en un equipo real: `python bench/benchmark.py --duration 10` (genera JSON + Markdown,
  incluido el uso por motor de GPU: 3D, VideoEncode, VideoProcessing).
