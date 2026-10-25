"""Long-running Flask server for the HW2 files stored in Cloud Storage."""

import json
import mimetypes
import os
from datetime import datetime, timezone
from functools import lru_cache
from uuid import uuid4

from flask import Flask, g, request
from werkzeug.exceptions import HTTPException
from google.api_core.exceptions import NotFound
from google.cloud import pubsub_v1, storage

if __package__:
    from .database import record_request
    from .timing import ENV_KEY, TimingMiddleware
else:
    from database import record_request
    from timing import ENV_KEY, TimingMiddleware

app = Flask(__name__)
app.wsgi_app = TimingMiddleware(app.wsgi_app)

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


def extract_request_metadata():
    """Extract the supplied client's headers; retain demographic group labels."""
    def header(field, default):
        value = request.headers.get(os.environ.get(f"REQUEST_HEADER_{field}", default))
        return value.strip() if value and value.strip() else None

    country = header("COUNTRY", "X-country")
    country = COUNTRY_LOOKUP.get((country or "").casefold(), country)
    filename = request.path.lstrip("/") or None
    if request.method == "POST":
        payload = request.get_json(silent=True)
        filename = payload.get("filename") if isinstance(payload, dict) else None
        if not isinstance(filename, str):
            filename = None
    return {
        "id": g.request_id,
        "country": country,
        "client_ip": header("CLIENT_IP", "X-client-ip") or request.remote_addr,
        "gender": header("GENDER", "X-gender"),
        "age": header("AGE", "X-age"),
        "income": header("INCOME", "X-income"),
        "client_time": header("TIME", "X-time"),
        "is_banned": country in FORBIDDEN_COUNTRIES,
        "requested_at": g.requested_at,
        "requested_file": filename,
        "experiment_id": os.environ.get("EXPERIMENT_ID", "manual"),
    }


def parse_request():
    """Extract metadata once and validate the filename without cloud I/O."""
    g.request_record = extract_request_metadata()
    g.filename_error = None
    filename = g.request_record["requested_file"]
    # Preserve the existing banned-country and unsupported-method precedence.
    if g.request_record["is_banned"] or request.method not in {"GET", "POST"}:
        return
    if request.method == "POST" and not isinstance(request.get_json(silent=True), dict):
        g.filename_error = "Provide a JSON object containing filename."
    elif not isinstance(filename, str) or not filename.strip():
        g.filename_error = "filename must be a non-empty string."
    elif any(part in ("", ".", "..") for part in filename.split("/")):
        g.filename_error = "filename must be a relative file path."


@app.before_request
def capture_request():
    g.request_id = str(uuid4())
    g.requested_at = datetime.now(timezone.utc)
    # Retain a minimal record even if header/body extraction raises unexpectedly.
    g.request_record = {
        "id": g.request_id, "requested_at": g.requested_at,
        "requested_file": request.path.lstrip("/") or None,
        "is_banned": False,
        "experiment_id": os.environ.get("EXPERIMENT_ID", "manual"),
    }
    timing = request.environ[ENV_KEY]
    timing.record.update(request_id=g.request_id,
                         experiment_id=g.request_record["experiment_id"])
    with timing.measure("request_parse"):
        parse_request()
    timing.record["validation_error"] = g.filename_error


def write_request_record(record):
    """Include pool acquisition, inserts, commit and connection return."""
    with request.environ[ENV_KEY].measure("database_write"):
        record_request(record)


def download_file(blob):
    """Measure the actual GCS download, including SDK retries and failures."""
    with request.environ[ENV_KEY].measure("gcs_read"):
        return blob.download_as_bytes()


@app.after_request
def persist_request(response):
    # All normal returns, framework errors and handled exceptions reach here.
    # Mark before attempting I/O, so Flask finalization cannot retry a commit.
    if getattr(g, "recording_attempted", False):
        return response
    g.recording_attempted = True
    record = {**g.request_record, "status_code": response.status_code}
    try:
        write_request_record(record)
    except Exception as exc:
        # A lost commit acknowledgement can leave the result unknown. Keep the
        # same ID in the fallback log for reconciliation; do not retry blindly.
        print(json.dumps({
            "severity": "CRITICAL", "event": "request_database_write_failed",
            "message": "Request persistence failed; transaction outcome may be unknown.",
            "error_type": type(exc).__name__,
            "record": {**record, "requested_at": record["requested_at"].isoformat()},
            "response_status": 503,
        }), flush=True)
        response = app.make_response(({
            "error": "Request logging unavailable.", "request_id": record["id"],
        }, 503))
    response.headers["X-Request-ID"] = record["id"]
    return response


@app.errorhandler(Exception)
def handle_unexpected_error(exc):
    if isinstance(exc, HTTPException):
        return exc
    print(json.dumps({
        "severity": "ERROR", "event": "unhandled_request_exception",
        "request_id": getattr(g, "request_id", None),
        "error_type": type(exc).__name__,
    }), flush=True)
    return {"error": "Internal server error."}, 500


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
    country = g.request_record["country"] if g.request_record["is_banned"] else None
    if country:
        # Extract only notification metadata; never access GCS for denied requests.
        filename = g.request_record["requested_file"]
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

    if g.filename_error:
        return {"error": g.filename_error}, 400
    filename = g.request_record["requested_file"]

    object_name = f"{OBJECT_PREFIX}/{filename}" if OBJECT_PREFIX else filename
    blob = get_storage_client().bucket(BUCKET_NAME).blob(object_name)
    try:
        contents = download_file(blob)
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
