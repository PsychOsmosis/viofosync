# Viofo Sync

Viofo Sync copies videos and photos from a Viofo dashcam to a NAS or local folder.

It supports three workflows:

- Wi-Fi sync from the dashcam HTTP interface
- Local import from a mounted SD card, SSD, or copied dashcam folder
- Optional normal-driving clip merging with `ffmpeg`

Photos are organized by the same grouping rules as videos.
Parking recordings such as `PF` and `PR` are imported but not merged.

## Quick Start

```yaml
services:
  viofosync:
    image: tt99112/viofosync:latest
    container_name: viofosync
    restart: unless-stopped
    volumes:
      - /dashcam-recordings:/recordings:rw
    environment:
      ADDRESS: dashcam.local
      TZ: Europe/London
      GROUPING: daily
      KEEP: 2w
```

Start it:

```bash
docker compose up -d
```

By default the container runs every 10 minutes. Set `RUN_ONCE=1` to run once and exit.

If you do not want root-owned files on the NAS, run the container as your NAS user:

```yaml
user: "1000:1000"
```

## Wi-Fi Sync

Set `ADDRESS` to the dashcam IP or hostname:

```yaml
environment:
  ADDRESS: dashcam.local
  GROUPING: daily
```

The dashcam must be powered on and reachable from the NAS. For automated use, station mode Wi-Fi is recommended.

Use `HTML=1` if the XML file listing is slow or unreliable:

```yaml
environment:
  HTML: 1
```

Wi-Fi sync always guards against downloading an active recording; no extra option is needed. Each run takes one complete file-list snapshot, then reads the camera's recording state once before applying file filters, age limits, download priority, stacked-file exclusions, or `READ_ONLY` selection. If the camera is recording or its state is unknown, the newest video capture is deferred across front, interior, and rear files, including parking and locked/RO recordings. Only an explicit stopped state makes that newest capture eligible.

Capture groups use filename timestamps rather than sequence numbers or lens/mode suffixes. When companion cameras have slightly different timestamps, the existing pairing limits also defer other lenses in the same mode within 10 seconds and 3 sequence numbers of the newest capture. Photos do not supersede an active video. Nonstandard XML video names use the listing timestamp.

The snapshot is not refreshed during the run. The next run reassesses the newest capture, so a previously deferred group becomes eligible when a newer capture appears or the camera explicitly reports that recording has stopped. The script does not start, stop, or pause camera recording. Existing download verification and deletion options still apply to eligible files.

HTML mode now visits all five normal, parking, photo, and RO directory pages to identify the newest capture before selecting files. Normal HTML sync still excludes RO files unless selected by `READ_ONLY` or included with `DELETE_LOCKED=1`. Camera requests, including listing, status, HEAD, downloads, and deletion, start at least one second apart, including after a failed request.

## Local Import

Use this when you remove the SD card or SSD and mount it on the NAS or another machine connected to the NAS.

```yaml
services:
  viofosync:
    image: tt99112/viofosync:latest
    volumes:
      - /dashcam-recordings:/recordings:rw
      - /path/to/dashcam-drive:/import:rw
    environment:
      IMPORT_SOURCE: /import
      GROUPING: daily
      RUN_ONCE: 1
```

Files are copied into `/recordings` by default. Set `MOVE_IMPORTED=1` if you want files removed from the import source after the destination copy is verified.

If both `IMPORT_SOURCE` and `ADDRESS` are set, each run checks the import source first. When Viofo media files are found it imports them; when the import source is empty or unavailable it falls back to normal Wi-Fi sync.

For fastest imports when the source and destination are on the same NAS volume, mount their parent folder once and use paths inside that one mount:

```yaml
volumes:
  - /volume1/NAS_HDD/Dash_Cam_Footage:/dashcam:rw
environment:
  DESTINATION: /dashcam/Sync
  IMPORT_SOURCE: /dashcam/SSD
  MOVE_IMPORTED: 1
```

This lets local imports move files by rename instead of copying data between separate container mounts.

Direct CLI usage:

```bash
./viofosync.py --import-source /path/to/dashcam-drive \
  --destination /path/to/recordings \
  --grouping daily
```

## Optional Deletion From Dashcam

Deletion is off by default. To delete unlocked camera files after verifying their local copies:

```yaml
environment:
  DELETE_AFTER_SYNC: 1
  DELETE_LOCKED: 0
```

Locked/read-only recordings (`ATTR=33` or an `RO` directory) remain protected by default. To include them, explicitly enable both options:

