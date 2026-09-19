"""Shared helpers for feeding documents to prompts without losing content."""

from __future__ import annotations

from ..parsing import Document
from ..parsing.chunking import Chunk, chunk_document


def chunks_for(
    document: Document,
    max_chunk_tokens: int,
    *,
    provider=None,
    model: str = "",
) -> list[Chunk]:
    """Split ``document`` to fit the per-call budget, verified where possible."""
    counter = None
    if provider is not None and model:
        counter = lambda text: provider.count_tokens(text, model=model)  # noqa: E731
    return chunk_document(document, max_chunk_tokens, count_tokens=counter)
