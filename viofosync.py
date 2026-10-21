#!/usr/bin/env python3

# Copyright (c) 2024 Rob Smith
# Based on BlackVueSync by Alessandro Colomba
# (https://github.com/acolomba)
# GPS extraction method by Sergei Franco
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation
# files (the "Software"), to deal in the Software without
# restriction, including without limitation the rights to use,
# copy, modify, merge, publish, distribute, sublicense, and/or
# sell copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following
# conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
# OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
# WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.

__version__ = "1.6"

import argparse
import datetime
import glob
import http.client
import logging
import os
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import namedtuple

# Logging
logging.basicConfig(
    format="%(asctime)s: %(levelname)s %(message)s"
)
logger = logging.getLogger()
cron_logger = logging.getLogger("cron")

# Globals
dry_run = False
read_only = False
delete_after_sync = False
delete_locked = False
_last_control_request_at = None
max_disk_used_percent = 90
cutoff_date = None
socket_timeout = 10.0

DEFAULT_DOWNLOAD_ATTEMPTS = 1
max_download_attempts = DEFAULT_DOWNLOAD_ATTEMPTS
RETRY_BACKOFF = 5  # seconds, multiplied by attempt number
DEFAULT_MERGE_GAP_SECONDS = 2.0
FALLBACK_SEGMENT_SECONDS = (180.0, 300.0)

# Recording namedtuple matching Viofo's file information
Recording = namedtuple(
    "Recording",
    "filename filepath size timecode datetime attr",
)

LocalRecording = namedtuple(
    "LocalRecording",
    "filepath filename size datetime sequence camera mode tag duration",
)

# Group name globs, keyed by grouping
group_name_globs = {
    "none": None,
    "daily": "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]",
    "weekly": "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]",
    "monthly": "[0-9][0-9][0-9][0-9]-[0-9][0-9]",
    "yearly": "[0-9][0-9][0-9][0-9]",
}

# Downloaded recording filename glob pattern
downloaded_filename_glob = (
    "[0-9][0-9][0-9][0-9]_[0-9][0-9][0-9][0-9]"
    "_[0-9][0-9][0-9][0-9][0-9][0-9]"
    "_*.*"
)

# Downloaded recording filename regular expression
downloaded_filename_re = re.compile(
    r"^(?P<year>\d{4})_(?P<month>\d{2})(?P<day>\d{2})"
    r"_(?P<hour>\d{2})(?P<minute>\d{2})(?P<second>\d{2})"
    r"_(?P<sequence>\d+)(?P<camera>.+)"
    r"\.(?P<extension>MP4|JPE?G)$",
    re.IGNORECASE,
)

# Viofo camera filename pattern
filename_re = re.compile(
    r"(?P<year>\d{4})_(?P<month>\d{2})(?P<day>\d{2})"
    r"_(?P<hour>\d{2})(?P<minute>\d{2})(?P<second>\d{2})"
    r"_(?P<sequence>\d+)(?P<camera>.+)"
    r"\.(?P<extension>MP4|JPE?G)",
    re.IGNORECASE,
)


def to_downloaded_recording(filename, grouping):
    """Extracts destination recording info from a filename."""
    m = downloaded_filename_re.match(filename)
    if m is None:
        return None

    recording_datetime = datetime.datetime(
        int(m.group("year")), int(m.group("month")),
        int(m.group("day")), int(m.group("hour")),
        int(m.group("minute")), int(m.group("second")),
    )
    return Recording(filename, None, None, None,
                     recording_datetime, None)


def parse_viofo_datetime(time_str):
    """Parse the datetime string from Viofo's format."""
    return datetime.datetime.strptime(time_str, "%Y/%m/%d %H:%M:%S")


def positive_int(value):
    """argparse type: integer >= 1."""
    try:
        parsed = int(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            f"invalid int value: '{value}'"
        ) from e

    if parsed < 1:
        raise argparse.ArgumentTypeError(
            "value must be greater than or equal to 1"
        )
    return parsed


def non_negative_float(value):
    """argparse type: float >= 0."""
    try:
        parsed = float(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            f"invalid float value: '{value}'"
        ) from e

    if parsed < 0:
        raise argparse.ArgumentTypeError(
            "value must be greater than or equal to 0"
        )
    return parsed


def parse_recording_filename(filename):
    """Parses a Viofo recording filename into recording metadata."""
    m = filename_re.match(os.path.basename(filename))
    if m is None:
        return None

    tag = m.group("camera").upper()
    mode = "parking" if tag.startswith("P") else "normal"
    camera = tag[1:] if mode == "parking" else tag

    recording_datetime = datetime.datetime(
        int(m.group("year")), int(m.group("month")),
        int(m.group("day")), int(m.group("hour")),
        int(m.group("minute")), int(m.group("second")),
    )
    return {
        "datetime": recording_datetime,
        "sequence": int(m.group("sequence")),
        "camera": camera,
        "mode": mode,
        "tag": tag,
    }


def is_video_file(filename):
    """Returns True when filename is a video recording."""
    return os.path.splitext(filename)[1].lower() == ".mp4"


def html_href_to_camera_path(dir_path, href):
    """Builds a camera file path from an HTML listing href."""
    filepath = urllib.parse.unquote(href).split("?", 1)[0]
    filepath = filepath.replace("\\", "/")
    if re.match(r"^[A-Z]:", filepath, re.IGNORECASE):
        return filepath
    if filepath.startswith("/"):
        return f"A:{filepath}"
    return f"A:{dir_path.rstrip('/')}/{filepath}"


def camera_control_request(url, timeout):
    """Pace camera HTTP requests at least one second apart, even on failure."""
    global _last_control_request_at
    if _last_control_request_at is not None:
        wait = 1.0 - (time.monotonic() - _last_control_request_at)
        if wait > 0:
            time.sleep(wait)
    _last_control_request_at = time.monotonic()
    return urllib.request.urlopen(url, timeout=timeout)


def get_dashcam_filenames(base_url):
    """Gets the recording filenames from the Viofo dashcam."""
    try:
        url = f"{base_url}/?custom=1&cmd=3015&par=1"
        with camera_control_request(url, socket_timeout) as response:
            if response.getcode() != 200:
                raise RuntimeError(
                    f"Error response from {base_url}; "
                    f"status code: {response.getcode()}"
                )
            xml_data = response.read().decode("utf-8")
        root = ET.fromstring(xml_data)

        recordings = []
        for file_elem in root.findall(".//File"):
            attr = int(file_elem.find("ATTR").text)
            if read_only and attr != 33:
                continue
            name = file_elem.find("NAME").text
            filepath = file_elem.find("FPATH").text
            size = int(file_elem.find("SIZE").text)
            timecode = int(file_elem.find("TIMECODE").text)
            ts = parse_viofo_datetime(
                file_elem.find("TIME").text
            )
            recording = Recording(
                name, filepath, size, timecode, ts, attr
            )
            recordings.append(recording)

        logger.info(f"Found {len(recordings)} recordings on dashcam")
        return recordings
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Cannot obtain recordings from {base_url}: {e}"
        ) from e
    except socket.timeout as e:
        raise UserWarning(
            f"Timeout communicating with dashcam at "
            f"{base_url}: {e}"
        ) from e
    except http.client.RemoteDisconnected as e:
        raise UserWarning(
            f"Dashcam disconnected without response; "
            f"address: {base_url}: {e}"
        ) from e
    except ET.ParseError as e:
        raise RuntimeError(
            f"Error parsing XML response from dashcam: {e}"
        ) from e


# HTML directory listing regex
html_file_re = re.compile(
    r'<a href="(?P<filepath>[^"]+\.(?:MP4|JPE?G))">'
    r'<b>(?P<filename>[^<]+)</b></a>'
    r'<td align=right>\s*(?P<size>[\d.]+)\s*(?P<unit>[KMGT]?B)',
    re.IGNORECASE,
)

# Directories to scrape on the dashcam
DCIM_DIRS = ["/DCIM/Movie", "/DCIM/Movie/Parking", "/DCIM/Photo"]
DCIM_DIRS_RO = ["/DCIM/Movie/RO", "/DCIM/Photo/RO"]


def parse_html_size(size_str, unit):
    """Converts '102.00 MB' style size to bytes."""
    multipliers = {
        "B": 1, "KB": 1 << 10, "MB": 1 << 20,
        "GB": 1 << 30, "TB": 1 << 40,
    }
    return int(float(size_str) * multipliers.get(unit, 1))


