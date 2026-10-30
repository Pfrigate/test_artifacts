"""CPU / GPU benchmark of the capture -> convert -> encode pipeline.

For every (backend, resolution, fps) combination it records ``--duration`` seconds through
the real code path and reports effective fps, dropped/duplicated frames, per-frame cost,
process CPU %, peak RSS and per-engine GPU utilisation (PDH), plus output bitrate.

    python bench/benchmark.py --duration 5 --backends synthetic,wgc,dxcam --out bench-results
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import psutil  # noqa: E402

from gpu_counters import GpuEngineSampler  # noqa: E402
from screenrecorder.core.capture_manager import CaptureManager  # noqa: E402
from screenrecorder.core.encoder import MediaFoundationEncoder, detect_capabilities  # noqa: E402
from screenrecorder.core.frame_processor import RESOLUTION_PRESETS, bitrate_for  # noqa: E402
from screenrecorder.core.models import Backend  # noqa: E402
from screenrecorder.native import require_native  # noqa: E402
from screenrecorder.winapi.win32 import enable_dpi_awareness  # noqa: E402


def run_case(native, cm, backend: str, preset: str, fps: int, duration: float, out_dir: Path, encoder_pref: str) -> dict:
    width, height = RESOLUTION_PRESETS[preset]
    name = f"{backend}_{preset}_{fps}fps"
    case = {"backend": backend, "resolution": preset, "fps": fps, "encoder_preference": encoder_pref}
    monitors = cm.monitors()
    if backend == "synthetic":
        spec = cm.spec_for_synthetic(width, height)
    else:
        if not monitors:
            return {**case, "skipped": "no monitor"}
        spec = cm.spec_for_monitor(monitors[0])

    device = native.Device(cm.adapter_for_monitor(spec.handle) if backend != "synthetic" else -1)
    encoder = MediaFoundationEncoder(
        device, out_dir / f"{name}.mp4", width, height, fps, bitrate_for(width, height, fps), encoder_preference=encoder_pref
    )
    session = cm.create_session(spec, Backend(backend), device, encoder, fps, cursor=True)

    proc = psutil.Process()
    gpu = GpuEngineSampler()
    proc.cpu_percent(None)
    gpu.sample()
    rss_peak = proc.memory_info().rss
    t0 = time.perf_counter()
    try:
        session.start()
    except Exception as exc:  # noqa: BLE001
        encoder.abort()
        return {**case, "skipped": f"start failed: {exc}"}
    while time.perf_counter() - t0 < duration:
        time.sleep(0.1)
        rss_peak = max(rss_peak, proc.memory_info().rss)
    stats = session.stats()
    elapsed = time.perf_counter() - t0
    cpu = proc.cpu_percent(None)
    gpu_engines = gpu.sample()
    gpu.close()
    session.stop()
    info = encoder.info()
    enc_stats = encoder.native.stats()
    path = encoder.finalize()
    size = path.stat().st_size if path else 0
    written = stats.get("frames_written", 0)
    return {
        **case,
        "encoder": info["encoder_name"],
        "hardware_encoder": info["hardware"],
        "converter": info["converter"],
        "adapter": device.adapter_name,
        "seconds": round(elapsed, 2),
        "frames_written": written,
        "effective_fps": round(written / elapsed, 2) if elapsed else 0,
        "frames_captured": stats.get("frames_captured", 0),
        "frames_duplicated": stats.get("frames_duplicated", 0),
        "frames_dropped": stats.get("frames_dropped", 0),
        "avg_frame_ms": round(stats.get("avg_tick_ms", 0.0), 3),
        "max_frame_ms": round(stats.get("max_tick_ms", 0.0), 3),
        "avg_convert_ms": round(enc_stats["avg_convert_ms"], 3),
        "avg_write_ms": round(enc_stats["avg_write_ms"], 3),
        "cpu_percent": round(cpu, 1),
        "cpu_percent_per_core": round(cpu / (psutil.cpu_count() or 1), 2),
        "rss_peak_mb": round(rss_peak / 2**20, 1),
        "gpu_engines_percent": {k: round(v, 1) for k, v in sorted(gpu_engines.items())},
        "output_mbps": round(size * 8 / max(1e-6, written / fps) / 1e6, 2) if written else 0,
        "error": stats.get("error", ""),
    }


def to_markdown(results: list[dict], header: dict) -> str:
    lines = [
        "# ScreenRecorder benchmark",
        "",
        f"- Machine: {header['machine']}",
        f"- Python: {header['python']}",
        f"- Encoders: {', '.join(header['encoders']) or 'n/a'}",
        "",
        "| backend | mode | encoder | fps eff. | dropped | dup | ms/frame | CPU % (1 core=100) | GPU engines % | RSS MB | Mbps |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        mode = f"{r['resolution']}@{r['fps']}"
        if "skipped" in r:
            lines.append(f"| {r['backend']} | {mode} | skipped: {r['skipped']} | | | | | | | | |")
            continue
        engines = ", ".join(f"{k} {v}" for k, v in r["gpu_engines_percent"].items()) or "n/a"
        hw = "HW" if r["hardware_encoder"] else "SW"
        lines.append(
            f"| {r['backend']} | {mode} | {r['encoder']} ({hw}, {r['converter']}) | {r['effective_fps']} | "
            f"{r['frames_dropped']} | {r['frames_duplicated']} | {r['avg_frame_ms']} | {r['cpu_percent']} | "
            f"{engines} | {r['rss_peak_mb']} | {r['output_mbps']} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--duration", type=float, default=5.0)
    parser.add_argument("--backends", default="synthetic,wgc,dxcam")
    parser.add_argument("--resolutions", default="720p,1080p,2160p")
    parser.add_argument("--fps", default="15,30,60")
    parser.add_argument("--encoder", default="auto", choices=["auto", "hardware", "software"])
    parser.add_argument("--out", default="bench-results")
    args = parser.parse_args()

    enable_dpi_awareness()
    native = require_native()
    cm = CaptureManager()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    caps = detect_capabilities(False)
    header = {
        "machine": f"{platform.platform()} · {platform.processor()} · {psutil.cpu_count()} logical CPUs",
        "python": sys.version.split()[0],
        "encoders": caps.describe(),
        "adapters": [a["description"] for a in native.enumerate_adapters()],
        "wgc_supported": cm.wgc_available(),
    }
    results = []
    for backend in args.backends.split(","):
        for preset in args.resolutions.split(","):
            for fps in (int(f) for f in args.fps.split(",")):
                print(f"-> {backend} {preset}@{fps}", flush=True)
                try:
                    result = run_case(native, cm, backend, preset, fps, args.duration, out_dir, args.encoder)
                except Exception as exc:  # noqa: BLE001 - benchmark keeps going
                    result = {"backend": backend, "resolution": preset, "fps": fps, "skipped": f"{type(exc).__name__}: {exc}"}
                print("   ", json.dumps(result), flush=True)
                results.append(result)
    (out_dir / "benchmark.json").write_text(json.dumps({"header": header, "results": results}, indent=2), encoding="utf-8")
    markdown = to_markdown(results, header)
    (out_dir / "benchmark.md").write_text(markdown, encoding="utf-8")
    print(markdown)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
