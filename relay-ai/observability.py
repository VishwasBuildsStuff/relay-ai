"""Small JSON log formatter for local operations and future log aggregation."""

from __future__ import annotations

import json
import logging
import time

from redaction import redact_sensitive_text


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
