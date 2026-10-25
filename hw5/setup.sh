#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source deploy/hw5-common.sh
command -v gcloud >/dev/null || die 'Install Google Cloud CLI first.'
command -v python3 >/dev/null || die 'Install Python 3 first.'
umask 077
HW5_TMP=$(mktemp -d)
trap 'rm -rf -- "$HW5_TMP"' EXIT
trap 'echo "Setup failed at line $LINENO. Resources may already exist; rerun after resolving the error or use cleanup.sh." >&2' ERR

gc services enable compute.googleapis.com sqladmin.googleapis.com \
    storage.googleapis.com pubsub.googleapis.com iam.googleapis.com \
    iamcredentials.googleapis.com secretmanager.googleapis.com logging.googleapis.com \
    cloudfunctions.googleapis.com run.googleapis.com cloudbuild.googleapis.com \
    artifactregistry.googleapis.com cloudscheduler.googleapis.com

# Enable completion and first API availability can be separated by propagation.
read_eventually scheduler jobs list --location="$REGION" --limit=1 --format=json >/dev/null

# Pause an existing job BEFORE starting SQL or deploying anything. Restore its
# previous state on successful completion; on failure it stays paused explicitly.
RESUME_JOB=true
if exists scheduler jobs list --location="$REGION" --filter="name=projects/$PROJECT_ID/locations/$REGION/jobs/$JOB"; then
    gc scheduler jobs describe "$JOB" --location="$REGION" --format=json | owned description
    JOB_STATE=$(gc scheduler jobs describe "$JOB" --location="$REGION" --format='value(state)')
    if [ "$JOB_STATE" = PAUSED ]; then RESUME_JOB=false; else
        set_scheduler_state PAUSED
    fi
fi

for name in hw5-vm hw5-stop-sql hw5-scheduler hw5-build; do
    email="$name@$PROJECT_ID.iam.gserviceaccount.com"
    if ! exists iam service-accounts list --filter="email=$email"; then
        gc iam service-accounts create "$name" --display-name="$OWNER"
    fi
    read_eventually iam service-accounts describe "$email" --format=json > "$HW5_TMP/account.json"
    owned account < "$HW5_TMP/account.json"
done
project_role "$VM_SA" roles/cloudsql.client
project_role "$VM_SA" roles/logging.logWriter
project_role "$BUILD_SA" roles/logging.logWriter
# Functions may copy source to a service-managed bucket during a build.
project_role "$BUILD_SA" roles/storage.objectViewer

if ! exists iam roles list --filter="name=projects/$PROJECT_ID/roles/hw5SqlStopper"; then
    gc iam roles create hw5SqlStopper --title="$OWNER" --stage=GA \
        --permissions=cloudsql.instances.get,cloudsql.instances.update
fi
gc iam roles describe hw5SqlStopper --format=json > "$HW5_TMP/role.json"
python3 - "$HW5_TMP/role.json" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
assert r.get('title') == 'cs528-hw5' and not r.get('deleted'), 'Conflicting custom role'
assert set(r['includedPermissions']) == {'cloudsql.instances.get', 'cloudsql.instances.update'}, 'Unexpected SQL role permissions'
PY
project_role "$FUNCTION_SA" "projects/$PROJECT_ID/roles/hw5SqlStopper"

