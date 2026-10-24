#!/bin/bash
# Run on the deployment workstation. Compatible with macOS Bash 3.2.
set -euo pipefail
trap 'printf "Deployment failed near line %s. Fix the error and rerun; resources are not deleted.\n" "$LINENO" >&2' ERR

PROJECT_ID=${PROJECT_ID:-cs-528-508021}
REGION=${REGION:-us-central1}
ZONE=${ZONE:-${REGION}-a}
NETWORK=${NETWORK:-default}
BUCKET_NAME=${BUCKET_NAME:-cs528hw2}
OBJECT_PREFIX=${OBJECT_PREFIX-pages}
LOG_OBJECT=${LOG_OBJECT:-forbidden_requests/log.txt}
TOPIC_ID=${TOPIC_ID:-forbidden-requests}
SUBSCRIPTION_ID=${SUBSCRIPTION_ID:-forbidden-requests-sub}
SERVER_VM=${SERVER_VM:-web-server-vm}
FORBIDDEN_VM=${FORBIDDEN_VM:-forbidden-service-vm}
SERVER_SA=${SERVER_SA:-hw4-web-server-sa}
FORBIDDEN_SA=${FORBIDDEN_SA:-hw4-forbidden-sa}
STATIC_IP_NAME=${STATIC_IP_NAME:-hw4-web-server-ip}
FIREWALL_NAME=${FIREWALL_NAME:-hw4-allow-web-8080}
WEB_TAG=${WEB_TAG:-hw4-web-server}
WEB_SOURCE_RANGES=${WEB_SOURCE_RANGES:-0.0.0.0/0}
UPDATE_EXISTING=${UPDATE_EXISTING:-false}
ENABLE_UNIFORM_ACCESS=${ENABLE_UNIFORM_ACCESS:-false}
WEB_ROLE=${WEB_ROLE:-hw4ObjectReader}
LOG_ROLE=${LOG_ROLE:-hw4ForbiddenLogWriter}

usage() {
    cat <<'EOF'
Usage: [PROJECT_ID=...] [BUCKET_NAME=...] bash deploy.sh

Creates/reuses APIs, service accounts, IAM, Pub/Sub, network, static IP,
firewall, and two Debian 12 VMs with startup scripts and application metadata.
The HW2 bucket must already exist. gcloud must be logged in as the deployer.

UPDATE_EXISTING=true       Update app metadata and stop/start existing VMs.
                          Default false: do not update metadata or restart them.
ENABLE_UNIFORM_ACCESS=true
                          Explicitly allow disabling bucket/object ACL access
                          in favor of uniform IAM, if not already enabled.
SERVER_MACHINE_TYPE=...   Existing VM: preserve its type unless explicitly set.
                          New VM default: f1-micro (check course requirements).
FORBIDDEN_MACHINE_TYPE=... Existing VM: preserve; new VM default: e2-micro.
REGION, ZONE, NETWORK, BUCKET_NAME, OBJECT_PREFIX, LOG_OBJECT, TOPIC_ID,
SUBSCRIPTION_ID, SERVER_VM, FORBIDDEN_VM, SERVER_SA, FORBIDDEN_SA,
STATIC_IP_NAME, FIREWALL_NAME, WEB_TAG, WEB_SOURCE_RANGES are configurable.
SERVER_SA/FORBIDDEN_SA are account IDs, not full email addresses.
No VMs, disks, buckets, addresses, or existing IAM bindings are deleted.
EOF
}

if [ "${1:-}" = --help ] || [ "${1:-}" = -h ]; then usage; exit 0; fi
if [ "$#" -ne 0 ]; then usage >&2; exit 2; fi
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
for dependency in gcloud python3; do
    command -v "$dependency" >/dev/null || fail "Install $dependency first."
done
for option in "$UPDATE_EXISTING" "$ENABLE_UNIFORM_ACCESS"; do
    case "$option" in true|false) ;; *) fail 'Boolean options must be true or false.' ;; esac
done
[ "$SERVER_VM" != "$FORBIDDEN_VM" ] || fail 'The two VM names must differ.'
[ "$SERVER_SA" != "$FORBIDDEN_SA" ] || fail 'Use separate service accounts.'
[ "$WEB_ROLE" != "$LOG_ROLE" ] || fail 'Use separate custom role IDs.'
[[ "$ZONE" == "$REGION"-* ]] || fail 'ZONE must belong to REGION.'
# Restrict values used in CEL expressions, environment files and CLI filters.
for value in "$PROJECT_ID" "$REGION" "$ZONE" "$NETWORK" "$BUCKET_NAME" \
    "$TOPIC_ID" "$SUBSCRIPTION_ID" "$SERVER_VM" "$FORBIDDEN_VM" "$SERVER_SA" \
    "$FORBIDDEN_SA" "$STATIC_IP_NAME" "$FIREWALL_NAME" "$WEB_TAG" "$WEB_ROLE" "$LOG_ROLE"; do
    [[ "$value" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]*$ ]] || fail "Invalid resource name: $value"
