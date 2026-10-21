"""Synthetic camera tests for the always-on Wi-Fi recording guard."""

import datetime
import http.client
import io
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import viofosync


BASE_URL = "http://camera.invalid"
PAYLOAD = b"synthetic video bytes"


class Response(io.BytesIO):
    def __init__(self, body=b"", status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {}

    def getcode(self):
        return self.status

    def getheader(self, name):
        return self.headers.get(name)


def recording(stamp="2026_1021_120000", sequence=10, tag="F",
              directory="/DCIM/Movie", attr=32, extension="MP4",
              listed_time=None):
    name = f"{stamp}_{sequence:06d}{tag}.{extension}"
    timestamp = datetime.datetime.strptime(stamp, "%Y_%m%d_%H%M%S")
    return viofosync.Recording(
        name, f"A:{directory}/{name}", len(PAYLOAD), 1,
        listed_time or timestamp, attr,
    )


def xml_listing(recordings):
    root = ET.Element("LIST")
    for rec in recordings:
        item = ET.SubElement(root, "File")
        fields = {
            "NAME": rec.filename, "FPATH": rec.filepath,
            "SIZE": rec.size, "TIMECODE": 1,
            "TIME": rec.datetime.strftime("%Y/%m/%d %H:%M:%S"),
            "ATTR": rec.attr,
        }
        for key, value in fields.items():
            ET.SubElement(item, key).text = str(value)
    return ET.tostring(root)


def html_listing(recordings, directory):
    return "".join(
        f'<a href="{rec.filename}"><b>{rec.filename}</b></a>'
        f'<td align=right>{rec.size} B'
        for rec in recordings
        if rec.filepath.rsplit("/", 1)[0] == f"A:{directory}"
    ).encode()


class CameraCase(unittest.TestCase):
    def setUp(self):
        options = patch.multiple(
            viofosync, dry_run=False, read_only=False,
            delete_after_sync=False, cutoff_date=None,
            socket_timeout=2.5, max_download_attempts=1,
            _last_control_request_at=None,
        )
        options.start()
        self.addCleanup(options.stop)
        # The same tests also run on the separately combined DELETE_LOCKED branch.
        locked = patch.object(viofosync, "delete_locked", False, create=True)
        locked.start()
        self.addCleanup(locked.stop)
        no_network = patch.object(
            viofosync.urllib.request, "urlopen",
            side_effect=AssertionError("Unexpected unmocked camera request"),
        )
        no_network.start()
        self.addCleanup(no_network.stop)


class RecordingStateTests(CameraCase):
    def state(self, body, status=200):
        with patch.object(
            viofosync, "camera_control_request",
            return_value=Response(body, status),
        ) as request:
            result = viofosync.get_recording_state(BASE_URL)
        request.assert_called_once_with(
            BASE_URL + "/?custom=1&cmd=3014", viofosync.socket_timeout
        )
        return result

    def test_explicit_stopped_or_recording_in_firmware_pairs(self):
        cases = [
            (b"<Response><Cmd>2001</Cmd><Status>0</Status></Response>", 0),
            (b"<Cmd>2001</Cmd><Status>1</Status>", 1),
            (b"<Cmd> 2001 </Cmd>\n<Status> 0 </Status>", 0),
            # Some firmware repeats a root or emits adjacent top-level pairs.
            (b"<r><Cmd>2001</Cmd><Status>0</Status></r>"
             b"<r><Cmd>2001</Cmd><Status>0</Status></r>", 0),
            (b"<Cmd>3014</Cmd><Status>0</Status>"
             b"<Cmd>2001</Cmd><Status>1</Status>", 1),
        ]
        for body, expected in cases:
            with self.subTest(body=body):
                self.assertEqual(self.state(body), expected)

    def test_missing_unsupported_invalid_conflicting_or_partial_is_unknown(self):
        cases = [
            b"", b"not a status response", b"<Status>0</Status>",
            b"<Cmd>3014</Cmd><Status>0</Status>",
            b"<Cmd>2001</Cmd>",
            b"<Cmd>2001</Cmd><Status></Status>",
            b"<Cmd>2001</Cmd><Status>2</Status>",
            b"<Cmd>2001</Cmd><Status>00</Status>",
            b"<Cmd>2001</Cmd><Status>false</Status>",
            b"<Cmd>2001</Cmd><Status>0</Status>"
            b"<Cmd>2001</Cmd><Status>1</Status>",
            b"<Cmd>2001</Cmd><Status>0</Status>"
            b"<Cmd>2001</Cmd><Status>unsupported</Status>",
            b"<Cmd>2001</Cmd><Status>0",
            b"<Cmd>2001</Cmd><Status>0</Status>"
            b"<Cmd>2001</Cmd><Status>1",
            b"<Cmd>2001</Cmd><Status>0</Status>\xff",
        ]
        for body in cases:
            with self.subTest(body=body):
                self.assertIsNone(self.state(body))

    def test_http_and_network_failures_are_unknown(self):
        self.assertIsNone(self.state(
            b"<Cmd>2001</Cmd><Status>0</Status>", status=500
        ))
        for error in [
            urllib.error.URLError("synthetic connection failure"),
            socket.timeout("synthetic timeout"),
            http.client.RemoteDisconnected("synthetic disconnect"),
        ]:
            with self.subTest(error=type(error).__name__):
                with patch.object(
                    viofosync, "camera_control_request", side_effect=error
                ) as request:
                    self.assertIsNone(viofosync.get_recording_state(BASE_URL))
                self.assertEqual(request.call_count, 1)


class CaptureGuardTests(CameraCase):
    def test_active_or_unknown_holds_every_newest_video_channel_and_mode(self):
        old = recording(stamp="2026_1021_115700", sequence=900)
        newest = [
            recording(sequence=10, tag="F"),
            recording(sequence=11, tag="I"),
            recording(sequence=12, tag="R"),
            recording(sequence=21, tag="PF", directory="/DCIM/Movie/Parking"),
            recording(sequence=22, tag="PI", directory="/DCIM/Movie/RO", attr=33),
            recording(sequence=23, tag="PR", directory="/DCIM/Movie/RO", attr=33),
        ]
        photo = recording(stamp="2026_1021_120500", extension="JPG")
        for state in (1, None):
            with self.subTest(state=state):
                snapshot = [old, newest[2], photo, *newest[:2], *newest[3:]]
                original = list(snapshot)
                with patch.object(viofosync, "get_recording_state", return_value=state) as query:
                    self.assertEqual(
                        viofosync.guard_active_recordings(BASE_URL, snapshot),
                        [old, photo],
                    )
                query.assert_called_once_with(BASE_URL)
                self.assertEqual(snapshot, original)

    def test_shifted_parking_companions_use_existing_time_and_sequence_bounds(self):
        newest = recording(stamp="2026_1021_120005", sequence=101, tag="PF")
        companions = [
            recording(stamp="2026_1021_120000", sequence=100, tag="PI"),
            recording(stamp="2026_1021_120003", sequence=102, tag="PR",
                      directory="/DCIM/Movie/RO", attr=33),
        ]
        eligible = [
            recording(stamp="2026_1021_115700", sequence=98, tag="PF"),
            recording(stamp="2026_1021_115954", sequence=100, tag="PI"),
            recording(stamp="2026_1021_120001", sequence=90, tag="PR"),
            recording(stamp="2026_1021_120004", sequence=100, tag="I"),
        ]
        for state in (1, None):
            with self.subTest(state=state):
                with patch.object(viofosync, "get_recording_state", return_value=state):
                    self.assertEqual(viofosync.guard_active_recordings(
                        BASE_URL, [*eligible, *companions, newest]
                    ), eligible)

    def test_filename_capture_time_wins_over_xml_metadata(self):
        old = recording(stamp="2026_1021_115700", listed_time=datetime.datetime(2026, 10, 22))
        new = recording(listed_time=datetime.datetime(2026, 10, 20))
        with patch.object(viofosync, "get_recording_state", return_value=1):
            self.assertEqual(viofosync.guard_active_recordings(BASE_URL, [new, old]), [old])

    def test_nonstandard_xml_video_names_use_listing_timestamp(self):
        old = recording()
        fallback = old._replace(
            filename="CUSTOM.MP4", filepath="A:/DCIM/Movie/CUSTOM.MP4",
            datetime=old.datetime + datetime.timedelta(minutes=3),
        )
        with patch.object(viofosync, "get_recording_state", return_value=None):
            self.assertEqual(viofosync.guard_active_recordings(BASE_URL, [old, fallback]), [old])

    def test_explicit_stopped_releases_newest(self):
        snapshot = [recording(), recording(sequence=11, tag="R")]
        with patch.object(viofosync, "get_recording_state", return_value=0) as query:
            self.assertEqual(viofosync.guard_active_recordings(BASE_URL, snapshot), snapshot)
        query.assert_called_once_with(BASE_URL)

    def test_each_invocation_supersedes_the_previously_held_group(self):
        old = recording(stamp="2026_1021_115700")
        current = recording()
        later = recording(stamp="2026_1021_120300")
        with patch.object(viofosync, "get_recording_state", side_effect=[1, 1, 0]) as query:
            self.assertEqual(viofosync.guard_active_recordings(BASE_URL, [old, current]), [old])
            self.assertEqual(viofosync.guard_active_recordings(BASE_URL, [old, current, later]), [old, current])
            self.assertEqual(viofosync.guard_active_recordings(BASE_URL, [old, current, later]), [old, current, later])
        self.assertEqual(query.call_count, 3)

    def test_empty_snapshot_does_not_query_and_photos_remain_eligible(self):
        with patch.object(viofosync, "get_recording_state", return_value=None) as query:
            self.assertEqual(viofosync.guard_active_recordings(BASE_URL, []), [])
            query.assert_not_called()
            photos = [recording(extension="JPEG")]
            self.assertEqual(viofosync.guard_active_recordings(BASE_URL, photos), photos)
            query.assert_called_once_with(BASE_URL)


class ListingAndSyncTests(CameraCase):
    def run_sync(self, records, *, html=False, state=1, only_locked=False,
                 filters=None, cutoff=None, stacked=(), delete=False,
                 priority="date", locked=False):
        requests = []
        state_body = (
            f"<Cmd>2001</Cmd><Status>{state}</Status>".encode()
            if state is not None else b"unsupported status response"
        )

        def respond(url, timeout):
            method = url.get_method() if isinstance(url, urllib.request.Request) else "GET"
            address = url.full_url if isinstance(url, urllib.request.Request) else url
            parsed = urllib.parse.urlsplit(address)
            query = urllib.parse.parse_qs(parsed.query)
            requests.append((method, address))
            command = query.get("cmd", [None])[0]
            if command == "3015":
                return Response(xml_listing(records))
            if command == "3014":
                return Response(state_body)
            if command == "4003":
                return Response(b"<Response><Status>0</Status></Response>")
            if parsed.path in viofosync.DCIM_DIRS + viofosync.DCIM_DIRS_RO:
                return Response(html_listing(records, parsed.path))
            name = urllib.parse.unquote(parsed.path).rsplit("/", 1)[-1]
            if name in {rec.filename for rec in records}:
                if method == "HEAD":
                    return Response(headers={"Content-Length": str(len(PAYLOAD))})
                return Response(PAYLOAD)
            raise AssertionError(f"Unexpected synthetic request: {method} {address}")

        with tempfile.TemporaryDirectory(prefix="viofosync-guard-") as destination:
            with patch.object(viofosync, "camera_control_request", side_effect=respond), patch.multiple(
                viofosync, read_only=only_locked, cutoff_date=cutoff,
                delete_after_sync=delete, delete_locked=locked,
            ), patch.object(viofosync, "destination_is_too_full", return_value=False), patch.object(
                viofosync, "load_stacked_names", return_value=set(stacked)
            ):
                self.assertTrue(viofosync.sync(
                    "camera.invalid", destination, "none", priority, filters,
                    SimpleNamespace(html=html, gps_extract=False),
                ))
            files = {p.name: p.read_bytes() for p in Path(destination).iterdir() if p.is_file()}
        self.assertTrue(all(body == PAYLOAD for body in files.values()))
        return requests, set(files)

    def assert_snapshot_once(self, requests, html, metadata=False):
        urls = [url for _, url in requests]
        self.assertEqual(urls.count(BASE_URL + "/?custom=1&cmd=3014"), 1)
        self.assertEqual(urls.count(BASE_URL + "/?custom=1&cmd=3015&par=1"), int(not html) + int(metadata))
        for directory in viofosync.DCIM_DIRS + viofosync.DCIM_DIRS_RO:
            self.assertEqual(urls.count(BASE_URL + directory), int(html))

    def test_xml_listing_is_complete_even_with_read_only_enabled(self):
        records = [recording(attr=32), recording(sequence=11, attr=33)]
        with patch.object(viofosync, "read_only", True), patch.object(
            viofosync, "camera_control_request", return_value=Response(xml_listing(records))
        ) as request:
            self.assertEqual(viofosync.get_dashcam_filenames(BASE_URL), records)
        self.assertEqual(request.call_count, 1)

    def test_html_listing_visits_all_directories_once_before_selection(self):
        records = [
            recording(directory=directory, sequence=10 + i)
            for i, directory in enumerate(viofosync.DCIM_DIRS + viofosync.DCIM_DIRS_RO)
        ]
        for only_locked in (False, True):
            with self.subTest(read_only=only_locked):
                def respond(url, timeout):
                    return Response(html_listing(records, urllib.parse.urlsplit(url).path))
                with patch.object(viofosync, "read_only", only_locked), patch.object(
                    viofosync, "camera_control_request", side_effect=respond
                ) as request:
                    listed = viofosync.get_dashcam_filenames_html(BASE_URL)
                self.assertEqual({r.filepath for r in listed}, {r.filepath for r in records})
                self.assertEqual([c.args[0] for c in request.call_args_list], [
                    BASE_URL + directory
                    for directory in viofosync.DCIM_DIRS + viofosync.DCIM_DIRS_RO
                ])

    def test_snapshot_once_and_deferred_files_never_head_download_or_delete(self):
        old = recording(stamp="2026_1021_115700")
        newest = [recording(sequence=i, tag=tag) for i, tag in enumerate(("F", "I", "R"), 20)]
        for html in (False, True):
            for state in (1, None):
                with self.subTest(html=html, state=state):
                    requests, files = self.run_sync([old, *newest], html=html, state=state, delete=True)
                    self.assertEqual(files, {old.filename})
                    # HTML enriches eligible deletion metadata in one XML batch;
                    # this never adds new recordings to the initial snapshot.
                    self.assert_snapshot_once(requests, html, metadata=html)
                    for rec in newest:
                        self.assertFalse(any(rec.filename in urllib.parse.unquote(url) for _, url in requests))
                    self.assertEqual(sum(method == "HEAD" for method, _ in requests), 1)
                    deletes = [url for _, url in requests if "cmd=4003" in url]
                    self.assertEqual(len(deletes), 1)
                    self.assertIn(old.filename, urllib.parse.unquote(deletes[0]))

    def test_locked_deletion_opt_in_never_bypasses_active_guard(self):
        old = recording(stamp="2026_1021_115700", directory="/DCIM/Movie/RO", attr=33)
        newest = [
            recording(sequence=i, tag=tag, directory="/DCIM/Movie/RO", attr=33)
            for i, tag in enumerate(("PF", "PI", "PR"), 20)
        ]
        for html in (False, True):
            for state in (1, None):
                with self.subTest(html=html, state=state):
                    requests, files = self.run_sync(
                        [old, *newest], html=html, state=state,
                        delete=True, locked=True,
                    )
                    self.assertEqual(files, {old.filename})
                    self.assert_snapshot_once(requests, html, metadata=html)
                    deletes = [url for _, url in requests if "cmd=4003" in url]
                    self.assertEqual(len(deletes), 1)
                    self.assertIn(old.filename, urllib.parse.unquote(deletes[0]))
                    for rec in newest:
                        self.assertFalse(any(
                            rec.filename in urllib.parse.unquote(url)
                            for _, url in requests
                        ))

    def test_read_only_selection_sees_the_newer_unlocked_capture(self):
        old_locked = recording(stamp="2026_1021_115700", directory="/DCIM/Movie/RO", attr=33)
        newest_unlocked = recording()
        for html in (False, True):
            with self.subTest(html=html):
                requests, files = self.run_sync([old_locked, newest_unlocked], html=html, only_locked=True)
                self.assertEqual(files, {old_locked.filename})
                self.assert_snapshot_once(requests, html)

    def test_read_only_selects_either_attribute_or_ro_path_after_guard(self):
        attr_locked = recording(stamp="2026_1021_115100", attr=33)
        path_locked = recording(stamp="2026_1021_115400", directory="/DCIM/Movie/RO", attr=32)
        unlocked = recording(stamp="2026_1021_115700")
        _, files = self.run_sync([attr_locked, path_locked, unlocked, recording()], only_locked=True)
        self.assertEqual(files, {attr_locked.filename, path_locked.filename})

    def test_html_normal_selection_still_uses_newer_ro_capture_for_guard(self):
        old_normal = recording(stamp="2026_1021_115700")
        newest_ro = recording(directory="/DCIM/Movie/RO", attr=33)
        requests, files = self.run_sync([old_normal, newest_ro], html=True)
        self.assertEqual(files, {old_normal.filename})
        self.assert_snapshot_once(requests, True)

    def test_filename_filter_does_not_make_an_older_matching_file_active(self):
        old = recording(stamp="2026_1021_115700", sequence=10)
        newest = recording(sequence=20)
        _, files = self.run_sync([old, newest], filters=["000010F"])
        self.assertEqual(files, {old.filename})

    def test_cutoff_and_stacked_selection_do_not_replace_newest_capture(self):
        old = recording(stamp="2026_1020_115700", listed_time=datetime.datetime(2026, 10, 21, 11, 57))
        newest = recording(listed_time=datetime.datetime(2026, 10, 20, 12))
        for options in ({"cutoff": datetime.date(2026, 10, 21)}, {"stacked": [newest.filename]}):
            with self.subTest(options=options):
                _, files = self.run_sync([old, newest], **options)
                self.assertEqual(files, {old.filename})

    def test_explicit_stopped_syncs_newest_in_both_listing_modes(self):
        records = [recording(stamp="2026_1021_115700"), recording()]
        for html in (False, True):
            with self.subTest(html=html):
                requests, files = self.run_sync(records, html=html, state=0, priority="rdate")
                self.assertEqual(files, {rec.filename for rec in records})
                self.assert_snapshot_once(requests, html)


class PacingTests(CameraCase):
    def test_request_starts_are_one_second_apart_even_after_failure(self):
        clock = [100.0]
        starts = []

        def sleep(seconds):
            self.assertGreater(seconds, 0)
            clock[0] += seconds

        def open_request(url, timeout):
            starts.append(clock[0])
            if len(starts) == 1:
                raise urllib.error.URLError("synthetic failure")
            return Response()

        with patch.object(viofosync.time, "monotonic", side_effect=lambda: clock[0]), patch.object(
            viofosync.time, "sleep", side_effect=sleep
        ), patch.object(viofosync.urllib.request, "urlopen", side_effect=open_request) as request:
            with self.assertRaises(urllib.error.URLError):
                viofosync.camera_control_request(BASE_URL + "/?custom=1&cmd=3014", 3.0)
            viofosync.camera_control_request(urllib.request.Request(BASE_URL + "/video.MP4", method="HEAD"), 3.0).close()
            viofosync.camera_control_request(BASE_URL + "/video.MP4", 3.0).close()
        self.assertEqual(starts, [100.0, 101.0, 102.0])
        self.assertTrue(all(b - a >= 1.0 for a, b in zip(starts, starts[1:])))
        self.assertEqual(request.call_count, 3)
        self.assertTrue(all(call.kwargs["timeout"] == 3.0 for call in request.call_args_list))


if __name__ == "__main__":
    unittest.main()