ensure_sql() {
    if ! exists sql instances list --filter="name=$SQL_INSTANCE"; then
        gc sql instances create "$SQL_INSTANCE" --database-version=POSTGRES_16 \
            --edition=ENTERPRISE --tier=db-f1-micro --region="$REGION" \
            --availability-type=zonal --storage-type=SSD --storage-size=10 --assign-ip
    fi
    gc sql instances describe "$SQL_INSTANCE" --format=json > "$HW5_TMP/sql.json"
    wait_for_sql_operations
    python3 - "$HW5_TMP/sql.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert d['databaseVersion'].startswith('POSTGRES_'), 'Existing instance is not PostgreSQL'
assert d['region'] == 'us-central1', 'Existing SQL region differs'
assert any(i['type'] == 'PRIMARY' for i in d.get('ipAddresses', [])), 'Public IP is required for this deployment'
PY
    policy=$(gc sql instances describe "$SQL_INSTANCE" --format='value(settings.activationPolicy)')
    if [ "$policy" != ALWAYS ]; then
        set_sql_activation ALWAYS
    fi
    if ! exists sql databases list --instance="$SQL_INSTANCE" --filter="name=$DB_NAME"; then
        gc sql databases create "$DB_NAME" --instance="$SQL_INSTANCE"
    fi
    # Secret is stable across repeated setup. Cleanup deletes it; a later setup
    # resets the SAME SQL role's password so its table ownership survives.
    if ! exists secrets list --filter="name~'/secrets/$SECRET$'"; then
        python3 -c 'import secrets; print(secrets.token_urlsafe(36), end="")' > "$HW5_TMP/password"
        gc secrets create "$SECRET" --replication-policy=automatic \
            --labels="managed-by=$OWNER" --data-file="$HW5_TMP/password"
    fi
    gc secrets describe "$SECRET" --format=json | owned labels
    gc secrets versions access latest --secret="$SECRET" --out-file="$HW5_TMP/password"
    # Pass the password through a private flags file, not a process-list argument.
    python3 - "$HW5_TMP/password" "$HW5_TMP/password.json" <<'PY'
import json, sys
from pathlib import Path
password = Path(sys.argv[1]).read_text()
assert password and all(c.isalnum() or c in '-_' for c in password), 'Unexpected secret format'
Path(sys.argv[2]).write_text(json.dumps({'--password': password}))
PY
    if exists sql users list --instance="$SQL_INSTANCE" --filter="name=$DB_USER"; then
        USER_ACTION=set-password
    else
        USER_ACTION=create
    fi
    # gcloud argument errors can echo expanded flags-file values. Capture output
    # privately and redact the password before forwarding any failure to tee/logs.
    if ! gc sql users "$USER_ACTION" "$DB_USER" --instance="$SQL_INSTANCE" \
        --flags-file="$HW5_TMP/password.json" > "$HW5_TMP/sql-user-output" 2>&1; then
        python3 - "$HW5_TMP/password" "$HW5_TMP/sql-user-output" <<'PY'
from pathlib import Path
import sys
password = Path(sys.argv[1]).read_text()
message = Path(sys.argv[2]).read_text()
print(message.replace(password, '[REDACTED]'), file=sys.stderr)
PY
        die 'SQL user configuration failed; password redacted from diagnostics.'
    fi
    echo 'SQL user configured using the managed secret.'
    gc secrets add-iam-policy-binding "$SECRET" --member="serviceAccount:$VM_SA" \
        --role=roles/secretmanager.secretAccessor --condition=None --quiet >/dev/null
    # Tables are checked/created by setup_schema.py in the VM's schema systemd
    # unit, on initial installation AND every restart. No local ADC is required.
}
ensure_sql
CONNECTION_NAME=$(gc sql instances describe "$SQL_INSTANCE" --format='value(connectionName)')

# Validate the reused HW2 bucket exists; never create or delete it here.
gc storage buckets describe "gs://$BUCKET_NAME" >/dev/null
gc storage buckets add-iam-policy-binding "gs://$BUCKET_NAME" \
    --member="serviceAccount:$VM_SA" --role=roles/storage.objectViewer \
    --condition=None --quiet >/dev/null
# Limit subscriber writes to its own object; retain the unmodified app code.
LOG_CONDITION="expression=resource.name == 'projects/_/buckets/$BUCKET_NAME/objects/forbidden_requests/hw5-log.txt',title=hw5-log-object"
gc storage buckets add-iam-policy-binding "gs://$BUCKET_NAME" \
    --member="serviceAccount:$VM_SA" --role=roles/storage.objectUser \
    --condition="$LOG_CONDITION" --quiet >/dev/null

if ! exists storage buckets list --filter="name=$ARTIFACT_BUCKET"; then
    gc storage buckets create "gs://$ARTIFACT_BUCKET" --location="$REGION" \
        --uniform-bucket-level-access --public-access-prevention
    gc storage buckets update "gs://$ARTIFACT_BUCKET" --update-labels="managed-by=$OWNER" \
        --clear-soft-delete
fi
gc storage buckets describe "gs://$ARTIFACT_BUCKET" --format=json | owned labels
gc storage buckets add-iam-policy-binding "gs://$ARTIFACT_BUCKET" \
    --member="serviceAccount:$VM_SA" --role=roles/storage.objectViewer \
    --condition=None --quiet >/dev/null

if ! exists pubsub topics list --filter="name=projects/$PROJECT_ID/topics/$TOPIC"; then
    gc pubsub topics create "$TOPIC" --labels="managed-by=$OWNER"
fi
gc pubsub topics describe "$TOPIC" --format=json | owned labels
if ! exists pubsub subscriptions list --filter="name=projects/$PROJECT_ID/subscriptions/$SUBSCRIPTION"; then
    gc pubsub subscriptions create "$SUBSCRIPTION" --topic="$TOPIC" --labels="managed-by=$OWNER"
