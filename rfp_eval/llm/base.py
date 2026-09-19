"""The provider interface every LLM backend implements.

One method carries the pipeline: ``complete_json``. Every stage asks for
structured JSON against a schema, which is what makes the map-reduce split
work -- Stage 2 gets clean typed input instead of free text it has to re-parse.
"""

from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable

from ..errors import SchemaError


@runtime_checkable
class LLMProvider(Protocol):
    """Structured-output LLM access."""

    name: str

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        model: str,
        max_tokens: int,
        stage: str,
        cache_system: bool = False,
    ) -> dict[str, Any]:
        """Return JSON conforming to ``schema``.

        ``cache_system`` asks the provider to cache the system prefix where it
        can -- Stage 1 sends a byte-identical system prompt and question list
        for every vendor in a run, which is the bulk of its repeated input.
        """
        ...

    def count_tokens(self, text: str, *, model: str) -> int:
        """Token count for ``text``. Used to verify chunk sizes."""
        ...


def parse_json_response(raw: str, stage: str) -> dict[str, Any]:
    """Parse a model's JSON response, with a tolerant fallback.

    Strict schema modes guarantee clean JSON, but a provider without strict
    support (or a refusal) can return prose. Rather than crash the run, dig out
    the outermost JSON object if there is one.
    """
    raw = (raw or "").strip()
    if not raw:
        raise SchemaError(stage, "model returned an empty response")

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            raise SchemaError(
                stage, "model did not return JSON", raw=raw[:2000]
            ) from None
        try:
            parsed = json.loads(raw[start : end + 1])
        except json.JSONDecodeError as exc:
            raise SchemaError(
                stage, f"model returned malformed JSON: {exc}", raw=raw[:2000]
            ) from exc

    if not isinstance(parsed, dict):
        raise SchemaError(
            stage, f"expected a JSON object, got {type(parsed).__name__}", raw=raw[:2000]
        )
    return parsed