def get_dashcam_filenames_html(base_url):
    """Gets recordings by scraping the HTML directory listings.

    Much faster than the XML API on cameras with many files.
    """
    dirs = (DCIM_DIRS_RO if read_only else
            DCIM_DIRS + (DCIM_DIRS_RO if delete_locked else []))
    recordings = []

    for dir_path in dirs:
        url = f"{base_url}{dir_path}"
        try:
            with camera_control_request(
                url, timeout=socket_timeout
            ) as resp:
                if resp.getcode() != 200:
                    logger.warning(
                        f"HTTP {resp.getcode()} for {url}, "
                        f"skipping"
                    )
                    continue
                html = resp.read().decode('utf-8')
        except urllib.error.HTTPError as e:
            if e.code == 404:
                logger.debug(
                    f"Directory not found: {dir_path}"
                )
                continue
            raise
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Cannot reach dashcam at {base_url}: {e}"
            ) from e
        except socket.timeout as e:
            raise UserWarning(
                f"Timeout communicating with dashcam at "
                f"{base_url}: {e}"
            ) from e

        for m in html_file_re.finditer(html):
            filepath = html_href_to_camera_path(
                dir_path, m.group("filepath")
            )
            filename = m.group("filename")
            size = parse_html_size(
                m.group("size"), m.group("unit").upper()
            )

            # Always extract datetime from the filename
            # (the actual recording timestamp)
            fm = filename_re.search(filename)
            if not fm:
                logger.warning(
                    f"Cannot parse date from filename: "
                    f"{filename}, skipping"
                )
                continue

            ts = datetime.datetime(
                int(fm.group("year")),
                int(fm.group("month")),
                int(fm.group("day")),
                int(fm.group("hour")),
                int(fm.group("minute")),
                int(fm.group("second")),
            )

            recordings.append(Recording(
                filename, filepath, size, None, ts, None
            ))

    logger.info(
        f"Found {len(recordings)} recordings on dashcam "
        f"(HTML mode)"
    )
    return recordings


def get_filepath(destination, group_name, filename):
    """Constructs a path from destination, group name and filename."""
    if group_name:
        return os.path.join(destination, group_name, filename)
    return os.path.join(destination, filename)


def get_remote_size(url, timeout):
    """HEAD request to get Content-Length of a remote file."""
    req = urllib.request.Request(url, method="HEAD")
    with camera_control_request(req, timeout=timeout) as resp:
        cl = resp.getheader("Content-Length")
    return int(cl) if cl and cl.isdigit() else None


def human_size(num_bytes):
    """Returns human-readable size string, e.g. '325.1 MB'."""
    if num_bytes == 0:
        return "0 B"
    for factor, suffix in [(1 << 30, "GB"), (1 << 20, "MB"),
                           (1 << 10, "KB"), (1, "B")]:
        if num_bytes >= factor:
            return f"{num_bytes / factor:.1f} {suffix}"
    return f"{num_bytes} B"


def human_speed(num_bytes, elapsed):
    """Returns human-readable speed string, e.g. '27.1 MB/s'."""
    bps = num_bytes / max(elapsed, 1e-9)
    if bps == 0:
        return "0 B/s"
    for factor, suffix in [(1 << 30, "GB/s"), (1 << 20, "MB/s"),
                           (1 << 10, "KB/s"), (1, "B/s")]:
        if bps >= factor:
            return f"{bps / factor:.1f} {suffix}"
    return f"{bps:.1f} B/s"


def download_file(base_url, recording, destination, group_name):
    """Downloads a file from the Viofo dashcam to the destination.

    Returns (downloaded: bool, speed_str: str|None,
    local_available: bool, verified: bool).
    Uses HEAD to check size, retries up to max_download_attempts,
    and verifies integrity after download.
    """
    if group_name:
        group_filepath = os.path.join(destination, group_name)
        ensure_destination(group_filepath)

    dest_filepath = get_filepath(
        destination, group_name, recording.filename
    )

    # Build download URL — strip drive letter (A: for SD, B: for SSD)
    # and normalise path separators
    cleaned = re.sub(r'^[A-Z]:', '', recording.filepath).replace(
        '\\', '/'
    )
    url = f"{base_url}/{cleaned.lstrip('/')}"

    # Check expected size via HEAD, falling back to exact XML metadata.
    try:
        expected_size = get_remote_size(url, socket_timeout)
    except Exception:
        expected_size = None
    if expected_size is None and recording.timecode is not None:
        expected_size = recording.size

    # Skip if already downloaded and size matches
    if os.path.exists(dest_filepath):
        local_size = os.path.getsize(dest_filepath)
        if expected_size is not None:
            if local_size == expected_size:
                logger.debug(
                    f"Skipping {recording.filename} "
                    f"({human_size(local_size)})"
                )
                return False, None, True, True
            # Size mismatch — re-download
            logger.info(
                f"Size mismatch for {recording.filename} "
                f"({human_size(local_size)}/"
                f"{human_size(expected_size)}), "
                f"re-downloading"
            )
        else:
            logger.debug(
                f"Already downloaded: {recording.filename}"
            )
            return False, None, local_size > 0, False

    if dry_run:
        logger.info(
            f"[DRY RUN] Would download: {recording.filename}"
        )
        return True, None, False, False

    # Atomic download with tempfile + retries
    dest_dir = os.path.dirname(dest_filepath)
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=dest_dir,
        prefix=f".{recording.filename}.",
        suffix=".part",
    )
    os.close(tmp_fd)

    try:
        for attempt in range(1, max_download_attempts + 1):
            try:
                start = time.perf_counter()
                with camera_control_request(
                    url, timeout=socket_timeout
                ) as resp, open(tmp_path, "wb") as out:
                    shutil.copyfileobj(resp, out)
                elapsed = time.perf_counter() - start
            except Exception as e:
                logger.warning(
                    f"Download attempt {attempt} failed for "
                    f"{recording.filename}: {e}"
                )
                if attempt < max_download_attempts:
                    time.sleep(RETRY_BACKOFF * attempt)
                continue

            actual_size = os.path.getsize(tmp_path)

            # Verify integrity
            if (expected_size is not None
                    and actual_size != expected_size):
                logger.warning(
                    f"Incomplete download of "
                    f"{recording.filename}: "
                    f"{human_size(actual_size)}/"
                    f"{human_size(expected_size)}"
                )
                if attempt < max_download_attempts:
                    time.sleep(RETRY_BACKOFF * attempt)
                continue

            # Success — atomic move into place
            os.replace(tmp_path, dest_filepath)
            size_str = human_size(actual_size)
            speed_str = human_speed(actual_size, elapsed)
            logger.info(
                f"Downloaded {recording.filename}: "
                f"{size_str} in {elapsed:.1f}s ({speed_str})"
            )
            return True, speed_str, True, expected_size is not None

        # All attempts exhausted
        logger.error(
            f"Failed to download {recording.filename} "
            f"after {max_download_attempts} attempts"
        )
        return False, None, False, False
    except socket.timeout as e:
        raise UserWarning(
            f"Timeout communicating with dashcam at "
            f"{base_url}: {e}"
        ) from e
    except http.client.RemoteDisconnected:
        cron_logger.warning(
            f"Remote end closed connection for "
            f"{recording.filename}; ignoring."
        )
        return False, None, False, False
    finally:
        # Clean up temp file if it still exists
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def verify_local_file(local_path, expected_size=None):
    """Returns True if local_path exists and matches expected size."""
    try:
        if not os.path.exists(local_path):
            return False
        local_size = os.path.getsize(local_path)
        if local_size <= 0:
            return False
        return expected_size is None or local_size == expected_size
    except OSError:
        return False


def is_exact_camera_path(filepath):
    """Accept a drive-qualified DCIM path without rewriting or guessing it."""
    if (not isinstance(filepath, str)
            or not re.match(r"^[A-Za-z]:[\\/]", filepath)
            or any(ord(char) < 32 or ord(char) == 127 for char in filepath)):
        return False
    parts = re.split(r"[\\/]", filepath[3:])
    return (
        len(parts) >= 3
        and parts[0].upper() == "DCIM"
        and parts[1].upper() in {"MOVIE", "PHOTO"}
        and all(part and part not in {".", ".."} for part in parts)
    )


def resolve_html_deletion_metadata(base_url, recordings):
    """Use one XML listing to supply unambiguous exact paths and sizes."""
    try:
        xml_recordings = get_dashcam_filenames(base_url)
    except Exception as e:
        logger.warning(
            f"XML deletion metadata unavailable; preserving camera files: {e}"
        )
        return recordings

    def key(recording):
        # Only a lookup key: the delete request always uses unchanged XML FPATH.
        path = recording.filepath or ""
        relative = re.sub(r"^[A-Za-z]:", "", path).replace("\\", "/")
        return recording.filename, relative.lstrip("/")

    by_path = {}
    for recording in xml_recordings:
        by_path.setdefault(key(recording), []).append(recording)

    resolved = []
    for recording in recordings:
        candidates = by_path.get(key(recording), [])
        if len(candidates) == 1:
            candidate = candidates[0]
            if (is_exact_camera_path(candidate.filepath)
                    and re.split(r"[\\/]", candidate.filepath)[-1]
                    == candidate.filename
                    and candidate.timecode is not None
                    and isinstance(candidate.size, int) and candidate.size > 0):
                resolved.append(candidate)
                continue
        resolved.append(recording)
    return resolved


def delete_from_camera(address, filepath, timeout):
    """Delete only the exact XML FPATH; never try alternate paths or encodings.

    Returns True for HTTP 200 with Status 0. Connection/parse errors propagate
    to the existing per-recording warning handler, without a destructive retry.
    """
    if not is_exact_camera_path(filepath):
        return False
    encoded = urllib.parse.quote(filepath, safe="")
    url = f"http://{address}/?custom=1&cmd=4003&str={encoded}"
    with camera_control_request(url, timeout) as resp:
        if resp.getcode() != 200:
            return False
        root = ET.fromstring(resp.read().decode("utf-8"))
    status = root.find(".//Status")
    return status is not None and (status.text or "").strip() == "0"


