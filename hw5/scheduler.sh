#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source deploy/hw5-common.sh
gc scheduler jobs describe "$JOB" --location="$REGION" --format=json | owned description
case "${1:-status}" in
    pause)
        set_scheduler_state PAUSED
        echo 'Scheduled future invocations paused. An already-running invocation is not cancelled.' ;;
    resume)
        set_scheduler_state ENABLED ;;
    run) gc scheduler jobs run "$JOB" --location="$REGION" ;;
    status) gc scheduler jobs describe "$JOB" --location="$REGION" \
        --format='yaml(name,state,schedule,timeZone,httpTarget.uri,httpTarget.oidcToken,lastAttemptTime,status)' ;;
    *) die 'Usage: scheduler.sh pause|resume|run|status' ;;
esac
