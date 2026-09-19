"""Split a parsed document into token-budgeted chunks.

The contract this module upholds: **nothing is ever dropped**. Every character
of every input unit appears in exactly one output chunk. A vendor document too
long for one call is split and each piece is sent, rather than truncated.

Splitting happens between ``TextUnit`` boundaries wherever possible, so a
spreadsheet row or a table stays intact. Only a single unit that is itself
larger than the budget gets divided internally, and that is recorded on the
chunk so the caller can surface it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from . import Document, TextUnit

# Conservative characters-per-token ratio used to size chunks before any
# verification call. English prose runs ~4 chars/token; tables and identifier
# soup run denser, so we assume 3.0 and let verification catch the rest.
CHARS_PER_TOKEN = 3.0

# Never build a chunk from an empty budget.
MIN_CHUNK_CHARS = 500


@dataclass
class Chunk:
    """One unit of work for a Stage 1 extraction call."""

    index: int
    total: int
    units: list[TextUnit] = field(default_factory=list)
    split_units: int = 0  # how many oversized units had to be divided

    @property
    def text(self) -> str:
        return "\n\n".join(u.rendered() for u in self.units)

    @property
    def char_count(self) -> int:
        return sum(len(u.text) for u in self.units)

    @property
    def locator_range(self) -> str:
        """Human-readable span, for progress messages."""
        if not self.units:
            return ""
        first, last = self.units[0].locator, self.units[-1].locator
        return first if first == last else f"{first} – {last}"


def estimate_tokens(text: str) -> int:
    """Cheap local estimate. Only used for sizing, never for billing claims."""
    return int(len(text) / CHARS_PER_TOKEN) + 1


def _split_oversized(unit: TextUnit, budget_chars: int) -> list[TextUnit]:
    """Divide a single unit larger than the whole budget.

    Prefers line boundaries, then sentence boundaries, and falls back to a hard
    character split. Pieces keep the original locator with a part suffix.
    """
    pieces: list[str] = []
    buffer = ""

    # Split on line breaks first, then sentences within an over-long line.
    segments: list[str] = []
    for line in unit.text.splitlines(keepends=True):
        if len(line) <= budget_chars:
            segments.append(line)
        else:
            segments.extend(re.split(r"(?<=[.!?])\s+", line))

    for segment in segments:
        if not segment:
            continue
        # A single segment still too big: cut it at the budget, losing nothing.
        while len(segment) > budget_chars:
            if buffer:
                pieces.append(buffer)
                buffer = ""
            pieces.append(segment[:budget_chars])
            segment = segment[budget_chars:]

        if len(buffer) + len(segment) > budget_chars and buffer:
            pieces.append(buffer)
            buffer = segment
        else:
            buffer += segment

    if buffer:
        pieces.append(buffer)

    total = len(pieces)
    return [
        TextUnit(
            text=piece,
            locator=f"{unit.locator} (part {i}/{total})" if unit.locator else "",
            section=unit.section,
        )
        for i, piece in enumerate(pieces, start=1)
    ]


def chunk_document(
    document: Document,
    max_tokens: int,
    *,
    count_tokens: Callable[[str], int] | None = None,
) -> list[Chunk]:
    """Pack ``document`` into chunks of at most ``max_tokens``.

    ``count_tokens`` is an optional provider-backed counter. When supplied, each
    assembled chunk is verified once and re-split if the local estimate was
    optimistic. It is called at most a handful of times per document, not per
    candidate split.
    """
    budget_chars = max(int(max_tokens * CHARS_PER_TOKEN), MIN_CHUNK_CHARS)

    # 1. Expand any unit that cannot fit in a chunk on its own.
    expanded: list[tuple[TextUnit, bool]] = []
    for unit in document.units:
        if len(unit.text) > budget_chars:
            expanded.extend((piece, True) for piece in _split_oversized(unit, budget_chars))
        else:
            expanded.append((unit, False))

    # 2. Greedily pack units into chunks, breaking only between units.
    groups: list[list[tuple[TextUnit, bool]]] = []
    current: list[tuple[TextUnit, bool]] = []
    current_chars = 0

    for unit, was_split in expanded:
        unit_chars = len(unit.rendered()) + 2  # + separator
        if current and current_chars + unit_chars > budget_chars:
            groups.append(current)
            current, current_chars = [], 0
        current.append((unit, was_split))
        current_chars += unit_chars

    if current:
        groups.append(current)

    if not groups:
        groups = [[]]

    # 3. Verify against the real tokenizer and re-split anything over budget.
    if count_tokens is not None:
        groups = _verify_groups(groups, max_tokens, count_tokens)

    total = len(groups)
    return [
        Chunk(
            index=i,
            total=total,
            units=[u for u, _ in group],
            split_units=sum(1 for _, was_split in group if was_split),
        )
        for i, group in enumerate(groups, start=1)
    ]


def _verify_groups(
    groups: list[list[tuple[TextUnit, bool]]],
    max_tokens: int,
    count_tokens: Callable[[str], int],
) -> list[list[tuple[TextUnit, bool]]]:
    """Re-split any group whose real token count exceeds the budget."""
    verified: list[list[tuple[TextUnit, bool]]] = []

    for group in groups:
        if not group:
            verified.append(group)
            continue

        text = "\n\n".join(u.rendered() for u, _ in group)
        try:
            actual = count_tokens(text)
        except Exception:
            # Counting is an optimization. If it fails, trust the estimate
            # rather than failing the whole run.
            verified.append(group)
            continue

        if actual <= max_tokens or len(group) == 1:
            verified.append(group)
            continue

        # Over budget: halve and re-verify each half.
        midpoint = len(group) // 2
        verified.extend(
            _verify_groups([group[:midpoint], group[midpoint:]], max_tokens, count_tokens)
        )

    return verified
