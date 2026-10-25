"""Fetch deployment inputs using VM identity; no secret values enter metadata."""
import base64
import hashlib
import io
import json
import os
import sys
from pathlib import Path
import tarfile
from urllib.parse import quote
from urllib.request import Request, urlopen

META = "http://metadata.google.internal/computeMetadata/v1/"


def metadata(path):
    with urlopen(Request(META + path, headers={"Metadata-Flavor": "Google"}), timeout=30) as response:
        return response.read().decode()


def fetch(url):
    token = json.loads(metadata("instance/service-accounts/default/token"))["access_token"]
    with urlopen(Request(url, headers={"Authorization": "Bearer " + token}), timeout=60) as response:
        return response.read()


def main():
    config = json.loads(metadata("instance/attributes/hw5-config"))
    project = config["project"]
    if "--configure-only" not in sys.argv:
        archive = fetch("https://storage.googleapis.com/storage/v1/b/" + config["artifact_bucket"]
                        + "/o/" + quote(config["artifact_object"], safe="") + "?alt=media")
        if hashlib.sha256(archive).hexdigest() != config["sha256"]:
            raise RuntimeError("Deployment artifact hash mismatch")
        root = Path("/opt/hw5")
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as package:
            for member in package.getmembers():
                target = (root / member.name).resolve()
                if not target.is_relative_to(root) or not (member.isfile() or member.isdir()):
                    raise RuntimeError("Invalid deployment archive member")
            package.extractall(root)
    secret = json.loads(fetch(f"https://secretmanager.googleapis.com/v1/projects/{project}/secrets/"
                              f"{config['secret']}/versions/latest:access"))
    password = base64.b64decode(secret["payload"]["data"]).decode()
    # Setup generates a URL-safe password; keep systemd EnvironmentFile syntax exact.
    if not password or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in password):
        raise RuntimeError("Unexpected password encoding")
    env = {
        "PROJECT_ID": project, "GOOGLE_CLOUD_PROJECT": project,
        "INSTANCE_CONNECTION_NAME": config["connection_name"],
        "DB_NAME": config["db_name"], "DB_USER": config["db_user"], "DB_PASS": password,
        "DB_IP_TYPE": "PUBLIC", "DB_POOL_SIZE": "5", "DB_MAX_OVERFLOW": "5",
        "BUCKET_NAME": config["bucket"], "OBJECT_PREFIX": config["object_prefix"],
        "PUBSUB_TOPIC": config["topic"], "SUBSCRIPTION_ID": config["subscription"],
        "LOG_OBJECT": "forbidden_requests/hw5-log.txt", "AUTH_MODE": "adc",
        "EXPERIMENT_ID": config["experiment_id"],
        "CLOUD_LOGGING_ENABLED": "true", "CLOUD_LOG_NAME": "hw5-web-server",
        "HW4_INSTANCE_ID": metadata("instance/id"),
        "HW4_INSTANCE_ZONE": metadata("instance/zone").rsplit("/", 1)[-1],
        "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
    }
    path = Path("/etc/hw5.env")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        for key, value in env.items():
            if any(c in value for c in "\n\r\"\\"):
                raise RuntimeError("Invalid environment configuration")
            stream.write(f'{key}="{value}"\n')
    path.chmod(0o600)


if __name__ == "__main__":
    main()
