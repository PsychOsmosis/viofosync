FROM python:3.12-slim-trixie

WORKDIR /app

# ffmpeg 7.x (trixie) has hstack_vaapi/vstack_vaapi. On amd64 also
# install Intel's VAAPI driver so the NAS iGPU (e.g. UGREEN DXP4800
# Plus, Pentium Gold 8505) can encode the stacked videos.
RUN sed -i 's/^Components: main.*/Components: main non-free non-free-firmware/' \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends bash ffmpeg \
    && if [ "$(dpkg --print-architecture)" = "amd64" ]; then \
        apt-get install -y --no-install-recommends \
            intel-media-va-driver-non-free vainfo; \
    fi \
    && rm -rf /var/lib/apt/lists/*

ENV ADDRESS="" \
    DESTINATION="/recordings" \
    TZ="Europe/London" \
    GROUPING="daily" \
    PRIORITY="date" \
    KEEP="" \
    SYNC_INTERVAL="600" \
    MAX_USED_DISK="90" \
    TIMEOUT="10" \
    DOWNLOAD_ATTEMPTS="1" \
    VERBOSE="0" \
    QUIET="0" \
    HTML="0" \
    READ_ONLY="0" \
    GPS_EXTRACT="0" \
    DELETE_AFTER_SYNC="0" \
    DELETE_LOCKED="0" \
    DRY_RUN="0" \
    RUN_ONCE="0" \
    IMPORT_SOURCE="" \
    MOVE_IMPORTED="0" \
    MERGE_CHUNKS="0" \
    MERGE_GPS="0" \
    MERGE_KEEP_GPX="0" \
    MERGE_GAP="2" \
    MERGED_DESTINATION="" \
    DELETE_MERGED_SOURCES="0" \
    STACK_CAMERAS="0" \
    STACK_ENCODER="auto" \
    STACK_BITRATE="45M" \
    STACK_ORIGINALS="keep" \
    STACK_LIMIT="0" \
    STACK_MIN_AGE="300" \
    STACK_PHOTOS="0" \
    LIBVA_DRIVER_NAME="iHD"

COPY . /app

RUN chmod +x /app/entrypoint.sh /app/viofosync.sh /app/viofosync.py

ENTRYPOINT ["/app/entrypoint.sh"]