```yaml
environment:
  DELETE_AFTER_SYNC: 1
  DELETE_LOCKED: 1
```

`DELETE_LOCKED=1` alone does not enable deletion. The CLI equivalents are `--delete-after-sync` and `--delete-locked`. `DRY_RUN=1` / `--dry-run` logs proposed actions and never sends a delete command, even when both options are enabled.

Deletion requires a positive exact XML file size, the camera's exact drive-qualified `FPATH`, and a local file that still matches that size immediately before deletion. This also permits deletion of an existing verified local copy. Missing, empty, incomplete, failed, changed, or unverified copies do not authorize deletion. Each eligible file receives one delete request using the unchanged XML path; there is no drive guessing, alternate-path probing, or immediate delete retry.

`HTML=1` remains supported. When deletion is enabled and eligible files exist, one XML listing supplies exact metadata for all candidates in that sync. The resulting lookup is cached for that run, rather than requesting XML for each file. Matching uses the filename and relative DCIM path; the exact XML drive and path are retained for deletion. Missing or ambiguous matches, or a failed XML lookup, preserve normal HTML downloads and skip deletion. No XML lookup is added when deletion is disabled or all candidates are excluded by filters, retention, stacking, or locked-file protection.

With `DELETE_LOCKED=1`, HTML mode scans both normal and `RO` directories. `READ_ONLY=1` still selects only locked recordings. Camera HTTP requests are spaced at least one second apart, including failed requests.

The owner verified this mechanism on an A329T running firmware 2.2: an `ATTR=33` file on the external SSD under `B:\DCIM\Movie\RO` was deleted with `cmd=4003` using the unchanged, URL-encoded `FPATH` supplied by XML `cmd=3015`. Its disappearance was independently verified. The branch tests use synthetic files and mocked camera responses; they perform no new live deletion.

## Optional Clip Merging

Merging is off by default.

Enable it with:

```yaml
environment:
  MERGE_CHUNKS: 1
  MERGE_GAP: 2
```

Merge behavior:

- `F` clips merge only with `F`
- `R` clips merge only with `R`
- `PF` and `PR` parking clips are not merged
- merge groups respect `GROUPING`

For example, with `GROUPING=daily`, a drive that crosses midnight becomes separate merged files for each day.

Merged files are written to `/recordings/merged` unless `MERGED_DESTINATION` is set.

Keep `DELETE_MERGED_SOURCES` disabled until you have checked the merged output:

```yaml
environment:
  DELETE_MERGED_SOURCES: 0
```

## Optional Camera Stacking

3-channel cameras (A329S etc.) can record front, interior and rear as separate
`F` / `I` / `R` files. Stacking combines each set into one video laid out like
the camera's own stacked mode: front on top at 4K, interior bottom-left and rear
bottom-right at 1080p (3840x3240). Audio comes from the front file.

```yaml
services:
  viofosync:
    image: tt99112/viofosync:latest
    devices:
      - /dev/dri:/dev/dri   # Intel iGPU for hardware encoding
    environment:
      STACK_CAMERAS: 1
      STACK_LIMIT: 1        # try one first, then set to 0
```

Behaviour:

- `F`/`I`/`R` sets are matched by timestamp and consecutive sequence numbers
  (`002556F`, `002557I`, `002558R`); parking `PF`/`PI`/`PR` sets too
- the stacked file takes the front file's name, in the same date folder
- originals move to `<destination>/_separate/<date>/` (`STACK_ORIGINALS=keep`)
  or are deleted (`STACK_ORIGINALS=delete`); `KEEP` also prunes `_separate`
- stacked names are recorded in `<destination>/.viofosync-stacked` so Wi-Fi sync
  and local import do not fetch the originals again
- output is checked for size (3840x3240) and duration before anything is moved
- newest recordings are stacked first; `STACK_LIMIT` caps how many per run

Encoding uses VAAPI on the Intel iGPU when `/dev/dri` is passed through and
falls back to CPU `libx264`, which is far too slow on a NAS CPU for 4K. Check
the GPU is visible with:

```bash
docker exec viofosync vainfo
```

## Configuration