done
[[ "$OBJECT_PREFIX" =~ ^[a-zA-Z0-9._/-]*$ ]] || fail 'Invalid OBJECT_PREFIX.'
[[ "$LOG_OBJECT" =~ ^[a-zA-Z0-9][a-zA-Z0-9._/-]*$ ]] || fail 'Invalid LOG_OBJECT.'
python3 - "$WEB_SOURCE_RANGES" <<'PY'
import ipaddress, sys
for value in sys.argv[1].split(','):
    if ipaddress.ip_network(value).version != 4:
        raise SystemExit('WEB_SOURCE_RANGES must contain IPv4 CIDRs')
PY

DEPLOY_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$DEPLOY_ROOT"
DEPLOY_TMP=$(mktemp -d)
trap 'rm -rf -- "$DEPLOY_TMP"' EXIT
for file in deploy/startup.sh deploy/hw4-web.service service1/server.py service1/requirements.txt \
    deploy/forbidden-startup.sh deploy/hw4-forbidden.service service2/subscriber.py service2/requirements.txt \
    deploy/validate_resource.py; do
    [ -s "$file" ] || fail "Missing deployment file: $file"
done
# Stage under simple temporary names so metadata paths contain no commas.
cp deploy/startup.sh "$DEPLOY_TMP/web-startup.sh"
cp deploy/hw4-web.service "$DEPLOY_TMP/web.service"
cp service1/server.py "$DEPLOY_TMP/server.py"
cp service1/requirements.txt "$DEPLOY_TMP/web-requirements.txt"
cp deploy/forbidden-startup.sh "$DEPLOY_TMP/forbidden-startup.sh"
cp deploy/hw4-forbidden.service "$DEPLOY_TMP/forbidden.service"
cp service2/subscriber.py "$DEPLOY_TMP/subscriber.py"
cp service2/requirements.txt "$DEPLOY_TMP/subscriber-requirements.txt"

retry() {
    local attempt
    for attempt in 1 2 3 4 5 6; do
        if "$@"; then return 0; fi
        [ "$attempt" -lt 6 ] || return 1
        printf 'Retrying after %s seconds (IAM propagation may take time)...\n' "$((attempt * 5))" >&2
        sleep "$((attempt * 5))"
    done
}

printf 'Deployment project: %s; zone: %s; update existing VMs: %s\n' "$PROJECT_ID" "$ZONE" "$UPDATE_EXISTING"
ACTIVE_ACCOUNT=$(gcloud auth list --filter=status:ACTIVE --format='value(account)')
[ -n "$ACTIVE_ACCOUNT" ] || fail 'Run gcloud auth login on your workstation first.'
gcloud projects describe "$PROJECT_ID" --format='value(projectId)' >/dev/null
# Inspect the existing HW2 bucket before provisioning resources.
gcloud storage buckets describe "gs://$BUCKET_NAME" --raw --format=json > "$DEPLOY_TMP/bucket.json"
BUCKET_PROJECT_NUMBER=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["projectNumber"])' "$DEPLOY_TMP/bucket.json")
BUCKET_PROJECT_ID=$(gcloud projects describe "$BUCKET_PROJECT_NUMBER" --format='value(projectId)')
[ -n "$BUCKET_PROJECT_ID" ] || fail 'Could not identify the bucket owner project.'
UNIFORM=$(python3 -c 'import json,sys; print(str(json.load(open(sys.argv[1])).get("iamConfiguration", {}).get("uniformBucketLevelAccess", {}).get("enabled", False)).lower())' "$DEPLOY_TMP/bucket.json")
if [ "$UNIFORM" != true ] && [ "$ENABLE_UNIFORM_ACCESS" != true ]; then
    fail 'Bucket uses ACLs. Review their usage, then explicitly set ENABLE_UNIFORM_ACCESS=true to migrate to uniform IAM.'
fi

gcloud services enable compute.googleapis.com iam.googleapis.com pubsub.googleapis.com \
    storage.googleapis.com logging.googleapis.com --project="$PROJECT_ID" --quiet
