"""Exercise Gemini adapter retry and failure behavior without network calls."""

import json
from types import SimpleNamespace

import pytest
from google.genai.errors import APIError

import providers
from schemas import TriageResult


def _triage_json():
    return json.dumps({
        "title": "Example issue",
        "severity": "low",
        "acceptance_criteria": ["The example returns a result."],
        "rationale": "Deterministic provider test.",
    })


def test_gemini_adapter_retries_transient_error_and_records_usage(monkeypatch):
    class ScriptedModels:
        def __init__(self):
            self.calls = []
            self.outcomes = [
                APIError(429, {"error": {"message": "try again", "status": "RESOURCE_EXHAUSTED"}}),
                SimpleNamespace(
                    text=_triage_json(),
                    usage_metadata=SimpleNamespace(
                        prompt_token_count=17,
                        candidates_token_count=9,
                        total_token_count=26,
                    ),
                ),
            ]

        def generate_content(self, **kwargs):
            self.calls.append(kwargs)
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    models = ScriptedModels()
    monkeypatch.setattr(providers.genai, "Client", lambda api_key: SimpleNamespace(models=models))
    delays = []
    monkeypatch.setattr(providers.time, "sleep", delays.append)
    monkeypatch.setattr(providers.random, "uniform", lambda _start, _end: 0.0)

    provider = providers.GeminiStructuredProvider("test-key", "test-model")
    result = provider.generate("system", "prompt", TriageResult)

    assert isinstance(result, TriageResult)
    assert result.title == "Example issue"
    assert len(models.calls) == 2
    assert models.calls[-1]["model"] == "test-model"
    assert models.calls[-1]["contents"] == "prompt"
    assert models.calls[-1]["config"].response_schema is TriageResult
    assert delays == [0.5]
    assert provider.last_usage == {
        "prompt_tokens": 17,
        "candidate_tokens": 9,
        "total_tokens": 26,
    }


@pytest.mark.parametrize("code", [400, 429, 503])
def test_gemini_adapter_stops_after_non_retryable_or_retry_exhaustion(monkeypatch, code):
    class AlwaysFailingModels:
        def __init__(self):
            self.calls = 0

        def generate_content(self, **_kwargs):
            self.calls += 1
            raise APIError(code, {"error": {"message": "provider unavailable", "status": "UNAVAILABLE"}})

    models = AlwaysFailingModels()
    monkeypatch.setattr(providers.genai, "Client", lambda api_key: SimpleNamespace(models=models))
    delays = []
    monkeypatch.setattr(providers.time, "sleep", delays.append)
    monkeypatch.setattr(providers.random, "uniform", lambda _start, _end: 0.0)

    provider = providers.GeminiStructuredProvider("test-key", "test-model")
    with pytest.raises(APIError):
        provider.generate("system", "prompt", TriageResult)

    expected_calls = 1 if code == 400 else provider.max_attempts
    assert models.calls == expected_calls
    assert len(delays) == expected_calls - 1
