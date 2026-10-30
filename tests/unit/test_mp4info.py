import struct

from screenrecorder.core.mp4info import read_mp4_info


def box(kind: str, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind.encode()) + payload


def full(version=0, flags=0) -> bytes:
    return struct.pack(">I", (version << 24) | flags)


def avc1(width, height, profile, level) -> bytes:
    visual = bytes(6) + struct.pack(">H", 1) + bytes(16) + struct.pack(">HH", width, height) + bytes(70 - 20)
    avcc = box("avcC", bytes([1, profile, 0, level, 0xFF, 0xE0]))
    return box("avc1", visual + avcc)


def make_mp4(moov_first=True, audio=False, profile=100, level=40) -> bytes:
    ftyp = box("ftyp", b"mp42" + struct.pack(">I", 0) + b"mp41isom")
    mvhd = box("mvhd", full() + struct.pack(">IIII", 0, 0, 1000, 5000) + bytes(80))
    stsd = box("stsd", full() + struct.pack(">I", 1) + avc1(1920, 1080, profile, level))
    video = box("trak", box("mdia", box("hdlr", full() + bytes(4) + b"vide" + bytes(12)) + box("minf", box("stbl", stsd))))
    tracks = video
    if audio:
        tracks += box("trak", box("mdia", box("hdlr", full() + bytes(4) + b"soun" + bytes(12))))
    moov = box("moov", mvhd + tracks)
    mdat = box("mdat", bytes(100))
    return ftyp + (moov + mdat if moov_first else mdat + moov)


def test_parse_powerpoint_compatible(tmp_path):
    path = tmp_path / "a.mp4"
    path.write_bytes(make_mp4())
    info = read_mp4_info(path)
    assert info.major_brand == "mp42"
    assert info.top_level == ["ftyp", "moov", "mdat"]
    assert info.moov_before_mdat
    assert info.duration == 5.0
    (track,) = info.video_tracks
    assert (track.codec, track.width, track.height, track.avc_profile, track.avc_level) == ("avc1", 1920, 1080, 100, 40)
    assert not info.has_audio
    assert info.powerpoint_issues() == []


def test_detects_problems(tmp_path):
    path = tmp_path / "b.mp4"
    path.write_bytes(make_mp4(moov_first=False, audio=True, profile=244, level=60))
    info = read_mp4_info(path)
    assert not info.moov_before_mdat
    assert info.has_audio
    issues = info.powerpoint_issues()
    assert any("profile" in i for i in issues) and any("level" in i for i in issues)
