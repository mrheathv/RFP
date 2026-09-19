"""OpenAI backend.

Provided so the organization can A/B the two vendors on the same documents.
The schema handed in is already strict-mode ready (see ``schema_utils``), which
is exactly what ``response_format.json_schema`` with ``strict: true`` requires.
"""

from __future__ import annotations

from typing import Any

from ..errors import ProviderError
from .base import parse_json_response

# OpenAI has no public token counter on the client, and tiktoken's mapping is
# model-specific and drifts. Sizing falls back to the local estimate.
_CHARS_PER_TOKEN = 3.0


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str, *, timeout: float = 900.0):
        try:
            import openai
        except ImportError as exc:  # pragma: no cover
            raise ProviderError(
                "openai", "init", f"the openai package is not installed: {exc}"
            ) from exc

        self._openai = openai
        self._client = openai.OpenAI(api_key=api_key or None, timeout=timeout)

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
        # cache_system is accepted for interface parity; OpenAI caches long
        # prefixes automatically and exposes no per-request control.
        try:
            response = self._client.chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": f"{stage}_output",
                        "strict": True,
                        "schema": schema,
                    },
                },
            )
        except Exception as exc:
            raise self._wrap(exc, stage) from exc

        choice = response.choices[0]

        if getattr(choice.message, "refusal", None):
            raise ProviderError(
                self.name, stage, f"the model declined this request: {choice.message.refusal}"
            )

        if choice.finish_reason == "length":
            raise ProviderError(
                self.name,
                stage,
                f"response hit the {max_tokens}-token output limit and was cut off. "
                "Raise MAX_OUTPUT_TOKENS or lower MAX_CHUNK_TOKENS.",
            )

        return parse_json_response(choice.message.content or "", stage)

    def count_tokens(self, text: str, *, model: str) -> int:
        return int(len(text) / _CHARS_PER_TOKEN) + 1

    # ------------------------------------------------------------------

    def _wrap(self, exc: Exception, stage: str) -> ProviderError:
        o = self._openai

        if isinstance(exc, o.AuthenticationError):
            return ProviderError(
                self.name, stage, "authentication failed -- check OPENAI_API_KEY"
            )
        if isinstance(exc, o.PermissionDeniedError):
            return ProviderError(self.name, stage, "API key lacks permission for this model")
        if isinstance(exc, o.NotFoundError):
            return ProviderError(self.name, stage, f"model not found: {exc}")
        if isinstance(exc, o.RateLimitError):
            return ProviderError(self.name, stage, f"rate limited: {exc}", retryable=True)
        if isinstance(exc, o.BadRequestError):
            return ProviderError(self.name, stage, f"bad request: {exc}")
        if isinstance(exc, o.APIStatusError):
            return ProviderError(
                self.name,
                stage,
                f"API error {exc.status_code}: {exc}",
                retryable=exc.status_code >= 500,
            )
        if isinstance(exc, o.APIConnectionError):
            return ProviderError(self.name, stage, f"network error: {exc}", retryable=True)
        return ProviderError(self.name, stage, f"{type(exc).__name__}: {exc}")
