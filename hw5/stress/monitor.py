"""Sample Linux VM metrics, or summarize samples inside client measurement windows."""

import argparse
import csv
import json
from pathlib import Path
import time


def cpu():
    values = [int(v) for v in Path("/proc/stat").read_text().splitlines()[0].split()[1:9]]
    # user, nice, system, idle, iowait, irq, softirq, steal; exclude duplicate guest.
    total = sum(values)
    busy = sum(values[i] for i in (0, 1, 2, 5, 6))
    return total, busy, values[4], values[7]


def memory():
    fields = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        fields[key] = int(value.split()[0])
    used = fields["MemTotal"] - fields["MemAvailable"]
    return used / 1024, used / fields["MemTotal"] * 100


def interface():
    for line in Path("/proc/net/route").read_text().splitlines()[1:]:
        fields = line.split()
        if fields[1] == "00000000" and int(fields[3], 16) & 1:
            return fields[0]
    raise RuntimeError("No default IPv4 interface; specify --interface")


def network(device):
    for line in Path("/proc/net/dev").read_text().splitlines()[2:]:
        name, counters = line.split(":", 1)
        if name.strip() == device:
            fields = counters.split()
            return int(fields[0]), int(fields[8])
    raise RuntimeError(f"Network interface disappeared: {device}")


def sample(args):
    device = args.interface or interface()
    fields = ["start_epoch", "end_epoch", "interval_seconds", "cpu_busy_pct",
              "cpu_iowait_pct", "cpu_steal_pct", "memory_used_mib", "memory_used_pct",
              "rx_mbps", "tx_mbps", "interface"]
    previous_cpu, previous_net = cpu(), network(device)
    previous_epoch, previous_time = time.time(), time.monotonic()
    deadline = previous_time + args.seconds
    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    print(f"Sampling {device} every {args.interval}s for {args.seconds}s -> {output}", flush=True)
    with output.open("x", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        try:
            while time.monotonic() < deadline:
                time.sleep(min(args.interval, max(0, deadline - time.monotonic())))
                current_cpu, current_net = cpu(), network(device)
                used_mib, used_pct = memory()
                now_epoch, now = time.time(), time.monotonic()
                elapsed = now - previous_time
                delta = [a - b for a, b in zip(current_cpu, previous_cpu)]
                if delta[0] <= 0 or min(delta[1:]) < 0:
                    raise RuntimeError("CPU counters invalid; repeat sampling")
                rx = (current_net[0] - previous_net[0]) * 8 / elapsed / 1_000_000
                tx = (current_net[1] - previous_net[1]) * 8 / elapsed / 1_000_000
                if min(rx, tx) < 0:
                    raise RuntimeError("Network counters reset; repeat sampling")
                writer.writerow({
                    "start_epoch": previous_epoch, "end_epoch": now_epoch,
                    "interval_seconds": elapsed,
                    "cpu_busy_pct": delta[1] / delta[0] * 100,
                    "cpu_iowait_pct": delta[2] / delta[0] * 100,
                    "cpu_steal_pct": delta[3] / delta[0] * 100,
                    "memory_used_mib": used_mib, "memory_used_pct": used_pct,
                    "rx_mbps": rx, "tx_mbps": tx, "interface": device,
                })
                handle.flush()
                previous_cpu, previous_net = current_cpu, current_net
                previous_epoch, previous_time = now_epoch, now
        except KeyboardInterrupt:
            print("Sampling stopped; collected rows preserved.")


def summarize(args):
    with Path(args.csv).expanduser().open() as handle:
        rows = list(csv.DictReader(handle))
    windows = sorted(Path(args.windows).expanduser().glob("phase-*/window.json"))
    if not windows:
        raise SystemExit("No phase-*/window.json files found")
    for path in windows:
        window = json.loads(path.read_text())
        if window["status"] != "completed_window":
            print(json.dumps({"phase": path.parent.name, "status": "INCOMPLETE"}))
            continue
        start, end = window["start_epoch"], window["end_epoch"]
        selected = [r for r in rows if float(r["start_epoch"]) >= start
                    and float(r["end_epoch"]) <= end]
        duration = end - start
        coverage = sum(float(r["interval_seconds"]) for r in selected)
        if not selected or coverage < duration * 0.95:
            print(json.dumps({"phase": path.parent.name, "status": "INSUFFICIENT_SAMPLES",
                              "coverage_seconds": coverage}))
            continue
        result = {"phase": path.parent.name, "clients": window["clients"],
                  "window_seconds": round(duration, 2), "samples": len(selected),
                  "coverage_seconds": round(coverage, 2)}
        for metric in ("cpu_busy_pct", "cpu_iowait_pct", "cpu_steal_pct",
                       "memory_used_mib", "memory_used_pct", "rx_mbps", "tx_mbps"):
            result[f"peak_{metric}"] = round(max(float(r[metric]) for r in selected), 3)
            result[f"mean_{metric}"] = round(sum(
                float(r[metric]) * float(r["interval_seconds"]) for r in selected
            ) / coverage, 3)
        print(json.dumps(result, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("sample")
    capture.add_argument("--seconds", type=int, default=1200)
    capture.add_argument("--interval", type=float, default=1)
    capture.add_argument("--interface")
    capture.add_argument("--output", required=True)
    summary = commands.add_parser("summarize")
    summary.add_argument("--csv", required=True)
    summary.add_argument("--windows", required=True)
    args = parser.parse_args()
    if args.command == "sample":
        if args.seconds <= 0 or args.interval <= 0:
            parser.error("Duration and interval must be positive")
        sample(args)
    else:
        summarize(args)


if __name__ == "__main__":
    main()
