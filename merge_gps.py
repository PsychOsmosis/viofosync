"""Preserve indexed VIOFO GPS in copy-concatenated, nonfragmented MP4 files.

Only a reserved prefix is rewritten. Native records retain their original bytes
and UTC clocks. The versioned GPX extension states media times only when video
sample tables prove the mapping; otherwise the route remains explicitly untimed.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import math
import os
import re
import stat
import struct
import subprocess
import uuid
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


GPS_UUID = uuid.UUID("3ed8a804-a841-539e-9f3c-82ac6baf4f89").bytes
GPX_NS = "http://www.topografix.com/GPX/1/1"
MERGE_NS = "urn:viofosync:merge-gps:1"
_MAX_MOOV = 16 * 1024 * 1024
_MAX_GPX = 2 * 1024 * 1024
_MAX_RECORDS = 12000
_MAX_RECORD_BYTES = 512 * 1024 * 1024
_MAX_SAMPLES = 2_000_000
_MAX_BOXES = 100000
_SECOND = 1_000_000
_ACTIVE = re.compile(b"A[NS][EW]")


class MergeGPSError(RuntimeError):
    """The requested GPS-preserving merge cannot be verified safely."""


class _TimingUnavailable(Exception):
    pass


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns)


def _open(path, *, write=False):
    flags = (os.O_RDWR if write else os.O_RDONLY) | os.O_CLOEXEC | os.O_NOFOLLOW
    fd = os.open(path, flags)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise MergeGPSError("GPS metadata requires a regular file")
    return fd


def _read(fd, offset, length, end):
    if offset < 0 or length < 0 or offset + length > end:
        raise MergeGPSError("GPS metadata points outside its file")
    data = os.pread(fd, length, offset)
    if len(data) != length:
        raise MergeGPSError("GPS metadata changed or is truncated")
    return data


def _write(fd, data, offset):
    while data:
        written = os.pwrite(fd, data, offset)
        if written <= 0:
            raise MergeGPSError("Cannot write complete GPS metadata")
        offset += written
        data = data[written:]


def _file_boxes(fd, end):
    offset = 0
    count = 0
    while offset < end:
        count += 1
        if count > _MAX_BOXES:
            raise MergeGPSError("Too many MP4 boxes")
        length, kind = struct.unpack(">I4s", _read(fd, offset, 8, end))
        header = 8
        if length == 1:
            length = struct.unpack(">Q", _read(fd, offset + 8, 8, end))[0]
            header = 16
        elif length == 0:
            length = end - offset
        if length < header or offset + length > end:
            raise MergeGPSError("Invalid MP4 box length")
        yield offset, length, kind, header
        offset += length


def _boxes(data):
    offset = 0
    count = 0
    while offset < len(data):
        count += 1
        if count > _MAX_BOXES or offset + 8 > len(data):
            raise MergeGPSError("Invalid MP4 metadata boxes")
        length, kind = struct.unpack_from(">I4s", data, offset)
        header = 8
        if length == 1:
            if offset + 16 > len(data):
                raise MergeGPSError("Truncated extended MP4 box")
            length = struct.unpack_from(">Q", data, offset + 8)[0]
            header = 16
        elif length == 0:
            length = len(data) - offset
        if length < header or offset + length > len(data):
            raise MergeGPSError("Invalid MP4 metadata length")
        yield kind, data[offset + header:offset + length]
        offset += length


def _one(data, kind, *, optional=False):
    matches = [payload for name, payload in _boxes(data) if name == kind]
    if not matches and optional:
        return None
    if len(matches) != 1:
        raise _TimingUnavailable()
    return matches[0]


def _moov(fd, size):
    boxes = list(_file_boxes(fd, size))
    if any(kind in (b"moof", b"mfra") for _, _, kind, _ in boxes):
        raise MergeGPSError("Fragmented MP4 GPS merging is unsupported")
    movies = [box for box in boxes if box[2] == b"moov"]
    if len(movies) != 1:
        raise MergeGPSError("Expected one MP4 movie box")
    position, length, _, header = movies[0]
    if length > _MAX_MOOV:
        raise MergeGPSError("MP4 movie metadata exceeds the supported limit")
    return boxes, movies[0], _read(fd, position + header, length - header, size)


def _clock_header(data):
    if data is None or not data:
        raise _TimingUnavailable()
    if data[0] == 0 and len(data) >= 20:
        scale, duration = struct.unpack_from(">II", data, 12)
    elif data[0] == 1 and len(data) >= 32:
        scale, duration = struct.unpack_from(">IQ", data, 20)
    else:
        raise _TimingUnavailable()
    if not scale or not 0 < duration / scale < 366 * 86400:
        raise _TimingUnavailable()
    return scale, duration


def _us(ticks, scale):
    # Integer nearest rounding, including negative composition timestamps.
    return (ticks * _SECOND + scale // 2) // scale


@dataclass(frozen=True)
class _Video:
    duration_us: int
    samples: tuple


def _video(moov):
    """Read presentation times solely from stts/ctts and ordinary edit lists."""
    try:
        movie_scale, _ = _clock_header(_one(moov, b"mvhd"))
        tracks = []
        for kind, track in _boxes(moov):
            if kind != b"trak":
                continue
            media = _one(track, b"mdia")
            handler = _one(media, b"hdlr")
            if len(handler) >= 12 and handler[8:12] == b"vide":
                tracks.append((track, media))
        if len(tracks) != 1:
            raise _TimingUnavailable()
        track, media = tracks[0]
        scale, duration = _clock_header(_one(media, b"mdhd"))
        table = _one(_one(media, b"minf"), b"stbl")
        sizes = _one(table, b"stsz")
        if len(sizes) < 12:
            raise _TimingUnavailable()
        sample_size, sample_count = struct.unpack_from(">II", sizes, 4)
        expected_sizes_length = 12 if sample_size else 12 + 4 * sample_count
        if len(sizes) != expected_sizes_length or not 0 < sample_count <= _MAX_SAMPLES:
            raise _TimingUnavailable()
        stts = _one(table, b"stts")
        if len(stts) < 8 or len(stts) != 8 + 8 * struct.unpack_from(">I", stts, 4)[0]:
            raise _TimingUnavailable()
        times = []
        ticks = 0
        for pos in range(8, len(stts), 8):
            count, delta = struct.unpack_from(">II", stts, pos)
            if not count or not delta or len(times) + count > sample_count:
                raise _TimingUnavailable()
            times.extend(range(ticks, ticks + count * delta, delta))
            ticks += count * delta
        if len(times) != sample_count or ticks != duration:
            raise _TimingUnavailable()
        ctts = _one(table, b"ctts", optional=True)
        if ctts is not None:
            if (len(ctts) < 8 or ctts[0] not in (0, 1)
                    or len(ctts) != 8 + 8 * struct.unpack_from(">I", ctts, 4)[0]):
                raise _TimingUnavailable()
            index = 0
            for pos in range(8, len(ctts), 8):
                count, shift = struct.unpack_from(">II" if ctts[0] == 0 else ">Ii", ctts, pos)
                if not count or index + count > sample_count:
                    raise _TimingUnavailable()
                for i in range(index, index + count):
                    times[i] += shift
                index += count
            if index != sample_count:
                raise _TimingUnavailable()
        edit_shift_us = 0
        edits = _one(track, b"edts", optional=True)
        if edits is not None:
            elst = _one(edits, b"elst")
            if len(elst) < 8 or elst[0] not in (0, 1):
                raise _TimingUnavailable()
            count = struct.unpack_from(">I", elst, 4)[0]
            width = 12 if elst[0] == 0 else 20
            if count not in (1, 2) or len(elst) != 8 + count * width:
                raise _TimingUnavailable()
            entries = [struct.unpack_from(">IiHH" if elst[0] == 0 else ">QqHH", elst, 8 + i * width)
                       for i in range(count)]
            if any(rate != 1 or fraction for _, _, rate, fraction in entries):
                raise _TimingUnavailable()
            if count == 2:
                if entries[0][1] != -1:
                    raise _TimingUnavailable()
                edit_shift_us += _us(entries[0][0], movie_scale)
            if entries[-1][1] < 0:
                raise _TimingUnavailable()
            edit_shift_us -= _us(entries[-1][1], scale)
        return _Video(_us(duration, scale), tuple(_us(t, scale) + edit_shift_us for t in times))
    except (struct.error, _TimingUnavailable):
        return None


def _video_duration(moov):
    try:
        values = []
        for kind, track in _boxes(moov):
            if kind != b"trak":
                continue
            media = _one(track, b"mdia")
            handler = _one(media, b"hdlr")
            if len(handler) >= 12 and handler[8:12] == b"vide":
                scale, ticks = _clock_header(_one(media, b"mdhd"))
                values.append(_us(ticks, scale))
        if len(values) != 1:
            raise _TimingUnavailable()
        return values[0]
    except (struct.error, _TimingUnavailable):
        raise MergeGPSError("Cannot establish the merged video duration") from None


def _coordinate(raw, hemi, limit):
    if not math.isfinite(raw) or raw < 0:
        return None
    degrees = int(raw // 100)
    minutes = raw - degrees * 100
    value = degrees + minutes / 60
    if not 0 <= minutes < 60 or value > limit:
        return None
    return -value if hemi in (ord("S"), ord("W")) else value


def _fix(data):
    for match in reversed(list(_ACTIVE.finditer(data))):
        offset = match.start() - 24
        if offset < 0 or offset + 44 > len(data):
            continue
        fields = struct.unpack_from("<6I", data, offset)
        hour, minute, second, year, month, day = fields
        try:
            clock = datetime(2000 + year, month, day, hour, minute, second, tzinfo=timezone.utc)
        except (ValueError, OverflowError):
            continue
        lat, lon, speed, course = struct.unpack_from("<4f", data, offset + 28)
        lat = _coordinate(lat, data[offset + 25], 90)
        lon = _coordinate(lon, data[offset + 26], 180)
        if lat is None or lon is None:
            continue
        return (int(clock.timestamp()) * _SECOND, lat, lon,
                speed * 0.514444 if math.isfinite(speed) and speed >= 0 else None,
                course if math.isfinite(course) and 0 <= course < 360 else None)
    return None


def _capture_start(recording, zone):
    clock = recording.datetime
    if not isinstance(clock, datetime) or getattr(recording, "mode", None) not in ("normal", "driving"):
        return None
    if clock.tzinfo is None:
        if zone is None:
            return None
        candidates = []
        for fold in (0, 1):
            candidate = clock.replace(tzinfo=zone, fold=fold)
            utc = candidate.astimezone(timezone.utc)
            if utc.astimezone(zone).replace(tzinfo=None) == clock:
                candidates.append(utc)
        if not candidates or len({c.timestamp() for c in candidates}) != 1:
            return None
        clock = candidates[0]
    return round(clock.timestamp() * _SECOND)


@dataclass(frozen=True)
class _Record:
    position: int
    length: int
    digest: bytes
    fix: object


@dataclass(frozen=True)
class _Source:
    path: str
    identity: tuple
    records: tuple
    video: object
    capture_start_us: object


def _native(fd, size, moov):
    indexes = [payload for kind, payload in _boxes(moov) if kind == b"gps "]
    if not indexes:
        return None, ()
    if len(indexes) != 1 or len(indexes[0]) < 8 or (len(indexes[0]) - 8) % 8:
        raise MergeGPSError("Invalid native GPS index")
    index = indexes[0]
    count = (len(index) - 8) // 8
    if struct.unpack_from(">I", index, 4)[0] != count or count > _MAX_RECORDS:
        raise MergeGPSError("Invalid native GPS index count")
    records = []
    for pos in range(8, len(index), 8):
        position, length = struct.unpack_from(">II", index, pos)
        if not 12 <= length <= 65536:
            raise MergeGPSError("Invalid native GPS record size")
        raw = _read(fd, position, length, size)
        if struct.unpack_from(">I4s4s", raw) != (length, b"free", b"GPS "):
            raise MergeGPSError("Invalid native GPS record")
        records.append(_Record(position, length, hashlib.sha256(raw).digest(), _fix(raw[12:])))
    return index[:4], tuple(records)


def _duration(path):
    try:
        result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                                capture_output=True, text=True, check=False)
    except OSError:
        raise MergeGPSError("ffprobe is required for GPS merging") from None
    try:
        value = Decimal(result.stdout.strip())
        if result.returncode or not value.is_finite() or not 0 < value < 366 * 86400:
            raise ValueError()
        duration_us = int((value * _SECOND).to_integral_value(rounding=ROUND_HALF_UP))
        if duration_us <= 0:
            raise ValueError()
    except (ValueError, InvalidOperation):
        raise MergeGPSError("Cannot establish an exact concat duration") from None
    return f"{duration_us // _SECOND}.{duration_us % _SECOND:06d}"


def _box(kind, payload):
    return struct.pack(">I4s", len(payload) + 8, kind) + payload


class MergeGPSPlan:
    """Prepared source identities, bounded native metadata and concat durations."""

    def __init__(self, sources, durations, reserve_bytes, flags):
        self.sources = tuple(sources)
        self.concat_durations = tuple(durations)
        self.reserve_bytes = reserve_bytes
        self._flags = flags

    def _assert_sources(self):
        for source in self.sources:
            fd = _open(source.path)
            try:
                if _identity(os.fstat(fd)) != source.identity:
                    raise MergeGPSError("A GPS merge source changed")
            finally:
                os.close(fd)

    def _gpx(self, output_moov):
        duration_us = _video_duration(output_moov)
        output_video = _video(output_moov)
        qualified = output_video is not None and all(s.video is not None and s.capture_start_us is not None
                                                    for s in self.sources)
        mapped = []
        cursor = 0
        previous = -1
        for source in self.sources:
            segment_times = []
            previous_capture = None
            if qualified:
                count = len(source.video.samples)
                output_samples = output_video.samples[cursor:cursor + count]
                cursor += count
                if len(output_samples) != count:
                    qualified = False
                else:
                    shift = output_samples[0] - source.video.samples[0]
                    if any(abs(out - original - shift) > 2 for out, original in zip(output_samples, source.video.samples)):
                        qualified = False
                    segment_start = min(output_samples)
                    source_start = min(source.video.samples)
                    source_end = source.video.duration_us
                    # A clipped or unusual edit list cannot qualify capture clocks.
                    if source_start < 0 or source_end <= source_start:
                        qualified = False
            for record in source.records:
                if record.fix is None:
                    continue
                media_time = None
                if qualified:
                    capture = record.fix[0]
                    offset = capture - source.capture_start_us
                    source_duration = source_end - source_start
                    if previous_capture is not None and capture < previous_capture:
                        qualified = False
                    elif 0 <= offset <= source_duration and capture != previous_capture:
                        candidate = segment_start + offset
                        if candidate < 0 or candidate < previous or candidate > duration_us:
                            qualified = False
                        elif candidate > previous:
                            media_time = candidate
                            previous = candidate
                    # Native records remain complete. For a qualified playback
                    # track only, omit out-of-video fixes and retain the first
                    # fix for a repeated clock second or coincident boundary.
                    # Even omitted fixes must not hide a backward capture clock.
                    previous_capture = capture
                segment_times.append((record.fix, media_time))
            mapped.append(segment_times)
        if output_video is not None and cursor != len(output_video.samples):
            qualified = False
        if previous < 0:
            # No eligible playback fix: preserve the complete untimed route.
            qualified = False
        ET.register_namespace("", GPX_NS)
        ET.register_namespace("vs", MERGE_NS)
        root = ET.Element(f"{{{GPX_NS}}}gpx", {"version": "1.1", "creator": "Viofosync"})
        extensions = ET.SubElement(root, f"{{{GPX_NS}}}extensions")
        ET.SubElement(extensions, f"{{{MERGE_NS}}}merge_gps", {
            "version": "1", "timing": "media" if qualified else "unavailable",
            "duration_us": str(duration_us)})
        track = ET.SubElement(root, f"{{{GPX_NS}}}trk")
        for segment_points in mapped:
            segment = ET.SubElement(track, f"{{{GPX_NS}}}trkseg")
            for fix, media_time in segment_points:
                if qualified and media_time is None:
                    continue
                clock, lat, lon, speed, course = fix
                point = ET.SubElement(segment, f"{{{GPX_NS}}}trkpt", {"lat": repr(lat), "lon": repr(lon)})
                ET.SubElement(point, f"{{{GPX_NS}}}time").text = datetime.fromtimestamp(clock // _SECOND, timezone.utc).isoformat().replace("+00:00", "Z")
                extra = ET.SubElement(point, f"{{{GPX_NS}}}extensions")
                if qualified:
                    ET.SubElement(extra, f"{{{MERGE_NS}}}media_time_us").text = str(media_time)
                    ET.SubElement(extra, f"{{{MERGE_NS}}}precision_us").text = str(_SECOND)
                if speed is not None:
                    ET.SubElement(extra, f"{{{MERGE_NS}}}speed_mps").text = repr(speed)
                if course is not None:
                    ET.SubElement(extra, f"{{{MERGE_NS}}}course_degrees").text = repr(course)
        # GPX 1.1 places root extensions after tracks.
        root.remove(extensions)
        root.append(extensions)
        data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        if len(data) > _MAX_GPX:
            raise MergeGPSError("Merged GPX exceeds the supported metadata limit")
        return data

    def embed_verify(self, output_path):
        """Patch a reserved prefix, verify every native record, return GPX bytes.

        The caller must publish this temporary output only after this succeeds.
        Sources are never modified. A no-GPS group returns None.
        """
        self._assert_sources()
        records = [record for source in self.sources for record in source.records]
        fd = _open(output_path, write=True)
        try:
            size = os.fstat(fd).st_size
            boxes, movie, moov = _moov(fd, size)
            if any(kind == b"gps " or kind == b"uuid" and payload[:16] == GPS_UUID
                   for kind, payload in _boxes(moov)):
                raise MergeGPSError("Merged output already contains GPS metadata")
            if not records:
                return None
            gpx = self._gpx(moov)
            pos, length, _, header = movie
            if header != 8:
                raise MergeGPSError("An extended movie header cannot use the reserved prefix")
            following = next((box for box in boxes if box[0] == pos + length), None)
            if following is None or following[2] != b"free" or following[3] != 8:
                raise MergeGPSError("Merged output has no usable GPS reservation")
            index_length = 16 + 8 * len(records)
            uuid_box = _box(b"uuid", GPS_UUID + gpx)
            new_length = length + index_length + len(uuid_box)
            position = pos + new_length
            entries = []
            for record in records:
                if position + record.length > 0x100000000:
                    raise MergeGPSError("Native GPS offsets exceed the 32-bit index")
                entries.append((position, record.length))
                position += record.length
            end = following[0] + following[1]
            if end - position < 8 or new_length > 0xffffffff:
                raise MergeGPSError("The reserved GPS prefix is too small")
            index = _box(b"gps ", self._flags + struct.pack(">I", len(records))
                         + b"".join(struct.pack(">II", *entry) for entry in entries))
            _write(fd, index + uuid_box, pos + length)
            entry_iter = iter(entries)
            for source in self.sources:
                source_fd = _open(source.path)
                try:
                    if _identity(os.fstat(source_fd)) != source.identity:
                        raise MergeGPSError("A GPS merge source changed")
                    for record in source.records:
                        raw = _read(source_fd, record.position, record.length, source.identity[2])
                        if hashlib.sha256(raw).digest() != record.digest:
                            raise MergeGPSError("A native GPS record changed")
                        target, _ = next(entry_iter)
                        _write(fd, raw, target)
                    if _identity(os.fstat(source_fd)) != source.identity:
                        raise MergeGPSError("A GPS merge source changed")
                finally:
                    os.close(source_fd)
            _write(fd, struct.pack(">I4s", end - position, b"free"), position)
            _write(fd, struct.pack(">I", new_length), pos)
            os.fsync(fd)
            _, _, embedded_moov = _moov(fd, size)
            _, actual = _native(fd, size, embedded_moov)
            if [r.digest for r in actual] != [r.digest for r in records]:
                raise MergeGPSError("Embedded native GPS verification failed")
            payloads = [p[16:] for kind, p in _boxes(embedded_moov)
                        if kind == b"uuid" and p[:16] == GPS_UUID]
            if payloads != [gpx] or os.fstat(fd).st_size != size:
                raise MergeGPSError("Embedded GPX verification failed")
            self._assert_sources()
            return gpx
        finally:
            os.close(fd)


def prepare(group):
    """Prepare exact concat durations and native GPS without reading media data.

    An explicitly set TZ is the camera-local filename timezone. Missing/invalid
    TZ, ambiguous DST, parking footage, or unqualified sample tables preserve GPS
    but deliberately omit media-time claims.
    """
    try:
        zone = ZoneInfo(os.environ["TZ"]) if os.environ.get("TZ") else None
    except (ZoneInfoNotFoundError, ValueError):
        zone = None
    sources = []
    durations = []
    flags = None
    reserve = 1024 * 1024
    count = 0
    total_bytes = 0
    total_samples = 0
    if len(group) > 256:
        raise MergeGPSError("Too many GPS merge sources")
    if not group:
        raise MergeGPSError("Cannot prepare an empty GPS merge")
    for recording in group:
        fd = _open(recording.filepath)
        try:
            before = _identity(os.fstat(fd))
            _, movie, moov = _moov(fd, before[2])
            source_flags, records = _native(fd, before[2], moov)
            if source_flags is not None:
                if flags is not None and flags != source_flags:
                    raise MergeGPSError("Incompatible native GPS index versions")
                flags = source_flags
            count += len(records)
            total_bytes += sum(record.length for record in records)
            if count > _MAX_RECORDS or total_bytes > _MAX_RECORD_BYTES:
                raise MergeGPSError("Native GPS metadata exceeds the supported limit")
            durations.append(_duration(recording.filepath))
            video = _video(moov)
            if video is not None:
                total_samples += len(video.samples)
                if total_samples > _MAX_SAMPLES:
                    video = None
            sources.append(_Source(recording.filepath, before, records, video,
                                   _capture_start(recording, zone)))
            if _identity(os.fstat(fd)) != before:
                raise MergeGPSError("A GPS merge source changed")
            reserve += 2 * movie[1]
        finally:
            os.close(fd)
    # Moov estimates have headroom for regenerated sample tables; unused bytes
    # remain a standards-compliant free box. Never move mdat to make this fit.
    reserve += total_bytes + 16 + 8 * count + 24 + _MAX_GPX + 8
    if reserve >= 0x7fffffff:
        raise MergeGPSError("The GPS reservation exceeds the muxer limit")
    return MergeGPSPlan(sources, durations, reserve, flags or bytes(4))
