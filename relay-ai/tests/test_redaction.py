"""Regression checks for secrets in API diagnostics and structured logs."""

import json
import logging
import sys

from observability import JsonLogFormatter
from redaction import redact_sensitive_text


def test_redacts_configured_credentials_and_common_token_formats(monkeypatch):
    gemini_key = "AIza" + "G" * 32
    shared_token = "relay-shared-token-123456"
    user_token = "relay-owner-token-654321"
    github_token = "ghp_" + "H" * 36
    openai_token = "sk-" + "O" * 28
    slack_token = "xoxb-" + "S" * 30
    aws_key = "AKIA" + "A" * 16
    monkeypatch.setenv("GEMINI_API_KEY", gemini_key)
    monkeypatch.setenv("RELAY_API_TOKEN", shared_token)
    monkeypatch.setenv("RELAY_AUTH_USERS_JSON", json.dumps({"owner": user_token}))

    sensitive = " ".join((
        f"api_key={gemini_key}",
        f"bearer {shared_token}",
        user_token,
        github_token,
        openai_token,
        slack_token,
        aws_key,
        'password="database-password"',
        "-----BEGIN PRIVATE KEY-----private-material-----END PRIVATE KEY-----",
    ))
    redacted = redact_sensitive_text(sensitive)

    for secret in (
        gemini_key, shared_token, user_token, github_token, openai_token,
        slack_token, aws_key, "database-password", "private-material",
    ):
        assert secret not in redacted
    assert "[REDACTED" in redacted


def test_json_log_formatter_redacts_message_and_exception_traceback(monkeypatch):
    secret = "log-api-token-987654321"
    monkeypatch.setenv("RELAY_API_TOKEN", secret)
    try:
        raise RuntimeError(f"upstream failure api_key={secret}")
    except RuntimeError:
        record = logging.LogRecord(
            name="relay.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg=f"Worker failed bearer {secret}",
            args=(),
            exc_info=sys.exc_info(),
        )

    rendered = JsonLogFormatter().format(record)
    parsed = json.loads(rendered)
    assert secret not in parsed["message"]
    assert secret not in parsed["exception"]
    assert "[REDACTED]" in parsed["message"]
    assert "[REDACTED]" in parsed["exception"]
