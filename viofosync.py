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

__version__ = "1.1"

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
max_disk_used_percent = 90
cutoff_date = None
socket_timeout = 10.0

MAX_DOWNLOAD_ATTEMPTS = 3
RETRY_BACKOFF = 5  # seconds, multiplied by attempt number

# Recording namedtuple matching Viofo's file information
Recording = namedtuple(
    "Recording",
    "filename filepath size timecode datetime attr",
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
    "_*[FR].MP4"
)

# Downloaded recording filename regular expression
downloaded_filename_re = re.compile(
    r"^(?P<year>\d{4})_(?P<month>\d{2})(?P<day>\d{2})"
    r"_(?P<hour>\d{2})(?P<minute>\d{2})(?P<second>\d{2})"
    r"_(?P<sequence>\d+)(?P<camera>.+)\.MP4$",
    re.IGNORECASE,
)

# Viofo camera filename pattern
filename_re = re.compile(
    r"(?P<year>\d{4})_(?P<month>\d{2})(?P<day>\d{2})"
    r"_(?P<hour>\d{2})(?P<minute>\d{2})(?P<second>\d{2})"
    r"_(?P<sequence>\d+)(?P<camera>.+)\.MP4",
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


def get_dashcam_filenames(base_url):
    """Gets the recording filenames from the Viofo dashcam."""
    try:
        url = f"{base_url}/?custom=1&cmd=3015&par=1"
        request = urllib.request.Request(url)
        response = urllib.request.urlopen(request)

        if response.getcode() != 200:
            raise RuntimeError(
                f"Error response from {base_url}; "
                f"status code: {response.getcode()}"
            )

        xml_data = response.read().decode('utf-8')
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
    r'<a href="(?P<filepath>[^"]+\.MP4)">'
    r'<b>(?P<filename>[^<]+)</b></a>'
    r'<td align=right>\s*(?P<size>[\d.]+)\s*(?P<unit>[KMGT]?B)',
    re.IGNORECASE,
)

# Directories to scrape on the dashcam
DCIM_DIRS = ["/DCIM/Movie", "/DCIM/Movie/Parking"]
DCIM_DIRS_RO = ["/DCIM/Movie/RO"]


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
    dirs = DCIM_DIRS_RO if read_only else DCIM_DIRS
    recordings = []

    for dir_path in dirs:
        url = f"{base_url}{dir_path}"
        try:
            with urllib.request.urlopen(
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
            filepath = m.group("filepath")
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
    with urllib.request.urlopen(req, timeout=timeout) as resp:
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

    Returns (downloaded: bool, speed_str: str|None).
    Uses HEAD to check size, retries up to MAX_DOWNLOAD_ATTEMPTS,
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

    # Check expected size via HEAD
    try:
        expected_size = get_remote_size(url, socket_timeout)
    except Exception:
        expected_size = None

    # Skip if already downloaded and size matches
    if os.path.exists(dest_filepath):
        if expected_size is not None:
            local_size = os.path.getsize(dest_filepath)
            if local_size == expected_size:
                logger.debug(
                    f"Skipping {recording.filename} "
                    f"({human_size(local_size)})"
                )
                return False, None
            # Size mismatch — re-download
            logger.info(
                f"Size mismatch for {recording.filename}, "
                f"re-downloading"
            )
        else:
            logger.debug(
                f"Already downloaded: {recording.filename}"
            )
            return False, None

    if dry_run:
        logger.info(
            f"[DRY RUN] Would download: {recording.filename}"
        )
        return True, None

    # Atomic download with tempfile + retries
    dest_dir = os.path.dirname(dest_filepath)
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=dest_dir,
        prefix=f".{recording.filename}.",
        suffix=".part",
    )
    os.close(tmp_fd)

    try:
        for attempt in range(1, MAX_DOWNLOAD_ATTEMPTS + 1):
            try:
                start = time.perf_counter()
                with urllib.request.urlopen(
                    url, timeout=socket_timeout
                ) as resp, open(tmp_path, "wb") as out:
                    shutil.copyfileobj(resp, out)
                elapsed = time.perf_counter() - start
            except Exception as e:
                logger.warning(
                    f"Download attempt {attempt} failed for "
                    f"{recording.filename}: {e}"
                )
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
            return True, speed_str

        # All attempts exhausted
        logger.error(
            f"Failed to download {recording.filename} "
            f"after {MAX_DOWNLOAD_ATTEMPTS} attempts"
        )
        return False, None
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
        return False, None
    finally:
        # Clean up temp file if it still exists
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


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


def sync(address, destination, grouping, download_priority,
         recording_filter, args):
    """Synchronizes dashcam recordings with destination dir."""
    logger.info(f"Starting sync for {address}")
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

    total = len(dashcam_recordings)
    for i, recording in enumerate(dashcam_recordings, start=1):
        if cutoff_date and recording.datetime.date() < cutoff_date:
            continue
        group_name = get_group_name(
            recording.datetime, grouping
        )
        logger.info(
            f"[{i}/{total}] Processing {recording.filename}"
        )
        downloaded, _ = download_file(
            base_url, recording, destination, group_name
        )
        if downloaded and args.gps_extract:
            dest_path = get_filepath(
                destination, group_name, recording.filename
            )
            extract_gps_data(dest_path)

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

    offset = 0
    hour, minute, second, year, month, day = struct.unpack_from(
        '<IIIIII', data, offset
    )
    offset += 24
    active, lat_hemi, lon_hemi = struct.unpack_from(
        '<ccc', data, offset
    )
    offset += 4
    lat_raw, lon_raw, speed, bearing = struct.unpack_from(
        '<ffff', data, offset
    )

    gps['DT']['Hour'] = hour
    gps['DT']['Minute'] = minute
    gps['DT']['Second'] = second
    gps['DT']['Year'] = year
    gps['DT']['Month'] = month
    gps['DT']['Day'] = day
    gps['DT']['DT'] = fix_time(
        hour, minute, second, year, month, day
    )

    gps['Loc']['Lat']['Hemi'] = lat_hemi.decode()
    gps['Loc']['Lon']['Hemi'] = lon_hemi.decode()
    gps['Loc']['Lat']['Raw'] = lat_raw
    gps['Loc']['Lon']['Raw'] = lon_raw
    gps['Loc']['Lat']['Float'] = fix_coordinates(
        gps['Loc']['Lat']['Hemi'], gps['Loc']['Lat']['Raw']
    )
    gps['Loc']['Lon']['Float'] = fix_coordinates(
        gps['Loc']['Lon']['Hemi'], gps['Loc']['Lon']['Raw']
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
        "with a local directory and extracts GPS data.",
    )
    parser.add_argument(
        "address", help="Dashcam IP address or hostname",
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
    global dry_run, read_only
    global max_disk_used_percent, cutoff_date, socket_timeout

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

    socket_timeout = args.timeout
    socket.setdefaulttimeout(socket_timeout)

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
        success = sync(
            args.address, args.destination, args.grouping,
            args.priority, args.filter, args,
        )
    except Exception:
        logger.exception("An error occurred during sync")
        return 1

    if success:
        logger.info("Viofo Sync completed successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
