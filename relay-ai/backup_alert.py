"""Send a minimal backup freshness failure to an operator-configured webhook."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlsplit


def send_failure_alert(service: str, webhook_url: str | None = None, timeout: float = 5.0) -> None:
    """POST a small failure notice; never include task, archive, or credential data."""
    url = webhook_url or os.environ.get("RELAY_BACKUP_ALERT_WEBHOOK_URL", "")
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    local_http = False
    if parsed.scheme == "http" and host:
        try:
            local_http = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local_http = host.lower() == "localhost"
    if (
        parsed.scheme != "https" and not local_http
        or not host or parsed.username is not None or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("Backup alert webhook must use HTTPS (HTTP is allowed only for loopback drills).")

    body = json.dumps({"status": "failed", "service": service}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "relay-ai-backup-alert/1"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError(f"Backup alert webhook returned HTTP {response.status}.")
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Backup alert webhook returned HTTP {error.code}.") from None
    except urllib.error.URLError as error:
        raise RuntimeError(f"Backup alert webhook delivery failed: {error.reason}.") from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", required=True, help="Name of the failed systemd unit")
    args = parser.parse_args()
    send_failure_alert(args.service)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
