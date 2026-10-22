"""Metadata-only fixtures for GPS preservation and honest merge timing."""
import hashlib
import os
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

import merge_gps as gps


B = gps._box
BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def movie(deltas=(1000, 1000), extra=b"", *, composition=None):
    duration = sum(deltas)
    header = bytes(12) + struct.pack(">II", 1000, duration)
    stts = bytes(4) + struct.pack(">I", len(deltas))
    stts += b"".join(struct.pack(">II", 1, delta) for delta in deltas)
    tables = B(b"stts", stts) + B(b"stsz", bytes(4) + struct.pack(">II", 1, len(deltas)))
    if composition is not None:
        tables += B(b"ctts", bytes(4) + struct.pack(">I", len(composition))
                    + b"".join(struct.pack(">II", 1, shift) for shift in composition))
    media = (B(b"mdhd", header) + B(b"hdlr", bytes(8) + b"vide")
             + B(b"minf", B(b"stbl", tables)))
    return B(b"moov", B(b"mvhd", header) + B(b"trak", B(b"mdia", media)) + extra)


def raw_fix(second, *, active=True, latitude=100.0):
    clock = BASE + timedelta(seconds=second)
    payload = struct.pack("<6I4s4f", clock.hour, clock.minute, clock.second,
                          clock.year - 2000, clock.month, clock.day,
                          b"ANE\0" if active else b"VNE\0", latitude, 200.0, 3.0, 4.0)
    return B(b"free", b"GPS " + payload + bytes(20))


def source(path, seconds=(0, 1), *, start=BASE, deltas=(1000, 1000), mode="normal"):
    records = [raw_fix(second) if isinstance(second, int) else second for second in seconds]
    ftyp = B(b"ftyp", b"isom" + bytes(4))
    media = B(b"mdat", b"SOURCE-MEDIA")
    index = B(b"gps ", bytes(4) + struct.pack(">I", len(records)) + bytes(8 * len(records))) if records else b""
    moov = movie(deltas, index)
    position = len(ftyp) + len(moov) + len(media)
    entries = []
    for record in records:
        entries.append(struct.pack(">II", position, len(record)))
        position += len(record)
    if records:
        moov = movie(deltas, B(b"gps ", bytes(4) + struct.pack(">I", len(records)) + b"".join(entries)))
    path.write_bytes(ftyp + moov + media + b"".join(records))
    return SimpleNamespace(filepath=str(path), datetime=start, mode=mode, duration=2.0)


def output(path, reserve, *, deltas=(1000, 1000), sparse=False, composition=None):
    ftyp = B(b"ftyp", b"isom" + bytes(4))
    moov = movie(deltas, composition=composition)
    spare = reserve - len(moov)
    if spare < 8:
        raise ValueError("test reservation too small")
    prefix = ftyp + moov + B(b"free", bytes(spare - 8))
    payload = b"MERGED-MEDIA-UNCHANGED"
    with path.open("wb") as handle:
        handle.write(prefix)
        if sparse:
            length = 0x100000100
            handle.write(struct.pack(">I4sQ", 1, b"mdat", length))
            handle.write(payload)
            handle.seek(len(prefix) + length - 1)
            handle.write(b"\0")
        else:
            handle.write(B(b"mdat", payload))
    return len(prefix), payload


def native(path):
    fd = gps._open(path)
    try:
        _, _, moov = gps._moov(fd, os.fstat(fd).st_size)
        return gps._native(fd, os.fstat(fd).st_size, moov)[1]
    finally:
        os.close(fd)


def marker(data):
    root = ET.fromstring(data)
    return root, root.find(f"{{{gps.GPX_NS}}}extensions/{{{gps.MERGE_NS}}}merge_gps")


class MergeGPSMetadataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="viofosync-gps-test-")
        self.root = Path(self.tmp.name)
        self.duration = mock.patch.object(gps, "_duration", return_value="2.200000")
        self.duration.start()

    def tearDown(self):
        self.duration.stop()
        self.tmp.cleanup()

    def test_native_records_and_original_utc_survive_audio_padding_and_wall_gap(self):
        first = source(self.root / "a.mp4")
        second = source(self.root / "b.mp4", (5, 6), start=BASE + timedelta(seconds=5))
        before = [Path(s.filepath).read_bytes() for s in (first, second)]
        plan = gps.prepare([first, second])
        path = self.root / "merged.mp4"
        media_pos, media = output(path, plan.reserve_bytes, deltas=(1000, 1200, 1000, 1000))
        original_media = path.read_bytes()[media_pos:]
        data = plan.embed_verify(path)
        root, status = marker(data)
        self.assertEqual(status.attrib, {"version": "1", "timing": "media", "duration_us": "4200000"})
        points = root.findall(f".//{{{gps.GPX_NS}}}trkpt")
        media_times = [int(point.find(f"{{{gps.GPX_NS}}}extensions/{{{gps.MERGE_NS}}}media_time_us").text)
                       for point in points]
        self.assertEqual(media_times, [0, 1000000, 2200000, 3200000])
        self.assertEqual([p.find(f"{{{gps.GPX_NS}}}time").text for p in points],
                         ["2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z", "2026-01-01T00:00:05Z", "2026-01-01T00:00:06Z"])
        self.assertEqual(path.read_bytes()[media_pos:], original_media)
        self.assertEqual([Path(s.filepath).read_bytes() for s in (first, second)], before)
        self.assertEqual([r.digest for r in native(path)], [hashlib.sha256(raw_fix(i)).digest() for i in (0, 1, 5, 6)])
        self.assertEqual(plan.concat_durations, ("2.200000", "2.200000"))
        self.assertEqual(list(root)[-1].tag, f"{{{gps.GPX_NS}}}extensions")
        self.assertEqual(len(root.findall(f".//{{{gps.GPX_NS}}}trkseg")), 2)

    def test_sparse_over_four_gib_uses_low_native_offsets_without_moving_mdat(self):
        recording = source(self.root / "a.mp4")
        plan = gps.prepare([recording])
        path = self.root / "large.mp4"
        media_pos, payload = output(path, plan.reserve_bytes, sparse=True)
        with path.open("rb") as handle:
            handle.seek(media_pos)
            before = handle.read(16 + len(payload))
        size = path.stat().st_size
        plan.embed_verify(path)
        self.assertGreater(size, 0xffffffff)
        self.assertEqual(path.stat().st_size, size)
        self.assertTrue(all(r.position < 0xffffffff for r in native(path)))
        with path.open("rb") as handle:
            handle.seek(media_pos)
            self.assertEqual(handle.read(len(before)), before)

    def test_no_gps_is_legitimate_and_does_not_rewrite_output(self):
        recording = source(self.root / "none.mp4", ())
        plan = gps.prepare([recording])
        path = self.root / "merged.mp4"
        output(path, plan.reserve_bytes)
        before = path.read_bytes()
        self.assertIsNone(plan.embed_verify(path))
        self.assertEqual(path.read_bytes(), before)

    def test_inactive_records_and_no_gps_source_are_retained(self):
        inactive = raw_fix(0, active=False)
        first = source(self.root / "a.mp4", (inactive, 1))
        second = source(self.root / "b.mp4", (), start=BASE + timedelta(seconds=5))
        plan = gps.prepare([first, second])
        path = self.root / "merged.mp4"
        output(path, plan.reserve_bytes, deltas=(1000, 1000, 1000, 1000))
        root, _ = marker(plan.embed_verify(path))
        self.assertEqual([r.digest for r in native(path)], [hashlib.sha256(inactive).digest(), hashlib.sha256(raw_fix(1)).digest()])
        self.assertEqual(len(root.findall(f".//{{{gps.GPX_NS}}}trkseg")), 2)
        self.assertEqual(len(root.findall(f".//{{{gps.GPX_NS}}}trkpt")), 1)

    def test_truncated_or_wrong_native_index_aborts_preparation(self):
        for mutation in ("count", "offset", "magic"):
            with self.subTest(mutation=mutation):
                recording = source(self.root / "a.mp4")
                path = Path(recording.filepath)
                data = bytearray(path.read_bytes())
                index = data.index(b"gps ") - 4
                if mutation == "count":
                    struct.pack_into(">I", data, index + 12, 99)
                elif mutation == "offset":
                    struct.pack_into(">I", data, index + 16, len(data) - 1)
                else:
                    data[data.index(b"GPS ")] = ord("X")
                path.write_bytes(data)
                with self.assertRaises(gps.MergeGPSError):
                    gps.prepare([recording])

    def test_source_identity_change_aborts_before_touching_output(self):
        recording = source(self.root / "a.mp4")
        plan = gps.prepare([recording])
        path = self.root / "merged.mp4"
        output(path, plan.reserve_bytes)
        before = path.read_bytes()
        with open(recording.filepath, "ab") as handle:
            handle.write(b"changed")
        with self.assertRaises(gps.MergeGPSError):
            plan.embed_verify(path)
        self.assertEqual(path.read_bytes(), before)

    def test_insufficient_reservation_aborts_before_touching_output(self):
        recording = source(self.root / "a.mp4")
        plan = gps.prepare([recording])
        path = self.root / "merged.mp4"
        output(path, len(movie()) + 8)
        before = path.read_bytes()
        with self.assertRaises(gps.MergeGPSError):
            plan.embed_verify(path)
        self.assertEqual(path.read_bytes(), before)

    def test_unqualified_video_samples_preserve_static_route(self):
        first = source(self.root / "a.mp4")
        second = source(self.root / "b.mp4", (5, 6), start=BASE + timedelta(seconds=5))
        plan = gps.prepare([first, second])
        path = self.root / "merged.mp4"
        # The second source's sample spacing has changed: no constant shift.
        output(path, plan.reserve_bytes, deltas=(1000, 1200, 900, 1100))
        root, status = marker(plan.embed_verify(path))
        self.assertEqual(status.get("timing"), "unavailable")
        self.assertFalse(root.findall(f".//{{{gps.MERGE_NS}}}media_time_us"))
        self.assertFalse(root.findall(f".//{{{gps.MERGE_NS}}}precision_us"))
        self.assertEqual(len(native(path)), 4)

    def test_missing_tz_ambiguous_dst_and_parking_do_not_guess(self):
        cases = [(None, datetime(2026, 1, 1), "driving"),
                 ("America/Toronto", datetime(2026, 11, 1, 1, 30), "driving"),
                 ("UTC", datetime(2026, 1, 1), "parking")]
        for zone, clock, mode in cases:
            with self.subTest(zone=zone, mode=mode):
                recording = source(self.root / "a.mp4", start=clock, mode=mode)
                with mock.patch.dict(os.environ, {}, clear=True):
                    if zone:
                        os.environ["TZ"] = zone
                    plan = gps.prepare([recording])
                path = self.root / "merged.mp4"
                output(path, plan.reserve_bytes)
                root, status = marker(plan.embed_verify(path))
                self.assertEqual(status.get("timing"), "unavailable")
                self.assertFalse(root.findall(f".//{{{gps.MERGE_NS}}}media_time_us"))

    def test_explicit_tz_qualifies_a_unique_naive_clock(self):
        recording = source(self.root / "a.mp4", start=datetime(2025, 12, 31, 19))
        with mock.patch.dict(os.environ, {"TZ": "America/Montreal"}):
            plan = gps.prepare([recording])
        path = self.root / "merged.mp4"
        output(path, plan.reserve_bytes)
        _, status = marker(plan.embed_verify(path))
        self.assertEqual(status.get("timing"), "media")

    def test_nonmonotonic_or_no_eligible_capture_clocks_are_static(self):
        for seconds in ((1, 0), (30, 31), (-1, 0, 1, 1, 0, 2),
                        (-7, -8, 0, 1), (0, 1, 602, 601)):
            with self.subTest(seconds=seconds):
                recording = source(self.root / "a.mp4", seconds)
                plan = gps.prepare([recording])
                path = self.root / "merged.mp4"
                output(path, plan.reserve_bytes)
                root, status = marker(plan.embed_verify(path))
                self.assertEqual(status.get("timing"), "unavailable")
                self.assertEqual(len(root.findall(f".//{{{gps.GPX_NS}}}trkpt")), len(seconds))

    def test_stale_boundary_fixes_leave_a_moving_qualified_playback_track(self):
        # Observed camera patterns: a -7-second leading fix and +602-second
        # tail around a nominal 600-second video. Exact endpoints stay eligible.
        for seconds in ((-7, -1, 0, 1, 599, 600),
                        (1, 2, 599, 600, 601, 602),
                        (-7, 0, 1, 599, 600, 602)):
            with self.subTest(seconds=seconds):
                records = [raw_fix(second, latitude=100.0 + i / 100)
                           for i, second in enumerate(seconds)]
                recording = source(self.root / "a.mp4", records,
                                   deltas=(1000,) * 600)
                before = Path(recording.filepath).read_bytes()
                plan = gps.prepare([recording])
                path = self.root / "merged.mp4"
                media_pos, _ = output(path, plan.reserve_bytes,
                                      deltas=(1000,) * 600)
                media_before = path.read_bytes()[media_pos:]
                root, status = marker(plan.embed_verify(path))
                self.assertEqual(status.get("timing"), "media")
                points = root.findall(f".//{{{gps.GPX_NS}}}trkpt")
                times = [int(point.find(
                    f"{{{gps.GPX_NS}}}extensions/{{{gps.MERGE_NS}}}media_time_us").text)
                         for point in points]
                self.assertEqual(times, [second * 1000000 for second in seconds
                                         if 0 <= second <= 600])
                self.assertEqual(len({point.get("lat") for point in points}),
                                 len(points))
                self.assertEqual([r.digest for r in native(path)],
                                 [hashlib.sha256(raw).digest() for raw in records])
                self.assertEqual(Path(recording.filepath).read_bytes(), before)
                self.assertEqual(path.read_bytes()[media_pos:], media_before)

    def test_no_eligible_boundary_fixes_keep_the_complete_untimed_route(self):
        for seconds in ((-7, -1), (601, 602), (-7, -1, 601, 602)):
            with self.subTest(seconds=seconds):
                recording = source(self.root / "a.mp4", seconds,
                                   deltas=(1000,) * 600)
                plan = gps.prepare([recording])
                path = self.root / "merged.mp4"
                output(path, plan.reserve_bytes, deltas=(1000,) * 600)
                root, status = marker(plan.embed_verify(path))
                self.assertEqual(status.get("timing"), "unavailable")
                self.assertFalse(root.findall(f".//{{{gps.MERGE_NS}}}media_time_us"))
                self.assertEqual(len(root.findall(f".//{{{gps.GPX_NS}}}trkpt")),
                                 len(seconds))
                self.assertEqual([r.digest for r in native(path)],
                                 [hashlib.sha256(raw_fix(i)).digest() for i in seconds])

    def test_source_with_only_stale_fixes_keeps_an_empty_playback_segment(self):
        first = source(self.root / "a.mp4", (-7, -1), deltas=(1000,) * 600)
        second = source(self.root / "b.mp4", (600, 601),
                        start=BASE + timedelta(seconds=600), deltas=(1000,) * 600)
        plan = gps.prepare([first, second])
        path = self.root / "merged.mp4"
        output(path, plan.reserve_bytes, deltas=(1000,) * 1200)
        root, status = marker(plan.embed_verify(path))
        self.assertEqual(status.get("timing"), "media")
        segments = root.findall(f".//{{{gps.GPX_NS}}}trkseg")
        self.assertEqual([len(segment) for segment in segments], [0, 2])
        times = root.findall(f".//{{{gps.MERGE_NS}}}media_time_us")
        self.assertEqual([int(t.text) for t in times], [600000000, 601000000])
        self.assertEqual([r.digest for r in native(path)],
                         [hashlib.sha256(raw_fix(i)).digest() for i in (-7, -1, 600, 601)])

    def test_qualified_gpx_normalizes_leading_and_repeated_seconds_only(self):
        seconds = (-1, 0, 1, 1, 2)
        for mode, expected_timing, expected_count in (("normal", "media", 3),
                                                      ("parking", "unavailable", 5)):
            with self.subTest(mode=mode):
                recording = source(self.root / "a.mp4", seconds, mode=mode)
                before = Path(recording.filepath).read_bytes()
                plan = gps.prepare([recording])
                path = self.root / "merged.mp4"
                output(path, plan.reserve_bytes)
                root, status = marker(plan.embed_verify(path))
                self.assertEqual(status.get("timing"), expected_timing)
                points = root.findall(f".//{{{gps.GPX_NS}}}trkpt")
                self.assertEqual(len(points), expected_count)
                times = root.findall(f".//{{{gps.MERGE_NS}}}media_time_us")
                self.assertEqual([int(t.text) for t in times], [0, 1000000, 2000000]
                                 if mode == "normal" else [])
                self.assertEqual([r.digest for r in native(path)],
                                 [hashlib.sha256(raw_fix(i)).digest() for i in seconds])
                self.assertEqual(Path(recording.filepath).read_bytes(), before)

    def test_coincident_source_boundary_keeps_first_timed_fix(self):
        first = source(self.root / "a.mp4", (0, 1, 2))
        second = source(self.root / "b.mp4", (2, 3, 4), start=BASE + timedelta(seconds=2))
        plan = gps.prepare([first, second])
        path = self.root / "merged.mp4"
        output(path, plan.reserve_bytes, deltas=(1000, 1000, 1000, 1000))
        root, status = marker(plan.embed_verify(path))
        self.assertEqual(status.get("timing"), "media")
        self.assertEqual([int(t.text) for t in root.findall(f".//{{{gps.MERGE_NS}}}media_time_us")],
                         [0, 1000000, 2000000, 3000000, 4000000])
        self.assertEqual(len(native(path)), 6)
        segments = root.findall(f".//{{{gps.GPX_NS}}}trkseg")
        self.assertEqual([len(segment) for segment in segments], [3, 2])

    def test_composition_offsets_qualify_only_when_preserved(self):
        recording = source(self.root / "a.mp4")
        plan = gps.prepare([recording])
        path = self.root / "merged.mp4"
        output(path, plan.reserve_bytes, composition=(100, 200))
        _, status = marker(plan.embed_verify(path))
        self.assertEqual(status.get("timing"), "unavailable")


if __name__ == "__main__":
    unittest.main()
