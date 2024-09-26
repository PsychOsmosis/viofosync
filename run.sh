#!/usr/bin/env bash

docker run -it --rm \
    -e ADDRESS=127.0.0.2 \
    -v $(pwd)/tmp:/recordings \
    -e DRY_RUN=1 \
    -e CRON=0 \
    -e RUN_ONCE=1 \
    -e VERBOSE=1 \
    --name viofosync \
acolomba/viofosync