def get_downloaded_recordings(destination, grouping):
    """Reads destination dir and returns set of (filename, date)."""
    group_name_glob = group_name_globs[grouping]
    filepath_glob = get_filepath(
        destination, group_name_glob, downloaded_filename_glob
    )
    downloaded_filepaths = glob.glob(filepath_glob)

    recordings = set()
    for filepath in downloaded_filepaths:
        filename = os.path.basename(filepath)
        m = downloaded_filename_re.match(filename)
        if m:
            recording_date = datetime.date(
                int(m.group("year")),
                int(m.group("month")),
                int(m.group("day")),
            )
            recordings.add((filename, recording_date))
    return recordings


def get_outdated_recordings(destination, grouping):
    """Returns filenames of recordings prior to the cutoff date."""
    if cutoff_date is None:
        return []

    downloaded = get_downloaded_recordings(destination, grouping)
    return [
        filename
        for filename, rec_date in downloaded
        if rec_date < cutoff_date
    ]


def cleanup_empty_dirs(destination, grouping):
    """Removes empty group directories under destination."""
    group_glob = group_name_globs[grouping]
    if not group_glob:
        return

    pattern = os.path.join(destination, group_glob)
    for dirpath in glob.glob(pattern):
        if os.path.isdir(dirpath) and not os.listdir(dirpath):
            if dry_run:
                logger.info(
                    f"[DRY RUN] Would remove empty dir: "
                    f"{dirpath}"
                )
            else:
                try:
                    os.rmdir(dirpath)
                    logger.info(
                        f"Removed empty directory: {dirpath}"
                    )
                except OSError as e:
                    logger.debug(
                        f"Could not remove {dirpath}: {e}"
                    )


def prepare_destination(destination, grouping):
    """Prepares destination: removes outdated recordings and
    their .gpx sidecars, then cleans up empty directories."""
    if not cutoff_date:
        return

    outdated = get_outdated_recordings(destination, grouping)

    for outdated_recording in outdated:
        if dry_run:
            logger.info(
                f"[DRY RUN] Would remove outdated: "
                f"{outdated_recording}"
            )
            continue

        logger.info(f"Removing outdated: {outdated_recording}")

        # Glob for the recording and any sidecars (.gpx etc)
        base = os.path.splitext(outdated_recording)[0]
        sidecar_glob = f"{base}.*"
        filepath_glob = get_filepath(
            destination, group_name_globs[grouping],
            sidecar_glob,
        )

        for filepath in glob.glob(filepath_glob):
            try:
                os.remove(filepath)
                logger.info(f"Removed: {filepath}")
            except OSError as e:
                logger.error(
                    f"Error removing {filepath}: {e}"
                )

    cleanup_empty_dirs(destination, grouping)

    separate_root = os.path.join(destination, STACK_SEPARATE_DIRNAME)
    if os.path.isdir(separate_root):
        prepare_destination(separate_root, grouping)


def destination_is_too_full(destination):
    """Returns True when destination disk usage exceeds the limit."""
    usage = shutil.disk_usage(destination)
    used_percent = int((usage.used / usage.total) * 100)
    if used_percent >= max_disk_used_percent:
        logger.warning(
            f"Disk usage is {used_percent}% at {destination}; "
            f"limit is {max_disk_used_percent}%"
        )
        return True
    return False


def path_is_within(path, root):
    """Returns True when path is inside root."""
    if not root:
        return False
    try:
        real_path = os.path.realpath(path)
        real_root = os.path.realpath(root)
        return os.path.commonpath([real_path, real_root]) == real_root
    except ValueError:
        return False


def local_file_is_locked(filepath):
    """Best-effort check for files copied from the RO folder."""
    parts = filepath.replace("\\", "/").upper().split("/")
    return "RO" in parts


def to_local_recording(filepath):
    """Builds LocalRecording metadata for a local Viofo file."""
    filename = os.path.basename(filepath)
    metadata = parse_recording_filename(filename)
    if metadata is None:
        return None
    try:
        size = os.path.getsize(filepath)
    except OSError as e:
        logger.warning(f"Cannot stat {filepath}: {e}")
        return None
    return LocalRecording(
        filepath, filename, size, metadata["datetime"],
        metadata["sequence"], metadata["camera"],
        metadata["mode"], metadata["tag"], None,
    )


def iter_local_recordings(source, excluded_roots=None):
    """Yields Viofo recordings found under a local source path."""
    excluded_roots = excluded_roots or []

    if os.path.isfile(source):
        recording = to_local_recording(source)
        if recording and (not read_only
                          or local_file_is_locked(source)):
            yield recording
        return

    if not os.path.isdir(source):
        raise RuntimeError(f"Import source is not a directory: {source}")

    for dirpath, dirnames, filenames in os.walk(source):
        dirnames[:] = [
            dirname for dirname in dirnames
            if not any(
                path_is_within(os.path.join(dirpath, dirname), root)
                for root in excluded_roots
            )
        ]

        for filename in filenames:
            filepath = os.path.join(dirpath, filename)
            if any(path_is_within(filepath, root)
                   for root in excluded_roots):
                continue
            if read_only and not local_file_is_locked(filepath):
                continue
            recording = to_local_recording(filepath)
            if recording:
                yield recording


def copy_or_move_local_recording(recording, destination,
                                 grouping, move_imported):
    """Copies or moves one local recording into the destination tree."""
    group_name = get_group_name(recording.datetime, grouping)
    if group_name:
        ensure_destination(os.path.join(destination, group_name))

    dest_path = get_filepath(
        destination, group_name, recording.filename
    )

    if os.path.realpath(recording.filepath) == os.path.realpath(
        dest_path
    ):
        logger.debug(f"Already organized: {recording.filename}")
        return False, dest_path

    if os.path.exists(dest_path):
        try:
            dest_size = os.path.getsize(dest_path)
        except OSError as e:
            logger.warning(f"Cannot stat {dest_path}: {e}")
            return False, None

        if dest_size == recording.size:
            logger.info(f"Already imported: {recording.filename}")
            if move_imported and not dry_run:
                try:
                    os.remove(recording.filepath)
                    logger.info(
                        f"Removed duplicate source: "
                        f"{recording.filepath}"
                    )
                except OSError as e:
                    logger.warning(
                        f"Could not remove duplicate source "
                        f"{recording.filepath}: {e}"
                    )
            return False, dest_path

        logger.warning(
            f"Destination exists with a different size, skipping: "
            f"{dest_path}"
        )
        return False, None

    if dry_run:
        action = "move" if move_imported else "copy"
        logger.info(
            f"[DRY RUN] Would {action} {recording.filepath} "
            f"to {dest_path}"
        )
        return True, dest_path

    try:
        if move_imported:
            shutil.move(recording.filepath, dest_path)
            action = "Moved"
        else:
            shutil.copy2(recording.filepath, dest_path)
            action = "Copied"
    except OSError as e:
        logger.error(f"Failed to import {recording.filepath}: {e}")
        return False, None

    if not verify_local_file(dest_path, recording.size):
        logger.error(
            f"Imported file failed verification: {dest_path}"
        )
        return False, None

    logger.info(f"{action} {recording.filename} to {dest_path}")
    return True, dest_path


def organize_local_recordings(source, destination, grouping,
                              move_imported, gps_extract):
    """Imports recordings from a mounted drive into the destination."""
    logger.info(f"Starting local import from {source}")
    ensure_destination(destination)
    prepare_destination(destination, grouping)

    recordings = sorted(
        iter_local_recordings(source),
        key=lambda r: r.datetime,
    )
    logger.info(f"Found {len(recordings)} local recordings")

    stacked_names = load_stacked_names(destination)
    imported = 0
    for recording in recordings:
        if cutoff_date and recording.datetime.date() < cutoff_date:
            continue
        if recording.filename in stacked_names:
            logger.info(f"Already imported and stacked: "
                        f"{recording.filename}")
            if move_imported and not dry_run:
                try:
                    os.remove(recording.filepath)
                except OSError as e:
                    logger.warning(
                        f"Could not remove {recording.filepath}: {e}"
                    )
            continue
        if destination_is_too_full(destination):
            logger.warning("Stopping local import due to disk usage")
            break
        changed, dest_path = copy_or_move_local_recording(
            recording, destination, grouping, move_imported
        )
        if changed:
            imported += 1
        if (changed and gps_extract and dest_path and not dry_run
                and is_video_file(recording.filename)):
            extract_gps_data(dest_path)

    logger.info(f"Local import complete: {imported} files imported")
    return True


def local_import_has_recordings(source):
    """Returns whether a local source currently has Viofo recordings."""
    try:
        next(iter_local_recordings(source))
    except StopIteration:
        return False
    except RuntimeError as e:
        logger.warning(f"Cannot scan import source {source}: {e}")
        return False
    return True


