"""Summarize request_timing JSON lines; ignore unrelated server log lines."""

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import statistics

STAGES = ("request_parse", "gcs_read", "database_write", "response_output")


def describe(values):
    """Nearest-rank percentiles; retain executed zero-duration samples."""
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "mean_ms": None, "p50_ms": None,
                "p95_ms": None, "max_ms": None}
    return {
        "count": len(ordered), "mean_ms": statistics.mean(ordered) / 1_000_000,
        "p50_ms": ordered[math.ceil(len(ordered) * 0.50) - 1] / 1_000_000,
        "p95_ms": ordered[math.ceil(len(ordered) * 0.95) - 1] / 1_000_000,
        "max_ms": ordered[-1] / 1_000_000,
    }


def summarize(records):
    result = {"request_count": len(records), "stages": {}}
    result["status_counts"] = dict(Counter(str(r["status_code"]) for r in records))
    for stage in STAGES:
        samples = [r["stages"][stage] for r in records]
        valid = [s for s in samples if s["duration_ns"] is not None]
        result["stages"][stage] = {
            **describe([s["duration_ns"] for s in valid]),
            "outcome_counts": dict(Counter(s["outcome"] for s in samples)),
            "completed_only": describe([s["duration_ns"] for s in valid
                                        if s["outcome"] == "completed"]),
        }
    result["total"] = describe([r["total_ns"] for r in records])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("--experiment-id", required=True)
    args = parser.parse_args()
    records, seen = [], set()
    with args.log.open() as source:
        for number, line in enumerate(source, 1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                if '"request_timing"' in line:
                    raise SystemExit(f"Malformed timing JSON on line {number}; use raw log output")
                continue
            if not isinstance(record, dict) or record.get("event") != "request_timing":
                continue
            if record.get("experiment_id") != args.experiment_id:
                continue
            if record.get("schema_version") != 1:
                raise SystemExit(f"Unsupported schema on line {number}")
            request_id = record.get("request_id")
            if not request_id or request_id in seen:
                raise SystemExit(f"Missing/duplicate request ID on line {number}")
            seen.add(request_id)
            for stage in STAGES:
                sample = record.get("stages", {}).get(stage, {})
                duration = sample.get("duration_ns")
                outcome = sample.get("outcome")
                if outcome not in {"not_executed", "completed", "failed", "closed_early"}:
                    raise SystemExit(f"Invalid stage outcome on line {number}")
                if (outcome == "not_executed") != (duration is None):
                    raise SystemExit(f"Inconsistent stage sample on line {number}")
                if duration is not None and (type(duration) is not int or duration < 0):
                    raise SystemExit(f"Invalid duration on line {number}")
            if type(record.get("total_ns")) is not int or record["total_ns"] < 0:
                raise SystemExit(f"Invalid total duration on line {number}")
            records.append(record)
    if not records:
        raise SystemExit("No timing records matched this experiment")
    print(json.dumps({"experiment_id": args.experiment_id,
                      "percentile_method": "nearest_rank",
                      **summarize(records)}, indent=2))


if __name__ == "__main__":
    main()
