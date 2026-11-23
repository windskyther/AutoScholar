"""Normalize provider-reported usage without inventing missing cache statistics."""

from typing import Any

from autoscholar.llm.models import TokenUsage


def _value(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def parse_usage(raw: Any) -> TokenUsage:
    input_tokens = raw.prompt_tokens
    output_tokens = raw.completion_tokens
    hit = _count(_value(raw, "prompt_cache_hit_tokens"))
    miss = _count(_value(raw, "prompt_cache_miss_tokens"))
    detail = _count(_value(_value(raw, "prompt_tokens_details"), "cached_tokens"))
    invalid = any(
        _value(raw, key) is not None and _count(_value(raw, key)) is None
        for key in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens")
    )
    if hit is not None and detail is not None and hit != detail:
        invalid = True
    hit = hit if hit is not None else detail
    if hit is None and miss is not None and miss <= input_tokens:
        hit = input_tokens - miss
    if miss is None and hit is not None and hit <= input_tokens:
        miss = input_tokens - hit
    if invalid or hit is None or miss is None or hit + miss != input_tokens:
        hit = miss = None
    reasoning = _count(_value(_value(raw, "completion_tokens_details"), "reasoning_tokens"))
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=raw.total_tokens,
        cached_input_tokens=hit,
        uncached_input_tokens=miss,
        reasoning_tokens=reasoning
        if reasoning is not None and reasoning <= output_tokens
        else None,
    )
