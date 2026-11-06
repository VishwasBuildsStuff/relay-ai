"""Verify backup monitor failures reach a configured alert receiver."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from backup_alert import send_failure_alert


def test_failure_alert_reaches_configured_loopback_receiver():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *_args):
            pass

    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    except PermissionError:
        pytest.skip("This execution environment does not allow binding a loopback receiver.")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        send_failure_alert("relay-ai-backup-age-check.service", f"http://127.0.0.1:{server.server_port}/alerts")
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

    assert received == [("/alerts", b'{"status": "failed", "service": "relay-ai-backup-age-check.service"}')]


@pytest.mark.parametrize("url", ["http://example.com/hook", "file:///tmp/alert", "https://user@example.com/hook"])
def test_alert_rejects_unsafe_destinations(url):
    with pytest.raises(ValueError):
        send_failure_alert("relay-ai-backup-age-check.service", url)