def get_video_duration(filepath):
    """Returns video duration in seconds using ffprobe."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                filepath,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "ffprobe is required for --merge-chunks"
        )

    if result.returncode != 0:
        logger.warning(
            f"Could not read duration for {filepath}: "
            f"{result.stderr.strip()}"
        )
        return None

    try:
        return float(result.stdout.strip())
    except ValueError:
        logger.warning(f"Could not parse duration for {filepath}")
        return None


def with_durations(recordings):
    """Adds ffprobe durations to LocalRecording entries."""
    hydrated = []
    for recording in recordings:
        duration = get_video_duration(recording.filepath)
        hydrated.append(recording._replace(duration=duration))
    return hydrated


def should_merge_after(previous, current, merge_gap):
    """Returns True when two chunks appear to be one session."""
    if (previous.camera != current.camera
            or previous.mode != current.mode):
        return False

    if previous.mode == "parking":
        return False

    if current.sequence <= previous.sequence:
        return False

    elapsed = (current.datetime - previous.datetime).total_seconds()
    if elapsed <= 0:
        return False

    if previous.duration is not None:
        previous_end = previous.datetime + datetime.timedelta(
            seconds=previous.duration
        )
        gap = (current.datetime - previous_end).total_seconds()
        return -1.0 <= gap <= merge_gap

    return any(
        abs(elapsed - segment_seconds) <= merge_gap
        for segment_seconds in FALLBACK_SEGMENT_SECONDS
    )


def merge_stream_key(recording, grouping):
    """Returns the grouping boundary and stream for merge decisions."""
    return (
        get_group_name(recording.datetime, grouping),
        recording.mode,
        recording.camera,
    )


def sort_stream_key(stream_key):
    """Sort helper that handles ungrouped recordings."""
    return tuple("" if value is None else value for value in stream_key)


def build_merge_groups(recordings, merge_gap, grouping):
    """Groups adjacent recordings by camera and time continuity."""
    groups = []

    for stream in sorted(
        {merge_stream_key(r, grouping) for r in recordings},
        key=sort_stream_key,
    ):
        camera_recordings = sorted(
            [r for r in recordings
             if merge_stream_key(r, grouping) == stream],
            key=lambda r: r.datetime,
        )
        current_group = []

        for recording in camera_recordings:
            if not current_group:
                current_group = [recording]
                continue

            if should_merge_after(
                current_group[-1], recording, merge_gap,
            ):
                current_group.append(recording)
            else:
                if len(current_group) > 1:
                    groups.append(current_group)
                current_group = [recording]

        if len(current_group) > 1:
            groups.append(current_group)

    return groups


def concat_file_line(filepath):
    """Formats one ffmpeg concat demuxer file line."""
    escaped = os.path.abspath(filepath).replace("'", "'\\''")
    return f"file '{escaped}'\n"


def merged_output_filename(group):
    """Builds a filename for a merged recording group."""
    start = group[0].datetime.strftime("%Y_%m%d_%H%M%S")
    end = group[-1].datetime.strftime("%Y%m%d_%H%M%S")
    tag = re.sub(r"[^A-Z0-9_-]", "", group[0].tag) or "CAM"
    return f"{start}_{tag}_to_{end}.MP4"


def remove_recording_and_sidecars(filepath):
    """Removes a source MP4 and sidecars with the same base name."""
    base = os.path.splitext(filepath)[0]
    for sidecar_path in glob.glob(f"{base}.*"):
        try:
            os.remove(sidecar_path)
            logger.info(f"Removed source: {sidecar_path}")
        except OSError as e:
            logger.warning(f"Could not remove {sidecar_path}: {e}")


def merge_recording_group(group, merged_destination,
                          grouping, delete_sources):
    """Merges one chunk group with ffmpeg concat copy."""
    group_name = get_group_name(group[0].datetime, grouping)
    output_dir = (
        os.path.join(merged_destination, group_name)
        if group_name else merged_destination
    )
    ensure_destination(output_dir)

    output_path = os.path.join(
        output_dir, merged_output_filename(group)
    )
    if os.path.exists(output_path):
        logger.info(f"Merged file already exists: {output_path}")
        return False

    if destination_is_too_full(output_dir):
        logger.warning("Skipping merge due to disk usage")
        return False

    if dry_run:
        logger.info(
            f"[DRY RUN] Would merge {len(group)} chunks into "
            f"{output_path}"
        )
        return True

    list_fd, list_path = tempfile.mkstemp(
        dir=output_dir, prefix=".concat-", suffix=".txt"
    )
    tmp_fd, tmp_output = tempfile.mkstemp(
        dir=output_dir,
        prefix=f".{os.path.basename(output_path)}.",
        suffix=".MP4",
    )
    os.close(list_fd)
    os.close(tmp_fd)

    try:
        with open(list_path, "w") as list_file:
            for recording in group:
                list_file.write(concat_file_line(recording.filepath))

        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-y", "-f", "concat", "-safe", "0",
                "-i", list_path, "-c", "copy", tmp_output,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            logger.error(
                f"Failed to merge into {output_path}: "
                f"{result.stderr.strip()}"
            )
            return False

        if not verify_local_file(tmp_output):
            logger.error(f"Merged file is empty: {tmp_output}")
            return False

        os.replace(tmp_output, output_path)
        logger.info(
            f"Merged {len(group)} chunks into {output_path}"
        )

        if delete_sources:
            for recording in group:
                remove_recording_and_sidecars(recording.filepath)

        return True
    finally:
        for path in (list_path, tmp_output):
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass


def merge_chunks(source, grouping, merged_destination,
                 merge_gap, delete_sources):
    """Finds and merges sequential local chunks under source."""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is required for --merge-chunks")
    if shutil.which("ffprobe") is None:
        raise RuntimeError("ffprobe is required for --merge-chunks")

    merged_destination = (
        merged_destination or os.path.join(source, "merged")
    )
    logger.info(
        f"Scanning {source} for chunks to merge into "
        f"{merged_destination}"
    )

    all_recordings = sorted(
        iter_local_recordings(
            source, excluded_roots=[
                merged_destination,
                os.path.join(source, STACK_SEPARATE_DIRNAME),
            ]
        ),
        key=lambda r: (r.mode, r.camera, r.datetime),
    )
    skipped_parking = sum(
        1 for recording in all_recordings
        if recording.mode == "parking"
    )
    if skipped_parking:
        logger.info(
            f"Skipping {skipped_parking} parking recordings "
            f"during merge"
        )
    recordings = [
        recording for recording in all_recordings
        if recording.mode != "parking"
        and is_video_file(recording.filename)
    ]
    recordings = with_durations(recordings)
    groups = build_merge_groups(recordings, merge_gap, grouping)
    logger.info(f"Found {len(groups)} merge groups")

    merged = 0
    for group in groups:
        if merge_recording_group(
            group, merged_destination, grouping, delete_sources
        ):
            merged += 1

    logger.info(f"Chunk merge complete: {merged} groups processed")
    return True


# --- Camera stacking (F / I / R -> one 3-up video) ---

STACK_CAMERAS = ("F", "I", "R")
STACK_LEDGER_NAME = ".viofosync-stacked"
STACK_SEPARATE_DIRNAME = "_separate"
DEFAULT_STACK_MIN_AGE_SECONDS = 300
DEFAULT_STACK_BITRATE = "45M"
STACK_VAAPI_QP = int(os.environ.get("STACK_QP", "26"))
VAAPI_DEVICE = os.environ.get("VAAPI_DEVICE", "/dev/dri/renderD128")

# Front on top at native size, interior bottom-left and rear
# bottom-right scaled to half the front width. Matches the layout
# the camera writes itself when it records in stacked mode.
STACK_FILTER_CPU = (
    "[0:v]scale=w=3840:h=2160,setsar=1[f];"
    "[1:v]scale=w=1920:h=1080,setsar=1[i];"
    "[2:v]scale=w=1920:h=1080,setsar=1[r];"
    "[i][r]hstack=inputs=2[b];"
    "[f][b]vstack=inputs=2,format=nv12[v]"
)
STACK_FILTER_VAAPI = (
    "[0:v]scale_vaapi=w=3840:h=2160:format=nv12[f];"
    "[1:v]scale_vaapi=w=1920:h=1080:format=nv12[i];"
    "[2:v]scale_vaapi=w=1920:h=1080:format=nv12[r];"
    "[i][r]hstack_vaapi=inputs=2[b];"
    "[f][b]vstack_vaapi=inputs=2[v]"
)


def stack_ledger_path(destination):
    """Path of the file listing originals already stacked."""
    return os.path.join(destination, STACK_LEDGER_NAME)


def load_stacked_names(destination):
    """Returns the set of original filenames that were stacked.

    Sync and import consult this so stacked-away I/R/F originals
    still on the camera are not downloaded again.
    """
    try:
        with open(stack_ledger_path(destination)) as fh:
            return {line.strip() for line in fh if line.strip()}
    except OSError:
        return set()


def record_stacked_names(destination, filenames):
    """Appends original filenames to the stacked ledger."""
    with open(stack_ledger_path(destination), "a") as fh:
        for filename in filenames:
            fh.write(f"{filename}\n")


# How far apart the F / I / R files of one moment may be. Viofo
# normally writes them with the same timestamp and consecutive
# sequence numbers, but not always (see build_stack_groups).
STACK_MAX_SEQUENCE_GAP = 3
STACK_MAX_TIME_GAP_SECONDS = 10


def build_stack_groups(recordings):
    """Groups F/I/R recordings that belong to the same moment.

    Viofo usually writes the three cameras with the same timestamp and
    consecutive sequence numbers, e.g. 002556F, 002557I, 002558R. It
    does not always: parking clips can come out as I, F, R (000788PI,
    000790PF, 000791PR) and snapshots can be stamped a few seconds
    apart (114920PF, 114923PI). So each front file is matched to the
    closest unused interior and rear file in the same folder, mode and
    file type whose timestamp is within STACK_MAX_TIME_GAP_SECONDS and
    whose sequence number is within STACK_MAX_SEQUENCE_GAP.
    Returns a list of dicts camera -> LocalRecording.
    """
    buckets = {}
    for recording in recordings:
        if recording.camera not in STACK_CAMERAS:
            continue
        ext = os.path.splitext(recording.filename)[1].lower()
        key = (os.path.dirname(recording.filepath), recording.mode, ext)
        buckets.setdefault(key, []).append(recording)

    groups = []
    for key in sorted(buckets):
        items = buckets[key]
        fronts = sorted((r for r in items if r.camera == "F"),
                        key=lambda r: (r.datetime, r.sequence))
        pools = {cam: [r for r in items if r.camera == cam]
                 for cam in ("I", "R")}
        used = set()
        for front in fronts:
            match = {}
            for cam in ("I", "R"):
                best = None
                for candidate in pools[cam]:
                    if candidate.filepath in used:
                        continue
                    seq_gap = abs(candidate.sequence - front.sequence)
                    time_gap = abs(
                        (candidate.datetime - front.datetime)
                        .total_seconds()
                    )
                    if (seq_gap == 0
                            or seq_gap > STACK_MAX_SEQUENCE_GAP
                            or time_gap > STACK_MAX_TIME_GAP_SECONDS):
                        continue
                    score = (time_gap, seq_gap)
                    if best is None or score < best[0]:
                        best = (score, candidate)
                if best is None:
                    break
                match[cam] = best[1]
            if len(match) == 2:
                used.add(match["I"].filepath)
                used.add(match["R"].filepath)
                groups.append({"F": front, "I": match["I"],
                               "R": match["R"]})
    return groups


def probe_video(filepath):
    """Returns (width, height, duration) of the first video stream."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration",
            "-of", "default=noprint_wrappers=1", filepath,
        ],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return None
    values = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip()
    try:
        return (int(values["width"]), int(values["height"]),
                float(values.get("duration") or 0))
    except (KeyError, ValueError):
        return None


