# Viofo Sync

Viofo Sync is a tool for synchronizing recordings from a Viofo dashcam (tested with A229 Pro) over Wi-Fi to a local directory.

It is designed to be run as a Docker container on a NAS or similar device.

This project is based on the great BlackVue Sync by Alessandro Colomba (https://github.com/acolomba) and uses GPX extraction from https://sergei.nz/extracting-gps-data-from-viofo-a119-and-other-novatek-powered-cameras/

## GPS Extraction

If you have a use for GPX files, they can be extracted from the video using the `GPS_EXTRACT` option detailed below.

## Hardware and Firmware Requirements

The dashcam must remain powered on and connected to Wi-Fi. It is recommended to use a hardwire kit, such as the Viofo HK4, and ideally, a dedicated dashcam battery to prevent draining the car battery.

The dashcam should be connected to the LAN using Wi-Fi station mode.

As of September 2024, the official A229 Pro firmware does not retain the previous Wi-Fi state after a reboot. However, Viofo support has provided special firmware upon request that retains this state. This feature may be officially released in the near future and is recommended to make downloads fully automated.

## Using the Docker Container

To use Viofo Sync as a Docker container, follow these steps:

1. **Install Docker:**

    Download from https://www.docker.com/ if you don't have it already.

2. **Run the Docker Container:**
   ```bash
   docker run -it --rm \
       -e ADDRESS=<DASHCAM_IP> \
       -e PUID=$(id -u) \
       -e PGID=$(id -g) \
       -e TZ="Europe/London" \
       -e KEEP=2w \
       -e GROUPING=daily
       -v /path/to/local/directory:/recordings \
       --name viofosync \
       robxyz/viofosync
   ```

   Replace `<DASHCAM_IP>` with the IP address of your dashcam and `/path/to/local/directory` with the path to your local directory where recordings will be stored.

## Configuration Options

The following environment variables can be set to configure the behavior of the Viofo Sync Docker container:

| Variable | Description | Default |
|---|---|---|
| `ADDRESS` | IP address or hostname of the dashcam | *(required)* |
| `PUID` | User ID for file permissions | |
| `PGID` | Group ID for file permissions | |
| `TZ` | Timezone (e.g. `Europe/London`) | |
| `KEEP` | Retention period — recordings older than this are deleted. Accepts `<number>[d\|w]` for days or weeks (e.g. `30d`, `4w`) | |
| `GROUPING` | Group recordings into subdirectories: `daily`, `weekly`, `monthly`, `yearly`, or `none` | `none` |
| `PRIORITY` | Download order: `date` (oldest first) or `rdate` (newest first) | `date` |
| `MAX_USED_DISK` | Stop downloading if disk usage exceeds this percentage (5-98) | `90` |
| `TIMEOUT` | Connection timeout in seconds | `30` |
| `DOWNLOAD_ATTEMPTS` | Number of attempts for each download (must be >= 1) | `1` |
| `VERBOSE` | Logging verbosity level (0 = normal, 1+ = debug) | `0` |
| `QUIET` | Set to any value to only log errors | |
| `CRON` | Set to 1 for reduced cron-mode logging (DEFAULTS TO 1 IF UNSET) | `1` |
| `GPS_EXTRACT` | Set to any value to extract GPS data and create `.gpx` files alongside recordings | |
| `READ_ONLY` | Set to any value to only sync read-only (locked) recordings | |
| `HTML` | Set to any value to use alternative HTML scraping instead of the XML API. Recommended for cameras that are slow or timeout responding to the XML file listing request | |
| `DRY_RUN` | Set to any value to show what would happen without downloading or deleting anything | |
| `RUN_ONCE` | Set to any value to sync once and exit instead of running on a cron schedule | |

## XML vs HTML Mode

By default, Viofo Sync uses the camera's XML API (`/?custom=1&cmd=3015&par=1`) to get the file listing. For some reason on my camera this started running very slowly so setting `HTML=1` switches to scraping the camera's HTTP directory listings (`/DCIM/Movie`, `/DCIM/Movie/Parking`, `/DCIM/Movie/RO`), which seem to load faster.

## License

This project is licensed under the MIT License. See the [COPYING](COPYING) file for details.
