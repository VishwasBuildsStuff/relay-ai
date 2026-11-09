import json
from pathlib import Path

from preflight import REQUIRED_CSP_DIRECTIVES, SIGNOFF_FIELDS, evaluate_preflight


def test_preflight_reports_ready_local_configuration_without_disclosing_tokens(tmp_path):
    root = tmp_path
    (root / "dist" / "assets").mkdir(parents=True)
    (root / "dist" / "assets" / "app.js").write_text("// app")
    (root / "dist" / "index.html").write_text('<script type="module" src="/assets/app.js"></script>')
    (root / "dist" / "relay-mark.svg").write_text("<svg />")
    (root / "deploy" / "nginx").mkdir(parents=True)
    (root / "deploy" / "nginx" / "relay-ai.conf").write_text("server_name relay.example.com;")
    secret = "preflight-test-token-do-not-print"
    env = {
        "RELAY_ENV": "production",
        "RELAY_AUTH_USERS_JSON": json.dumps({"operator": secret}),
        "RELAY_CORS_ORIGINS": "https://relay.example.test",
        "RELAY_SANDBOX_IMAGE": "sha256:" + "a" * 64,
        "RELAY_ALLOW_UNSAFE_SUBPROCESS": "false",
    }

    checks = evaluate_preflight(root, env)
    statuses = {check["id"]: check["status"] for check in checks}
    rendered = json.dumps(checks)

    assert statuses["production-mode"] == "PASS"
    assert statuses["authentication"] == "PASS"
    assert statuses["cors"] == "PASS"
    assert statuses["sandbox-image"] == "PASS"
    assert statuses["frontend-build"] == "PASS"
    assert statuses["nginx-template"] == "ACTION REQUIRED"
    assert statuses["delivery-headers"] == "FAIL"
    assert secret not in rendered


def test_preflight_flags_missing_and_unsafe_production_configuration(tmp_path):
    checks = evaluate_preflight(tmp_path, {
        "RELAY_ENV": "production",
        "RELAY_API_TOKEN": "token",
        "RELAY_CORS_ORIGINS": "http://insecure.example.test",
        "RELAY_SANDBOX_IMAGE": "relay-ai-sandbox:latest",
        "RELAY_ALLOW_UNSAFE_SUBPROCESS": "true",
    })
    statuses = {check["id"]: check["status"] for check in checks}

    assert statuses["authentication"] == "PASS"
    assert statuses["cors"] == "ACTION REQUIRED"
    assert statuses["sandbox-image"] == "ACTION REQUIRED"
    assert statuses["host-subprocess"] == "FAIL"
    assert statuses["frontend-build"] == "ACTION REQUIRED"


def test_preflight_rejects_frontend_assets_resolving_outside_dist(tmp_path):
    from preflight import _check_frontend

    dist = tmp_path / "dist"
    dist.mkdir()
    (tmp_path / "outside.js").write_text("// outside the build")
    (dist / "relay-mark.svg").write_text("<svg />")
    (dist / "index.html").write_text('<script src="/assets/../../outside.js"></script>')

    check = _check_frontend(tmp_path)

    assert check["status"] == "FAIL"
    assert "assets are missing" in check["message"]


def test_shared_token_does_not_mask_invalid_user_or_role_configuration():
    from preflight import _auth_is_configured

    valid, message = _auth_is_configured({
        "RELAY_API_TOKEN": "shared",
        "RELAY_AUTH_USERS_JSON": "not-json",
    })
    assert valid is False
    assert "not valid JSON" in message

    valid, message = _auth_is_configured({
        "RELAY_API_TOKEN": "shared",
        "RELAY_AUTH_USERS_JSON": '{"operator":"token"}',
        "RELAY_AUTH_ROLES_JSON": '{"missing":"admin"}',
    })
    assert valid is False
    assert "configured usernames" in message


def test_complete_evidence_signoffs_clear_manual_release_gates(tmp_path):
    (tmp_path / "dist" / "assets").mkdir(parents=True)
    (tmp_path / "dist" / "assets" / "app.js").write_text("// app")
    (tmp_path / "dist" / "relay-mark.svg").write_text("<svg />")
    (tmp_path / "dist" / "index.html").write_text('<script src="/assets/app.js"></script>')
    (tmp_path / "deploy" / "nginx").mkdir(parents=True)
    from preflight import REQUIRED_DELIVERY_HEADERS

    nginx = "server_name relay.example.test;\n" + "\n".join(
        f'add_header {header} "{("; ".join(REQUIRED_CSP_DIRECTIVES) + "; connect-src \'self\' https:") if header == "Content-Security-Policy" else "configured"}" always;'
        for header in REQUIRED_DELIVERY_HEADERS
    )
    (tmp_path / "deploy" / "nginx" / "relay-ai.conf").write_text(nginx)
    signoff = {field: f"evidence: {field}" for field in SIGNOFF_FIELDS}

    checks = evaluate_preflight(tmp_path, {
        "RELAY_ENV": "production",
        "RELAY_API_TOKEN": "configured",
        "RELAY_CORS_ORIGINS": "https://relay.example.test",
        "RELAY_SANDBOX_IMAGE": "sha256:" + "a" * 64,
        "RELAY_ALLOW_UNSAFE_SUBPROCESS": "false",
    }, signoff)
    statuses = {check["id"]: check["status"] for check in checks}

    assert statuses["nginx-template"] == "PASS"
    assert statuses["delivery-headers"] == "PASS"
    assert statuses["host-and-owner-signoff"] == "PASS"
    assert all(status == "PASS" for status in statuses.values())


def test_preflight_rejects_permissive_content_security_policy(tmp_path):
    (tmp_path / "deploy" / "nginx").mkdir(parents=True)
    from preflight import REQUIRED_DELIVERY_HEADERS

    nginx = "server_name relay.example.test;\n" + "\n".join(
        f'add_header {header} "{"default-src *" if header == "Content-Security-Policy" else "configured"}" always;'
        for header in REQUIRED_DELIVERY_HEADERS
    )
    (tmp_path / "deploy" / "nginx" / "relay-ai.conf").write_text(nginx)

    checks = evaluate_preflight(tmp_path, {})
    delivery = next(check for check in checks if check["id"] == "delivery-headers")

    assert delivery["status"] == "FAIL"
    assert "CSP directive" in delivery["message"]
