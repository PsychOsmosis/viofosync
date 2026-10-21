"""Merge option, publication safety, and real stream-copy integration tests.

Run with python3 -m unittest discover -s tests -p test_merge_gps_options.py -v.
All media and GPS fixtures are synthetic and use a temporary directory.
"""
import builtins
import datetime
from decimal import Decimal
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import viofosync as sync

ROOT = Path(__file__).resolve().parents[1]
GPX = b'<?xml version="1.0"?><gpx version="1.1"><trk/></gpx>\n'


class MergeOptionTests(unittest.TestCase):
    def parse(self, *flags):
        with mock.patch.object(sys, "argv", ["viofosync.py", *flags]):
            return sync.parse_args()

    def test_cli_gps_defaults_off_and_flags_are_independent(self):
        defaults = self.parse()
        self.assertFalse(defaults.merge_gps)
        self.assertFalse(defaults.merge_keep_gpx)
        self.assertTrue(self.parse("--merge-gps").merge_gps)
        self.assertFalse(self.parse("--merge-gps").merge_keep_gpx)
        enabled = self.parse("--merge-gps", "--merge-keep-gpx")
        self.assertTrue(enabled.merge_gps)
        self.assertTrue(enabled.merge_keep_gpx)

    def test_run_propagates_both_cli_options_to_merge_chunks(self):
        with mock.patch.object(sys, "argv", [
                "viofosync.py", "--merge-chunks", "--merge-gps", "--merge-keep-gpx",
                "--delete-merged-sources", "--destination", "/synthetic/recordings"]), \
                mock.patch.object(sync, "merge_chunks", return_value=True) as merge, \
                mock.patch.object(sync.socket, "setdefaulttimeout"), \
                mock.patch.object(sync.logger, "setLevel"):
            self.assertEqual(sync.run(), 0)
        self.assertEqual(merge.call_args.args,
                         ("/synthetic/recordings", "none", None, 2.0, True, True, True))

    def test_shell_env_boolean_flags_and_cli_passthrough(self):
        with tempfile.TemporaryDirectory(prefix="merge options ") as directory:
            # The shell launches this harmless argument recorder instead of the app.
            Path(directory, "viofosync.py").write_text(
                "import json,sys; print(json.dumps(sys.argv[1:]))\n"
            )
            env = {"PATH": os.defpath, "APP_DIR": directory,
                   "PYTHON_BIN": sys.executable}
            for value in (None, "", "0", "false", "False", "no", "off",
                          "1", "true", "True", "TRUE", "yes", "on"):
                with self.subTest(value=value):
                    current = dict(env)
                    if value is not None:
                        current.update(MERGE_GPS=value, MERGE_KEEP_GPX=value)
                    result = subprocess.run(
                        ["bash", str(ROOT / "viofosync.sh"), "--merge-chunks"],
                        env=current, capture_output=True, text=True, check=True,
                    )
                    args = json.loads(result.stdout)
                    enabled = value in ("1", "true", "True", "TRUE", "yes", "on")
                    self.assertEqual("--merge-gps" in args, enabled)
                    self.assertEqual("--merge-keep-gpx" in args, enabled)
                    self.assertIn("--merge-chunks", args)


class MergeWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="merge-gps-workflow-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.destination = self.root / "merged"
        self.group = []
        self.source_bytes = {}
        for index in range(2):
            name = f"2026_1021_00000{index}_00000{index + 1}F.MP4"
            path = self.source / name
            path.write_bytes(f"synthetic source {index}".encode())
            Path(str(path) + ".gpx").write_bytes(f"source GPX {index}".encode())
            for item in (path, Path(str(path) + ".gpx")):
                self.source_bytes[item] = item.read_bytes()
            self.group.append(sync.LocalRecording(
                str(path), name, path.stat().st_size,
                datetime.datetime(2026, 10, 21, 0, 0, index),
                index + 1, "F", "normal", "F", 1.0,
            ))
        self.output = self.destination / sync.merged_output_filename(self.group)
        self.gpx = Path(str(self.output) + ".gpx")
        self.plan = types.SimpleNamespace(
            reserve_bytes=65536, concat_durations=("1.000000", "1.000000"),
            embed_verify=mock.Mock(side_effect=self.embed),
        )
        self.gps_module = types.SimpleNamespace(prepare=mock.Mock(return_value=self.plan))
        self.ffmpeg = mock.Mock(side_effect=self.concat)
        self.concat_text = None
        self.command = None
        for patcher in (
            mock.patch.object(sync, "dry_run", False),
            mock.patch.object(sync, "destination_is_too_full", return_value=False),
            mock.patch.dict(sys.modules, {"merge_gps": self.gps_module}),
            mock.patch.object(sync.subprocess, "run", self.ffmpeg),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def concat(self, command, **kwargs):
        self.command = command
        self.concat_text = Path(command[command.index("-i") + 1]).read_text()
        Path(command[-1]).write_bytes(b"stream-copied video/audio")
        return subprocess.CompletedProcess(command, 0, "", "")

    def embed(self, path):
        with open(path, "ab") as media:
            media.write(b" + verified native GPS")
        return GPX

    def merge(self, gps=True, keep=True, delete=True):
        return sync.merge_recording_group(
            self.group, str(self.destination), "none", delete, gps, keep,
        )

    def assert_sources_preserved(self):
        for path, content in self.source_bytes.items():
            self.assertEqual(path.read_bytes(), content, str(path))

    def assert_no_work_files(self):
        if self.destination.exists():
            self.assertFalse([p.name for p in self.destination.iterdir()
                              if p.name.startswith(".")])

    def reject_gps_import(self, name, *args, **kwargs):
        if name == "merge_gps":
            raise AssertionError("GPS module must not be imported in this path")
        return self.original_import(name, *args, **kwargs)

    def test_default_legacy_merge_does_not_load_or_extract_gps(self):
        self.original_import = builtins.__import__
        with mock.patch("builtins.__import__", side_effect=self.reject_gps_import):
            self.assertTrue(self.merge(gps=False, keep=False))
        self.gps_module.prepare.assert_not_called()
        self.assertNotIn("-moov_size", self.command)
        self.assertNotIn("duration ", self.concat_text)
        self.assertEqual(self.output.read_bytes(), b"stream-copied video/audio")
        self.assertFalse(self.gpx.exists())
        self.assertTrue(all(not p.exists() for p in self.source_bytes))
        self.assert_no_work_files()

    def test_keep_flag_alone_preserves_legacy_merge_behavior(self):
        self.assertTrue(self.merge(gps=False, keep=True, delete=False))
        self.gps_module.prepare.assert_not_called()
        self.assertFalse(self.gpx.exists())
        self.assert_sources_preserved()

    def test_gps_enabled_embeds_before_cleanup_without_retaining_gpx(self):
        self.assertTrue(self.merge(keep=False))
        self.plan.embed_verify.assert_called_once()
        self.assertIn(b"verified native GPS", self.output.read_bytes())
        self.assertEqual(self.command[self.command.index("-c") + 1], "copy")
        self.assertEqual(self.command[self.command.index("-moov_size") + 1], "65536")
        self.assertEqual(self.concat_text.count("duration 1.000000\n"), 2)
        self.assertFalse(self.gpx.exists())
        self.assertTrue(all(not p.exists() for p in self.source_bytes))
        self.assert_no_work_files()

    def test_retained_gpx_is_exact_verified_bytes_before_source_cleanup(self):
        original_cleanup = sync.remove_recording_and_sidecars
        def cleanup(path):
            self.assertEqual(self.gpx.read_bytes(), GPX)
            self.assertIn(b"verified native GPS", self.output.read_bytes())
            original_cleanup(path)
        with mock.patch.object(sync, "remove_recording_and_sidecars", side_effect=cleanup) as remove:
            self.assertTrue(self.merge())
        self.assertEqual(remove.call_count, 2)
        self.assertEqual(self.gpx.read_bytes(), GPX)
        self.assert_no_work_files()

    def test_legitimate_no_gps_still_merges_without_empty_sidecar(self):
        self.plan.embed_verify.side_effect = None
        self.plan.embed_verify.return_value = None
        self.assertTrue(self.merge())
        self.assertFalse(self.gpx.exists())
        self.assertTrue(self.output.exists())
        self.assertTrue(all(not p.exists() for p in self.source_bytes))

    def test_dry_run_does_no_gps_or_ffmpeg_work_or_deletion(self):
        self.original_import = builtins.__import__
        with mock.patch.object(sync, "dry_run", True), \
                mock.patch("builtins.__import__", side_effect=self.reject_gps_import):
            self.assertTrue(self.merge())
        self.gps_module.prepare.assert_not_called()
        self.ffmpeg.assert_not_called()
        self.assert_sources_preserved()
        self.assertFalse(self.output.exists())
        self.assertFalse(self.gpx.exists())
        self.assert_no_work_files()

    def test_partial_or_malformed_gps_preparation_failure_keeps_sources(self):
        self.gps_module.prepare.side_effect = ValueError("synthetic incomplete native GPS index")
        self.assertFalse(self.merge())
        self.ffmpeg.assert_not_called()
        self.assert_sources_preserved()
        self.assertFalse(self.output.exists())
        self.assertFalse(self.gpx.exists())

    def test_ffmpeg_failure_keeps_sources_and_publishes_nothing(self):
        self.ffmpeg.side_effect = None
        self.ffmpeg.return_value = subprocess.CompletedProcess([], 1, "", "synthetic failure")
        self.assertFalse(self.merge())
        self.plan.embed_verify.assert_not_called()
        self.assert_sources_preserved()
        self.assertFalse(self.output.exists())
        self.assertFalse(self.gpx.exists())
        self.assert_no_work_files()

    def test_empty_ffmpeg_output_keeps_sources(self):
        self.ffmpeg.side_effect = None
        self.ffmpeg.return_value = subprocess.CompletedProcess([], 0, "", "")
        self.assertFalse(self.merge())
        self.plan.embed_verify.assert_not_called()
        self.assert_sources_preserved()
        self.assertFalse(self.output.exists())
        self.assert_no_work_files()

    def test_embedded_verification_failure_keeps_sources(self):
        self.plan.embed_verify.side_effect = RuntimeError("synthetic metadata mismatch")
        self.assertFalse(self.merge())
        self.assert_sources_preserved()
        self.assertFalse(self.output.exists())
        self.assertFalse(self.gpx.exists())
        self.assert_no_work_files()

    def test_gpx_publication_failure_keeps_sources_and_removes_temporary_files(self):
        with mock.patch.object(sync.os, "link", side_effect=OSError("synthetic GPX publication failure")):
            self.assertFalse(self.merge())
        self.assert_sources_preserved()
        self.assertFalse(self.output.exists())
        self.assertFalse(self.gpx.exists())
        self.assert_no_work_files()

    def test_mp4_publication_failure_removes_only_our_published_gpx(self):
        original_link, original_replace = os.link, os.replace
        def fail_destination(operation):
            def invoke(source, destination, *args, **kwargs):
                if Path(destination) == self.output:
                    raise OSError("synthetic MP4 publication failure")
                return operation(source, destination, *args, **kwargs)
            return invoke
        with mock.patch.object(sync.os, "link", side_effect=fail_destination(original_link)), \
                mock.patch.object(sync.os, "replace", side_effect=fail_destination(original_replace)):
            try:
                result = self.merge()
            except OSError:
                result = False
        self.assertFalse(result)
        self.assert_sources_preserved()
        self.assertFalse(self.output.exists())
        self.assertFalse(self.gpx.exists())
        self.assert_no_work_files()

    def test_existing_output_keeps_sources_without_gps_work(self):
        self.destination.mkdir()
        self.output.write_bytes(b"existing merged MP4")
        self.assertFalse(self.merge())
        self.assertEqual(self.output.read_bytes(), b"existing merged MP4")
        self.gps_module.prepare.assert_not_called()
        self.ffmpeg.assert_not_called()
        self.assert_sources_preserved()

    def test_dangling_output_symlink_is_not_replaced(self):
        self.destination.mkdir()
        self.output.symlink_to("missing-output.MP4")
        self.assertFalse(self.merge())
        self.assertTrue(self.output.is_symlink())
        self.assert_sources_preserved()

    def test_existing_gpx_is_not_overwritten(self):
        self.destination.mkdir()
        self.gpx.write_bytes(b"existing merged GPX")
        self.assertFalse(self.merge())
        self.assertEqual(self.gpx.read_bytes(), b"existing merged GPX")
        self.ffmpeg.assert_not_called()
        self.assert_sources_preserved()
        self.assertFalse(self.output.exists())

    def test_gpx_created_during_merge_is_not_overwritten(self):
        def concurrent_gpx(command, **kwargs):
            result = self.concat(command, **kwargs)
            self.gpx.write_bytes(b"concurrent GPX")
            return result
        self.ffmpeg.side_effect = concurrent_gpx
        self.assertFalse(self.merge())
        self.assertEqual(self.gpx.read_bytes(), b"concurrent GPX")
        self.assert_sources_preserved()
        self.assertFalse(self.output.exists())
        self.assert_no_work_files()

    def test_mp4_created_during_merge_is_not_overwritten(self):
        def concurrent_mp4(command, **kwargs):
            result = self.concat(command, **kwargs)
            self.output.write_bytes(b"concurrent MP4")
            return result
        self.ffmpeg.side_effect = concurrent_mp4
        try:
            result = self.merge()
        except FileExistsError:
            result = False
        self.assertFalse(result)
        self.assertEqual(self.output.read_bytes(), b"concurrent MP4")
        self.assert_sources_preserved()
        self.assertFalse(self.gpx.exists())
        self.assert_no_work_files()

    def test_merge_chunks_propagates_both_options_to_group_workflow(self):
        with mock.patch.object(sync.shutil, "which", return_value="/synthetic/tool"), \
                mock.patch.object(sync, "iter_local_recordings", return_value=iter(self.group)), \
                mock.patch.object(sync, "with_durations", side_effect=lambda group: group), \
                mock.patch.object(sync, "merge_recording_group", return_value=True) as merge:
            self.assertTrue(sync.merge_chunks(
                str(self.source), "none", str(self.destination), 2.0, True, True, True,
            ))
        self.assertEqual(merge.call_args.args[-3:], (True, True, True))


def atom(kind, payload):
    return struct.pack(">I4s", len(payload) + 8, kind) + payload


def native_gps(second=0):
    hour, remainder = divmod(second, 3600)
    minute, second = divmod(remainder, 60)
    payload = struct.pack("<6I4s4f", hour, minute, second, 26, 10, 21,
                          b"ANE\0", 4530.0, 7340.0, 20.0, 90.0) + bytes(20)
    return atom(b"free", b"GPS " + payload)


class NativeExtractorLargeFileTests(unittest.TestCase):
    def test_native_extractor_reaches_moov_after_sparse_64bit_mdat(self):
        with tempfile.TemporaryDirectory(prefix="merge-gps-large-") as directory:
            path = Path(directory, "synthetic-64bit.MP4")
            prefix = atom(b"ftyp", b"isom\0\0\0\0")
            gps = native_gps()
            mdat_size = (1 << 32) + 16
            index = atom(b"gps ", struct.pack(">IIII", 0, 1, len(prefix), len(gps)))
            with path.open("wb") as media:
                media.write(prefix + gps)
                media.write(struct.pack(">I4sQ", 1, b"mdat", mdat_size))
                media.seek(len(prefix) + len(gps) + mdat_size)
                media.write(atom(b"moov", index))
            self.assertGreater(path.stat().st_size, 1 << 32)
            self.assertLess(path.stat().st_blocks * 512, 1024 * 1024)
            with path.open("rb") as media:
                points = sync.parse_moov(media)
            self.assertEqual(len(points), 1)
            self.assertEqual(points[0]["DT"]["DT"], "2026-10-21T00:00:00Z")
            self.assertAlmostEqual(points[0]["Loc"]["Lat"]["Float"], 45.5)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "real stream-copy integration requires ffmpeg and ffprobe")
