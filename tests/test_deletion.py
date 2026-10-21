"""Deletion safety tests. All recordings and camera responses are synthetic."""

import datetime
import http.client
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import viofosync as app


NAME = "2026_1021_120000_000001F.MP4"
CAMERA_PATH = "B:\\DCIM\\Movie\\RO\\" + NAME
ADDRESS = "synthetic-camera.invalid"
PAYLOAD = b"synthetic recording"
STAMP = datetime.datetime(2026, 10, 21, 12)


def recording(filepath=CAMERA_PATH, attr=33, size=len(PAYLOAD), timecode=1):
    return app.Recording(NAME, filepath, size, timecode, STAMP, attr)


class Response(io.BytesIO):
    def __init__(self, data=b"", status=200, length=None):
        super().__init__(data)
        self.status = status
        self.length = length

    def getcode(self):
        return self.status

    def getheader(self, name):
        return self.length if name.lower() == "content-length" else None


class InterruptedResponse(Response):
    def read(self, size=-1):
        if self.tell():
            raise http.client.IncompleteRead(b"", len(PAYLOAD))
        return super().read(3)


class ConfigurationTests(unittest.TestCase):
    def test_deletion_defaults_are_disabled(self):
        self.assertFalse(app.delete_after_sync)
        self.assertFalse(app.delete_locked)
        with mock.patch.object(sys, "argv", ["viofosync.py", ADDRESS]):
            args = app.parse_args()
        self.assertFalse(args.delete_after_sync)
        self.assertFalse(args.delete_locked)

    def test_cli_options_are_independent(self):
        for flags, expected in [
            (["--delete-after-sync"], (True, False)),
            (["--delete-locked"], (False, True)),
            (["--delete-after-sync", "--delete-locked"], (True, True)),
        ]:
            with self.subTest(flags=flags), mock.patch.object(
                sys, "argv", ["viofosync.py", ADDRESS] + flags
            ):
                args = app.parse_args()
                self.assertEqual(
                    (args.delete_after_sync, args.delete_locked), expected
                )

    def test_run_applies_both_options(self):
        observed = []

        def synced(*args):
            observed.append((app.delete_after_sync, app.delete_locked))
            return True

        with mock.patch.object(sys, "argv", [
            "viofosync.py", ADDRESS, "--delete-after-sync", "--delete-locked"
        ]), mock.patch.object(app, "sync", side_effect=synced), \
                mock.patch.object(app, "delete_after_sync", False), \
                mock.patch.object(app, "delete_locked", False):
            self.assertEqual(app.run(), 0)
        self.assertEqual(observed, [(True, True)])

    def test_shell_boolean_options_do_not_imply_each_other(self):
        # A tiny synthetic app records argv; the wrapper never starts a sync.
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            stub = Path(directory) / "viofosync.py"
            stub.write_text(
                "import json, sys\nprint(json.dumps(sys.argv[1:]))\n",
                encoding="utf-8",
            )
            for after, locked in [
                (None, None), ("0", "0"), ("0", "1"), ("1", "0"),
                ("1", "1"), ("true", "YES"), ("false", "off"),
            ]:
                env = {
                    "PATH": os.defpath, "APP_DIR": directory,
                    "PYTHON_BIN": sys.executable,
                }
                if after is not None:
                    env["DELETE_AFTER_SYNC"] = after
                if locked is not None:
                    env["DELETE_LOCKED"] = locked
                with self.subTest(after=after, locked=locked):
                    result = subprocess.run(
                        ["bash", str(ROOT / "viofosync.sh")],
                        env=env, cwd=ROOT, check=True,
                        capture_output=True, text=True,
                    )
                    argv = json.loads(result.stdout)
                    self.assertEqual(
                        "--delete-after-sync" in argv,
                        after in ("1", "true"),
                    )
                    self.assertEqual(
                        "--delete-locked" in argv,
                        locked in ("1", "YES"),
                    )


class CameraTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=ROOT / "tests")
        self.addCleanup(self.directory.cleanup)
        self.destination = Path(self.directory.name)
        self.local_path = self.destination / NAME
        self.local_path.write_bytes(PAYLOAD)
        self.clock = 100.0
        patches = [
            mock.patch.multiple(
                app, delete_after_sync=True, delete_locked=False,
                dry_run=False, read_only=False, cutoff_date=None,
                max_download_attempts=1, _last_control_request_at=None,
            ),
            mock.patch.object(app.time, "monotonic", side_effect=lambda: self.clock),
            mock.patch.object(app.time, "sleep", side_effect=self.advance),
            mock.patch.object(app, "destination_is_too_full", return_value=False),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(
            app.urllib.request, "urlopen",
            side_effect=AssertionError("Unexpected network request in test"),
        )
        self.urlopen = patch.start()
        self.addCleanup(patch.stop)

    def advance(self, seconds):
        self.clock += seconds

    def handle(self, rec=None):
        app._handle_camera_deletion(
            ADDRESS, rec or recording(), str(self.local_path)
        )


class DeletionPolicyTests(CameraTestCase):
    def test_locked_files_require_both_flags(self):
        variants = [recording(), recording(attr=32), recording(
            filepath="B:\\DCIM\\Movie\\" + NAME, attr=33
        )]
        for rec in variants:
            for after, locked in [(False, False), (False, True), (True, False)]:
                with self.subTest(rec=rec, after=after, locked=locked), \
                        mock.patch.object(app, "delete_after_sync", after), \
                        mock.patch.object(app, "delete_locked", locked), \
                        mock.patch.object(app, "delete_from_camera") as delete:
                    self.handle(rec)
                    delete.assert_not_called()

    def test_unlocked_files_keep_existing_deletion_behavior(self):
        rec = recording(filepath="B:\\DCIM\\Movie\\" + NAME, attr=32)
        with mock.patch.object(app, "delete_from_camera", return_value=True) as delete:
            self.handle(rec)
        delete.assert_called_once_with(ADDRESS, rec.filepath, app.socket_timeout)

    def test_locked_opt_in_deletes_attr_and_ro_path_variants(self):
        with mock.patch.object(app, "delete_locked", True):
            for rec in [recording(), recording(attr=32), recording(
                filepath="B:\\DCIM\\Movie\\" + NAME, attr=33
            )]:
                with self.subTest(rec=rec), mock.patch.object(
                    app, "delete_from_camera", return_value=True
                ) as delete:
                    self.handle(rec)
                    delete.assert_called_once_with(
                        ADDRESS, rec.filepath, app.socket_timeout
                    )

    def test_unresolved_html_metadata_never_authorizes_deletion(self):
        for locked in (False, True):
            with self.subTest(locked=locked), \
                    mock.patch.object(app, "delete_locked", locked), \
                    mock.patch.object(app, "delete_from_camera") as delete:
                self.handle(recording(timecode=None, attr=None))
                delete.assert_not_called()

    def test_dry_run_logs_without_delete_request(self):
        with mock.patch.object(app, "delete_locked", True), \
                mock.patch.object(app, "dry_run", True), \
                self.assertLogs(level="INFO") as messages:
            self.handle()
        self.assertTrue(any("DRY RUN" in item for item in messages.output))
        self.urlopen.assert_not_called()

    def test_current_local_copy_must_match_positive_exact_xml_size(self):
        with mock.patch.object(app, "delete_locked", True):
            for data, size in [
                (PAYLOAD[:-1], len(PAYLOAD)), (PAYLOAD + b"x", len(PAYLOAD)),
                (b"", len(PAYLOAD)), (PAYLOAD, None), (PAYLOAD, 0),
                (PAYLOAD, -1),
            ]:
                self.local_path.write_bytes(data)
                with self.subTest(data=data, size=size), mock.patch.object(
                    app, "delete_from_camera"
                ) as delete:
                    self.handle(recording(size=size))
                    delete.assert_not_called()
            self.local_path.unlink()
            with mock.patch.object(app, "delete_from_camera") as delete:
                self.handle()
                delete.assert_not_called()

    def test_metadata_path_must_match_recording_filename(self):
        with mock.patch.object(app, "delete_locked", True), \
                mock.patch.object(app, "delete_from_camera") as delete:
            self.handle(recording(filepath="B:\\DCIM\\Movie\\another.MP4"))
        delete.assert_not_called()

    def test_delete_errors_warn_once_without_retry(self):
        with mock.patch.object(app, "delete_locked", True), \
                mock.patch.object(app, "delete_from_camera", side_effect=
                                  urllib.error.URLError("synthetic failure")) as delete, \
                self.assertLogs(level="WARNING") as messages:
            self.handle()
        delete.assert_called_once()
        self.assertTrue(any("Failed to delete" in item for item in messages.output))


class DeleteRequestTests(CameraTestCase):
    def test_exact_b_drive_path_is_encoded_once_in_one_request(self):
        self.urlopen.side_effect = None
        self.urlopen.return_value = Response(b"<Function><Status>0</Status></Function>")
        self.assertTrue(app.delete_from_camera(ADDRESS, CAMERA_PATH, 7))
        url, = self.urlopen.call_args.args
        if isinstance(url, app.urllib.request.Request):
            url = url.full_url
        self.assertEqual(url, "http://" + ADDRESS +
                         "/?custom=1&cmd=4003&str=B%3A%5CDCIM%5CMovie%5CRO%5C" + NAME)
        self.assertNotIn("A%3A", url)
        self.urlopen.assert_called_once()
        self.assertEqual(self.urlopen.call_args.kwargs["timeout"], 7)

    def test_ambiguous_or_unsafe_paths_make_no_request(self):
        for path in [
            None, "", NAME, "/DCIM/Movie/" + NAME,
            "B:DCIM/Movie/" + NAME, "B:/Other/" + NAME,
            "B:/DCIM/Movie/../" + NAME, "B:/DCIM/Movie/./" + NAME,
        ]:
            with self.subTest(path=path):
                self.assertFalse(app.delete_from_camera(ADDRESS, path, 7))
        self.urlopen.assert_not_called()

    def test_non_success_http_or_xml_response_is_not_retried(self):
        for status, data in [
            (503, b"<Function><Status>0</Status></Function>"),
            (200, b"<Function><Status>-1</Status></Function>"),
            (200, b"<Function><Status>1</Status></Function>"),
            (200, b"<Function/>"),
        ]:
            with self.subTest(status=status, data=data):
                self.urlopen.reset_mock()
                self.urlopen.side_effect = None
                self.urlopen.return_value = Response(data, status=status)
                self.assertFalse(app.delete_from_camera(ADDRESS, CAMERA_PATH, 7))
                self.urlopen.assert_called_once()

    def test_http_error_is_not_retried(self):
        self.urlopen.side_effect = urllib.error.HTTPError(
            "http://" + ADDRESS, 403, "synthetic rejection", {}, None
        )
        with self.assertRaises(urllib.error.HTTPError):
            app.delete_from_camera(ADDRESS, CAMERA_PATH, 7)
        self.urlopen.assert_called_once()

    def test_connection_and_xml_errors_propagate_without_retry(self):
        for failure in [urllib.error.URLError("synthetic failure"), None]:
            with self.subTest(failure=failure):
                self.urlopen.reset_mock()
                self.urlopen.side_effect = failure
                self.urlopen.return_value = Response(b"not xml")
                expected = urllib.error.URLError if failure else ET.ParseError
                with self.assertRaises(expected):
                    app.delete_from_camera(ADDRESS, CAMERA_PATH, 7)
                self.urlopen.assert_called_once()


class SyncDeletionTests(CameraTestCase):
    def install_camera(self, rec=None, transfer=PAYLOAD, head_length=None,
                       html=False, xml_records=None, xml_error=None):
        rec = rec or recording()
        self.deletes = []
        self.downloads = []
        self.xml_requests = []
        self.request_starts = []
        if xml_records is None:
            xml_records = [rec]
        listing = "<LIST>" + "".join(
            (
                "<File><NAME>{}</NAME><FPATH>{}</FPATH><SIZE>{}</SIZE>"
                "<TIMECODE>1</TIMECODE><TIME>2026/10/21 12:00:00</TIME>"
                "<ATTR>{}</ATTR></File>"
            ).format(item.filename, item.filepath, item.size, item.attr)
            for item in xml_records
        ) + "</LIST>"
        relative = rec.filepath[2:].replace("\\", "/")
        html_directory = relative.rsplit("/", 1)[0]

        def serve(request, **kwargs):
            self.request_starts.append(self.clock)
            url = request.full_url if isinstance(request, app.urllib.request.Request) else request
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            command = query.get("cmd", [None])[0]
            if command == "3015":
                self.xml_requests.append(url)
                if xml_error:
                    raise xml_error
                return Response(listing.encode())
            if command == "4003":
                self.deletes.append(url)
                return Response(b"<Function><Status>0</Status></Function>")
            if isinstance(request, app.urllib.request.Request) and request.get_method() == "HEAD":
                return Response(length=head_length)
            if html and urllib.parse.urlsplit(url).path in app.DCIM_DIRS + app.DCIM_DIRS_RO:
                page = b""
                if urllib.parse.urlsplit(url).path == html_directory:
                    page = ('<a href="{}"><b>{}</b></a><td align=right>1.00 MB'.format(
                        NAME, NAME
                    )).encode()
                return Response(page)
            self.downloads.append(url)
            if isinstance(transfer, Exception):
                raise transfer
            if transfer is InterruptedResponse:
                return InterruptedResponse(PAYLOAD)
            return Response(transfer)

        self.urlopen.side_effect = serve

    def sync(self, html=False, recording_filter=None):
        return app.sync(
            ADDRESS, str(self.destination), "none", "date", recording_filter,
            SimpleNamespace(html=html, gps_extract=False),
        )

    def test_successful_download_and_existing_verified_copy_are_deleted(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                self.local_path.unlink(missing_ok=True)
                if existing:
                    self.local_path.write_bytes(PAYLOAD)
                self.install_camera()
                with mock.patch.object(app, "delete_locked", True):
                    self.assertTrue(self.sync())
                self.assertEqual(self.local_path.read_bytes(), PAYLOAD)
                self.assertEqual(len(self.downloads), 0 if existing else 1)
                self.assertEqual(len(self.deletes), 1)
                self.assertEqual(
                    urllib.parse.parse_qs(urllib.parse.urlsplit(self.deletes[0]).query)["str"],
                    [CAMERA_PATH],
                )

    def test_partial_failed_and_interrupted_transfers_do_not_delete_or_replace_copy(self):
        for transfer in [b"", b"short", urllib.error.URLError("synthetic failure"),
                         InterruptedResponse]:
            with self.subTest(transfer=transfer):
                self.local_path.write_bytes(b"previous verified copy")
                self.install_camera(transfer=transfer)
                with mock.patch.object(app, "delete_locked", True):
                    self.assertTrue(self.sync())
                self.assertEqual(self.local_path.read_bytes(), b"previous verified copy")
                self.assertEqual(self.deletes, [])
                self.assertEqual(list(self.destination.glob("*.part")), [])
                self.assertEqual(list(self.destination.glob(".*.part")), [])

    def test_download_retries_finish_before_one_delete(self):
        self.local_path.unlink()
        self.install_camera()
        serve = self.urlopen.side_effect

        def retrying(request, **kwargs):
            response = serve(request, **kwargs)
            if isinstance(request, str) and request.endswith(NAME):
                if len(self.downloads) == 1:
                    return Response(b"short")
                if len(self.downloads) == 2:
                    raise urllib.error.URLError("synthetic retryable download failure")
            return response

        self.urlopen.side_effect = retrying
        with mock.patch.object(app, "max_download_attempts", 3), \
                mock.patch.object(app, "delete_locked", True):
            self.assertTrue(self.sync())
        self.assertEqual(self.local_path.read_bytes(), PAYLOAD)
        self.assertEqual(len(self.downloads), 3)
        self.assertEqual(len(self.deletes), 1)
        self.assertEqual(list(self.destination.glob(".*.part")), [])

    def test_html_exact_xml_metadata_enables_one_b_drive_delete(self):
        self.local_path.unlink()
        self.install_camera(html=True)
        with mock.patch.object(app, "delete_locked", True):
            self.assertTrue(self.sync(html=True))
        self.assertEqual(self.local_path.read_bytes(), PAYLOAD)
        self.assertEqual(len(self.downloads), 1)
        self.assertEqual(len(self.xml_requests), 1)
        self.assertEqual(len(self.deletes), 1)
        self.assertEqual(
            urllib.parse.parse_qs(urllib.parse.urlsplit(self.deletes[0]).query)["str"],
            [CAMERA_PATH],
        )
        self.assertTrue(all(
            second - first >= 1.0 for first, second in
            zip(self.request_starts, self.request_starts[1:])
        ))

    def test_html_one_xml_lookup_supplies_multiple_candidates(self):
        second_name = NAME.replace("000001", "000002")
        second = recording()._replace(
            filename=second_name, filepath=CAMERA_PATH.replace(NAME, second_name)
        )
        (self.destination / second_name).write_bytes(PAYLOAD)
        self.install_camera(html=True, xml_records=[recording(), second])
        html_records = [item._replace(
            filepath=item.filepath.replace("B:", "A:"), timecode=None, attr=None,
            size=1048576,
        ) for item in [recording(), second]]
        with mock.patch.object(app, "delete_locked", True), \
                mock.patch.object(app, "get_dashcam_filenames_html", return_value=html_records):
            self.assertTrue(self.sync(html=True))
        self.assertEqual(len(self.xml_requests), 1)
        self.assertEqual(len(self.deletes), 2)
        self.assertEqual(self.downloads, [])

    def test_html_missing_ambiguous_and_failed_xml_preserve_download_without_delete(self):
        for metadata in [
            {"xml_records": []},
            {"xml_records": [recording(), recording(filepath=CAMERA_PATH.replace("B:", "A:"))]},
            {"xml_error": urllib.error.URLError("synthetic metadata failure")},
        ]:
            with self.subTest(metadata=metadata):
                self.local_path.unlink(missing_ok=True)
                self.install_camera(html=True, head_length=str(len(PAYLOAD)), **metadata)
                with mock.patch.object(app, "delete_locked", True):
                    self.assertTrue(self.sync(html=True))
                self.assertEqual(self.local_path.read_bytes(), PAYLOAD)
                self.assertEqual(len(self.downloads), 1)
                self.assertEqual(len(self.xml_requests), 1)
                self.assertEqual(self.deletes, [])

    def test_unknown_size_html_download_is_preserved_without_delete(self):
        self.local_path.unlink()
        self.install_camera(html=True, xml_records=[])
        with mock.patch.object(app, "delete_locked", True):
            self.assertTrue(self.sync(html=True))
        self.assertEqual(self.local_path.read_bytes(), PAYLOAD)
        self.assertEqual(len(self.downloads), 1)
        self.assertEqual(self.deletes, [])

    def test_html_no_extra_xml_lookup_when_deletion_disabled(self):
        self.install_camera(html=True, head_length=str(len(PAYLOAD)))
        with mock.patch.object(app, "delete_after_sync", False), \
                mock.patch.object(app, "delete_locked", True):
            self.assertTrue(self.sync(html=True))
        self.assertEqual(self.xml_requests, [])
        self.assertEqual(self.deletes, [])

    def test_html_protected_ro_files_download_without_extra_xml_lookup(self):
        self.local_path.unlink()
        self.install_camera(html=True, head_length=str(len(PAYLOAD)))
        with mock.patch.object(app, "read_only", True):
            self.assertTrue(self.sync(html=True))
        self.assertEqual(self.xml_requests, [])
        self.assertEqual(self.deletes, [])
        self.assertEqual(len(self.downloads), 1)
        self.assertEqual(self.local_path.read_bytes(), PAYLOAD)

    def test_html_no_extra_xml_lookup_for_filtered_expired_or_stacked_files(self):
        for excluded_by in ("filter", "keep", "stacked"):
            with self.subTest(excluded_by=excluded_by):
                self.install_camera(html=True)
                cutoff = STAMP.date() + datetime.timedelta(days=1) if excluded_by == "keep" else None
                stacked = {NAME} if excluded_by == "stacked" else set()
                with mock.patch.object(app, "delete_locked", True), \
                        mock.patch.object(app, "cutoff_date", cutoff), \
                        mock.patch.object(app, "load_stacked_names", return_value=stacked):
                    self.assertTrue(self.sync(
                        html=True, recording_filter=["no match"] if excluded_by == "filter" else None
                    ))
                self.assertEqual(self.xml_requests, [])
                self.assertEqual(self.deletes, [])

    def test_html_adds_ro_directories_only_for_opt_in_and_respects_read_only(self):
        for locked, read_only, expected in [
            (False, False, app.DCIM_DIRS),
            (True, False, app.DCIM_DIRS + app.DCIM_DIRS_RO),
            (True, True, app.DCIM_DIRS_RO),
        ]:
            with self.subTest(locked=locked, read_only=read_only):
                visited = []

                def serve(url, **kwargs):
                    visited.append(urllib.parse.urlsplit(url).path)
                    return Response()

                self.urlopen.side_effect = serve
                with mock.patch.object(app, "delete_locked", locked), \
                        mock.patch.object(app, "read_only", read_only):
                    self.assertEqual(app.get_dashcam_filenames_html("http://" + ADDRESS), [])
                self.assertEqual(visited, expected)

    def test_delete_locked_alone_never_deletes(self):
        self.install_camera()
        with mock.patch.object(app, "delete_after_sync", False), \
                mock.patch.object(app, "delete_locked", True):
            self.assertTrue(self.sync())
        self.assertEqual(self.deletes, [])

    def test_dry_run_does_not_download_or_delete(self):
        self.local_path.unlink()
        self.install_camera()
        with mock.patch.object(app, "delete_locked", True), \
                mock.patch.object(app, "dry_run", True):
            self.assertTrue(self.sync())
        self.assertFalse(self.local_path.exists())
        self.assertEqual(self.downloads, [])
        self.assertEqual(self.deletes, [])


class ControlPacingTests(CameraTestCase):
    def test_listing_and_deletion_share_one_second_start_spacing(self):
        starts = []

        def serve(request, **kwargs):
            starts.append(self.clock)
            url = request.full_url if isinstance(request, app.urllib.request.Request) else request
            return Response(b"<LIST/>" if "cmd=3015" in url else
                            b"<Function><Status>0</Status></Function>")

        self.urlopen.side_effect = serve
        app.get_dashcam_filenames("http://" + ADDRESS)
        self.assertTrue(app.delete_from_camera(ADDRESS, CAMERA_PATH, 7))
        app.get_dashcam_filenames("http://" + ADDRESS)
        self.assertEqual(starts, [100.0, 101.0, 102.0])

    def test_failed_control_requests_also_consume_the_interval(self):
        starts = []

        def serve(request, **kwargs):
            starts.append(self.clock)
            if len(starts) == 1:
                raise urllib.error.URLError("synthetic failure")
            return Response(b"<Function><Status>0</Status></Function>")

        self.urlopen.side_effect = serve
        with self.assertRaises(urllib.error.URLError):
            app.delete_from_camera(ADDRESS, CAMERA_PATH, 7)
        self.assertTrue(app.delete_from_camera(ADDRESS, CAMERA_PATH, 7))
        self.assertEqual(starts, [100.0, 101.0])

    def test_elapsed_request_time_counts_towards_start_spacing(self):
        starts = []

        def serve(request, **kwargs):
            starts.append(self.clock)
            self.advance(0.4)
            return Response(b"<Function><Status>0</Status></Function>")

        self.urlopen.side_effect = serve
        app.delete_from_camera(ADDRESS, CAMERA_PATH, 7)
        app.delete_from_camera(ADDRESS, CAMERA_PATH, 7)
        self.assertEqual(starts, [100.0, 101.0])
        self.assertAlmostEqual(self.clock, 101.4)

    def test_head_requests_share_camera_request_pacing(self):
        self.urlopen.side_effect = None
        self.urlopen.return_value = Response(length="19")
        self.assertEqual(app.get_remote_size("http://" + ADDRESS + "/synthetic.MP4", 7), 19)
        self.urlopen.return_value = Response(length="19")
        self.assertEqual(app.get_remote_size("http://" + ADDRESS + "/synthetic.MP4", 7), 19)
        self.assertEqual(app._last_control_request_at, 101.0)
        self.assertEqual(self.clock, 101.0)


if __name__ == "__main__":
    unittest.main()
