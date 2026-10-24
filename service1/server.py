"""Long-running Flask server for the HW2 files stored in Cloud Storage."""

import json
import mimetypes
import os
from datetime import datetime, timezone
from functools import lru_cache
from uuid import uuid4

from flask import Flask, request
from google.api_core.exceptions import NotFound
from google.cloud import pubsub_v1, storage

app = Flask(__name__)

BUCKET_NAME = os.environ.get("BUCKET_NAME", "cs528hw2")
OBJECT_PREFIX = os.environ.get("OBJECT_PREFIX", "pages").strip("/")
PUBSUB_TOPIC = os.environ.get(
    "PUBSUB_TOPIC", "projects/cs-528-508021/topics/forbidden-requests"
)
FORBIDDEN_COUNTRIES = {
    "North Korea", "Iran", "Cuba", "Myanmar", "Iraq",
    "Libya", "Sudan", "Zimbabwe", "Syria",
}
COUNTRY_LOOKUP = {country.casefold(): country for country in FORBIDDEN_COUNTRIES}
CLOUD_LOGGING_ENABLED = os.environ.get("CLOUD_LOGGING_ENABLED", "false").lower() == "true"
CLOUD_LOG_NAME = os.environ.get("CLOUD_LOG_NAME", "hw4-web-server")


@lru_cache(maxsize=1)
def get_publisher():
    return pubsub_v1.PublisherClient()


@lru_cache(maxsize=1)
def get_storage_client():
    # On Google Cloud, credentials come from the attached service account.
    return storage.Client()


@lru_cache(maxsize=1)
def get_cloud_logger():
    # Lazy initialization keeps local use independent of Logging credentials.
    from google.cloud import logging as cloud_logging
    from google.cloud.logging_v2.resource import Resource

    client = cloud_logging.Client()
    instance_id = os.environ.get("HW4_INSTANCE_ID")
    zone = os.environ.get("HW4_INSTANCE_ZONE")
    if instance_id and zone:
        resource = Resource(type="gce_instance", labels={
            "project_id": client.project,
            "instance_id": instance_id,
            "zone": zone,
        })
        return client.logger(CLOUD_LOG_NAME, resource=resource)
    return client.logger(CLOUD_LOG_NAME)


def log_http_error(request, event, status, message, *, severity="ERROR", **details):
    """Keep a local copy and explicitly send structured events to Cloud Logging."""
    record = {
        "severity": severity,
        "event": event,
        "message": message,
        "method": request.method,
        "path": request.path,
        "status": status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **details,
    }
    print(message, flush=True)
    print(json.dumps(record), flush=True)
    if CLOUD_LOGGING_ENABLED:
        try:
            # Set the LogEntry severity, not just a field inside jsonPayload.
            # This synchronous call submits the event before responding.
            get_cloud_logger().log_struct(record, severity=severity)
        except Exception as exc:
            # Preserve HTTP behavior if logging fails; diagnose through journal.
            # Do not recursively call this helper from its own failure handler.
            print(json.dumps({
                "severity": "ERROR",
                "event": "cloud_logging_failed",
                "original_event": event,
                "message": "Failed to write event to Cloud Logging.",
                "error": str(exc),
            }), flush=True)


@app.before_request
def reject_unsupported_methods():
    # Run before routing, including Flask's automatic HEAD/OPTIONS handling.
    if request.method not in {"GET", "POST"}:
        log_http_error(
            request, "unsupported_method", 501,
            f"HTTP method not implemented: {request.method} {request.path!r}",
        )
        return {"error": "HTTP method not implemented."}, 501


@app.route("/", methods=["GET", "POST"], provide_automatic_options=False)
@app.route("/<path:filename_path>", methods=["GET", "POST"],
           provide_automatic_options=False)
def serve_file(filename_path=None):
    # Check before payload validation or any GCS access.
    country = COUNTRY_LOOKUP.get(request.headers.get("X-country", "").strip().casefold())
    if country:
        # Extract only notification metadata; never access GCS for denied requests.
        if request.method == "POST":
            payload = request.get_json(silent=True)
            filename = payload.get("filename") if isinstance(payload, dict) else None
        else:
            filename = request.path.lstrip("/")
        if not isinstance(filename, str) or not filename.strip():
            filename = None
        event = {
            "event": "forbidden_request",
            "event_id": str(uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "country": country,
            "filename": filename,
            "method": request.method,
            "path": request.path,
            "status": 400,
            "message": f"Permission denied: request from {country}",
        }
        log_http_error(
            request, event["event"], 400, event["message"],
            severity="CRITICAL",
            country=country, filename=filename, event_id=event["event_id"],
        )
        try:
            # Wait for acknowledgement before returning; background work may stop
            # after an HTTP function finishes handling its request.
            message_id = get_publisher().publish(
                PUBSUB_TOPIC, json.dumps(event).encode("utf-8")
            ).result(timeout=10)
        except Exception as exc:
            # Notification failure must never allow access to the file.
            log_http_error(
                request, "forbidden_publish_failed", 400,
                "Failed to publish forbidden request notification.",
                country=country, event_id=event["event_id"], error=str(exc),
            )
        else:
            print(json.dumps({
                "severity": "INFO",
                "event": "forbidden_request_published",
                "message": "Pub/Sub acknowledged forbidden request notification.",
                "event_id": event["event_id"],
                "message_id": message_id,
                "topic": PUBSUB_TOPIC,
                "country": country,
                "filename": filename,
            }), flush=True)
        return {"error": "Permission denied", "country": country}, 400

    if request.method == "GET":
        filename = filename_path
    else:  # POST; all other methods are rejected by before_request.
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return {"error": "Provide a JSON object containing filename."}, 400
        filename = payload.get("filename")
    if not isinstance(filename, str) or not filename.strip():
        return {"error": "filename must be a non-empty string."}, 400

    # Filenames are relative to OBJECT_PREFIX, e.g. 1.html -> pages/1.html.
    if any(part in ("", ".", "..") for part in filename.split("/")):
        return {"error": "filename must be a relative file path."}, 400

    object_name = f"{OBJECT_PREFIX}/{filename}" if OBJECT_PREFIX else filename
    blob = get_storage_client().bucket(BUCKET_NAME).blob(object_name)
    try:
        contents = blob.download_as_bytes()
    except NotFound:
        log_http_error(
            request, "file_not_found", 404,
            f"File not found: {filename!r}",
            severity="WARNING",
            filename=filename, bucket=BUCKET_NAME, object_name=object_name,
        )
        return {"error": "File not found."}, 404

    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return contents, 200, {"Content-Type": content_type}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
