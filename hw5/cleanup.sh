#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source deploy/hw5-common.sh
trap 'echo "Cleanup failed at line $LINENO; resolve the error and rerun. Remaining resources may still incur charges." >&2' ERR
umask 077
HW5_TMP=$(mktemp -d)
trap 'rm -rf -- "$HW5_TMP"' EXIT

for name in hw5-vm hw5-stop-sql hw5-scheduler hw5-build; do
    email="$name@$PROJECT_ID.iam.gserviceaccount.com"
    if exists iam service-accounts list --filter="email=$email"; then
        gc iam service-accounts describe "$email" --format=json | owned account
    fi
done

if exists scheduler jobs list --location="$REGION" --filter="name=projects/$PROJECT_ID/locations/$REGION/jobs/$JOB"; then
    gc scheduler jobs describe "$JOB" --location="$REGION" --format=json | owned description
    gc scheduler jobs delete "$JOB" --location="$REGION" --quiet
fi
# Stop the database early, including after a partially failed setup. NEVER delete.
if exists sql instances list --filter="name=$SQL_INSTANCE"; then
    wait_for_sql_operations
    POLICY=$(gc sql instances describe "$SQL_INSTANCE" --format='value(settings.activationPolicy)')
    if [ "$POLICY" != NEVER ]; then
        set_sql_activation NEVER
    fi
fi
if exists compute instances list --filter="name=$VM"; then
    gc compute instances describe "$VM" --zone="$ZONE" --format=json | owned labels
    gc compute instances delete "$VM" --zone="$ZONE" --quiet
fi
# Static IPs survive VM deletion and must be explicitly released.
if exists compute addresses list --filter="name=$ADDRESS"; then
    gc compute addresses describe "$ADDRESS" --region="$REGION" --format=json | owned description
    gc compute addresses delete "$ADDRESS" --region="$REGION" --quiet
fi
if exists functions list --regions="$REGION" --filter="name=projects/$PROJECT_ID/locations/$REGION/functions/$FUNCTION"; then
    gc functions describe "$FUNCTION" --gen2 --region="$REGION" --format=json > "$HW5_TMP/function.json"
    owned labels < "$HW5_TMP/function.json"
    # Delete only this function's source prefix, never a shared system bucket.
    python3 - "$HW5_TMP/function.json" "$HW5_TMP/source-prefix" <<'PY'
import json, sys
from pathlib import Path
source = json.load(open(sys.argv[1])).get('buildConfig', {}).get('source', {}).get('storageSource', {})
bucket, obj = source.get('bucket', ''), source.get('object', '')
prefix = ''
if bucket.startswith('gcf-v2-sources-') and obj.startswith('hw5-stop-sql/'):
    prefix = f'gs://{bucket}/hw5-stop-sql/'
elif bucket and bucket != 'cs-528-508021-hw5-deploy':
    print('NOTE: source object retained for manual review:', bucket, obj)
Path(sys.argv[2]).write_text(prefix)
PY
    SOURCE_PREFIX=$(cat "$HW5_TMP/source-prefix")
    if [ -n "$SOURCE_PREFIX" ]; then gc storage rm --recursive "$SOURCE_PREFIX"; fi
    gc functions delete "$FUNCTION" --gen2 --region="$REGION" --quiet
fi
if exists artifacts repositories list --location="$REGION" --filter="name=projects/$PROJECT_ID/locations/$REGION/repositories/$REPOSITORY"; then
    gc artifacts repositories describe "$REPOSITORY" --location="$REGION" --format=json | owned labels
    gc artifacts repositories delete "$REPOSITORY" --location="$REGION" --quiet
fi
if exists pubsub subscriptions list --filter="name=projects/$PROJECT_ID/subscriptions/$SUBSCRIPTION"; then
    gc pubsub subscriptions describe "$SUBSCRIPTION" --format=json | owned labels
    gc pubsub subscriptions delete "$SUBSCRIPTION" --quiet
fi
if exists pubsub topics list --filter="name=projects/$PROJECT_ID/topics/$TOPIC"; then
    gc pubsub topics describe "$TOPIC" --format=json | owned labels
    gc pubsub topics delete "$TOPIC" --quiet
fi
if exists compute firewall-rules list --filter="name=$FIREWALL"; then
    gc compute firewall-rules describe "$FIREWALL" --format=json | owned description
    gc compute firewall-rules delete "$FIREWALL" --quiet
fi
if exists compute networks subnets list --regions="$REGION" --filter="name=$SUBNET"; then
    gc compute networks subnets describe "$SUBNET" --region="$REGION" --format=json | owned description
    gc compute networks subnets delete "$SUBNET" --region="$REGION" --quiet