def vaapi_available():
    """True when a VAAPI render node is visible in the container."""
    return os.path.exists(VAAPI_DEVICE) and os.access(
        VAAPI_DEVICE, os.R_OK | os.W_OK
    )


_ffmpeg_encoders = None


def ffmpeg_has_encoder(name):
    """True when the local ffmpeg build lists the given encoder."""
    global _ffmpeg_encoders
    if _ffmpeg_encoders is None:
        try:
            out = subprocess.run(
                ["ffmpeg", "-hide_banner", "-encoders"],
                capture_output=True, text=True, check=False,
            ).stdout
        except OSError:
            out = ""
        _ffmpeg_encoders = set(re.findall(r"^ \S+ (\S+)", out, re.M))
    return name in _ffmpeg_encoders


# Other hardware encoders, used on a desktop/laptop worker. Each takes
# the CPU-stacked nv12 frames. Tried in this order with auto.
HW_ENCODERS = [
    ("nvenc", "h264_nvenc",
     lambda b: ["-preset", "p5", "-rc", "vbr", "-b:v", b,
                "-maxrate", b]),
    ("qsv", "h264_qsv",
     lambda b: ["-low_power", "1", "-b:v", b, "-maxrate", b]),
    ("amf", "h264_amf",
     lambda b: ["-rc", "vbr_peak", "-b:v", b, "-maxrate", b]),
    ("videotoolbox", "h264_videotoolbox",
     lambda b: ["-b:v", b]),
]


def stack_commands(inputs, output, is_photo, encoder, bitrate):
    """Yields (label, argv) ffmpeg attempts in order of preference."""
    base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]

    if is_photo:
        argv = list(base)
        for path in inputs:
            argv += ["-i", path]
        argv += [
            "-filter_complex",
            STACK_FILTER_CPU.replace(",format=nv12", ""),
            "-map", "[v]", "-frames:v", "1", "-q:v", "2", output,
        ]
        yield "jpeg", argv
        return

    rate = ["-b:v", bitrate, "-maxrate", bitrate,
            "-bufsize", bitrate]
    # Intel's low-power H.264 encoder ignores bitrate targets at
    # 3840x3240 (VBR/CBR both come out ~330 Mbps), so use constant
    # QP. QP 26 gives ~50 Mbps, close to the camera's own stacked files.
    vaapi_rate = ["-rc_mode", "CQP", "-qp", str(STACK_VAAPI_QP)]
    audio_meta = ["-map", "0:a?", "-c:a", "copy",
                  "-map_metadata", "0", "-movflags", "+faststart",
                  "-f", "mp4", output]

    use_vaapi = encoder in ("auto", "vaapi") and vaapi_available()
    if encoder == "vaapi" and not use_vaapi:
        logger.warning(
            f"STACK_ENCODER=vaapi but {VAAPI_DEVICE} is not "
            f"available; is /dev/dri passed to the container?"
        )

    if use_vaapi:
        # 1. Everything on the iGPU: decode, scale, stack, encode.
        argv = list(base) + ["-vaapi_device", VAAPI_DEVICE]
        for path in inputs:
            argv += ["-hwaccel", "vaapi",
                     "-hwaccel_output_format", "vaapi", "-i", path]
        argv += ["-filter_complex", STACK_FILTER_VAAPI,
                 "-map", "[v]", "-c:v", "h264_vaapi",
                 "-low_power", "1"] + vaapi_rate
        yield "vaapi", argv + audio_meta

        # 2. CPU decode/stack, iGPU encode (older ffmpeg / drivers).
        argv = list(base) + ["-vaapi_device", VAAPI_DEVICE]
        for path in inputs:
            argv += ["-i", path]
        argv += ["-filter_complex",
                 STACK_FILTER_CPU.replace("[v]", ",hwupload[v]"),
                 "-map", "[v]", "-c:v", "h264_vaapi",
                 "-low_power", "1"] + vaapi_rate
        yield "vaapi-upload", argv + audio_meta

    if not use_vaapi:
        for label, codec, opts in HW_ENCODERS:
            if encoder not in ("auto", label):
                continue
            if encoder == "auto" and not ffmpeg_has_encoder(codec):
                continue
            argv = list(base)
            for path in inputs:
                argv += ["-i", path]
            argv += ["-filter_complex", STACK_FILTER_CPU,
                     "-map", "[v]", "-c:v", codec] + opts(bitrate)
            yield label, argv + audio_meta

    if encoder in ("auto", "x264"):
        argv = list(base)
        for path in inputs:
            argv += ["-i", path]
        argv += ["-filter_complex", STACK_FILTER_CPU,
                 "-map", "[v]", "-c:v", "libx264",
                 "-preset", "veryfast", "-pix_fmt", "yuv420p"] + rate
        yield "x264", argv + audio_meta


STACK_LOCK_STALE_SECONDS = 6 * 3600


def acquire_stack_lock(front_path):
    """Claims a group so two workers never stack the same clip."""
    lock_path = os.path.join(
        os.path.dirname(front_path),
        f".{os.path.basename(front_path)}.stacklock",
    )
    try:
        if time.time() - os.path.getmtime(lock_path) > \
                STACK_LOCK_STALE_SECONDS:
            os.remove(lock_path)
    except OSError:
        pass
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None
    except OSError as e:
        logger.warning(f"Cannot create lock {lock_path}: {e}")
        return None
    with os.fdopen(fd, "w") as fh:
        fh.write(f"{socket.gethostname()} {os.getpid()}\n")
    return lock_path


def stack_recording_group(group, destination, grouping, encoder,
                          bitrate, originals, min_age, workdir=None):
    """Builds one stacked file from an F/I/R group.

    The stacked file takes the front file's name so it sits where
    the camera's own stacked recordings would.
    """
    front = group["F"]
    paths = [group[cam].filepath for cam in STACK_CAMERAS]
    names = [group[cam].filename for cam in STACK_CAMERAS]
    is_photo = not is_video_file(front.filename)

    now = time.time()
    for path in paths:
        try:
            if now - os.path.getmtime(path) < min_age:
                logger.debug(f"Skipping recent file, may be in use: "
                             f"{path}")
                return False
        except OSError:
            return False

    if not is_photo:
        front_info = probe_video(front.filepath)
        if front_info is None:
            logger.warning(f"Cannot read {front.filepath}; skipping")
            return False
        if front_info[1] > 2160:
            logger.debug(f"Already stacked: {front.filename}")
            return False

    output_dir = os.path.dirname(front.filepath)
    output_path = front.filepath

    if destination_is_too_full(output_dir):
        logger.warning("Skipping stacking due to disk usage")
        return False

    if dry_run:
        logger.info(
            f"[DRY RUN] Would stack {', '.join(names)} into "
            f"{output_path}"
        )
        return True

    lock_path = acquire_stack_lock(front.filepath)
    if lock_path is None:
        logger.debug(f"Another worker has {front.filename}; skipping")
        return False
    try:
        return _stack_locked(group, destination, grouping, encoder,
                             bitrate, originals, workdir, paths, names,
                             is_photo, output_dir, output_path)
    finally:
        try:
            os.remove(lock_path)
        except OSError:
            pass


