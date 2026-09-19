"""Configuration loaded from the environment (and overridable per run).

``Settings`` is a plain dataclass with no Streamlit dependency, so the same
config drives the UI, the tests, and any future batch runner.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from dotenv import load_dotenv

from .errors import ConfigError

# Load .env once at import. Existing environment variables win, so a shell
# export or a Streamlit secret always overrides the file.
load_dotenv(override=False)

ANTHROPIC = "anthropic"
OPENAI = "openai"
FAKE = "fake"

VALID_PROVIDERS = (ANTHROPIC, OPENAI, FAKE)
VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class Settings:
    provider: str = ANTHROPIC

    anthropic_api_key: str = ""
    openai_api_key: str = ""

    stage0_model: str = "claude-opus-5"
    stage1_model: str = "claude-opus-5"
    stage2_model: str = "claude-opus-5"

    effort: str = "high"
    max_chunk_tokens: int = 60_000
    max_output_tokens: int = 32_000

    stage2_include_verbatim: bool = False
    include_appendix: bool = True

    data_dir: Path = field(default_factory=lambda: Path("data"))

    # ---------------------------------------------------------------- load

    @classmethod
    def from_env(cls) -> "Settings":
        provider = (_env("LLM_PROVIDER", ANTHROPIC) or ANTHROPIC).lower()
        if provider not in VALID_PROVIDERS:
            raise ConfigError(
                f"LLM_PROVIDER must be one of {', '.join(VALID_PROVIDERS)}, got {provider!r}"
            )

        # Model defaults differ per provider, so read the matching trio.
        if provider == OPENAI:
            s0 = _env("OPENAI_STAGE0_MODEL", "gpt-4o")
            s1 = _env("OPENAI_STAGE1_MODEL", "gpt-4o")
            s2 = _env("OPENAI_STAGE2_MODEL", "gpt-4o")
        else:
            s0 = _env("STAGE0_MODEL", "claude-opus-5")
            s1 = _env("STAGE1_MODEL", "claude-opus-5")
            s2 = _env("STAGE2_MODEL", "claude-opus-5")

        effort = (_env("ANTHROPIC_EFFORT", "high") or "high").lower()
        if effort not in VALID_EFFORTS:
            effort = "high"

        return cls(
            provider=provider,
            anthropic_api_key=_env("ANTHROPIC_API_KEY"),
            openai_api_key=_env("OPENAI_API_KEY"),
            stage0_model=s0,
            stage1_model=s1,
            stage2_model=s2,
            effort=effort,
            max_chunk_tokens=_env_int("MAX_CHUNK_TOKENS", 60_000),
            max_output_tokens=_env_int("MAX_OUTPUT_TOKENS", 32_000),
            stage2_include_verbatim=_env_bool("STAGE2_INCLUDE_VERBATIM", False),
            include_appendix=_env_bool("INCLUDE_APPENDIX", True),
            data_dir=Path(_env("DATA_DIR", "data")),
        )

    def with_overrides(self, **kwargs) -> "Settings":
        """Return a copy with fields replaced -- used by the sidebar controls."""
        clean = {k: v for k, v in kwargs.items() if v is not None}
        return replace(self, **clean)

    # ------------------------------------------------------------ helpers

    def model_for(self, stage: str) -> str:
        return {
            "stage0": self.stage0_model,
            "stage1": self.stage1_model,
            "stage2": self.stage2_model,
        }.get(stage, self.stage1_model)

    def validate(self) -> None:
        """Raise ConfigError if the selected provider cannot actually run."""
        if self.provider == ANTHROPIC and not self.anthropic_api_key:
            raise ConfigError(
                "LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set. "
                "Add it to .env, or switch the provider to 'fake' to run offline."
            )
        if self.provider == OPENAI and not self.openai_api_key:
            raise ConfigError(
                "LLM_PROVIDER=openai but OPENAI_API_KEY is not set. "
                "Add it to .env, or switch the provider to 'fake' to run offline."
            )
        if self.max_chunk_tokens < 1000:
            raise ConfigError("MAX_CHUNK_TOKENS must be at least 1000.")

    def snapshot(self) -> dict:
        """Config recorded into the run manifest. Deliberately omits API keys."""
        return {
            "provider": self.provider,
            "stage0_model": self.stage0_model,
            "stage1_model": self.stage1_model,
            "stage2_model": self.stage2_model,
            "effort": self.effort,
            "max_chunk_tokens": self.max_chunk_tokens,
            "max_output_tokens": self.max_output_tokens,
            "stage2_include_verbatim": self.stage2_include_verbatim,
        }
