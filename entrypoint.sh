#!/usr/bin/env bash

/setuid.sh \
&& su -m dashcam /viofosync.sh \
&& [[ -z $RUN_ONCE ]] \
&& crond -f