if [ "$BUCKET_PROJECT_ID" != "$PROJECT_ID" ]; then
    gcloud services enable iam.googleapis.com --project="$BUCKET_PROJECT_ID" --quiet
fi
gcloud compute zones describe "$ZONE" --project="$PROJECT_ID" --format='value(name)' >/dev/null

SERVER_EMAIL="${SERVER_SA}@${PROJECT_ID}.iam.gserviceaccount.com"
FORBIDDEN_EMAIL="${FORBIDDEN_SA}@${PROJECT_ID}.iam.gserviceaccount.com"
ensure_account() {
    local id=$1 email=$2 found disabled
    found=$(gcloud iam service-accounts list --project="$PROJECT_ID" --filter="email=$email" --format='value(email)')
    if [ -z "$found" ]; then
        gcloud iam service-accounts create "$id" --project="$PROJECT_ID" --display-name="$id" --quiet
    fi
    disabled=$(retry gcloud iam service-accounts describe "$email" --project="$PROJECT_ID" --format='value(disabled)')
    case "$disabled" in True|true) fail "Service account is disabled: $email" ;; esac
}
ensure_account "$SERVER_SA" "$SERVER_EMAIL"
ensure_account "$FORBIDDEN_SA" "$FORBIDDEN_EMAIL"

ensure_role() {
    local id=$1 permissions=$2 found
    found=$(gcloud iam roles list --project="$BUCKET_PROJECT_ID" \
        --filter="name=projects/$BUCKET_PROJECT_ID/roles/$id" --format='value(name)')
    if [ -z "$found" ]; then
        gcloud iam roles create "$id" --project="$BUCKET_PROJECT_ID" \
            --title="$id" --permissions="$permissions" --stage=GA --quiet
    else
        gcloud iam roles describe "$id" --project="$BUCKET_PROJECT_ID" --format=json > "$DEPLOY_TMP/role.json"
        python3 deploy/validate_resource.py role "$DEPLOY_TMP/role.json" "$permissions"
    fi
}
ensure_role "$WEB_ROLE" storage.objects.get
ensure_role "$LOG_ROLE" storage.objects.get,storage.objects.create,storage.objects.delete

if [ "$UNIFORM" != true ]; then
    gcloud storage buckets update "gs://$BUCKET_NAME" --uniform-bucket-level-access --quiet
fi
retry gcloud storage buckets add-iam-policy-binding "gs://$BUCKET_NAME" \
    --member="serviceAccount:$SERVER_EMAIL" --role="projects/$BUCKET_PROJECT_ID/roles/$WEB_ROLE" \
    --condition=None --quiet >/dev/null
retry gcloud storage buckets add-iam-policy-binding "gs://$BUCKET_NAME" \
    --member="serviceAccount:$FORBIDDEN_EMAIL" --role="projects/$BUCKET_PROJECT_ID/roles/$LOG_ROLE" \
    --condition="expression=resource.name == 'projects/_/buckets/$BUCKET_NAME/objects/$LOG_OBJECT',title=hw4_forbidden_log_only" \
    --quiet >/dev/null
retry gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$SERVER_EMAIL" --role=roles/logging.logWriter --condition=None --quiet >/dev/null

FOUND_TOPIC=$(gcloud pubsub topics list --project="$PROJECT_ID" \
    --filter="name=projects/$PROJECT_ID/topics/$TOPIC_ID" --format='value(name)')
if [ -z "$FOUND_TOPIC" ]; then
    gcloud pubsub topics create "$TOPIC_ID" --project="$PROJECT_ID" --quiet
fi
FOUND_SUB=$(gcloud pubsub subscriptions list --project="$PROJECT_ID" \
    --filter="name=projects/$PROJECT_ID/subscriptions/$SUBSCRIPTION_ID" --format='value(name)')
if [ -z "$FOUND_SUB" ]; then
    gcloud pubsub subscriptions create "$SUBSCRIPTION_ID" --topic="$TOPIC_ID" --project="$PROJECT_ID" --quiet
fi
gcloud pubsub subscriptions describe "$SUBSCRIPTION_ID" --project="$PROJECT_ID" --format=json > "$DEPLOY_TMP/subscription.json"
python3 deploy/validate_resource.py subscription "$DEPLOY_TMP/subscription.json" "$PROJECT_ID" "$TOPIC_ID"
retry gcloud pubsub topics add-iam-policy-binding "$TOPIC_ID" --project="$PROJECT_ID" \
    --member="serviceAccount:$SERVER_EMAIL" --role=roles/pubsub.publisher --quiet >/dev/null
