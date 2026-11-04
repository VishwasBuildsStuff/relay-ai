"""Model provider boundary for production Gemini calls and deterministic local workflows."""

from __future__ import annotations

from typing import Any, Dict, Protocol, Type

from google import genai
from google.genai import types


class StructuredProvider(Protocol):
    def generate(self, system_instruction: str, prompt: str, schema_cls: Type[Any]) -> Any:
        """Return a validated instance of schema_cls."""


class GeminiStructuredProvider:
    def __init__(self, api_key: str, model_name: str):
        self.client = genai.Client(api_key=api_key)
        self.model_name = model_name

    def generate(self, system_instruction: str, prompt: str, schema_cls: Type[Any]) -> Any:
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            response_mime_type="application/json",
            response_schema=schema_cls,
            temperature=0.2,
        )
        response = self.client.models.generate_content(
            model=self.model_name,
            contents=prompt,
            config=config,
        )
        if not response.text:
            raise ValueError("Empty response received from Gemini model.")
        return schema_cls.model_validate_json(response.text)


class FakeStructuredProvider:
    """Deterministic provider for development; outputs must be supplied explicitly."""

    def __init__(self, responses: Dict[Type[Any], Any]):
        self.responses = responses

    def generate(self, system_instruction: str, prompt: str, schema_cls: Type[Any]) -> Any:
        if schema_cls not in self.responses:
            raise ValueError(f"No fake response configured for {schema_cls.__name__}.")
        result = self.responses[schema_cls]
        if isinstance(result, schema_cls):
            return result
        if isinstance(result, str):
            return schema_cls.model_validate_json(result)
        return schema_cls.model_validate(result)
