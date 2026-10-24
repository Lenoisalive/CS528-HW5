#!/bin/bash
# Compute Engine Debian 12 startup script for the Pub/Sub reporting VM.
set -euo pipefail
trap 'echo "HW4 forbidden startup failed at line $LINENO" >&2' ERR

export DEBIAN_FRONTEND=noninteractive
APP_DIR=/opt/hw4-forbidden
METADATA_URL=http://metadata.google.internal/computeMetadata/v1

echo "HW4 forbidden: installing system dependencies"
apt-get -o Acquire::Retries=5 update
apt-get -o Acquire::Retries=5 install -y python3 python3-venv curl ca-certificates

metadata() {
    curl --fail --silent --show-error --retry 5 --retry-delay 2 \
        --retry-connrefused --connect-timeout 5 --max-time 30 \
        -H 'Metadata-Flavor: Google' "${METADATA_URL}/$1"
}

HW4_STAGE_DIR=$(mktemp -d)
trap 'rm -rf -- "$HW4_STAGE_DIR"' EXIT
metadata instance/attributes/hw4-subscriber-py > "$HW4_STAGE_DIR/subscriber.py"
metadata instance/attributes/hw4-subscriber-requirements > "$HW4_STAGE_DIR/requirements.txt"
metadata instance/attributes/hw4-forbidden-unit > "$HW4_STAGE_DIR/hw4-forbidden.service"
HW4_PROJECT_ID=$(metadata project/project-id)
test -s "$HW4_STAGE_DIR/subscriber.py"
test -s "$HW4_STAGE_DIR/requirements.txt"
test -s "$HW4_STAGE_DIR/hw4-forbidden.service"

if ! id hw4-forbidden >/dev/null 2>&1; then
    useradd --system --user-group --home-dir "$APP_DIR" \
        --no-create-home --shell /usr/sbin/nologin hw4-forbidden
fi
install -d -m 0755 "$APP_DIR"
if systemctl is-active --quiet hw4-forbidden.service; then
    systemctl stop hw4-forbidden.service
fi
install -m 0644 "$HW4_STAGE_DIR/subscriber.py" "$APP_DIR/subscriber.py"
install -m 0644 "$HW4_STAGE_DIR/requirements.txt" "$APP_DIR/requirements.txt"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install --no-cache-dir -r "$APP_DIR/requirements.txt"

cat > /etc/hw4-forbidden.env <<EOF
PROJECT_ID=${HW4_PROJECT_ID}
GOOGLE_CLOUD_PROJECT=${HW4_PROJECT_ID}
AUTH_MODE=adc
SUBSCRIPTION_ID=forbidden-requests-sub
BUCKET_NAME=cs528hw2
LOG_OBJECT=forbidden_requests/log.txt
PYTHONUNBUFFERED=1
PYTHONDONTWRITEBYTECODE=1
EOF
CONFIG_HTTP=$(curl --silent --show-error --retry 5 --retry-delay 2 \
    --retry-connrefused --connect-timeout 5 --max-time 30 \
    -H 'Metadata-Flavor: Google' --output "$HW4_STAGE_DIR/forbidden-config.env" \
    --write-out '%{http_code}' "$METADATA_URL/instance/attributes/hw4-forbidden-config")
case "$CONFIG_HTTP" in
    200) cat "$HW4_STAGE_DIR/forbidden-config.env" >> /etc/hw4-forbidden.env ;;
    404) ;; # Preserve defaults for older manual deployment commands.
    *) echo "Unable to read forbidden configuration: HTTP $CONFIG_HTTP" >&2; exit 1 ;;
esac
chmod 0644 /etc/hw4-forbidden.env
install -m 0644 "$HW4_STAGE_DIR/hw4-forbidden.service" \
    /etc/systemd/system/hw4-forbidden.service

systemctl daemon-reload
systemctl enable hw4-forbidden.service
systemctl restart hw4-forbidden.service
echo "HW4 forbidden startup complete: systemd started the subscriber."
echo "Verify message delivery and GCS permissions with an end-to-end request."