def _stack_locked(group, destination, grouping, encoder, bitrate,
                  originals, workdir, paths, names, is_photo,
                  output_dir, output_path):
    """Stacks one group while holding its lock."""
    front = group["F"]
    # Another worker may have finished it just before we got the lock.
    if not all(os.path.exists(p) for p in paths):
        return False
    if names[0] in load_stacked_names(destination):
        return False

    tmp_fd, tmp_output = tempfile.mkstemp(
        dir=output_dir, prefix=f".{front.filename}.",
        suffix=os.path.splitext(front.filename)[1],
    )
    os.close(tmp_fd)

    try:
        ok = False
        started = time.time()
        enc_inputs, enc_output, local_dir = paths, tmp_output, None
        if workdir:
            # Copy the three files locally first: one sequential read
            # each is far faster over SMB than three interleaved reads.
            os.makedirs(workdir, exist_ok=True)
            local_dir = tempfile.mkdtemp(dir=workdir, prefix="stack-")
            enc_inputs = []
            for path in paths:
                local = os.path.join(local_dir, os.path.basename(path))
                shutil.copyfile(path, local)
                enc_inputs.append(local)
            enc_output = os.path.join(
                local_dir, "out" + os.path.splitext(front.filename)[1])
        for label, argv in stack_commands(
            enc_inputs, enc_output, is_photo, encoder, bitrate
        ):
            logger.info(f"Stacking {front.filename} ({label})")
            result = subprocess.run(
                argv, capture_output=True, text=True, check=False
            )
            if result.returncode == 0 and verify_local_file(
                enc_output
            ):
                ok = True
                break
            logger.warning(
                f"{label} stacking failed for {front.filename}: "
                f"{result.stderr.strip()[-600:]}"
            )
        if not ok:
            logger.error(f"Could not stack {front.filename}")
            return False
        if local_dir:
            shutil.copyfile(enc_output, tmp_output)

        if not is_photo:
            out_info = probe_video(tmp_output)
            front_info = probe_video(front.filepath)
            if (out_info is None or out_info[:2] != (3840, 3240)
                    or abs(out_info[2] - front_info[2]) > 2.0):
                logger.error(
                    f"Stacked output failed verification for "
                    f"{front.filename}: {out_info}"
                )
                return False

        # Move or delete the separate originals, then put the
        # stacked file in the front file's place.
        if originals == "keep":
            group_name = get_group_name(front.datetime, grouping)
            keep_dir = os.path.join(
                destination, STACK_SEPARATE_DIRNAME, group_name or ""
            )
            ensure_destination(keep_dir)
            for path in paths:
                shutil.move(path, os.path.join(
                    keep_dir, os.path.basename(path)))
        else:
            for path in paths[1:]:
                remove_recording_and_sidecars(path)

        os.replace(tmp_output, output_path)
        record_stacked_names(destination, names)
        elapsed = time.time() - started
        logger.info(
            f"Stacked {front.filename} in {elapsed:.0f}s "
            f"({human_size(os.path.getsize(output_path))})"
        )
        return True
    finally:
        if os.path.exists(tmp_output):
            try:
                os.remove(tmp_output)
            except OSError:
                pass
        if workdir and "local_dir" in locals() and local_dir:
            shutil.rmtree(local_dir, ignore_errors=True)


def stack_cameras(destination, grouping, encoder, bitrate,
                  originals, limit, min_age, include_photos,
                  order="newest", workdir=None):
    """Finds separate F/I/R recordings and stacks them."""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is required for --stack-cameras")

    separate_root = os.path.join(destination, STACK_SEPARATE_DIRNAME)
    merged_root = os.path.join(destination, "merged")
    recordings = [
        r for r in iter_local_recordings(
            destination, excluded_roots=[separate_root, merged_root]
        )
        if include_photos or is_video_file(r.filename)
    ]
    groups = build_stack_groups(recordings)
    # Newest first by default so fresh footage is ready soonest; a
    # second worker can use oldest so the two meet in the middle.
    groups.sort(key=lambda g: g["F"].datetime,
                reverse=(order != "oldest"))
    logger.info(f"Found {len(groups)} F/I/R groups to stack")
    if encoder in ("auto", "vaapi"):
        logger.info(
            "Hardware encoder: "
            + ("VAAPI " + VAAPI_DEVICE if vaapi_available()
               else "not available, using CPU (slow)")
        )

    stacked = 0
    for group in groups:
        if limit and stacked >= limit:
            logger.info(f"Reached STACK_LIMIT of {limit}")
            break
        if stack_recording_group(
            group, destination, grouping, encoder, bitrate,
            originals, min_age, workdir,
        ):
            stacked += 1

    logger.info(f"Camera stacking complete: {stacked} groups stacked")
    return True


def _handle_camera_deletion(address, recording, dest_path):
    """Delete an eligible XML-verified recording after local verification."""
    if not delete_after_sync:
        return

    is_locked = (
        (recording.filepath and local_file_is_locked(recording.filepath))
        or recording.attr == 33
    )
    if is_locked and not delete_locked:
        logger.info(
            f"Skipping deletion of locked file: {recording.filename}"
        )
        return

    # Unresolved HTML metadata has a guessed drive and rounded size. Only an
    # exact, unambiguous XML FPATH and size may authorize deletion in either mode.
    if (recording.timecode is None
            or not is_exact_camera_path(recording.filepath)
            or re.split(r"[\\/]", recording.filepath)[-1] != recording.filename
            or not isinstance(recording.size, int) or recording.size <= 0):
        logger.warning(
            f"Skipping camera deletion for {recording.filename}: "
            "exact XML path and size unavailable"
        )
        return

    if dry_run:
        logger.info(
            f"[DRY RUN] Would delete from camera: {recording.filename}"
        )
        return

    if not verify_local_file(dest_path, recording.size):
        logger.warning(
            f"Failed to delete from camera: {recording.filename} "
            "(local file missing, empty or size changed)"
        )
        return

    try:
        if delete_from_camera(
            address, recording.filepath, socket_timeout
        ):
            logger.info(
                f"Deleted from camera: {recording.filename}"
            )
        else:
            logger.warning(
                f"Failed to delete from camera: {recording.filename} "
                "(camera returned error)"
            )
    except Exception as e:
        logger.warning(
            f"Failed to delete from camera: {recording.filename} ({e})"
        )


def sync(address, destination, grouping, download_priority,
         recording_filter, args):
    """Synchronizes dashcam recordings with destination dir."""
    logger.info(f"Starting sync for {address}")
    ensure_destination(destination)
    prepare_destination(destination, grouping)

    base_url = f"http://{address}"
    try:
        if args.html:
            dashcam_recordings = get_dashcam_filenames_html(
                base_url
            )
        else:
            dashcam_recordings = get_dashcam_filenames(base_url)
    except (RuntimeError, UserWarning) as e:
        logger.error(f"Sync aborted: {e}")
        return False

    dashcam_recordings.sort(
        key=lambda r: r.datetime,
        reverse=(download_priority == "rdate"),
    )

    if recording_filter:
        dashcam_recordings = [
            r for r in dashcam_recordings
            if any(f in r.filename for f in recording_filter)
        ]
        logger.info(
            f"Filtered to {len(dashcam_recordings)} recordings"
        )

    stacked_names = load_stacked_names(destination)
    if args.html and delete_after_sync:
        eligible = [
            r for r in dashcam_recordings
            if (not cutoff_date or r.datetime.date() >= cutoff_date)
            and r.filename not in stacked_names
            and (delete_locked or not local_file_is_locked(r.filepath or ""))
        ]
        if eligible:
            resolved = resolve_html_deletion_metadata(base_url, eligible)
            replacements = {id(old): new for old, new in zip(eligible, resolved)}
            dashcam_recordings = [
                replacements.get(id(r), r) for r in dashcam_recordings
            ]
    total = len(dashcam_recordings)
    for i, recording in enumerate(dashcam_recordings, start=1):
        if cutoff_date and recording.datetime.date() < cutoff_date:
            continue
        if recording.filename in stacked_names:
            logger.debug(
                f"Skipping {recording.filename}: already stacked"
            )
            continue
        if destination_is_too_full(destination):
            logger.warning("Stopping sync due to disk usage")
            break
        group_name = get_group_name(
            recording.datetime, grouping
        )
        logger.info(
            f"[{i}/{total}] Processing {recording.filename}"
        )
        downloaded, _, local_available, verified = download_file(
            base_url, recording, destination, group_name
        )
        dest_path = get_filepath(
            destination, group_name, recording.filename
        )
        if downloaded and local_available:
            if (args.gps_extract and not dry_run
                    and is_video_file(recording.filename)):
                extract_gps_data(dest_path)
        if delete_after_sync and (dry_run or (local_available and verified)):
            _handle_camera_deletion(
                address, recording, dest_path
            )
        elif delete_after_sync and local_available:
            logger.warning(
                f"Skipping camera deletion for "
                f"{recording.filename}: local file size could "
                f"not be verified"
            )

    logger.info("Sync complete")
    return True


