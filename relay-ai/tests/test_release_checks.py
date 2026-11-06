"""Local production configuration, browser-origin, and authorization checks."""

import json
import os
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from main import app


RELAY_DIR = Path(__file__).resolve().parents[1]
PINNED_TEST_IMAGE = "sha256:" + "a" * 64


def _import_production(tmp_path, **overrides):
    environment = os.environ.copy()
    for key in (
        "RELAY_API_TOKEN", "RELAY_AUTH_USERS_JSON", "RELAY_AUTH_ROLES_JSON",
        "RELAY_CORS_ORIGINS", "RELAY_SANDBOX_IMAGE", "RELAY_ALLOW_UNSAFE_SUBPROCESS",
    ):
        environment.pop(key, None)
    environment.update({
        "RELAY_ENV": "production",
        "RELAY_AUTH_USERS_JSON": json.dumps({"release-check": "local-test-token"}),
        "RELAY_AUTH_ROLES_JSON": json.dumps({"release-check": "operator"}),
        "RELAY_CORS_ORIGINS": "https://relay.example.test",
        "RELAY_SANDBOX_IMAGE": PINNED_TEST_IMAGE,
        "RELAY_DB_PATH": str(tmp_path / "release.sqlite3"),
        "RELAY_APPLIED_ROOT": str(tmp_path / "applied"),
    })
    environment.update(overrides)
    return subprocess.run(
        [sys.executable, "-c", "import main"],
        cwd=RELAY_DIR,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_production_configuration_imports_with_explicit_auth_cors_and_digest(tmp_path):
    result = _import_production(tmp_path)
    assert result.returncode == 0, result.stderr


def test_production_configuration_rejects_insecure_cors_origins(tmp_path):
    for origin in (
        "*",
        "http://relay.example.test",
        "https://relay.example.test/path",
        "https://user:pass@relay.example.test",
        "https://relay.example.test/?x=1",
    ):
        result = _import_production(tmp_path / str(len(origin)), RELAY_CORS_ORIGINS=origin)
        assert result.returncode != 0, origin
        assert "CORS" in result.stderr, result.stderr


def test_production_configuration_requires_auth_and_immutable_sandbox_image(tmp_path):
    no_auth = _import_production(
        tmp_path / "no-auth",
        RELAY_AUTH_USERS_JSON="{}",
        RELAY_AUTH_ROLES_JSON="{}",
    )
    assert no_auth.returncode != 0
    assert "requires RELAY_API_TOKEN" in no_auth.stderr

    mutable_image = _import_production(tmp_path / "mutable-image", RELAY_SANDBOX_IMAGE="relay-ai-sandbox:latest")
    assert mutable_image.returncode != 0
    assert "SHA-256" in mutable_image.stderr


def test_browser_origin_and_user_roles_are_enforced(monkeypatch):
    monkeypatch.setattr("main.AUTH_USERS", {"viewer": "viewer-token", "operator": "operator-token"})
    monkeypatch.setattr("main.AUTH_ROLES", {"viewer": "viewer", "operator": "operator"})
    monkeypatch.setattr("main._auth_roles_configured", True)
    monkeypatch.setattr("main.API_TOKEN", None)
    client = TestClient(app)

    assert client.get("/health").status_code == 401
    viewer = client.get("/api/v1/session", headers={"Authorization": "Bearer viewer-token"})
    assert viewer.status_code == 200 and viewer.json()["role"] == "viewer"
    assert client.post("/api/v1/tasks", json={}, headers={"Authorization": "Bearer viewer-token"}).status_code == 403
    operator = client.get("/api/v1/session", headers={"Authorization": "Bearer operator-token"})
    assert operator.status_code == 200 and operator.json()["role"] == "operator"

    allowed = client.options(
        "/api/v1/session",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"},
    )
    rejected = client.options(
        "/api/v1/session",
        headers={"Origin": "https://attacker.example", "Access-Control-Request-Method": "GET"},
    )
    assert allowed.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert "access-control-allow-origin" not in rejected.headers
