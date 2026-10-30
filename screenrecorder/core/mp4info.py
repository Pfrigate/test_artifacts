"""Minimal ISO-BMFF (MP4) box reader used to verify PowerPoint compatibility of the files we
write: H.264 track, profile/level, no audio track, moov placed before mdat."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterator

_CONTAINERS = {"moov", "trak", "mdia", "minf", "stbl", "edts", "dinf", "udta"}


@dataclass
class TrackInfo:
    handler: str = ""
    codec: str = ""
    width: int = 0
    height: int = 0
    avc_profile: int = 0
    avc_level: int = 0


@dataclass
class Mp4Info:
    major_brand: str = ""
    compatible_brands: list[str] = field(default_factory=list)
    top_level: list[str] = field(default_factory=list)
    timescale: int = 0
    duration: float = 0.0
    tracks: list[TrackInfo] = field(default_factory=list)

    @property
    def moov_before_mdat(self) -> bool:
        return "moov" in self.top_level and "mdat" in self.top_level and (
            self.top_level.index("moov") < self.top_level.index("mdat")
        )

    @property
    def video_tracks(self) -> list[TrackInfo]:
        return [t for t in self.tracks if t.handler == "vide"]

    @property
    def has_audio(self) -> bool:
        return any(t.handler == "soun" for t in self.tracks)

    def powerpoint_issues(self) -> list[str]:
        """Empty list when the file matches what PowerPoint (Windows/Mac) plays natively."""
        issues = []
        videos = self.video_tracks
        if len(videos) != 1:
            issues.append(f"expected exactly one video track, found {len(videos)}")
        for track in videos:
            if track.codec != "avc1":
                issues.append(f"video codec {track.codec!r} is not H.264 (avc1)")
            if track.avc_profile not in (66, 77, 100):
                issues.append(f"H.264 profile_idc {track.avc_profile} not baseline/main/high")
            if track.avc_level > 52:
                issues.append(f"H.264 level {track.avc_level / 10:.1f} above 5.2")
            if track.width % 2 or track.height % 2:
                issues.append("odd frame dimensions")
        if self.duration <= 0:
            issues.append("zero duration")
        return issues


def _iter_boxes(data: bytes, start: int, end: int) -> Iterator[tuple[str, int, int]]:
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, pos)
        header = 8
        if size == 1:
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            return
        yield kind.decode("latin-1"), pos + header, pos + size
        pos += size


def _read_top_level(f: BinaryIO) -> Iterator[tuple[str, int, int]]:
    f.seek(0, 2)
    file_size = f.tell()
    pos = 0
    while pos + 8 <= file_size:
        f.seek(pos)
        header = f.read(16)
        size, kind = struct.unpack_from(">I4s", header, 0)
        header_len = 8
        if size == 1:
            size = struct.unpack_from(">Q", header, 8)[0]
            header_len = 16
        elif size == 0:
            size = file_size - pos
        if size < header_len:
            return
        yield kind.decode("latin-1"), pos + header_len, pos + size
        pos += size


def _parse_stsd(data: bytes, start: int, end: int, track: TrackInfo) -> None:
    # FullBox header (4) + entry_count (4), then sample entries.
    for kind, body, entry_end in _iter_boxes(data, start + 8, end):
        track.codec = kind
        if kind in ("avc1", "avc3", "hvc1", "hev1", "mp4v"):
            # VisualSampleEntry: 6 reserved + 2 data_ref + 16 pre_defined/reserved, then size.
            track.width, track.height = struct.unpack_from(">HH", data, body + 24)
            for child, cbody, _cend in _iter_boxes(data, body + 78, entry_end):
                if child == "avcC":
                    track.avc_profile = data[cbody + 1]
                    track.avc_level = data[cbody + 3]
        return


def _walk(data: bytes, start: int, end: int, info: Mp4Info, track: TrackInfo | None) -> None:
    for kind, body, box_end in _iter_boxes(data, start, end):
        if kind == "mvhd":
            version = data[body]
            if version == 1:
                timescale, duration = struct.unpack_from(">IQ", data, body + 20)
            else:
                timescale, duration = struct.unpack_from(">II", data, body + 12)
            info.timescale = timescale
            info.duration = duration / timescale if timescale else 0.0
        elif kind == "trak":
            new_track = TrackInfo()
            info.tracks.append(new_track)
            _walk(data, body, box_end, info, new_track)
        elif kind == "hdlr" and track is not None:
            track.handler = data[body + 8 : body + 12].decode("latin-1")
        elif kind == "stsd" and track is not None:
            _parse_stsd(data, body, box_end, track)
        elif kind in _CONTAINERS:
            _walk(data, body, box_end, info, track)


def read_mp4_info(path: Path | str) -> Mp4Info:
    info = Mp4Info()
    with open(path, "rb") as f:
        for kind, body, end in _read_top_level(f):
            info.top_level.append(kind)
            if kind == "ftyp":
                f.seek(body)
                payload = f.read(end - body)
                info.major_brand = payload[:4].decode("latin-1")
                info.compatible_brands = [
                    payload[i : i + 4].decode("latin-1") for i in range(8, len(payload) - 3, 4)
                ]
            elif kind == "moov":
                f.seek(body)
                payload = f.read(end - body)
                _walk(payload, 0, len(payload), info, None)
    return info
