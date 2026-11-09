"""Request telemetry remains bounded and is exposed through the API."""

import re

from fastapi.testclient import TestClient
from fastapi.routing import APIRoute

import main
from observability import RequestMetrics


def test_request_metrics_normalize_labels_and_accumulate_histogram_buckets():
    metrics = RequestMetrics()
    metrics.observe("/api/v1/tasks/{task_id}", "get", 200, 50)
    metrics.observe("/api/v1/tasks/{task_id}", "GET", 404, 150)
    metrics.observe("/api/v1/tasks/{task_id}", "UNREGISTERED", 500, 70_000)

    observations, counts = metrics.snapshot()

    assert counts == {
        ("/api/v1/tasks/{task_id}", "GET", "2xx"): 1,
        ("/api/v1/tasks/{task_id}", "GET", "4xx"): 1,
        ("/api/v1/tasks/{task_id}", "OTHER", "5xx"): 1,
    }
    assert observations[("/api/v1/tasks/{task_id}", "GET")] == {
        "count": 2,
        "duration_sum": 200,
        "buckets": [1, 2, 2, 2, 2, 2],
    }
    assert observations[("/api/v1/tasks/{task_id}", "OTHER")]["buckets"] == [0, 0, 0, 0, 0, 0]


def test_request_id_and_request_metrics_are_exposed(monkeypatch):
    monkeypatch.setattr(main, "api_request_metrics", RequestMetrics())
    monkeypatch.setattr(main, "API_TOKEN", None)
    monkeypatch.setattr(main, "AUTH_USERS", {})
    client = TestClient(main.app)

    response = client.get("/api/v1/session")
    request_id = response.headers.get("X-Request-ID", "")
    assert response.status_code == 200
    assert re.fullmatch(r"[0-9a-f]{32}", request_id)

    exposition = client.get("/metrics").text
    assert 'relay_api_requests_total{route="/api/v1/session",method="GET",status_class="2xx"} 1' in exposition
    assert 'relay_api_request_duration_ms_count{route="/api/v1/session",method="GET"} 1' in exposition
    assert 'relay_api_request_duration_ms_bucket{route="/api/v1/session",method="GET",le="+Inf"} 1' in exposition


def test_unexpected_error_returns_safe_body_with_correlated_id_and_5xx_metric(monkeypatch):
    monkeypatch.setattr(main, "api_request_metrics", RequestMetrics())
    monkeypatch.setattr(main, "API_TOKEN", None)
    monkeypatch.setattr(main, "AUTH_USERS", {})

    def fail_unexpectedly():
        raise RuntimeError("private provider diagnostic must not reach the client")

    route = APIRoute("/__test__/unexpected-error", fail_unexpectedly, methods=["GET"])
    main.app.router.routes.append(route)
    try:
        response = TestClient(main.app, raise_server_exceptions=False).get(route.path)
        request_id = response.headers.get("X-Request-ID")

        assert response.status_code == 500
        assert request_id and response.json() == {
            "detail": "Internal server error.",
            "request_id": request_id,
        }
        assert "private provider diagnostic" not in response.text

        exposition = TestClient(main.app).get("/metrics").text
        assert (
            'relay_api_requests_total{route="/__test__/unexpected-error",method="GET",status_class="5xx"} 1'
            in exposition
        )
    finally:
        main.app.router.routes.remove(route)
