"""Report RELAY AI deployment prerequisites without printing secret values."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
IMAGE_DIGEST = re.compile(r"(?:sha256:[0-9a-f]{64}|[^@\s]+@sha256:[0-9a-f]{64})")
SIGNOFF_FIELDS = (
    "target_host", "deployment_revision", "primary_owner", "backup_owner", "security_contact",
    "nginx_config_test", "auth_cors_proxy_test", "sandbox_isolation", "gemini_data_approval",
    "backup_alert_delivery", "restore_drill", "accessibility_review", "load_review", "slo_owner",
)
REQUIRED_DELIVERY_HEADERS = (
    "Content-Security-Policy",
    "Permissions-Policy",
    "Referrer-Policy",
    "Strict-Transport-Security",
    "X-Content-Type-Options",
    "X-Frame-Options",
)
REQUIRED_CSP_DIRECTIVES = (
    "default-src 'self'",
    "base-uri 'self'",
    "object-src 'none'",
    "frame-ancestors 'none'",
    "script-src 'self'",
    "style-src 'self'",
)


class _AssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.assets: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name in {"src", "href"} and value and value.startswith("/assets/"):
                self.assets.append(value)


def _result(check_id: str, status: str, message: str) -> dict[str, str]:
    return {"id": check_id, "status": status, "message": message}


def _valid_production_origins(raw: str) -> bool:
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    if not origins or "*" in origins:
        return False
    for origin in origins:
        try:
            parsed = urlsplit(origin)
            parsed.port
        except ValueError:
            return False
        if (
            "*" in origin
            or any(char.isspace() for char in origin)
            or parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            return False
    return True


def _auth_is_configured(env: Mapping[str, str]) -> tuple[bool, str]:
    shared_token = env.get("RELAY_API_TOKEN", "").strip()
    try:
        users = json.loads(env.get("RELAY_AUTH_USERS_JSON", "{}"))
    except json.JSONDecodeError:
        return False, "RELAY_AUTH_USERS_JSON is not valid JSON."
    if not isinstance(users, dict):
        return False, "RELAY_AUTH_USERS_JSON must be a user-to-token object."
    if any(
        not isinstance(name, str) or not name or len(name) > 100
        or not isinstance(token, str) or not token or len(token) > 4096
        for name, token in users.items()
    ):
        return False, "Per-user names and tokens must be non-empty strings."
    if len(set(users.values())) != len(users):
        return False, "Each user must have a unique token."
    try:
        roles = json.loads(env.get("RELAY_AUTH_ROLES_JSON", "{}"))
    except json.JSONDecodeError:
        return False, "RELAY_AUTH_ROLES_JSON is not valid JSON."
    if not isinstance(roles, dict) or any(
        not isinstance(name, str) or name not in users
        or not isinstance(role, str) or role not in {"viewer", "operator"}
        for name, role in roles.items()
    ):
        return False, "Roles must map configured usernames to 'viewer' or 'operator'."
    if shared_token:
        return True, "Shared bearer authentication is configured."
    if users:
        return True, "Unique per-user bearer authentication is configured."
    return False, "Configure RELAY_API_TOKEN or unique per-user tokens."


def _check_frontend(root: Path) -> dict[str, str]:
    dist_root = root / "dist"
    index = dist_root / "index.html"
    if not index.is_file():
        return _result("frontend-build", "ACTION REQUIRED", "Run npm run build to create dist/index.html.")
    parser = _AssetParser()
    try:
        parser.feed(index.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return _result("frontend-build", "FAIL", "The generated index.html could not be read.")
    dist_resolved = dist_root.resolve()
    asset_paths = [(dist_root / asset.lstrip("/")).resolve() for asset in parser.assets]
    if (
        not asset_paths
        or any(not path.is_relative_to(dist_resolved) or not path.is_file() for path in asset_paths)
        or not (dist_root / "relay-mark.svg").is_file()
    ):
        return _result("frontend-build", "FAIL", "One or more bundled frontend assets are missing.")
    return _result("frontend-build", "PASS", f"Built entry point, {len(parser.assets)} bundled asset(s), and app mark are present.")


def evaluate_preflight(
    root: Path,
    env: Mapping[str, str],
    signoff: Mapping[str, str] | None = None,
) -> list[dict[str, str]]:
    """Evaluate local deployment prerequisites; never include secret values."""
    checks: list[dict[str, str]] = []
    production = env.get("RELAY_ENV", "development").strip().lower() == "production"
    checks.append(_result(
        "production-mode",
        "PASS" if production else "ACTION REQUIRED",
        "RELAY_ENV is production." if production else "Set RELAY_ENV=production in the protected service environment.",
    ))

    auth_ok, auth_message = _auth_is_configured(env)
    checks.append(_result("authentication", "PASS" if auth_ok else "ACTION REQUIRED", auth_message))

    cors_ok = _valid_production_origins(env.get("RELAY_CORS_ORIGINS", ""))
    checks.append(_result(
        "cors",
        "PASS" if cors_ok else "ACTION REQUIRED",
        "Explicit bare HTTPS origins are configured." if cors_ok else "Set an explicit comma-separated HTTPS origin allow-list.",
    ))

    image = env.get("RELAY_SANDBOX_IMAGE", "").strip()
    image_ok = bool(IMAGE_DIGEST.fullmatch(image))
    checks.append(_result(
        "sandbox-image",
        "PASS" if image_ok else "ACTION REQUIRED",
        "Sandbox image is pinned by SHA-256." if image_ok else "Set RELAY_SANDBOX_IMAGE to the built image ID or repository digest.",
    ))

    unsafe_subprocess = env.get("RELAY_ALLOW_UNSAFE_SUBPROCESS", "false").strip().lower() == "true"
    checks.append(_result(
        "host-subprocess",
        "FAIL" if unsafe_subprocess else "PASS",
        "Unsafe host subprocess mode is enabled." if unsafe_subprocess else "Unsafe host subprocess mode is disabled.",
    ))

    checks.append(_check_frontend(root))

    nginx_config = root / "deploy" / "nginx" / "relay-ai.conf"
    try:
        nginx_text = nginx_config.read_text(encoding="utf-8")
    except OSError:
        checks.append(_result("nginx-template", "FAIL", "The Nginx deployment template is missing or unreadable."))
    else:
        configured_headers = set(re.findall(r"^\s*add_header\s+([A-Za-z-]+)\s+", nginx_text, re.MULTILINE))
        missing_headers = [header for header in REQUIRED_DELIVERY_HEADERS if header not in configured_headers]
        csp_match = re.search(
            r'^\s*add_header\s+Content-Security-Policy\s+"([^"]+)"\s+always;?',
            nginx_text,
            re.MULTILINE,
        )
        csp_directives = {directive.strip() for directive in csp_match.group(1).split(";")} if csp_match else set()
        missing_csp = [directive for directive in REQUIRED_CSP_DIRECTIVES if directive not in csp_directives]
        missing_headers.extend(f"CSP directive {directive}" for directive in missing_csp)
        checks.append(_result(
            "delivery-headers",
            "FAIL" if missing_headers else "PASS",
            f"Nginx is missing required delivery headers: {', '.join(missing_headers)}." if missing_headers
            else "Nginx configures a restrictive CSP plus transport, framing, content-type, referrer, and permissions headers.",
        ))
        has_examples = "relay.example.com" in nginx_text or "/etc/letsencrypt/live/relay.example.com/" in nginx_text
        nginx_evidence = (signoff or {}).get("nginx_config_test", "").strip()
        checks.append(_result(
            "nginx-template",
            "ACTION REQUIRED" if has_examples else "PASS" if nginx_evidence else "MANUAL",
            "Replace sample hostname and certificate paths, then run nginx -t on the target host." if has_examples
            else "Nginx syntax-check evidence is recorded." if nginx_evidence
            else "Run nginx -t against the installed configuration and record its evidence reference.",
        ))

    missing_signoffs = [
        field for field in SIGNOFF_FIELDS
        if not isinstance((signoff or {}).get(field), str) or not (signoff or {}).get(field, "").strip()
    ]
    checks.append(_result(
        "host-and-owner-signoff",
        "MANUAL" if signoff is None else "ACTION REQUIRED" if missing_signoffs else "PASS",
        "Copy deploy/RELEASE_SIGNOFF_TEMPLATE.json and record all required evidence references; do not include secrets."
        if signoff is None
        else f"Record evidence for: {', '.join(missing_signoffs)}."
        if missing_signoffs
        else "All required host, security, operations, accessibility, and SLO sign-off references are recorded.",
    ))
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON without secret values")
    parser.add_argument("--strict", action="store_true", help="Return nonzero unless every check is PASS")
    parser.add_argument("--signoff-file", type=Path, help="JSON file containing completed deployment evidence references")
    args = parser.parse_args(argv)

    signoff = None
    if args.signoff_file:
        try:
            loaded = json.loads(args.signoff_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            print("Could not read a valid JSON sign-off file.", file=sys.stderr)
            return 2
        if not isinstance(loaded, dict):
            print("The sign-off file must contain a JSON object.", file=sys.stderr)
            return 2
        signoff = loaded
    checks = evaluate_preflight(PROJECT_ROOT, os.environ, signoff)
    if args.json:
        print(json.dumps(checks, indent=2))
    else:
        for check in checks:
            print(f"{check['status']:16} {check['id']}: {check['message']}")
        passed = sum(check["status"] == "PASS" for check in checks)
        print(f"\n{passed}/{len(checks)} checks passed.")

    failures = any(check["status"] == "FAIL" for check in checks)
    incomplete = any(check["status"] != "PASS" for check in checks)
    if failures or (args.strict and incomplete):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
