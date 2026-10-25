"""Per-request wall-clock phases and the WSGI output lifecycle."""

from contextlib import contextmanager
import json
import time

STAGES = ("request_parse", "gcs_read", "database_write", "response_output")
ENV_KEY = "hw5.timing"


class RequestTiming:
    def __init__(self):
        self.started_ns = time.perf_counter_ns()
        self.record = {
            "event": "request_timing", "schema_version": 1,
            "request_id": None, "experiment_id": None, "status_code": None,
            "stages": {name: {"duration_ns": None, "outcome": "not_executed"}
                       for name in STAGES},
        }
        self.emitted = False

    @contextmanager
    def measure(self, name):
        started = time.perf_counter_ns()
        outcome = "completed"
        try:
            yield
        except BaseException:
            outcome = "failed"
            raise
        finally:
            self.record["stages"][name] = {
                "duration_ns": time.perf_counter_ns() - started, "outcome": outcome,
            }

    def emit(self):
        if self.emitted:
            return
        self.emitted = True
        self.record["total_ns"] = time.perf_counter_ns() - self.started_ns
        print(json.dumps(self.record), flush=True)


class TimedResponse:
    """Measure first iteration through server close, including consumer waits."""
    def __init__(self, iterable, timing):
        self.iterable = iterable
        self.iterator = iter(iterable)
        self.timing = timing
        self.started_ns = None
        self.exhausted = False
        self.failed = False
        self.closed = False
        self.bytes_yielded = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self.closed:
            raise StopIteration
        if self.started_ns is None:
            self.started_ns = time.perf_counter_ns()
        try:
            chunk = next(self.iterator)
        except StopIteration:
            self.exhausted = True
            raise
        except BaseException:
            self.failed = True
            self.close()
            raise
        self.bytes_yielded += len(chunk)
        return chunk

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            close = getattr(self.iterable, "close", None)
            if close is not None:
                close()
        except BaseException:
            self.failed = True
            raise
        finally:
            outcome = ("failed" if self.failed else
                       "completed" if self.exhausted else "closed_early")
            if self.started_ns is not None:
                self.timing.record["stages"]["response_output"] = {
                    "duration_ns": time.perf_counter_ns() - self.started_ns,
                    "outcome": outcome,
                }
            self.timing.record["output_lifecycle"] = outcome
            self.timing.record["bytes_yielded"] = self.bytes_yielded
            self.timing.emit()


class TimingMiddleware:
    def __init__(self, application):
        self.application = application

    def __call__(self, environ, start_response):
        timing = RequestTiming()
        environ[ENV_KEY] = timing

        def capture_status(status, headers, exc_info=None):
            timing.record["status_code"] = int(status.split(" ", 1)[0])
            return start_response(status, headers, exc_info)

        try:
            return TimedResponse(self.application(environ, capture_status), timing)
        except BaseException:
            timing.record["output_lifecycle"] = "application_failed"
            timing.emit()
            raise
