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

HTML mode now visits all five normal, parking, photo, and RO directory pages to identify the newest capture before selecting files. Normal HTML sync still excludes RO files unless selected by `READ_ONLY`. Camera requests, including listing, status, HEAD, downloads, and deletion, start at least one second apart, including after a failed request.

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

Deletion is off by default.

Enable it only when you trust the destination:

```yaml
environment:
  DELETE_AFTER_SYNC: 1
```

The script deletes a dashcam file only after the local copy has been verified. Locked/read-only recordings are skipped.

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
| `DELETE_AFTER_SYNC` | `0` | Set to `1` to delete verified files from the dashcam |
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
python3 -m py_compile viofosync.py
python3 -m unittest discover -s tests -v
bash -n entrypoint.sh
bash -n viofosync.sh
```

Build the image locally:

```bash
docker build -t viofosync:local .
```

## License

MIT. See [COPYING](COPYING).