fi
if exists compute networks list --filter="name=$NETWORK"; then
    gc compute networks describe "$NETWORK" --format=json | owned description
    gc compute networks delete "$NETWORK" --quiet
fi
if exists storage buckets list --filter="name=$ARTIFACT_BUCKET"; then
    gc storage buckets describe "gs://$ARTIFACT_BUCKET" --format=json | owned labels
    gc storage rm --recursive "gs://$ARTIFACT_BUCKET"
fi
if exists secrets list --filter="name~'/secrets/$SECRET$'"; then
    gc secrets describe "$SECRET" --format=json | owned labels
    gc secrets delete "$SECRET" --quiet
fi
# Revoke only this workload's grants on the reused bucket, leaving objects intact.
gc storage buckets get-iam-policy "gs://$BUCKET_NAME" --format=json > "$HW5_TMP/bucket-iam.json"
python3 - "$HW5_TMP/bucket-iam.json" "$VM_SA" "$HW5_TMP/bucket-roles" <<'PY'
import json, sys
from pathlib import Path
roles = []
for b in json.load(open(sys.argv[1])).get('bindings', []):
    if 'serviceAccount:' + sys.argv[2] not in b['members']:
        continue
    if b['role'] == 'roles/storage.objectViewer' and not b.get('condition'):
        roles.append('viewer')
    if b['role'] == 'roles/storage.objectUser' and b.get('condition', {}).get('title') == 'hw5-log-object':
        roles.append('writer')
Path(sys.argv[3]).write_text('\n'.join(roles))
PY
while IFS= read -r role || [ -n "$role" ]; do
    case "$role" in
        viewer) gc storage buckets remove-iam-policy-binding "gs://$BUCKET_NAME" \
            --member="serviceAccount:$VM_SA" --role=roles/storage.objectViewer --condition=None --quiet >/dev/null ;;
        writer) gc storage buckets remove-iam-policy-binding "gs://$BUCKET_NAME" \
            --member="serviceAccount:$VM_SA" --role=roles/storage.objectUser \
            --condition="expression=resource.name == 'projects/_/buckets/$BUCKET_NAME/objects/forbidden_requests/hw5-log.txt',title=hw5-log-object" --quiet >/dev/null ;;
    esac
done < "$HW5_TMP/bucket-roles"

gc projects get-iam-policy "$PROJECT_ID" --format=json > "$HW5_TMP/project-iam.json"
python3 - "$HW5_TMP/project-iam.json" "$HW5_TMP/project-roles" <<'PY'
import json, sys
from pathlib import Path
project = 'cs-528-508021'
allowed = {
    f'serviceAccount:hw5-vm@{project}.iam.gserviceaccount.com': {'roles/cloudsql.client', 'roles/logging.logWriter', 'roles/monitoring.metricWriter'},
    f'serviceAccount:hw5-build@{project}.iam.gserviceaccount.com': {'roles/logging.logWriter', 'roles/storage.objectViewer'},
    f'serviceAccount:hw5-stop-sql@{project}.iam.gserviceaccount.com': {f'projects/{project}/roles/hw5SqlStopper'},
}
lines = []
for binding in json.load(open(sys.argv[1])).get('bindings', []):
    if binding.get('condition'):
        continue
    for member in binding['members']:
        if binding['role'] in allowed.get(member, set()):
            lines.append(member + ' ' + binding['role'])
Path(sys.argv[2]).write_text('\n'.join(lines))
PY
while read -r member role || [ -n "$member" ]; do
    if [ -n "$member" ]; then
        gc projects remove-iam-policy-binding "$PROJECT_ID" --member="$member" \
            --role="$role" --condition=None --quiet >/dev/null
    fi
done < "$HW5_TMP/project-roles"

# setup uses VM ADC, not an interactive ADC login. Earlier local phases did use
# ADC; remove that credential if present. gcloud CLI login remains available.
GCLOUD_CONFIG=$(gc info --format='value(config.paths.global_config_dir)')
if [ -f "$GCLOUD_CONFIG/application_default_credentials.json" ]; then
    gc auth application-default revoke --quiet
fi
echo 'Cleanup complete: SQL stopped, HW2 bucket/data retained, VM/static IP/function/job and deployment artifacts removed.'
echo 'Non-billable service accounts, API enablement and custom role are retained for reuse.'
echo 'SQL data/storage, SQL-assigned IP, backups, retained HW2 objects and any retained managed source objects may still be billed.'