def ensure_destination(destination):
    """Ensures the destination directory exists and is writable."""
    if not os.path.exists(destination):
        os.makedirs(destination)
    elif not os.path.isdir(destination):
        raise RuntimeError(
            f"Not a directory: {destination}"
        )
    elif not os.access(destination, os.W_OK):
        raise RuntimeError(
            f"Not writable: {destination}"
        )


def get_group_name(recording_datetime, grouping):
    """Determines the group name for a recording datetime."""
    if grouping == "daily":
        return recording_datetime.strftime("%Y-%m-%d")
    elif grouping == "weekly":
        delta = datetime.timedelta(
            days=recording_datetime.weekday()
        )
        return (recording_datetime - delta).strftime("%Y-%m-%d")
    elif grouping == "monthly":
        return recording_datetime.strftime("%Y-%m")
    elif grouping == "yearly":
        return recording_datetime.strftime("%Y")
    return None


# --- GPS Extraction Functions ---

def fix_time(hour, minute, second, year, month, day):
    return (
        f"{year + 2000:04d}-{month:02d}-{day:02d}"
        f"T{hour:02d}:{minute:02d}:{second:02d}Z"
    )


def fix_coordinates(hemisphere, coordinate):
    minutes = coordinate % 100.0
    degrees = coordinate - minutes
    coordinate = degrees / 100.0 + (minutes / 60.0)
    if hemisphere in ['S', 'W']:
        return -1 * float(coordinate)
    return float(coordinate)


def fix_speed(speed):
    return speed * 0.514444


def get_atom_info(eight_bytes):
    try:
        atom_size, atom_type = struct.unpack('>I4s', eight_bytes)
        return int(atom_size), atom_type.decode()
    except (struct.error, UnicodeDecodeError):
        return 0, ''


def get_gps_atom_info(eight_bytes):
    atom_pos, atom_size = struct.unpack('>II', eight_bytes)
    return int(atom_pos), int(atom_size)


def get_gps_offset(data):
    """Finds GPS payload position by scanning for A{N,S}{E,W}
    pattern. Supports newer VIOFO cameras (e.g. A329S) where
    GPS data sits at a variable offset within the payload."""
    pointer = len(data) - 20
    while pointer > 0:
        try:
            active, lon_hemi, lat_hemi = struct.unpack_from(
                '<sss', data, pointer
            )
            active = active.decode()
            lon_hemi = lon_hemi.decode()
            lat_hemi = lat_hemi.decode()
        except UnicodeDecodeError:
            pointer -= 1
            continue
        if (active == 'A'
                and lon_hemi in ('N', 'S')
                and lat_hemi in ('E', 'W')):
            return pointer - 24
        pointer -= 1
    return -1


def get_gps_data(data):
    gps = {
        'DT': {
            'Year': None, 'Month': None, 'Day': None,
            'Hour': None, 'Minute': None, 'Second': None,
            'DT': None,
        },
        'Loc': {
            'Lat': {'Raw': None, 'Hemi': None, 'Float': None},
            'Lon': {'Raw': None, 'Hemi': None, 'Float': None},
            'Speed': None, 'Bearing': None,
        },
    }

    offset = get_gps_offset(data)
    if offset < 0:
        return None

    try:
        hour, minute, second = struct.unpack_from(
            '<III', data, offset
        )
        offset += 12
        year, month, day = struct.unpack_from(
            '<III', data, offset
        )
        offset += 12
        _, lat_hemi, lon_hemi = struct.unpack_from(
            '<sss', data, offset
        )
        offset += 4
        lat_raw, lon_raw = struct.unpack_from(
            '<ff', data, offset
        )
        offset += 8
        speed, bearing = struct.unpack_from(
            '<ff', data, offset
        )

        gps['Loc']['Lat']['Hemi'] = lat_hemi.decode()
        gps['Loc']['Lon']['Hemi'] = lon_hemi.decode()
    except (struct.error, UnicodeDecodeError) as e:
        logger.debug(f"Skipping: bad GPS data. Error: {e}")
        return None

    gps['DT']['Hour'] = hour
    gps['DT']['Minute'] = minute
    gps['DT']['Second'] = second
    gps['DT']['Year'] = year
    gps['DT']['Month'] = month
    gps['DT']['Day'] = day
    gps['DT']['DT'] = fix_time(
        hour, minute, second, year, month, day
    )

    gps['Loc']['Lat']['Raw'] = lat_raw
    gps['Loc']['Lon']['Raw'] = lon_raw
    gps['Loc']['Lat']['Float'] = fix_coordinates(
        gps['Loc']['Lat']['Hemi'], lat_raw
    )
    gps['Loc']['Lon']['Float'] = fix_coordinates(
        gps['Loc']['Lon']['Hemi'], lon_raw
    )
    gps['Loc']['Speed'] = fix_speed(speed)
    gps['Loc']['Bearing'] = bearing

    return gps


def get_gps_atom(gps_atom_info, f):
    atom_pos, atom_size = gps_atom_info
    try:
        f.seek(atom_pos)
        data = f.read(atom_size)
    except OverflowError as e:
        logger.error(
            f"Skipping at {atom_pos:x}: "
            f"seek or read error: {e}"
        )
        return None

    if len(data) < 12:
        logger.debug(
            f"Skipping at {atom_pos:x}: "
            f"atom too small ({len(data)} bytes)"
        )
        return None

    expected_type, expected_magic = 'free', 'GPS '
    atom_size1, atom_type, magic = struct.unpack_from(
        '>I4s4s', data
    )
    try:
        atom_type = atom_type.decode()
        magic = magic.decode()
        if (atom_size != atom_size1
                or atom_type != expected_type
                or magic != expected_magic):
            logger.error(
                f"Skipping atom at {atom_pos:x} "
                f"(size:{atom_size1}/{atom_size}, "
                f"type:{atom_type}/{expected_type}, "
                f"magic:{magic}/{expected_magic})"
            )
            return None
    except UnicodeDecodeError as e:
        logger.error(
            f"Skipping at {atom_pos:x}: "
            f"garbage atom type or magic: {e}"
        )
        return None

    return get_gps_data(data[12:])


def parse_moov(in_fh):
    gps_data = []
    offset = 0
    while True:
        atom_size, atom_type = get_atom_info(in_fh.read(8))
        if atom_size == 0:
            break

        if atom_type == 'moov':
            sub_offset = offset + 8
            while sub_offset < (offset + atom_size):
                sub_atom_size, sub_atom_type = get_atom_info(
                    in_fh.read(8)
                )

                if sub_atom_type == 'gps ':
                    gps_offset = 16 + sub_offset
                    in_fh.seek(gps_offset, 0)
                    while gps_offset < (sub_offset
                                        + sub_atom_size):
                        data = get_gps_atom(
                            get_gps_atom_info(in_fh.read(8)),
                            in_fh,
                        )
                        if data:
                            gps_data.append(data)
                        gps_offset += 8
                        in_fh.seek(gps_offset, 0)

                sub_offset += sub_atom_size
                in_fh.seek(sub_offset, 0)

        offset += atom_size
        in_fh.seek(offset, 0)
    return gps_data


def generate_gpx(gps_data, out_file):
    gpx = '<?xml version="1.0" encoding="UTF-8"?>\n'
    gpx += '<gpx version="1.0"\n'
    gpx += '\tcreator="Viofo GPS Extractor"\n'
    gpx += '\txmlns:xsi='
    gpx += '"http://www.w3.org/2001/XMLSchema-instance"\n'
    gpx += '\txmlns="http://www.topografix.com/GPX/1/0"\n'
    gpx += (
        '\txsi:schemaLocation='
        '"http://www.topografix.com/GPX/1/0 '
        'http://www.topografix.com/GPX/1/0/gpx.xsd">\n'
    )
    gpx += f"\t<name>{out_file}</name>\n"
    gpx += f"\t<trk><name>{out_file}</name><trkseg>\n"
    for gps in gps_data:
        if gps:
            lat = gps['Loc']['Lat']['Float']
            lon = gps['Loc']['Lon']['Float']
            gpx += f'\t\t<trkpt lat="{lat}" lon="{lon}">'
            gpx += f"<time>{gps['DT']['DT']}</time>"
            gpx += f"<speed>{gps['Loc']['Speed']}</speed>"
            gpx += (
                f"<course>{gps['Loc']['Bearing']}</course>"
                f"</trkpt>\n"
            )
    gpx += '\t</trkseg></trk>\n'
    gpx += '</gpx>\n'
    return gpx


def extract_gps_data(file_path):
    logger.info(f"Extracting GPS data from {file_path}")

    with open(file_path, "rb") as in_fh:
        gps_data = parse_moov(in_fh)

    logger.info(f"Found {len(gps_data)} GPS data points")

    if gps_data:
        gpx_file = file_path + ".gpx"
        gpx_content = generate_gpx(
            gps_data, os.path.basename(gpx_file)
        )
        with open(gpx_file, "w") as f:
            logger.info(f"Writing GPS data to '{gpx_file}'")
            f.write(gpx_content)
    else:
        logger.warning("No GPS data found in the file")


