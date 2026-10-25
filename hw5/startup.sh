#!/bin/bash
set -euo pipefail
trap 'echo "HW5 startup failed at line $LINENO; completion marker not written" >&2' ERR
LOCK=/var/log/hw5_startup_already_done
if [ -f "$LOCK" ]; then
    echo 'HW5 installation already complete; skipping package installation.'
    python3 /opt/hw5-bootstrap.py --configure-only
    systemctl restart hw5-schema.service
    systemctl restart hw5-web.service hw5-subscriber.service
    echo 'HW5 systemd services restarted with current configuration.'
    exit 0
fi
# All setup happens here as root, never through gcloud compute ssh --command.
export DEBIAN_FRONTEND=noninteractive
apt-get -o Acquire::Retries=5 update
apt-get -o Acquire::Retries=5 install -y python3 python3-venv curl ca-certificates
install -d -m 0755 /opt/hw5
curl --fail --silent --show-error --retry 5 \
    -H 'Metadata-Flavor: Google' \
    http://metadata.google.internal/computeMetadata/v1/instance/attributes/hw5-bootstrap \
    -o /opt/hw5-bootstrap.py
# Allow new IAM bindings a bounded propagation period.
BOOTSTRAPPED=false
for attempt in {1..12}; do
    if python3 /opt/hw5-bootstrap.py; then BOOTSTRAPPED=true; break; fi
    sleep 10
done
[ "$BOOTSTRAPPED" = true ]
if ! id hw5 >/dev/null 2>&1; then
    useradd --system --user-group --home-dir /opt/hw5 --no-create-home --shell /usr/sbin/nologin hw5
fi
python3 -m venv /opt/hw5/.venv
/opt/hw5/.venv/bin/python -m pip install --no-cache-dir \
    -r /opt/hw5/requirements-db.txt -r /opt/hw5/service2/requirements.txt 'gunicorn>=23,<24'

install -m 0644 /opt/hw5/deploy/hw5-schema.service /etc/systemd/system/hw5-schema.service
install -m 0644 /opt/hw5/deploy/hw5-web.service /etc/systemd/system/hw5-web.service
install -m 0644 /opt/hw5/deploy/hw5-subscriber.service /etc/systemd/system/hw5-subscriber.service
systemctl daemon-reload
systemctl enable hw5-schema.service hw5-web.service hw5-subscriber.service
systemctl restart hw5-schema.service
systemctl restart hw5-web.service hw5-subscriber.service
# Check service state and TCP readiness without creating a request/log row.
for attempt in {1..30}; do
    if systemctl is-active --quiet hw5-web.service hw5-subscriber.service && \
       /opt/hw5/.venv/bin/python -c 'import socket; socket.create_connection(("127.0.0.1",8080),2).close()'; then
        touch "$LOCK"
        echo 'HW5 installation complete; schema ready and services active.'
        exit 0
    fi
    sleep 2
done
echo 'HW5 services did not become ready.' >&2
exit 1