fi
gc pubsub subscriptions describe "$SUBSCRIPTION" --format=json | owned labels
SUB_TOPIC=$(gc pubsub subscriptions describe "$SUBSCRIPTION" --format='value(topic)')
[ "$SUB_TOPIC" = "projects/$PROJECT_ID/topics/$TOPIC" ] || die 'Subscription points at another topic.'
gc pubsub topics add-iam-policy-binding "$TOPIC" --member="serviceAccount:$VM_SA" --role=roles/pubsub.publisher --quiet >/dev/null
gc pubsub subscriptions add-iam-policy-binding "$SUBSCRIPTION" --member="serviceAccount:$VM_SA" --role=roles/pubsub.subscriber --quiet >/dev/null

if ! exists compute networks list --filter="name=$NETWORK"; then
    gc compute networks create "$NETWORK" --subnet-mode=custom --description="$OWNER"
fi
gc compute networks describe "$NETWORK" --format=json | owned description
if ! exists compute networks subnets list --regions="$REGION" --filter="name=$SUBNET"; then
    gc compute networks subnets create "$SUBNET" --region="$REGION" --network="$NETWORK" \
        --range=10.128.0.0/24 --description="$OWNER"
fi
gc compute networks subnets describe "$SUBNET" --region="$REGION" --format=json | owned description
if ! exists compute addresses list --filter="name=$ADDRESS"; then
    gc compute addresses create "$ADDRESS" --region="$REGION" --network-tier=PREMIUM --description="$OWNER"
fi
gc compute addresses describe "$ADDRESS" --region="$REGION" --format=json | owned description
STATIC_IP=$(gc compute addresses describe "$ADDRESS" --region="$REGION" --format='value(address)')
if ! exists compute firewall-rules list --filter="name=$FIREWALL"; then
    gc compute firewall-rules create "$FIREWALL" --network="$NETWORK" \
        --direction=INGRESS --action=ALLOW --rules=tcp:8080 \
        --source-ranges=0.0.0.0/0 --target-tags=hw5-web --description="$OWNER"
fi
gc compute firewall-rules describe "$FIREWALL" --format=json | owned description

HASH=$(python3 deploy/package_app.py "$HW5_TMP/app.tar.gz")
gc storage cp "$HW5_TMP/app.tar.gz" "gs://$ARTIFACT_BUCKET/app-$HASH.tar.gz"
python3 - "$HW5_TMP/config.json" "$HASH" "$CONNECTION_NAME" "${HW5_EXPERIMENT_ID:-hw5-deployment}" <<'PY'
import json, sys
from pathlib import Path
path, sha, connection, experiment = sys.argv[1:]
Path(path).write_text(json.dumps({
    'project': 'cs-528-508021', 'artifact_bucket': 'cs-528-508021-hw5-deploy',
    'artifact_object': f'app-{sha}.tar.gz', 'sha256': sha,
    'secret': 'hw5-db-password', 'connection_name': connection,
    'db_name': 'hw5', 'db_user': 'hw5_app', 'bucket': 'cs528hw2',
    'object_prefix': 'pages', 'topic': 'projects/cs-528-508021/topics/hw5-forbidden-requests',
    'subscription': 'hw5-forbidden-sub', 'experiment_id': experiment,
}))
PY
METADATA="startup-script=startup.sh,hw5-bootstrap=deploy/vm_bootstrap.py,hw5-config=$HW5_TMP/config.json"
if exists compute instances list --filter="name=$VM"; then
    gc compute instances describe "$VM" --zone="$ZONE" --format=json > "$HW5_TMP/vm.json"
    owned labels < "$HW5_TMP/vm.json"
    python3 - "$HW5_TMP/vm.json" "$HASH" "$VM_SA" "$STATIC_IP" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
m = {i['key']: i['value'] for i in d.get('metadata', {}).get('items', [])}
assert json.loads(m['hw5-config'])['sha256'] == sys.argv[2], 'VM has different application code. Export logs, then cleanup/setup to deploy this revision.'
assert d['serviceAccounts'][0]['email'] == sys.argv[3], 'VM identity mismatch'
assert d['networkInterfaces'][0]['accessConfigs'][0]['natIP'] == sys.argv[4], 'VM static IP mismatch'
assert d['status'] in ('RUNNING', 'TERMINATED'), 'VM is transitioning; retry later'
PY
    gc compute instances add-metadata "$VM" --zone="$ZONE" --metadata-from-file="$METADATA"
    STATE=$(gc compute instances describe "$VM" --zone="$ZONE" --format='value(status)')
    if [ "$STATE" = RUNNING ]; then gc compute instances stop "$VM" --zone="$ZONE" --quiet; fi
    gc compute instances start "$VM" --zone="$ZONE" --quiet
else
    gc compute instances create "$VM" --zone="$ZONE" --machine-type=e2-small \
        --image-family=debian-12 --image-project=debian-cloud --boot-disk-size=10GB \
        --boot-disk-type=pd-standard --network="$NETWORK" --subnet="$SUBNET" \
        --address="$STATIC_IP" --network-tier=PREMIUM --tags=hw5-web \
        --service-account="$VM_SA" --scopes=cloud-platform --labels="managed-by=$OWNER" \
        --metadata-from-file="$METADATA"
