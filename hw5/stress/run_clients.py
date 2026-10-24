"""Run the instructor's Linux HTTP client in bounded, timed phases."""

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time


def stop_clients(processes):
    # Each process group includes the PyInstaller launcher and its child.
    for process in processes:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 5
    for process in processes:
        try:
            process.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
    for process in processes:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    for process in processes:
        process.wait()


def wait_until(deadline, processes):
    while time.monotonic() < deadline:
        for index, process in enumerate(processes, 1):
            code = process.poll()
            if code is not None:
                raise RuntimeError(
                    f"Client {index} exited early with code {code}; inspect its log. "
                    "The phase is incomplete, not automatically a server failure."
                )
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True, help="Server IP, without http://")
    parser.add_argument("--max-index", type=int, required=True)
    parser.add_argument("--client", default="./http-client")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--clients", nargs="+", type=int, choices=range(1, 5),
                        default=[1, 2, 3, 4])
    parser.add_argument("--seconds", type=int, default=180)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--cooldown", type=int, default=30)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--output", required=True, help="New result directory")
    args = parser.parse_args()
    if args.max_index < 0 or args.seconds <= 0 or min(args.warmup, args.cooldown) < 0:
        parser.error("Invalid index or duration")
    client = Path(args.client).expanduser().resolve()
    if not client.is_file() or not os.access(client, os.X_OK):
        parser.error("Client must be an executable file")
    root = Path(args.output).expanduser()
    root.mkdir(parents=True, exist_ok=False)

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    for phase_index, count in enumerate(args.clients, 1):
        folder = root / f"phase-{phase_index}-{count}-clients"
        folder.mkdir()
        processes, handles = [], []
        record = {
            "clients": count, "server": args.server, "port": args.port,
            "max_index": args.max_index, "requested_seconds": args.seconds,
            "warmup_seconds": args.warmup, "status": "starting", "commands": [],
        }
        print(f"Starting {count} client(s); warmup {args.warmup}s", flush=True)
        failure = None
        try:
            for index in range(count):
                command = [str(client), "-d", args.server, "-p", str(args.port),
                           "-b", "none", "-w", "none", "-n", "1000000",
                           "-i", str(args.max_index), "-r", str(42 + index),
                           "-t", str(args.timeout), "-v"]
                record["commands"].append(command)
                handle = (folder / f"client-{index + 1}.log").open("xb")
                handles.append(handle)
                processes.append(subprocess.Popen(
                    command, stdout=handle, stderr=subprocess.STDOUT,
                    start_new_session=True,
                    env={**os.environ, "PYTHONUNBUFFERED": "1"},
                ))
            wait_until(time.monotonic() + args.warmup, processes)
            record["start_epoch"] = time.time()
            started = time.monotonic()
            print(f"Measuring {count} client(s) for {args.seconds}s", flush=True)
            wait_until(started + args.seconds, processes)
            record["end_epoch"] = time.time()
            record["elapsed_seconds"] = time.monotonic() - started
            record["status"] = "completed_window"
        except (Exception, KeyboardInterrupt) as exc:
            failure = exc
            record["status"] = "incomplete"
            record["error"] = str(exc) or "Interrupted"
            record["end_epoch"] = time.time()
        finally:
            # Record the cutoff before stopping; SIGTERM at cutoff is not a timeout.
            record["pre_stop_exit_codes"] = [p.poll() for p in processes]
            stop_clients(processes)
            for handle in handles:
                handle.close()
            record["post_stop_exit_codes"] = [p.returncode for p in processes]
            (folder / "window.json").write_text(json.dumps(record, indent=2) + "\n")
        print(f"{record['status']}: {folder}", flush=True)
        if failure is not None:
            raise SystemExit(f"Stopped experiment: {record['error']}")
        if phase_index < len(args.clients):
            print(f"Cooldown {args.cooldown}s; no clients running", flush=True)
            time.sleep(args.cooldown)
    print("Finished. HTTP success/error counts still require inspection of client logs.")


if __name__ == "__main__":
    main()
