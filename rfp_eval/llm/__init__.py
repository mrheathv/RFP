"""Provider selection."""

from __future__ import annotations

from ..config import ANTHROPIC, FAKE, OPENAI, Settings
from ..errors import ConfigError
from .base import LLMProvider, parse_json_response

__all__ = ["LLMProvider", "get_provider", "parse_json_response"]


def get_provider(settings: Settings) -> LLMProvider:
    """Build the provider named by ``settings``.

    Validation happens here rather than at import so the UI can render and let
    the user fix a missing key instead of crashing on startup.
    """
    settings.validate()

    if settings.provider == ANTHROPIC:
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(settings.anthropic_api_key, effort=settings.effort)

    if settings.provider == OPENAI:
        from .openai_provider import OpenAIProvider

        return OpenAIProvider(settings.openai_api_key)

    if settings.provider == FAKE:
        from .fake import FakeProvider

        return FakeProvider()

    raise ConfigError(f"unknown provider: {settings.provider!r}")
