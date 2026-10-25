#!/usr/bin/env bash
# Shared, fixed resource inventory. Never derive the project from gcloud config.
PROJECT_ID=cs-528-508021
REGION=us-central1
ZONE=us-central1-f
SQL_INSTANCE=hw5-postgres
DB_NAME=hw5
DB_USER=hw5_app
SECRET=hw5-db-password
VM=hw5-app
NETWORK=hw5-network
SUBNET=hw5-subnet
ADDRESS=hw5-web-ip
FIREWALL=hw5-web-http
BUCKET_NAME=cs528hw2
OBJECT_PREFIX=pages
ARTIFACT_BUCKET=cs-528-508021-hw5-deploy
REPOSITORY=hw5-functions
TOPIC=hw5-forbidden-requests
SUBSCRIPTION=hw5-forbidden-sub
FUNCTION=hw5-stop-sql
JOB=hw5-hourly-stop-sql
OWNER=cs528-hw5
VM_SA=hw5-vm@${PROJECT_ID}.iam.gserviceaccount.com
FUNCTION_SA=hw5-stop-sql@${PROJECT_ID}.iam.gserviceaccount.com
SCHEDULER_SA=hw5-scheduler@${PROJECT_ID}.iam.gserviceaccount.com
BUILD_SA=hw5-build@${PROJECT_ID}.iam.gserviceaccount.com

gc() { gcloud --quiet --project="$PROJECT_ID" "$@"; }
die() { echo "ERROR: $*" >&2; exit 1; }
# Retry only read commands affected by new-resource/API propagation. Never retry
# mutations here: an ambiguous create result must not create duplicate resources.
read_eventually() {
    local attempt result_file error_file
    result_file=$(mktemp)
    error_file=$(mktemp)
    for attempt in {1..12}; do
        if gc "$@" > "$result_file" 2> "$error_file"; then
            cat "$result_file"
            rm -f -- "$result_file" "$error_file"
            return 0
        fi
        if ! python3 - "$error_file" <<'PY'
import re, sys
from pathlib import Path
message = Path(sys.argv[1]).read_text()
transient = ('NOT_FOUND' in message or 'SERVICE_DISABLED' in message
             or 'has not been used in project' in message
             or bool(re.search(r'API .*not enabled', message, re.IGNORECASE)))
raise SystemExit(0 if transient else 1)
PY
        then
            cat "$error_file" >&2
            rm -f -- "$result_file" "$error_file"
            return 1
        fi
        if [ "$attempt" -lt 12 ]; then
            printf 'Waiting for API/resource visibility (%s/12): %s\n' "$attempt" "$*" >&2
            sleep 10
        fi
    done
    cat "$error_file" >&2
    rm -f -- "$result_file" "$error_file"
    return 1
}
# Call only with a list command and an exact resource filter. Authorization/API
# errors abort; they must never be interpreted as 'resource does not exist'.
exists() {
    local result
    result=$(gc "$@" --format='value(name)') || exit 1
    [ -n "$result" ]
}
owned() { python3 deploy/hw5_validate.py "$1"; }

wait_for_sql_operations() {
    local pending operation
    pending=$(gc sql operations list --instance="$SQL_INSTANCE" \
        --filter='status!=DONE' --format='value(name)') || return 1
    for operation in $pending; do
        printf 'Waiting for existing SQL operation: %s\n' "$operation" >&2
        gc sql operations wait "$operation" --timeout=1800 || return 1
    done
}

set_sql_activation() {
    local desired output_file operation
    desired=$1
    case "$desired" in ALWAYS|NEVER) ;; *) die 'Invalid SQL activation policy.' ;; esac
    wait_for_sql_operations || return 1
    output_file=$(mktemp)
    printf 'Setting SQL activation policy to %s; waiting for the cloud operation...\n' "$desired" >&2
    if gc sql instances patch "$SQL_INSTANCE" --activation-policy="$desired" > "$output_file" 2>&1; then
        cat "$output_file"
        rm -f -- "$output_file"
        return 0
    fi
    cat "$output_file" >&2
    # Continue waiting only for the SDK's explicit timeout, not other API errors.
    operation=$(python3 - "$output_file" <<'PY'
import re, sys
from pathlib import Path
message = Path(sys.argv[1]).read_text()
match = re.search(r'/operations/([A-Za-z0-9-]+) is taking longer than expected', message)
if match:
    print(match[1])
PY
    )
    rm -f -- "$output_file"
    if [ -z "$operation" ]; then return 1; fi
    printf 'Cloud SQL operation is still active; extending wait for %s (up to 1800 seconds).\n' "$operation" >&2
    gc sql operations wait "$operation" --timeout=1800
}
# A new Scheduler job can be visible before its internal retry-policy resource.
# Re-read state before every retry, including when the prior action's outcome was
# ambiguous. Only issue pause/resume when a transition is still required.
set_scheduler_state() {
    local desired action state attempt error_file
    desired=$1
    case "$desired" in
        PAUSED) action=pause ;;
        ENABLED) action=resume ;;
        *) die "Unsupported Scheduler state: $desired" ;;
    esac
    error_file=$(mktemp)
    for attempt in {1..12}; do
        if ! state=$(read_eventually scheduler jobs describe "$JOB" --location="$REGION" --format='value(state)'); then
            rm -f -- "$error_file"
            return 1
        fi
        if [ "$state" = "$desired" ]; then
            rm -f -- "$error_file"
            return 0
        fi
        if [ "$state" != PAUSED ] && [ "$state" != ENABLED ]; then
            echo "Scheduler is in unexpected state $state; refusing automatic transition." >&2
            rm -f -- "$error_file"
            return 1
        fi
        if gc scheduler jobs "$action" "$JOB" --location="$REGION" > "$error_file" 2>&1; then
            # The API has accepted the transition. Confirm with another read.
            state=$(read_eventually scheduler jobs describe "$JOB" --location="$REGION" --format='value(state)') || {
                rm -f -- "$error_file"; return 1;
            }
            if [ "$state" = "$desired" ]; then
                cat "$error_file"
                rm -f -- "$error_file"
                return 0
            fi
        else
            if ! python3 - "$error_file" <<'PY'
from pathlib import Path
import sys
message = Path(sys.argv[1]).read_text()
retryable = any(code in message for code in (
    'NOT_FOUND', 'UNAVAILABLE', 'INTERNAL', 'DEADLINE_EXCEEDED',
    'RESOURCE_EXHAUSTED', 'FAILED_PRECONDITION',
))
raise SystemExit(0 if retryable else 1)
PY
            then
                cat "$error_file" >&2
                rm -f -- "$error_file"
                return 1
            fi
        fi
        if [ "$attempt" -lt 12 ]; then
            printf 'Waiting for Scheduler transition to %s (%s/12).\n' "$desired" "$attempt" >&2
            sleep 10
        fi
    done
    cat "$error_file" >&2
    echo "Scheduler did not reach $desired; inspect scheduler.sh status before proceeding." >&2
    rm -f -- "$error_file"
    return 1
}
project_role() {
    gc projects add-iam-policy-binding "$PROJECT_ID" \
        --member="serviceAccount:$1" --role="$2" --condition=None --quiet >/dev/null
}