class RealFFmpegMergeTests(unittest.TestCase):
    def command(self, *args):
        return subprocess.run(args, check=True, capture_output=True, text=True).stdout

    def packet_hashes(self, path, stream):
        data = self.command(
            "ffprobe", "-v", "error", "-select_streams", stream,
            "-show_packets", "-show_data_hash", "sha256",
            "-show_entries", "packet=data_hash", "-of", "json", str(path),
        )
        return [packet["data_hash"] for packet in json.loads(data)["packets"]]

    def video_slice_hashes(self, path):
        # MP4 concat may insert SPS/PPS while retaining each compressed picture.
        # These tiny x264 fixtures use four-byte AVCC NAL lengths.
        packets = json.loads(self.command(
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_packets", "-show_entries", "packet=pos,size",
            "-of", "json", str(path),
        ))["packets"]
        hashes = []
        with path.open("rb") as media:
            for packet in packets:
                media.seek(int(packet["pos"]))
                data = media.read(int(packet["size"]))
                offset, slices = 0, []
                while offset < len(data):
                    size = struct.unpack_from(">I", data, offset)[0]
                    self.assertGreater(size, 0)
                    end = offset + 4 + size
                    self.assertLessEqual(end, len(data))
                    nal = data[offset + 4:end]
                    if 1 <= (nal[0] & 31) <= 5:
                        slices.append(hashlib.sha256(nal).hexdigest())
                    offset = end
                self.assertTrue(slices)
                hashes.append(slices)
        return hashes

    def mdat_hashes(self, path):
        content = Path(path).read_bytes()
        hashes, offset = [], 0
        while offset < len(content):
            size, kind = struct.unpack_from(">I4s", content, offset)
            header = 8
            if size == 1:
                size = struct.unpack_from(">Q", content, offset + 8)[0]
                header = 16
            elif size == 0:
                size = len(content) - offset
            self.assertGreaterEqual(size, header)
            self.assertLessEqual(offset + size, len(content))
            if kind == b"mdat":
                hashes.append(hashlib.sha256(content[offset + header:offset + size]).digest())
            offset += size
        self.assertTrue(hashes)
        return hashes

    def add_native_gps(self, path, second):
        content = path.read_bytes()
        offset = 0
        while offset < len(content):
            size, kind = struct.unpack_from(">I4s", content, offset)
            self.assertGreaterEqual(size, 8)
            if kind == b"moov":
                break
            offset += size
        self.assertEqual(offset + size, len(content), "synthetic source moov must be last")
        gps = native_gps(second)
        gps_index = atom(b"gps ", struct.pack(">IIII", 0, 1, len(content) + 24, len(gps)))
        path.write_bytes(content[:offset] + atom(b"moov", content[offset + 8:] + gps_index) + gps)

    def test_real_merge_preserves_native_gps_gpx_and_compressed_av_payloads(self):
        import merge_gps
        with tempfile.TemporaryDirectory(prefix="merge-gps-real-") as directory:
            root = Path(directory)
            group = []
            expected_video, expected_audio = [], []
            for index, color in enumerate(("red", "blue")):
                # Capture clocks deliberately differ from concat media offsets.
                second = index * 60
                captured = datetime.datetime(2026, 10, 21, 0, index, 0,
                                             tzinfo=datetime.timezone.utc)
                name = f"{captured:%Y_%m%d_%H%M%S}_00000{index + 1}F.MP4"
                path = root / name
                self.command(
                    "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    f"color=c={color}:s=96x64:r=5:d=2", "-f", "lavfi", "-i",
                    "sine=frequency=440:sample_rate=16000:duration=2",
                    "-c:v", "libx264", "-threads", "1", "-preset", "ultrafast",
                    "-bf", "0", "-g", "5", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-b:a", "32k", "-shortest", "-y", str(path),
                )
                self.add_native_gps(path, second)
                expected_video.extend(self.video_slice_hashes(path))
                expected_audio.extend(self.packet_hashes(path, "a:0"))
                group.append(sync.LocalRecording(
                    str(path), name, path.stat().st_size,
                    captured,
                    index + 1, "F", "normal", "F", 2.0,
                ))
            destination = root / "merged"
            original_embed = merge_gps.MergeGPSPlan.embed_verify
            def verify_unchanged_media(plan, output_path):
                before = self.mdat_hashes(output_path)
                gpx = original_embed(plan, output_path)
                self.assertEqual(self.mdat_hashes(output_path), before)
                return gpx
            with mock.patch.object(sync, "dry_run", False), \
                    mock.patch.object(sync, "destination_is_too_full", return_value=False), \
                    mock.patch.object(merge_gps.MergeGPSPlan, "embed_verify", verify_unchanged_media):
                self.assertTrue(sync.merge_recording_group(
                    group, str(destination), "none", False, True, True,
                ))
            output = destination / sync.merged_output_filename(group)
            self.assertTrue(expected_video)
            self.assertTrue(expected_audio)
            self.assertEqual(self.video_slice_hashes(output), expected_video)
            self.assertEqual(self.packet_hashes(output, "a:0"), expected_audio)
            with output.open("rb") as media:
                points = sync.parse_moov(media)
            self.assertEqual([p["DT"]["DT"] for p in points],
                             ["2026-10-21T00:00:00Z", "2026-10-21T00:01:00Z"])
            sidecar = Path(str(output) + ".gpx").read_bytes()
            xml = ET.fromstring(sidecar)
            gps_points = xml.findall("{*}trk/{*}trkseg/{*}trkpt")
            self.assertEqual(len(gps_points), 2)
            marker = xml.find("{*}extensions/{urn:viofosync:merge-gps:1}merge_gps")
            self.assertIsNotNone(marker)
            self.assertEqual(marker.get("timing"), "media")
            packets = json.loads(self.command(
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_packets", "-show_entries", "packet=pts_time",
                "-of", "json", str(output),
            ))["packets"]
            # Ten source frames at 5 fps occupy each two-second segment. The
            # second capture clock is 60 s, but its media offset must be ~2 s.
            expected_offsets = [int(Decimal(packets[i]["pts_time"]) * 1000000)
                                for i in (0, 10)]
            actual_offsets = [int(point.find(
                "{*}extensions/{urn:viofosync:merge-gps:1}media_time_us").text)
                for point in gps_points]
            time_base = Fraction(self.command(
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=time_base", "-of",
                "default=noprint_wrappers=1:nokey=1", str(output),
            ).strip())
            # ffprobe rounds an edit-list movie-time shift to video ticks.
            # The direct MP4 clock proof can differ by at most one such tick.
            quantum_us = time_base * 1000000
            for actual, expected in zip(actual_offsets, expected_offsets):
                self.assertLessEqual(abs(actual - expected), quantum_us)
            self.assertEqual(actual_offsets[1] - actual_offsets[0], 2000000)
            self.assertLess(actual_offsets[1], 3000000)
            video_duration = self.command(
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=duration", "-of",
                "default=noprint_wrappers=1:nokey=1", str(output),
            ).strip()
            self.assertEqual(int(marker.get("duration_us")),
                             int(Decimal(video_duration) * 1000000))
            # Exact UTF-8 GPX bytes also live inside the MP4 user-type metadata.
            content = output.read_bytes()
            self.assertEqual(content.count(sidecar), 1)
            self.assertTrue(all(Path(item.filepath).exists() for item in group))


if __name__ == "__main__":
    unittest.main()
