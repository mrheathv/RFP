"""Anthropic (Claude) backend.

Streaming is used for every call: Stage 1 sends whole vendor documents and
asks for large structured outputs, and a non-streaming request at those sizes
risks an HTTP timeout. ``get_final_message()`` gives us the assembled response
without having to handle individual stream events.
"""

from __future__ import annotations

from typing import Any

from ..errors import ProviderError
from .base import parse_json_response

# Models that accept output_config.effort and adaptive thinking. Anything else
# (an older model a user pins in .env) gets a plain request.
_EFFORT_CAPABLE_PREFIXES = ("claude-opus-5", "claude-opus-4-", "claude-sonnet-5", "claude-fable-")


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, *, effort: str = "high", timeout: float = 900.0):
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise ProviderError(
                "anthropic", "init", f"the anthropic package is not installed: {exc}"
            ) from exc

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=api_key or None, timeout=timeout)
        self._effort = effort

    # ------------------------------------------------------------------

    def _supports_effort(self, model: str) -> bool:
        return any(model.startswith(p) for p in _EFFORT_CAPABLE_PREFIXES)

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
        # A cached system prefix pays off across vendors in the same run, where
        # the system prompt and question list are byte-identical every time.
        system_param: Any = system
        if cache_system:
            system_param = [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]

        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system_param,
            "messages": [{"role": "user", "content": user}],
            "output_config": {"format": {"type": "json_schema", "schema": schema}},
        }

        if self._supports_effort(model):
            kwargs["output_config"]["effort"] = self._effort
            kwargs["thinking"] = {"type": "adaptive"}

        try:
            with self._client.messages.stream(**kwargs) as stream:
                message = stream.get_final_message()
        except Exception as exc:
            raise self._wrap(exc, stage) from exc

        if getattr(message, "stop_reason", None) == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None) or "unspecified"
            raise ProviderError(
                self.name,
                stage,
                f"the model declined this request (category: {category}). "
                "This usually means the document content tripped a safety classifier.",
            )

        if getattr(message, "stop_reason", None) == "max_tokens":
            raise ProviderError(
                self.name,
                stage,
                f"response hit the {max_tokens}-token output limit and was cut off. "
                "Raise MAX_OUTPUT_TOKENS or lower MAX_CHUNK_TOKENS so each call "
                "has less to summarize.",
            )

        text = "".join(
            block.text for block in message.content if getattr(block, "type", "") == "text"
        )
        return parse_json_response(text, stage)

    def count_tokens(self, text: str, *, model: str) -> int:
        try:
            result = self._client.messages.count_tokens(
                model=model,
                messages=[{"role": "user", "content": text}],
            )
            return int(result.input_tokens)
        except Exception as exc:
            raise self._wrap(exc, "count_tokens") from exc

    # ------------------------------------------------------------------

    def _wrap(self, exc: Exception, stage: str) -> ProviderError:
        """Map SDK exceptions onto ProviderError, preserving the real message.

        Most specific first. ``retryable`` tells the caller whether trying the
        same call again could plausibly succeed.
        """
        a = self._anthropic

        if isinstance(exc, a.AuthenticationError):
            return ProviderError(
                self.name, stage, "authentication failed -- check ANTHROPIC_API_KEY"
            )
        if isinstance(exc, a.PermissionDeniedError):
            return ProviderError(self.name, stage, "API key lacks permission for this model")
        if isinstance(exc, a.NotFoundError):
            return ProviderError(
                self.name, stage, f"model not found or unavailable: {exc}"
            )
        if isinstance(exc, a.RateLimitError):
            return ProviderError(self.name, stage, f"rate limited: {exc}", retryable=True)
        if isinstance(exc, a.BadRequestError):
            return ProviderError(self.name, stage, f"bad request: {exc}")
        if isinstance(exc, a.APIStatusError):
            return ProviderError(
                self.name,
                stage,
                f"API error {exc.status_code}: {exc}",
                retryable=exc.status_code >= 500,
            )
        if isinstance(exc, a.APIConnectionError):
            return ProviderError(
                self.name, stage, f"network error: {exc}", retryable=True
            )
        return ProviderError(self.name, stage, f"{type(exc).__name__}: {exc}")
