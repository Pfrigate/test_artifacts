"""Media Foundation encoder: synthetic GPU frames and CPU uploads -> MP4, verified with the
MF Source Reader and the MP4 box parser."""

import numpy as np
import pytest

from screenrecorder.core.encoder import MediaFoundationEncoder
from screenrecorder.core.frame_processor import bitrate_for
from screenrecorder.core.selftest import verify_mp4
from screenrecorder.native import native

MODES = [(w, h, fps) for (w, h) in [(1280, 720), (1920, 1080), (3840, 2160)] for fps in (15, 30, 60)]


def test_device(device):
    assert device.feature_level in ("10_0", "10_1", "11_0", "11_1")
    assert device.removed_reason() == 0
    print("adapter:", device.adapter_name, "hardware:", device.is_hardware, "vp:", device.video_processor_available)


def test_encoders_detected(capabilities):
    assert not capabilities.error
    assert capabilities.encoders, "no H.264 encoder MFT found"
    for line in capabilities.describe():
        print(line)
    assert capabilities.supports(1920, 1080, 30)


@pytest.mark.parametrize("width,height,fps", MODES, ids=[f"{h}p{f}" for (_, h, f) in MODES])
def test_synthetic_modes(device, output_dir, width, height, fps):
    frames = fps  # one second of video
    enc = MediaFoundationEncoder(
        device, output_dir / f"synthetic_{height}p{fps}.mp4", width, height, fps, bitrate_for(width, height, fps)
    )
    for _ in range(frames):
        enc.write_synthetic()
    info = enc.info()
    path = enc.finalize()
    assert path is not None and path.exists() and not enc.part_path.exists()
    report = verify_mp4(path, width, height, fps, frames)
    print(info, report["probe"], report["top_level_boxes"], report["warnings"])
    assert report["ok"], report["problems"]
    assert report["probe"]["duration"] == pytest.approx(1.0, abs=1.5 / fps)
    assert report["probe"]["keyframe_count"] >= 1


@pytest.mark.parametrize("converter", ["video_processor", "shader"])
def test_converters(device, output_dir, converter):
    if converter == "video_processor" and not device.video_processor_available:
        pytest.skip("device has no D3D11 video support")
    enc = MediaFoundationEncoder(device, output_dir / f"converter_{converter}.mp4", 1920, 1080, 30, 8_000_000, converter=converter)
    assert enc.info()["converter"] == converter
    assert enc.info()["input_subtype"] == ("NV12" if converter == "video_processor" else "RGB32")
    for _ in range(30):
        enc.write_synthetic()
    report = verify_mp4(enc.finalize(), 1920, 1080, 30, 30)
    assert report["ok"], report["problems"]


def test_software_encoder(device, output_dir):
    enc = MediaFoundationEncoder(device, output_dir / "software.mp4", 1280, 720, 30, 4_000_000, encoder_preference="software")
    assert not enc.info()["hardware"]
    for _ in range(15):
        enc.write_synthetic()
    assert verify_mp4(enc.finalize(), 1280, 720, 30, 15)["ok"]


def test_cpu_upload_non_contiguous_letterboxed(device, output_dir):
    """DXcam-style path: numpy BGRA view with a row stride larger than width*4."""
    big = np.zeros((900, 1400, 4), dtype=np.uint8)
    view = big[100:800, 200:1201]  # 1001x700, odd width, non-contiguous rows
    assert not view.flags["C_CONTIGUOUS"]
    enc = MediaFoundationEncoder(device, output_dir / "cpu_upload.mp4", 1280, 720, 30, 4_000_000)
    for i in range(30):
        view[..., 0] = (i * 8) % 256
        view[..., 1] = 128
        view[..., 2] = 255 - (i * 8) % 256
        view[..., 3] = 255
        assert enc.write_bgra(view) == i
    report = verify_mp4(enc.finalize(), 1280, 720, 30, 30)
    assert report["ok"], report["problems"]


def test_explicit_indices_keep_wall_clock_timeline(device, output_dir):
    enc = MediaFoundationEncoder(device, output_dir / "gaps.mp4", 1280, 720, 30, 4_000_000)
    frame = np.full((720, 1280, 4), 200, dtype=np.uint8)
    for index in (0, 1, 2, 10, 11):
        enc.write_bgra(frame, index)
    with pytest.raises(native.NativeError):
        enc.write_bgra(frame, 11)  # non-monotonic timestamps are rejected
    report = verify_mp4(enc.finalize(), 1280, 720, 30)
    assert report["ok"], report["problems"]
    # Either the 5 samples are kept with their timestamps, or the encoder fills the gap by
    # repeating frames (Microsoft's software MFT does); the timeline must reach index 11.
    assert report["probe"]["frame_count"] in (5, 12)
    assert 11 / 30 - 1e-3 <= report["probe"]["last_pts"] <= 12 / 30 + 1e-3


def test_zero_frames_leaves_no_file(device, output_dir):
    enc = MediaFoundationEncoder(device, output_dir / "empty.mp4", 1280, 720, 30, 4_000_000)
    assert enc.finalize() is None
    assert not enc.part_path.exists() and not (output_dir / "empty.mp4").exists()


@pytest.mark.parametrize("size", [(1281, 720), (32, 32), (8192, 4320)])
def test_invalid_sizes_rejected(device, output_dir, size):
    with pytest.raises(native.NativeError):
        native.Encoder(device, str(output_dir / "invalid.mp4"), size[0], size[1], 30, 4_000_000)


def test_letterbox_matches_python():
    from screenrecorder.core.frame_processor import letterbox

    for src in [(1920, 1080), (1080, 1920), (1001, 700), (2560, 1080), (17, 9)]:
        for dst in [(1280, 720), (1920, 1080), (3840, 2160)]:
            assert native.letterbox(*src, *dst) == letterbox(*src, *dst).as_tuple()


def test_thumbnail(device, output_dir):
    enc = MediaFoundationEncoder(device, output_dir / "thumb.mp4", 1280, 720, 30, 4_000_000)
    for _ in range(30):
        enc.write_synthetic()
    path = enc.finalize()
    decoders = native.enumerate_decoders()
    print("H.264 decoders:", decoders)
    try:
        data, w, h = native.thumbnail(str(path), 0.5)
    except native.NativeError as exc:
        if not decoders:
            pytest.skip(f"no H.264 decoder MFT on this system: {exc}")
        raise
    assert (w, h) == (1280, 720) and len(data) == w * h * 4
    pixels = np.frombuffer(data, dtype=np.uint8).reshape(h, w, 4)
    assert pixels[..., :3].std() > 1.0  # not a blank frame
