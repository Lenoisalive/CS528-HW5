"""Authenticated HTTP function: stop only the configured Cloud SQL instance."""
import json
import os
import time

import google.auth
from google.auth.transport.requests import AuthorizedSession
import functions_framework

BASE_URL = "https://sqladmin.googleapis.com/sql/v1beta4"


def stop_instance(session, project, instance):
    url = f"{BASE_URL}/projects/{project}/instances/{instance}"
    response = session.get(url, timeout=30)
    response.raise_for_status()
    current = response.json()
    if current.get("settings", {}).get("activationPolicy") == "NEVER":
        return {"action": "already_disabled", "instance": instance}
    if current.get("state") != "RUNNABLE":
        raise RuntimeError("Instance is not ready; retry later")
    response = session.patch(url, json={"settings": {"activationPolicy": "NEVER"}}, timeout=30)
    response.raise_for_status()
    operation = response.json()["name"]
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        response = session.get(f"{BASE_URL}/projects/{project}/operations/{operation}", timeout=30)
        response.raise_for_status()
        result = response.json()
        if result.get("status") == "DONE":
            if result.get("error"):
                raise RuntimeError("SQL stop operation failed")
            return {"action": "stopped", "instance": instance, "operation": operation}
        time.sleep(3)
    raise TimeoutError("SQL stop operation still pending")


@functions_framework.http
def stop_database(request):
    # Cloud Run IAM validates the Scheduler OIDC token before invoking this code.
    # Never take project/instance from request content.
    if request.method != "POST":
        return {"error": "POST required"}, 405
    try:
        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        with AuthorizedSession(credentials) as session:
            result = stop_instance(session, os.environ["TARGET_PROJECT"], os.environ["SQL_INSTANCE"])
        print(json.dumps({"event": "scheduled_sql_stop", **result}), flush=True)
        return result, 200
    except Exception as exc:
        print(json.dumps({"severity": "ERROR", "event": "scheduled_sql_stop_failed",
                          "error_type": type(exc).__name__}), flush=True)
        return {"error": "Stop failed or still pending; retry required."}, 503
