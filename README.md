# Viofo Sync

Viofo Sync is a tool for synchronizing recordings from a Viofo dashcam (tested with A229 Pro) over Wi-Fi to a local directory.

It is designed to be run as a Docker container on a NAS or similar device.

This project is based on the great BlackVue Sync by Alessandro Colomba (https://github.com/acolomba) and uses GPX extraction from https://sergei.nz/extracting-gps-data-from-viofo-a119-and-other-novatek-powered-cameras/

## GPS extraction

If you have a use for GPX files, they can be extracted from the video using the option detailed below.

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

- `ADDRESS`: The IP address or hostname of the dashcam.
- `PUID` and `PGID`: User and group IDs for file permissions.
- `KEEP`: Retention period for downloaded recordings (e.g., '30d' for 30 days).
- `GROUPING`: Group recordings by time period ('daily', 'weekly', 'monthly', 'yearly', 'none').
- `PRIORITY`: Download priority ('date' for oldest first, 'rdate' for newest first).
- `MAX_USED_DISK`: Maximum disk usage percentage before stopping downloads.
- `TIMEOUT`: Connection timeout in seconds.
- `VERBOSE`: Increase logging verbosity (set to a number greater than zero).
- `QUIET`: Quiet mode, only logs unexpected errors.
- `CRON`: Enable cron mode for logging.
- `GPS_EXTRACT`: Enable GPS data extraction and GPX file creation.
- `DRY_RUN`: Perform a trial run without downloading files.

## License

This project is licensed under the MIT License. See the [COPYING](COPYING) file for details.