| Variable | Default | Description |
|---|---:|---|
| `ADDRESS` | | Dashcam IP or hostname for Wi-Fi sync |
| `DESTINATION` | `/recordings` | Folder where recordings are written |
| `IMPORT_SOURCE` | | Local folder to import before Wi-Fi sync |
| `GROUPING` | `none` | `none`, `daily`, `weekly`, `monthly`, or `yearly` |
| `KEEP` | | Delete destination recordings older than this, e.g. `30d` or `4w` |
| `PRIORITY` | `date` | `date` for oldest first, `rdate` for newest first |
| `SYNC_INTERVAL` | `600` | Seconds between runs when `RUN_ONCE` is not set |
| `RUN_ONCE` | `0` | Set to `1` to run once and exit |
| `HTML` | `0` | Set to `1` to list files from the dashcam HTML directory pages |
| `READ_ONLY` | `0` | Set to `1` to sync only locked/read-only recordings |
| `GPS_EXTRACT` | `0` | Set to `1` to write `.gpx` files beside recordings |
| `DELETE_AFTER_SYNC` | `0` | Set to `1` to delete verified unlocked files from the dashcam |
| `DELETE_LOCKED` | `0` | Set to `1` to also delete verified locked files; requires `DELETE_AFTER_SYNC=1` |
| `MOVE_IMPORTED` | `0` | Set to `1` to move local import files instead of copying |
| `MERGE_CHUNKS` | `0` | Set to `1` to merge normal driving chunks |
| `MERGE_GAP` | `2` | Allowed seconds between consecutive normal chunks |
| `MERGED_DESTINATION` | `/recordings/merged` | Optional merged-file output folder |
| `DELETE_MERGED_SOURCES` | `0` | Set to `1` to delete source chunks after a merge |
| `STACK_CAMERAS` | `0` | Set to `1` to stack separate F/I/R files into one video |
| `STACK_ENCODER` | `auto` | `auto` (VAAPI if available, else CPU), `vaapi`, or `x264` |
| `STACK_BITRATE` | `45M` | Video bitrate for CPU (x264) stacking |
| `STACK_QP` | `26` | Quality for iGPU stacking; lower is bigger/better (26 ≈ 50 Mbps) |
| `STACK_ORIGINALS` | `keep` | `keep` moves originals to `_separate/`, `delete` removes them |
| `STACK_LIMIT` | `0` | Maximum groups stacked per run; `0` means no limit |
| `STACK_MIN_AGE` | `300` | Skip files modified in the last N seconds |
| `STACK_PHOTOS` | `0` | Set to `1` to also stack F/I/R photo snapshots |
| `MAX_USED_DISK` | `90` | Stop downloading when destination disk usage reaches this percent |
| `TIMEOUT` | `10` | Dashcam connection timeout in seconds |
| `DOWNLOAD_ATTEMPTS` | `1` | Retry attempts per file |
| `VERBOSE` | `0` | Set above `0` for debug logging |
| `QUIET` | `0` | Set to `1` to log only errors |
| `DRY_RUN` | `0` | Set to `1` to show actions without changing files |

Boolean options accept `1`, `true`, `yes`, or `on` to enable them. Use `0`, `false`, an empty value, or anything else to disable them.

## Docker Hub Publishing

This repo includes a GitHub Actions workflow that publishes:

```text
tt99112/viofosync:latest
```

on pushes to `main`.

Add these repository secrets in GitHub before publishing:

```text
DOCKER_USERNAME
DOCKER_PASSWORD
```

## Notes

- Viofo filenames provide the recording start time.
- Normal chunk merging uses filename start time plus `ffprobe` duration.
- Parking timelapse duration is not real elapsed time, so parking files stay separate.
- This project is based on BlackVue Sync by Alessandro Colomba and includes Viofo GPS extraction work based on Sergei Franco's research.

## Development

Run the basic local checks:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile viofosync.py
python3 -m unittest discover -s tests -v
bash -n entrypoint.sh
bash -n viofosync.sh
```

The tests use synthetic files and mocked HTTP responses; they do not contact a dashcam or delete camera files.

### Feature-branch image and deployment

The published `latest` image is built from `main`; pushing a feature branch does not update it. From a checkout of `feature/delete-locked-recordings`, build a separate image tag:

```bash
docker build -t viofosync:delete-locked .
```

After operator approval to replace the running container, select that tag in the deployment configuration. Start with deletion disabled while checking the destination and logs:

```yaml
services:
  viofosync:
    image: viofosync:delete-locked
    environment:
      HTML: 1
      DELETE_AFTER_SYNC: 0
      DELETE_LOCKED: 0
```

Replacing or starting the deployed container and enabling camera deletion require explicit operator approval. Building and testing the branch do not perform those actions. Preserve the existing volumes, scheduling, merge, and stacking settings when preparing the deployment.

## License

MIT. See [COPYING](COPYING).
