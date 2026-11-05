"""Operator-configured model pricing for transparent, explicitly labeled estimates."""

from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation


def configured_gemini_pricing() -> tuple[Decimal, Decimal, str] | None:
    """Return input/output USD per million tokens and a required operator label."""
    input_value = os.environ.get("RELAY_GEMINI_INPUT_USD_PER_MILLION")
    output_value = os.environ.get("RELAY_GEMINI_OUTPUT_USD_PER_MILLION")
    label = os.environ.get("RELAY_GEMINI_PRICING_LABEL", "").strip()
    if input_value is None and output_value is None:
        if label:
            raise ValueError("RELAY_GEMINI_PRICING_LABEL requires both Gemini token rate settings.")
        return None
    if input_value is None or output_value is None:
        raise ValueError("Configure both Gemini input and output rates to enable estimated cost attribution.")
    if not label or len(label) > 200:
        raise ValueError("RELAY_GEMINI_PRICING_LABEL must identify the operator's rate source (max 200 characters).")
    try:
        input_rate = Decimal(input_value)
        output_rate = Decimal(output_value)
    except InvalidOperation as error:
        raise ValueError("Gemini token rates must be valid non-negative decimal USD amounts.") from error
    if (
        not input_rate.is_finite() or not output_rate.is_finite()
        or input_rate < 0 or output_rate < 0
        or input_rate > Decimal("1000000") or output_rate > Decimal("1000000")
    ):
        raise ValueError("Gemini token rates must be finite decimal USD amounts between 0 and 1,000,000 per million tokens.")
    return input_rate, output_rate, label


def estimate_gemini_cost_usd(
    prompt_tokens: int,
    candidate_tokens: int,
    pricing: tuple[Decimal, Decimal, str],
) -> Decimal:
    input_rate, output_rate, _ = pricing
    return (
        Decimal(prompt_tokens) * input_rate + Decimal(candidate_tokens) * output_rate
    ) / Decimal(1_000_000)
