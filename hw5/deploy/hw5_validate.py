"""Reject unrelated resources before reuse/deletion. Read resource JSON on stdin."""
import json
import sys

kind = sys.argv[1]
try:
    data = json.load(sys.stdin)
except json.JSONDecodeError:
    raise SystemExit("No valid resource JSON received; inspect the preceding gcloud error.") from None
owner = "cs528-hw5"
if kind == "description":
    valid = data.get("description") == owner
elif kind == "account":
    valid = data.get("displayName") == owner and not data.get("disabled", False)
else:
    valid = data.get("labels", {}).get("managed-by") == owner
if not valid:
    raise SystemExit("Resource ownership marker mismatch; refusing reuse/deletion.")
