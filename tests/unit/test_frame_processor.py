import pytest

from screenrecorder.core.frame_processor import (
    bitrate_for,
    build_plan,
    clamp_to_h264,
    h264_level,
    letterbox,
    macroblocks,
    output_size,
)
from screenrecorder.core.models import Rect


@pytest.mark.parametrize(
    "src,dst,expected",
    [
        ((1920, 1080), (1920, 1080), Rect(0, 0, 1920, 1080)),
        ((3840, 2160), (1280, 720), Rect(0, 0, 1280, 720)),
        ((1080, 1920), (1920, 1080), Rect(656, 0, 606, 1080)),  # portrait -> pillarbox
        ((2560, 1080), (1920, 1080), Rect(0, 134, 1920, 810)),  # ultrawide -> letterbox
        ((1000, 700), (1280, 720), Rect(126, 0, 1028, 720)),
    ],
)
def test_letterbox(src, dst, expected):
    assert letterbox(*src, *dst) == expected


@pytest.mark.parametrize("src", [(1366, 768), (1001, 333), (333, 1001), (4096, 2160), (17, 9)])
def test_letterbox_is_even_and_inside(src):
    r = letterbox(*src, 1920, 1080)
    assert r.x % 2 == 0 and r.y % 2 == 0 and r.width % 2 == 0 and r.height % 2 == 0
    assert r.right <= 1920 and r.bottom <= 1080
    # Aspect ratio preserved within rounding of one 2-pixel step.
    assert abs(r.width / r.height - src[0] / src[1]) < 0.02 * (src[0] / src[1]) + 0.01


def test_output_size_presets():
    assert output_size("720p", 2560, 1440) == (1280, 720)
    assert output_size("1080p", 1080, 1920) == (1920, 1080)
    assert output_size("2160p", 1920, 1080) == (3840, 2160)


def test_output_size_source_aspect():
    w, h = output_size("1080p", 1080, 1920, aspect_mode="source")
    assert h == 1080 and w == 606


def test_output_size_native_clamped_and_even():
    assert output_size("native", 1365, 767) == (1364, 766)
    w, h = output_size("native", 5120, 2880)
    assert w <= 4096 and macroblocks(w, h) <= 36864 and w % 2 == 0 and h % 2 == 0
    assert abs(w / h - 16 / 9) < 0.01


def test_clamp_to_h264_minimum():
    assert clamp_to_h264(10, 10) == (64, 64)


def test_bitrate_monotonic_and_bounded():
    assert bitrate_for(1280, 720, 30) < bitrate_for(1920, 1080, 30) < bitrate_for(3840, 2160, 30)
    assert bitrate_for(1920, 1080, 30) < bitrate_for(1920, 1080, 60) < 2 * bitrate_for(1920, 1080, 30)
    assert bitrate_for(3840, 2160, 60, "high") <= 80_000_000
    assert bitrate_for(64, 64, 15, "low") >= 1_000_000


@pytest.mark.parametrize(
    "size,fps,level",
    [((1280, 720), 30, "3.1"), ((1920, 1080), 30, "4.0"), ((1920, 1080), 60, "4.2"), ((3840, 2160), 30, "5.1"), ((3840, 2160), 60, "5.2")],
)
def test_h264_level(size, fps, level):
    assert h264_level(*size, fps) == level


def test_build_plan_region():
    plan = build_plan((2560, 1440), Rect(100, 100, 801, 451), "720p", 30, backend="wgc")
    assert plan.crop == Rect(100, 100, 801, 451)
    assert plan.output_size == (1280, 720)
    assert plan.pixel_format == "NV12" and plan.data_path == "gpu"
    plan = build_plan((1920, 1080), Rect(1900, 1000, 500, 500), "720p", 30, backend="dxcam", video_processor=False)
    assert plan.crop == Rect(1900, 1000, 20, 80)
    assert plan.pixel_format == "RGB32" and plan.data_path == "cpu-upload"
