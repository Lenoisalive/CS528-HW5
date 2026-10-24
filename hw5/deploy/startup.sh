#!/bin/bash
# Compute Engine Debian 12 startup script; runs as root via the guest agent.
set -euo pipefail
trap 'echo "HW4 startup failed at line $LINENO" >&2' ERR

export DEBIAN_FRONTEND=noninteractive
APP_DIR=/opt/hw4-web-server
METADATA_URL=http://metadata.google.internal/computeMetadata/v1

echo "HW4: installing system dependencies"
apt-get -o Acquire::Retries=5 update
apt-get -o Acquire::Retries=5 install -y python3 python3-venv curl ca-certificates

metadata() {
    curl --fail --silent --show-error --retry 5 --retry-delay 2 \
        --retry-connrefused --connect-timeout 5 --max-time 30 \
        -H 'Metadata-Flavor: Google' "${METADATA_URL}/$1"
}

HW4_STAGE_DIR=$(mktemp -d)
trap 'rm -rf -- "$HW4_STAGE_DIR"' EXIT
metadata instance/attributes/hw4-server-py > "$HW4_STAGE_DIR/server.py"
metadata instance/attributes/hw4-requirements > "$HW4_STAGE_DIR/requirements.txt"
metadata instance/attributes/hw4-service-unit > "$HW4_STAGE_DIR/hw4-web.service"
HW4_PROJECT_ID=$(metadata project/project-id)
HW4_INSTANCE_ID=$(metadata instance/id)
HW4_INSTANCE_ZONE_PATH=$(metadata instance/zone)
HW4_INSTANCE_ZONE=${HW4_INSTANCE_ZONE_PATH##*/}
test -s "$HW4_STAGE_DIR/server.py"
test -s "$HW4_STAGE_DIR/requirements.txt"
test -s "$HW4_STAGE_DIR/hw4-web.service"

# This local OS user is separate from the Google Cloud service account.
if ! id hw4-web >/dev/null 2>&1; then
    useradd --system --user-group --home-dir "$APP_DIR" \
        --no-create-home --shell /usr/sbin/nologin hw4-web
fi
install -d -m 0755 "$APP_DIR"

# Stop any previous deployment before replacing its files/dependencies.
if systemctl is-active --quiet hw4-web.service; then
    systemctl stop hw4-web.service
fi
install -m 0644 "$HW4_STAGE_DIR/server.py" "$APP_DIR/server.py"
install -m 0644 "$HW4_STAGE_DIR/requirements.txt" "$APP_DIR/requirements.txt"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install --no-cache-dir \
    -r "$APP_DIR/requirements.txt" 'gunicorn>=23,<24'

cat > /etc/hw4-web.env <<EOF
GOOGLE_CLOUD_PROJECT=${HW4_PROJECT_ID}
CLOUD_LOGGING_ENABLED=true
CLOUD_LOG_NAME=hw4-web-server
HW4_INSTANCE_ID=${HW4_INSTANCE_ID}
HW4_INSTANCE_ZONE=${HW4_INSTANCE_ZONE}
BUCKET_NAME=cs528hw2
OBJECT_PREFIX=pages
PUBSUB_TOPIC=projects/${HW4_PROJECT_ID}/topics/forbidden-requests
PYTHONUNBUFFERED=1
PYTHONDONTWRITEBYTECODE=1
EOF
# deploy.sh supplies resource names through an optional metadata environment file.
# Older manual deployment commands omit this attribute and retain the defaults.
CONFIG_HTTP=$(curl --silent --show-error --retry 5 --retry-delay 2 \
    --retry-connrefused --connect-timeout 5 --max-time 30 \
    -H 'Metadata-Flavor: Google' --output "$HW4_STAGE_DIR/web-config.env" \
    --write-out '%{http_code}' "$METADATA_URL/instance/attributes/hw4-web-config")
case "$CONFIG_HTTP" in
    200) cat "$HW4_STAGE_DIR/web-config.env" >> /etc/hw4-web.env ;;
    404) ;; # Optional configuration was not supplied.
    *) echo "Unable to read web configuration: HTTP $CONFIG_HTTP" >&2; exit 1 ;;
esac
chmod 0644 /etc/hw4-web.env
install -m 0644 "$HW4_STAGE_DIR/hw4-web.service" \
    /etc/systemd/system/hw4-web.service

systemctl daemon-reload
systemctl enable hw4-web.service
systemctl restart hw4-web.service

# GET / deliberately returns 400 without accessing GCS; verify HTTP readiness.
for attempt in {1..30}; do
    status=$(curl --silent --output /dev/null --write-out '%{http_code}' \
        --max-time 2 http://127.0.0.1:8080/ || true)
    if [ "$status" = 400 ]; then
        echo "HW4 startup complete: HTTP server listening on port 8080."
        echo "GCS access must still be verified with a real filename."
        exit 0
    fi
    sleep 2
done
journalctl -u hw4-web.service -n 50 --no-pager
echo "HW4: HTTP server did not become ready" >&2
exit 1
