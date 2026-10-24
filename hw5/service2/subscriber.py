"""Pub/Sub subscriber for a VM or local use, with stdout and a deduplicated GCS log."""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import google.auth
from google.api_core.exceptions import NotFound, PreconditionFailed
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.cloud import pubsub_v1, storage
from google.oauth2.credentials import Credentials

PROJECT_ID = os.environ.get("PROJECT_ID", "cs-528-508021")
SERVICE_ACCOUNT = os.environ.get(
    "SERVICE_ACCOUNT", f"hw3-microservice-sa@{PROJECT_ID}.iam.gserviceaccount.com"
)
SUBSCRIPTION_ID = os.environ.get("SUBSCRIPTION_ID", "forbidden-requests-sub")
BUCKET_NAME = os.environ.get("BUCKET_NAME", "cs528hw2")
LOG_OBJECT = os.environ.get("LOG_OBJECT", "forbidden_requests/log.txt")
AUTH_MODE = os.environ.get("AUTH_MODE", "impersonation").lower()


def make_credentials():
    """Use the VM's attached identity, or preserve HW3 local impersonation."""
    if AUTH_MODE == "adc":
        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        return credentials
    if AUTH_MODE != "impersonation":
        raise ValueError("AUTH_MODE must be 'adc' or 'impersonation'.")

    def refresh_token(request, scopes=None):
        started = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            result = subprocess.run(
                ["gcloud", "auth", "print-access-token",
                 f"--impersonate-service-account={SERVICE_ACCOUNT}",
                 f"--project={PROJECT_ID}", "--lifetime=3600", "--quiet"],
                check=True, capture_output=True, text=True, timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            # Do not print stdout: it can contain an access token.
            raise RefreshError(
                "Unable to impersonate service account. Check gcloud login, "
                "IAM Credentials API and Service Account Token Creator permission."
            ) from exc
        token = result.stdout.strip()
        if not token:
            raise RefreshError("gcloud returned an empty service account token.")
        # Refresh conservatively before the requested one-hour token expires.
        return token, started + timedelta(minutes=50)

    return Credentials(token=None, refresh_handler=refresh_token)


def format_record(event, message_id):
    if not isinstance(event, dict):
        raise ValueError("Pub/Sub payload must be a JSON object.")
    for field in ("country", "method"):
        if not isinstance(event.get(field), str) or not event[field].strip():
            raise ValueError(f"Missing or invalid {field} in Pub/Sub message.")
    event_id = event.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        event_id = f"pubsub:{message_id}"
    marker = f"Event ID: {json.dumps(event_id, ensure_ascii=True)}"

    def display(value):
        # Keep untrusted values on one line, including file names and event IDs.
        if value is None:
            return "(unknown)"
        return json.dumps(str(value), ensure_ascii=True)[1:-1]

    record = (
        "FORBIDDEN REQUEST:\n"
        f"Country: {display(event['country'])}\n"
        f"File: {display(event.get('filename'))}\n"
        f"Method: {display(event['method'])}\n"
        f"Timestamp: {display(event.get('timestamp'))}\n"
        f"Path: {display(event.get('path'))}\n"
        f"{marker}\n"
        f"Pub/Sub message ID: {display(message_id)}\n\n"
    )
    return marker, record


def append_record(bucket, marker, record, attempts=5):
    """Compare-and-swap the log; detect replay after a write succeeded but ack failed."""
    for attempt in range(attempts):
        try:
            current = bucket.get_blob(LOG_OBJECT, timeout=30)
            if current is None:
                generation, contents = 0, b""
            else:
                generation = current.generation
                contents = current.download_as_bytes(
                    if_generation_match=generation, timeout=30
                )
            if marker.encode("utf-8") in contents.splitlines():
                return False
            separator = b"" if not contents or contents.endswith(b"\n") else b"\n"
            bucket.blob(LOG_OBJECT).upload_from_string(
                contents + separator + record.encode("utf-8"),
                content_type="text/plain; charset=utf-8",
                if_generation_match=generation, timeout=30,
            )
            return True
        except (PreconditionFailed, NotFound):
            # Another writer changed/deleted the object; read the latest version.
            if attempt + 1 == attempts:
                raise
            time.sleep(0.1 * (2 ** attempt))


def make_callback(bucket):
    def callback(message):
        try:
            event = json.loads(message.data.decode("utf-8"))
            marker, record = format_record(event, message.message_id)
            # Escape untrusted values so the notification remains on one line.
            country = json.dumps(event["country"], ensure_ascii=True)[1:-1]
            print(f"CRITICAL: Forbidden request from {country}", flush=True)
            print(record, end="", flush=True)
            appended = append_record(bucket, marker, record)
        except Exception as exc:
            print(
                f"Message {message.message_id}: processing failed ({type(exc).__name__}: {exc}); "
                "leaving message for redelivery.", file=sys.stderr, flush=True,
            )
            message.nack()
            return
        message.ack()
        print("Saved to GCS." if appended else "Already in GCS; duplicate acknowledged.", flush=True)

    return callback


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-auth", action="store_true",
        help="Check credentials without consuming messages or writing GCS.",
    )
    args = parser.parse_args()
    credentials = make_credentials()
    if args.check_auth:
        credentials.refresh(Request())
        print(f"Credentials acquired using authentication mode: {AUTH_MODE}")
        if AUTH_MODE == "impersonation":
            print(f"Impersonated identity: {SERVICE_ACCOUNT}")
        if credentials.expiry is not None:
            print(f"Credential expiry (UTC): {credentials.expiry.isoformat()}")
        print("Token is not displayed. Pub/Sub and GCS permissions are not checked here.")
        return
    storage_client = storage.Client(project=PROJECT_ID, credentials=credentials)
    subscriber = pubsub_v1.SubscriberClient(credentials=credentials)
    subscription = subscriber.subscription_path(PROJECT_ID, SUBSCRIPTION_ID)
    with storage_client, subscriber:
        future = subscriber.subscribe(
            subscription, callback=make_callback(storage_client.bucket(BUCKET_NAME)),
            flow_control=pubsub_v1.types.FlowControl(max_messages=1),
            await_callbacks_on_shutdown=True,
        )
        print(f"Listening: {subscription}\nAuthentication mode: {AUTH_MODE}\n"
              f"Log: gs://{BUCKET_NAME}/{LOG_OBJECT}", flush=True)
        try:
            future.result()
        except KeyboardInterrupt:
            print("Stopping subscriber...", flush=True)
        finally:
            future.cancel()
            future.result()


if __name__ == "__main__":
    main()
