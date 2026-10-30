"""FrameProcessor: decides output geometry, pixel format and the data path for frames.

The per-frame GPU work (crop, scale, letterbox, BGRA->NV12) happens in the native
``FrameConverter`` (ID3D11VideoProcessor, or a pixel shader fallback). This module holds
the pure, testable policy that configures it, mirroring the native letterbox maths.
"""

from __future__ import annotations

from dataclasses import dataclass

from screenrecorder.core.models import Rect

RESOLUTION_PRESETS: dict[str, tuple[int, int]] = {
    "720p": (1280, 720),
    "1080p": (1920, 1080),
    "2160p": (3840, 2160),
}
FPS_CHOICES = (15, 30, 60)
QUALITY_BITS_PER_PIXEL = {"low": 0.07, "medium": 0.12, "high": 0.18}

# H.264 level 5.2 frame-size limit (36864 macroblocks) — 4096x2304 at most.
MAX_MACROBLOCKS = 36864
MAX_DIMENSION = 4096
MIN_DIMENSION = 64

# (level, max macroblocks/s, max frame macroblocks) from ITU-T H.264 Table A-1.
_H264_LEVELS = (
    ("3.1", 108000, 3600),
    ("3.2", 216000, 5120),
    ("4.0", 245760, 8192),
    ("4.1", 245760, 8192),
    ("4.2", 522240, 8704),
    ("5.0", 589824, 22080),
    ("5.1", 983040, 36864),
    ("5.2", 2073600, 36864),
)


def even_floor(value: float) -> int:
    v = int(value)
    return max(2, v - (v % 2))


def macroblocks(width: int, height: int) -> int:
    return ((width + 15) // 16) * ((height + 15) // 16)


def clamp_to_h264(width: int, height: int) -> tuple[int, int]:
    """Scale (keeping aspect) so the frame fits H.264 level 5.2 and is even-sized."""
    w, h = float(width), float(height)
    scale = min(1.0, MAX_DIMENSION / w, MAX_DIMENSION / h)
    w, h = w * scale, h * scale
    while macroblocks(int(w), int(h)) > MAX_MACROBLOCKS:
        w, h = w * 0.98, h * 0.98
    w = max(MIN_DIMENSION, w)
    h = max(MIN_DIMENSION, h)
    return even_floor(w), even_floor(h)


def letterbox(src_w: int, src_h: int, dst_w: int, dst_h: int) -> Rect:
    """Same arithmetic as native ``letterbox_rect``: fit, centre, even coordinates."""
    if src_w <= 0 or src_h <= 0:
        return Rect(0, 0, dst_w, dst_h)
    if src_w * dst_h >= dst_w * src_h:
        w, h = dst_w, src_h * dst_w // src_w
    else:
        w, h = src_w * dst_h // src_h, dst_h
    w = max(2, w & ~1)
    h = max(2, h & ~1)
    x = ((dst_w - w) // 2) & ~1
    y = ((dst_h - h) // 2) & ~1
    return Rect(x, y, w, h)


def output_size(preset: str, source_w: int, source_h: int, aspect_mode: str = "letterbox") -> tuple[int, int]:
    """Encoded frame size.

    * ``native``: the source size (clamped to H.264 limits, even).
    * preset + ``letterbox``: the exact preset canvas (16:9, matches PowerPoint slides);
      sources of other aspect ratios get black bars.
    * preset + ``source``: the largest size with the source aspect that fits the preset.
    """
    if preset == "native" or preset not in RESOLUTION_PRESETS:
        return clamp_to_h264(max(source_w, MIN_DIMENSION), max(source_h, MIN_DIMENSION))
    box_w, box_h = RESOLUTION_PRESETS[preset]
    if aspect_mode != "source" or source_w <= 0 or source_h <= 0:
        return box_w, box_h
    fit = letterbox(source_w, source_h, box_w, box_h)
    return clamp_to_h264(max(fit.width, MIN_DIMENSION), max(fit.height, MIN_DIMENSION))


def bitrate_for(width: int, height: int, fps: int, quality: str = "medium") -> int:
    """Target average bitrate (bps). Screen content compresses well, so higher frame rates
    get sub-linear scaling; clamped to 1..80 Mbps (well inside level 5.1/5.2 limits)."""
    bpp = QUALITY_BITS_PER_PIXEL.get(quality, QUALITY_BITS_PER_PIXEL["medium"])
    effective_fps = 30.0 * (fps / 30.0) ** 0.7
    bitrate = width * height * effective_fps * bpp
    return int(min(80_000_000, max(1_000_000, bitrate)))


def h264_level(width: int, height: int, fps: int) -> str:
    frame_mbs = macroblocks(width, height)
    rate = frame_mbs * fps
    for level, max_rate, max_frame in _H264_LEVELS:
        if frame_mbs <= max_frame and rate <= max_rate:
            return level
    return "6.0"


@dataclass(frozen=True)
class FramePlan:
    source_size: tuple[int, int]
    crop: Rect | None
    output_size: tuple[int, int]
    content_rect: Rect  # where the image lands inside the output (rest is black)
    pixel_format: str  # encoder input: NV12 (video processor) or RGB32 (shader fallback)
    data_path: str  # "gpu" (WGC textures) or "cpu-upload" (DXcam numpy frames)
    bitrate: int
    level: str

    @property
    def needs_scaling(self) -> bool:
        w, h = (self.crop.width, self.crop.height) if self.crop else self.source_size
        return (self.content_rect.width, self.content_rect.height) != (w, h)


def build_plan(
    source_size: tuple[int, int],
    crop: Rect | None,
    preset: str,
    fps: int,
    quality: str = "medium",
    aspect_mode: str = "letterbox",
    backend: str = "wgc",
    video_processor: bool = True,
) -> FramePlan:
    src_w, src_h = source_size
    if crop is not None:
        crop = crop.intersected(Rect(0, 0, src_w, src_h))
        if crop.width < 2 or crop.height < 2:
            crop = None
    content_w, content_h = (crop.width, crop.height) if crop else (src_w, src_h)
    out_w, out_h = output_size(preset, content_w, content_h, aspect_mode)
    return FramePlan(
        source_size=source_size,
        crop=crop,
        output_size=(out_w, out_h),
        content_rect=letterbox(content_w, content_h, out_w, out_h),
        pixel_format="NV12" if video_processor else "RGB32",
        data_path="cpu-upload" if backend == "dxcam" else "gpu",
        bitrate=bitrate_for(out_w, out_h, fps, quality),
        level=h264_level(out_w, out_h, fps),
    )
