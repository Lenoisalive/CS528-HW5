"""Capture a small local client run without cloud credentials or dependencies."""

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--output", required=True, help="New JSONL file; never overwritten")
    args = parser.parse_args()

    class Handler(BaseHTTPRequestHandler):
        def capture(self):
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 1024 * 1024:
                self.send_error(413)
                return
            body = self.rfile.read(length)
            record = {
                "received_at": datetime.now(timezone.utc).isoformat(),
                "method": self.command, "path": self.path,
                "peer_ip": self.client_address[0],
                "headers": list(self.headers.raw_items()),
                "body": body.decode("utf-8", errors="replace"),
            }
            output.write(json.dumps(record, ensure_ascii=False) + "\n")
            output.flush()
            payload = b"<html><body>probe ok</body></html>\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD":
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = do_OPTIONS = do_PATCH = capture

    with open(args.output, "x", encoding="utf-8") as output:
        with HTTPServer(("127.0.0.1", args.port), Handler) as server:
            print(f"Listening on http://127.0.0.1:{args.port}; capture: {args.output}", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                print("Stopped; captured requests preserved.")


if __name__ == "__main__":
    main()
