"""Exception types shared across the pipeline.

Each carries enough context for the UI to tell the user *which* file or vendor
failed, which is the difference between a useful error and a stack trace.
"""


class RfpEvalError(Exception):
    """Base class for every error this package raises deliberately."""


class ParseError(RfpEvalError):
    """A source document could not be read or contained no usable text."""

    def __init__(self, filename: str, reason: str):
        self.filename = filename
        self.reason = reason
        super().__init__(f"{filename}: {reason}")


class ProviderError(RfpEvalError):
    """An LLM call failed. Wraps the provider's own message verbatim."""

    def __init__(self, provider: str, stage: str, reason: str, retryable: bool = False):
        self.provider = provider
        self.stage = stage
        self.reason = reason
        self.retryable = retryable
        super().__init__(f"[{provider}/{stage}] {reason}")


class SchemaError(RfpEvalError):
    """The model returned JSON that did not satisfy the expected schema."""

    def __init__(self, stage: str, reason: str, raw: str | None = None):
        self.stage = stage
        self.reason = reason
        # Truncated for display only -- the full payload is written to the run
        # directory so a bad extraction can be inspected after the fact.
        self.raw = raw
        super().__init__(f"[{stage}] {reason}")


class VendorFailure(RfpEvalError):
    """One vendor's Stage 1 run failed. Never aborts the other vendors."""

    def __init__(self, vendor_name: str, reason: str):
        self.vendor_name = vendor_name
        self.reason = reason
        super().__init__(f"{vendor_name}: {reason}")


class ConfigError(RfpEvalError):
    """Missing or contradictory configuration (e.g. no API key for a provider)."""