retry gcloud pubsub subscriptions add-iam-policy-binding "$SUBSCRIPTION_ID" --project="$PROJECT_ID" \
    --member="serviceAccount:$FORBIDDEN_EMAIL" --role=roles/pubsub.subscriber --quiet >/dev/null

FOUND_NETWORK=$(gcloud compute networks list --project="$PROJECT_ID" --filter="name=$NETWORK" --format='value(name)')
if [ -z "$FOUND_NETWORK" ]; then
    gcloud compute networks create "$NETWORK" --project="$PROJECT_ID" --subnet-mode=auto --quiet
fi
AUTO_NETWORK=$(gcloud compute networks describe "$NETWORK" --project="$PROJECT_ID" --format='value(autoCreateSubnetworks)')
case "$AUTO_NETWORK" in True|true) ;; *) fail 'This script requires an auto-mode VPC; custom subnets are not configured automatically.' ;; esac

FOUND_ADDRESS=$(gcloud compute addresses list --project="$PROJECT_ID" --filter="name=$STATIC_IP_NAME" --format='value(region.basename())')
if [ -z "$FOUND_ADDRESS" ]; then
    gcloud compute addresses create "$STATIC_IP_NAME" --project="$PROJECT_ID" --region="$REGION" --network-tier=PREMIUM --quiet
else
    [ "$FOUND_ADDRESS" = "$REGION" ] || fail 'An address with this name exists in another or multiple regions.'
fi
gcloud compute addresses describe "$STATIC_IP_NAME" --project="$PROJECT_ID" --region="$REGION" --format=json > "$DEPLOY_TMP/address.json"
python3 deploy/validate_resource.py address "$DEPLOY_TMP/address.json" "$REGION"
STATIC_IP=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["address"])' "$DEPLOY_TMP/address.json")
python3 - "$DEPLOY_TMP/address.json" "$PROJECT_ID" "$ZONE" "$SERVER_VM" <<'PY'
import json, sys
path, project, zone, name = sys.argv[1:]
for user in json.load(open(path)).get('users', []):
    if not user.endswith(f'/projects/{project}/zones/{zone}/instances/{name}'):
        raise SystemExit('Static IP is in use by another resource; choose another STATIC_IP_NAME.')
PY

FOUND_FIREWALL=$(gcloud compute firewall-rules list --project="$PROJECT_ID" --filter="name=$FIREWALL_NAME" --format='value(name)')
if [ -z "$FOUND_FIREWALL" ]; then
    gcloud compute firewall-rules create "$FIREWALL_NAME" --project="$PROJECT_ID" \
        --network="$NETWORK" --direction=INGRESS --priority=1000 --action=ALLOW \
        --rules=tcp:8080 --source-ranges="$WEB_SOURCE_RANGES" --target-tags="$WEB_TAG" --quiet
fi
gcloud compute firewall-rules describe "$FIREWALL_NAME" --project="$PROJECT_ID" --format=json > "$DEPLOY_TMP/firewall.json"
python3 deploy/validate_resource.py firewall "$DEPLOY_TMP/firewall.json" "$PROJECT_ID" "$NETWORK" "$WEB_TAG" "$WEB_SOURCE_RANGES"

cat > "$DEPLOY_TMP/web.env" <<EOF
BUCKET_NAME=$BUCKET_NAME
OBJECT_PREFIX=$OBJECT_PREFIX
PUBSUB_TOPIC=projects/$PROJECT_ID/topics/$TOPIC_ID
EOF
cat > "$DEPLOY_TMP/forbidden.env" <<EOF
BUCKET_NAME=$BUCKET_NAME
LOG_OBJECT=$LOG_OBJECT
SUBSCRIPTION_ID=$SUBSCRIPTION_ID
EOF
WEB_METADATA="startup-script=$DEPLOY_TMP/web-startup.sh,hw4-server-py=$DEPLOY_TMP/server.py,hw4-requirements=$DEPLOY_TMP/web-requirements.txt,hw4-service-unit=$DEPLOY_TMP/web.service,hw4-web-config=$DEPLOY_TMP/web.env"
FORBIDDEN_METADATA="startup-script=$DEPLOY_TMP/forbidden-startup.sh,hw4-subscriber-py=$DEPLOY_TMP/subscriber.py,hw4-subscriber-requirements=$DEPLOY_TMP/subscriber-requirements.txt,hw4-forbidden-unit=$DEPLOY_TMP/forbidden.service,hw4-forbidden-config=$DEPLOY_TMP/forbidden.env"