# --- CLI ---

def parse_args():
    """Parses the command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Synchronizes Viofo dashcam recordings "
        "with a local directory, imports mounted media, "
        "and extracts GPS data.",
    )
    parser.add_argument(
        "address", nargs="?",
        help="Dashcam IP address or hostname",
    )
    parser.add_argument(
        "-d", "--destination", default=os.getcwd(),
        help="Destination directory for downloads",
    )
    parser.add_argument(
        "-g", "--grouping", default="none",
        choices=["none", "daily", "weekly", "monthly", "yearly"],
        help="Group recordings by time period",
    )
    parser.add_argument(
        "-k", "--keep",
        help="Keep recordings for period (e.g. '30d', '4w')",
    )
    parser.add_argument(
        "-p", "--priority", default="date",
        choices=["date", "rdate"],
        help="Download priority: oldest or newest first",
    )
    parser.add_argument(
        "-f", "--filter", nargs="+",
        help="Filter recordings by filename pattern",
    )
    parser.add_argument(
        "-u", "--max-used-disk", default=90,
        metavar="DISK%", type=int, choices=range(5, 99),
        help="Stop if disk usage exceeds this percent",
    )
    parser.add_argument(
        "-t", "--timeout", default=10.0,
        metavar="TIMEOUT", type=float,
        help="Connection timeout in seconds",
    )
    parser.add_argument(
        "--download-attempts",
        default=DEFAULT_DOWNLOAD_ATTEMPTS,
        metavar="ATTEMPTS",
        type=positive_int,
        help="How many times to retry a failed download",
    )
    parser.add_argument(
        "-v", "--verbose", action="count", default=0,
        help="Increase output verbosity",
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true",
        help="Only log errors; overrides verbosity",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would happen without doing it",
    )
    parser.add_argument(
        "--read-only", action="store_true",
        help="Only manage read-only (locked) recordings",
    )
    parser.add_argument(
        "--cron", action="store_true",
        help="Cron mode: reduced logging verbosity",
    )
    parser.add_argument(
        "--gps-extract", action="store_true",
        help="Extract GPS data and create GPX files",
    )
    parser.add_argument(
        "--delete-after-sync", action="store_true",
        help="Delete verified files from camera "
        "(locked/RO files require --delete-locked)",
    )
    parser.add_argument(
        "--delete-locked", action="store_true",
        help="Also delete verified locked/RO files; requires "
        "--delete-after-sync",
    )
    parser.add_argument(
        "--import-source",
        help="Import and organize recordings from a local mounted "
        "drive or directory instead of syncing over Wi-Fi",
    )
    parser.add_argument(
        "--move-imported", action="store_true",
        help="Move local import files into the destination instead "
        "of copying them",
    )
    parser.add_argument(
        "--merge-chunks", action="store_true",
        help="Merge sequential chunks by camera using ffmpeg",
    )
    parser.add_argument(
        "--merge-gap", default=DEFAULT_MERGE_GAP_SECONDS,
        metavar="SECONDS", type=non_negative_float,
        help="Maximum gap between chunks to merge",
    )
    parser.add_argument(
        "--merged-destination",
        help="Directory for merged files; defaults to "
        "<destination>/merged",
    )
    parser.add_argument(
        "--delete-merged-sources", action="store_true",
        help="Delete source chunks and sidecars after a successful "
        "merge",
    )
    parser.add_argument(
        "--stack-cameras", action="store_true",
        help="Combine separate front/interior/rear recordings into "
        "one stacked video (front on top, interior bottom-left, "
        "rear bottom-right)",
    )
    parser.add_argument(
        "--stack-encoder", default="auto",
        choices=["auto", "vaapi", "nvenc", "qsv", "amf",
                 "videotoolbox", "x264"],
        help="auto uses VAAPI when /dev/dri is available, otherwise "
        "any NVIDIA/Intel/AMD/Apple hardware encoder ffmpeg has, "
        "then CPU x264",
    )
    parser.add_argument(
        "--stack-bitrate", default=DEFAULT_STACK_BITRATE,
        help="Video bitrate for stacked files, e.g. 45M",
    )
    parser.add_argument(
        "--stack-originals", default="keep",
        choices=["keep", "delete"],
        help="keep moves the separate files to "
        "<destination>/_separate/; delete removes them",
    )
    parser.add_argument(
        "--stack-limit", default=0, type=int, metavar="N",
        help="Stack at most N groups per run (0 = no limit)",
    )
    parser.add_argument(
        "--stack-min-age", default=DEFAULT_STACK_MIN_AGE_SECONDS,
        type=int, metavar="SECONDS",
        help="Skip files modified less than this many seconds ago",
    )
    parser.add_argument(
        "--stack-order", default="newest",
        choices=["newest", "oldest"],
        help="Which end of the backlog to start from",
    )
    parser.add_argument(
        "--stack-workdir",
        help="Local scratch folder; inputs are copied here before "
        "encoding (use on a PC that reads the NAS over the network)",
    )
    parser.add_argument(
        "--stack-photos", action="store_true",
        help="Also stack F/I/R photo snapshots",
    )
    parser.add_argument(
        "--html", action="store_true",
        help="Use fast HTML directory scraping instead of "
        "slow XML API to list recordings",
    )
    parser.add_argument(
        "--version", action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser.parse_args()


# --- Entry point ---

def run():
    global dry_run, read_only, delete_after_sync, delete_locked
    global max_disk_used_percent, cutoff_date, socket_timeout
    global max_download_attempts

    args = parse_args()

    if args.quiet:
        logger.setLevel(logging.ERROR)
        cron_logger.setLevel(logging.ERROR)
    elif args.cron:
        logger.setLevel(logging.WARNING)
        cron_logger.setLevel(logging.INFO)
    else:
        logger.setLevel(
            logging.DEBUG if args.verbose > 0 else logging.INFO
        )

    logger.info("Starting Viofo Sync")

    dry_run = args.dry_run
    if dry_run:
        logger.info("[DRY RUN] No action will be taken.")

    read_only = args.read_only
    if read_only:
        logger.info("READ ONLY mode: locked files only.")

    delete_after_sync = args.delete_after_sync
    delete_locked = args.delete_locked
    if delete_after_sync:
        logger.info(
            "DELETE AFTER SYNC: camera files will be deleted "
            "after successful download."
        )

    socket_timeout = args.timeout
    socket.setdefaulttimeout(socket_timeout)
    max_download_attempts = args.download_attempts
    max_disk_used_percent = args.max_used_disk

    if args.keep:
        keep_match = re.fullmatch(
            r"(?P<range>\d+)(?P<unit>[dw]?)", args.keep
        )
        if keep_match is None:
            raise RuntimeError(
                "KEEP must be in the format <number>[dw]"
            )

        keep_range = int(keep_match.group("range"))
        if keep_range < 1:
            raise RuntimeError("KEEP must be greater than one.")

        keep_unit = keep_match.group("unit") or "d"
        if keep_unit == "d":
            delta = datetime.timedelta(days=keep_range)
        elif keep_unit == "w":
            delta = datetime.timedelta(weeks=keep_range)
        else:
            raise RuntimeError(
                f"unknown KEEP unit: {keep_unit}"
            )

        cutoff_date = datetime.datetime.now().date() - delta
        logger.info(f"Recording cutoff date: {cutoff_date}")

    try:
        if args.import_source:
            if local_import_has_recordings(args.import_source):
                success = organize_local_recordings(
                    args.import_source, args.destination,
                    args.grouping, args.move_imported,
                    args.gps_extract,
                )
            elif args.address:
                logger.info(
                    "Import source has no Viofo media files; "
                    "falling back to dashcam sync"
                )
                success = sync(
                    args.address, args.destination, args.grouping,
                    args.priority, args.filter, args,
                )
            else:
                success = organize_local_recordings(
                    args.import_source, args.destination,
                    args.grouping, args.move_imported,
                    args.gps_extract,
                )
        elif args.address:
            success = sync(
                args.address, args.destination, args.grouping,
                args.priority, args.filter, args,
            )
        elif args.merge_chunks or args.stack_cameras:
            success = True
        else:
            raise RuntimeError(
                "address is required unless --import-source, "
                "--merge-chunks or --stack-cameras is set"
            )

        # Stack whatever is already on disk even when the dashcam
        # is out of Wi-Fi range and the sync step failed.
        if args.stack_cameras:
            sync_ok = success
            success = stack_cameras(
                args.destination, args.grouping,
                args.stack_encoder, args.stack_bitrate,
                args.stack_originals, args.stack_limit,
                args.stack_min_age, args.stack_photos,
                args.stack_order, args.stack_workdir,
            ) and sync_ok

        if success and args.merge_chunks:
            success = merge_chunks(
                args.destination, args.grouping,
                args.merged_destination, args.merge_gap,
                args.delete_merged_sources,
            )
    except Exception:
        logger.exception("An error occurred")
        return 1

    if success:
        logger.info("Viofo Sync completed successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
