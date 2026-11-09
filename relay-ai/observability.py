"""Small JSON log formatter for local operations and future log aggregation."""

from __future__ import annotations

import json
import logging
import threading
import time

from redaction import redact_sensitive_text


class RequestMetrics:
    """Bounded, process-local API request counters and latency histogram buckets."""

    BUCKETS_MS = (100, 500, 1000, 5000, 15000, 60000)
    METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._observations: dict[tuple[str, str], dict] = {}
        self._counts: dict[tuple[str, str, str], int] = {}

    def observe(self, route: str, method: str, status_code: int, duration_ms: float) -> None:
        route_label = route if route.startswith("/") else "unmatched"
        method_label = method.upper() if method.upper() in self.METHODS else "OTHER"
        status_class = f"{status_code // 100}xx"
        key = (route_label, method_label)
        with self._lock:
            observation = self._observations.setdefault(
                key,
                {"count": 0, "duration_sum": 0.0, "buckets": [0] * len(self.BUCKETS_MS)},
            )
            observation["count"] += 1
            observation["duration_sum"] += max(0.0, duration_ms)
            for index, bound in enumerate(self.BUCKETS_MS):
                if duration_ms <= bound:
                    observation["buckets"][index] += 1
            count_key = (route_label, method_label, status_class)
            self._counts[count_key] = self._counts.get(count_key, 0) + 1

    def snapshot(self) -> tuple[dict[tuple[str, str], dict], dict[tuple[str, str, str], int]]:
        with self._lock:
            observations = {
                key: {
                    "count": value["count"],
                    "duration_sum": value["duration_sum"],
                    "buckets": list(value["buckets"]),
                }
                for key, value in self._observations.items()
            }
            return observations, dict(self._counts)


class JsonLogFormatter(logging.Formatter):
    """Serialize standard log records and a small allow-list of useful context."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_sensitive_text(record.getMessage()),
        }
        for key in (
            "task_id", "stage", "attempt", "request_id", "method", "route", "status_code", "duration_ms"
        ):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = redact_sensitive_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging() -> None:
    """Configure JSON logs unless the host application already owns logging."""
    root = logging.getLogger()
    if root.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    root.addHandler(handler)
    root.setLevel(logging.INFO)
