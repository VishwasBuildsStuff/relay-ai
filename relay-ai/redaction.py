"""Best-effort removal of credentials from diagnostics and execution output."""

from __future__ import annotations

import json
import os
import re


_SENSITIVE_ENV_NAME = re.compile(r"(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.IGNORECASE)
_KEY_VALUE_SECRET = re.compile(
    r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|passwd|secret|authorization)[\"']?\s*[:=]\s*[\"']?)(?:bearer\s+)?([^\s\"',;}]+)"
)
_BEARER = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_GOOGLE_KEY = re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b")
_GITHUB_TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
_OPENAI_TOKEN = re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")
_SLACK_TOKEN = re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")
_AWS_ACCESS_KEY = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
    re.DOTALL,
)


def _configured_secrets() -> list[str]:
    secrets: set[str] = set()
    for name, value in os.environ.items():
        if _SENSITIVE_ENV_NAME.search(name) and value and len(value) >= 6:
            secrets.add(value)
    try:
        users = json.loads(os.environ.get("RELAY_AUTH_USERS_JSON", "{}"))
        if isinstance(users, dict):
            secrets.update(value for value in users.values() if isinstance(value, str) and len(value) >= 6)
    except (TypeError, ValueError):
        pass
    return sorted(secrets, key=len, reverse=True)


def redact_sensitive_text(value: object) -> str:
    """Redact configured credentials and common token formats from a diagnostic string."""
    text = str(value)
    for secret in _configured_secrets():
        text = text.replace(secret, "[REDACTED]")
    text = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", text)
    text = _KEY_VALUE_SECRET.sub(r"\1[REDACTED]", text)
    text = _BEARER.sub(r"\1[REDACTED]", text)
    text = _GOOGLE_KEY.sub("[REDACTED GOOGLE API KEY]", text)
    text = _GITHUB_TOKEN.sub("[REDACTED GITHUB TOKEN]", text)
    text = _OPENAI_TOKEN.sub("[REDACTED API TOKEN]", text)
    text = _SLACK_TOKEN.sub("[REDACTED SLACK TOKEN]", text)
    return _AWS_ACCESS_KEY.sub("[REDACTED AWS ACCESS KEY]", text)