fi

if ! exists artifacts repositories list --location="$REGION" --filter="name=projects/$PROJECT_ID/locations/$REGION/repositories/$REPOSITORY"; then
    gc artifacts repositories create "$REPOSITORY" --location="$REGION" \
        --repository-format=docker --labels="managed-by=$OWNER"
fi
gc artifacts repositories describe "$REPOSITORY" --location="$REGION" --format=json | owned labels
gc artifacts repositories add-iam-policy-binding "$REPOSITORY" --location="$REGION" \
    --member="serviceAccount:$BUILD_SA" --role=roles/artifactregistry.writer --quiet >/dev/null
if exists functions list --regions="$REGION" --filter="name=projects/$PROJECT_ID/locations/$REGION/functions/$FUNCTION"; then
    gc functions describe "$FUNCTION" --gen2 --region="$REGION" --format=json | owned labels
fi
gc functions deploy "$FUNCTION" --gen2 --region="$REGION" --runtime=python312 \
    --source=cloud_function --stage-bucket="$ARTIFACT_BUCKET" \
    --entry-point=stop_database --trigger-http --no-allow-unauthenticated \
    --service-account="$FUNCTION_SA" \
    --build-service-account="projects/$PROJECT_ID/serviceAccounts/$BUILD_SA" \
    --docker-repository="projects/$PROJECT_ID/locations/$REGION/repositories/$REPOSITORY" \
    --set-env-vars="TARGET_PROJECT=$PROJECT_ID,SQL_INSTANCE=$SQL_INSTANCE" \
    --update-labels="managed-by=$OWNER" --memory=256Mi --timeout=300s \
    --min-instances=0 --max-instances=1 --concurrency=1 --quiet
FUNCTION_URL=$(gc functions describe "$FUNCTION" --gen2 --region="$REGION" --format='value(serviceConfig.uri)')
RUN_SERVICE=$(gc functions describe "$FUNCTION" --gen2 --region="$REGION" --format='value(serviceConfig.service)')
gc run services add-iam-policy-binding "${RUN_SERVICE##*/}" --region="$REGION" \
    --member="serviceAccount:$SCHEDULER_SA" --role=roles/run.invoker --quiet >/dev/null
PROJECT_NUMBER=$(gc projects describe "$PROJECT_ID" --format='value(projectNumber)')
# Preserve the Google-managed Scheduler service agent's token minting role.
gc beta services identity create --service=cloudscheduler.googleapis.com >/dev/null
project_role "service-$PROJECT_NUMBER@gcp-sa-cloudscheduler.iam.gserviceaccount.com" roles/cloudscheduler.serviceAgent
if exists scheduler jobs list --location="$REGION" --filter="name=projects/$PROJECT_ID/locations/$REGION/jobs/$JOB"; then
    JOB_ACTION=update
else
    JOB_ACTION=create
fi
gc scheduler jobs "$JOB_ACTION" http "$JOB" --location="$REGION" \
    --schedule='0 * * * *' --time-zone=Etc/UTC --description="$OWNER" \
    --uri="$FUNCTION_URL" --http-method=POST \
    --oidc-service-account-email="$SCHEDULER_SA" --oidc-token-audience="$FUNCTION_URL" \
    --attempt-deadline=320s --max-retry-attempts=3 --min-backoff=30s --max-backoff=120s
# New jobs start enabled: pause during final readiness verification as well.
JOB_STATE=$(gc scheduler jobs describe "$JOB" --location="$REGION" --format='value(state)')
if [ "$JOB_STATE" != PAUSED ]; then set_scheduler_state PAUSED; fi

# TCP readiness is not enough: this intentional invalid request must return 400
# after its database transaction, not 503. Count it as a deployment probe.
READY=false
for attempt in {1..90}; do
    CODE=$(curl --silent --output "$HW5_TMP/probe.json" --write-out '%{http_code}' \
        --connect-timeout 5 --max-time 10 "http://$STATIC_IP:8080/" || true)
    if [ "$CODE" = 400 ]; then READY=true; break; fi
    sleep 5
done
[ "$READY" = true ] || die "VM/database not ready. Inspect serial logs for $VM. Scheduler remains paused."
if [ "$RESUME_JOB" = true ]; then set_scheduler_state ENABLED; fi
printf 'Setup complete. Web URL: http://%s:8080/10.html\n' "$STATIC_IP"
printf 'SQL connection: %s\n' "$CONNECTION_NAME"
printf 'Scheduler restored to enabled=%s. Pause it before active work or load tests.\n' "$RESUME_JOB"
printf 'One readiness request was logged. Use a new EXPERIMENT_ID for measurements.\n'