deploy_vm() {
    local name=$1 email=$2 requested_machine=$3 default_machine=$4 metadata=$5 address=$6 tag=$7
    local found_zone machine state
    local extra=()
    found_zone=$(gcloud compute instances list --project="$PROJECT_ID" --filter="name=$name" --format='value(zone.basename())')
    if [ -n "$found_zone" ]; then
        [ "$found_zone" = "$ZONE" ] || fail "VM $name exists in another or multiple zones."
        gcloud compute instances describe "$name" --project="$PROJECT_ID" --zone="$ZONE" --format=json > "$DEPLOY_TMP/vm.json"
        machine=$requested_machine
        if [ -z "$machine" ]; then
            machine=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["machineType"].rsplit("/",1)[-1])' "$DEPLOY_TMP/vm.json")
        fi
        python3 deploy/validate_resource.py vm "$DEPLOY_TMP/vm.json" "$PROJECT_ID" "$NETWORK" "$email" "$machine" "$address" "$tag"
        if [ "$UPDATE_EXISTING" != true ]; then
            printf 'Reusing %s (%s); existing application metadata and power state are unchanged.\n' "$name" "$machine"
            return 0
        fi
        gcloud compute instances add-metadata "$name" --project="$PROJECT_ID" --zone="$ZONE" --metadata-from-file="$metadata" --quiet
        state=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["status"])' "$DEPLOY_TMP/vm.json")
        if [ "$state" = RUNNING ]; then
            gcloud compute instances stop "$name" --project="$PROJECT_ID" --zone="$ZONE" --quiet
        fi
        gcloud compute instances start "$name" --project="$PROJECT_ID" --zone="$ZONE" --quiet
    else
        machine=${requested_machine:-$default_machine}
        gcloud compute machine-types describe "$machine" --project="$PROJECT_ID" --zone="$ZONE" --format='value(name)' >/dev/null
        if [ "$address" != - ]; then extra+=(--address="$address" --network-tier=PREMIUM); fi
        if [ "$tag" != - ]; then extra+=(--tags="$tag"); fi
        # Bash 3.2 nounset treats empty arrays as unset; use separate branches.
        if [ "$address" != - ] || [ "$tag" != - ]; then
            retry gcloud compute instances create "$name" --project="$PROJECT_ID" --zone="$ZONE" \
                --machine-type="$machine" --image-family=debian-12 --image-project=debian-cloud \
                --boot-disk-size=10GB --boot-disk-type=pd-standard --network="$NETWORK" \
                --service-account="$email" --scopes=cloud-platform --metadata-from-file="$metadata" "${extra[@]}" --quiet
        else
            retry gcloud compute instances create "$name" --project="$PROJECT_ID" --zone="$ZONE" \
                --machine-type="$machine" --image-family=debian-12 --image-project=debian-cloud \
                --boot-disk-size=10GB --boot-disk-type=pd-standard --network="$NETWORK" \
                --service-account="$email" --scopes=cloud-platform --metadata-from-file="$metadata" --quiet
        fi
    fi
}

# Subscription and consumer are provisioned before exposing a new producer VM.
deploy_vm "$FORBIDDEN_VM" "$FORBIDDEN_EMAIL" "${FORBIDDEN_MACHINE_TYPE:-}" e2-micro "$FORBIDDEN_METADATA" - -
deploy_vm "$SERVER_VM" "$SERVER_EMAIL" "${SERVER_MACHINE_TYPE:-}" f1-micro "$WEB_METADATA" "$STATIC_IP" "$WEB_TAG"

printf '\nInfrastructure commands completed. New/restarted VMs install and start services automatically.\n'
printf 'Startup scripts may still be installing dependencies; this is not an end-to-end test result.\n'
printf 'Web URL: http://%s:8080/10.html (replace filename with an existing object)\n' "$STATIC_IP"
printf 'Bucket owner project: %s; VM/PubSub project: %s\n' "$BUCKET_PROJECT_ID" "$PROJECT_ID"
printf 'Read startup logs: gcloud compute instances get-serial-port-output %s --project=%s --zone=%s --port=1\n' "$SERVER_VM" "$PROJECT_ID" "$ZONE"
printf 'Read startup logs: gcloud compute instances get-serial-port-output %s --project=%s --zone=%s --port=1\n' "$FORBIDDEN_VM" "$PROJECT_ID" "$ZONE"
printf 'Reused VMs receive current code only when UPDATE_EXISTING=true.\n'
