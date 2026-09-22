"""Stage 1 -- the map step. One vendor document in, one VendorSummary out.

The call shape depends on document length:

* fits in one chunk -> 1 extraction call + 1 assessment call
* N chunks          -> N extraction calls + 1 assessment call

Answers from multiple chunks are merged **in code**, not by a model: picking
the best answer per question id is mechanical, and doing it deterministically
is both cheaper and more reliable than asking a model to reconcile N JSON
blobs. Only the judgment half -- summary, consolidated findings, scores -- is
worth an LLM call.

Nothing is ever truncated. A long document costs more calls, not less content.
"""

from __future__ import annotations

import json
from typing import Callable

from ..errors import VendorFailure
from ..llm.base import LLMProvider
from ..models import (
    AnswerStatus,
    ChunkExtraction,
    Confidence,
    ExtractedAnswer,
    Finding,
    Questionnaire,
    VendorAssessment,
    VendorSummary,
)
from ..parsing import Document
from ..prompts import format_questions, load_prompt
from ..schema_utils import strict_schema
from ..storage import RunStore
from .chunk_helpers import chunks_for

# Preference order when the same question is answered in more than one chunk.
_STATUS_RANK = {
    AnswerStatus.ANSWERED: 3,
    AnswerStatus.PARTIAL: 2,
    AnswerStatus.UNANSWERED: 1,
    AnswerStatus.NOT_FOUND: 0,
}
_CONFIDENCE_RANK = {Confidence.HIGH: 3, Confidence.MEDIUM: 2, Confidence.LOW: 1}


def merge_answers(
    extractions: list[ChunkExtraction], questionnaire: Questionnaire
) -> list[ExtractedAnswer]:
    """Reduce per-chunk answers to one answer per question, deterministically.

    A higher status wins; ties break on confidence. Every question in the
    questionnaire gets a row, so an unanswered question shows up as a real gap
    rather than as a missing key.
    """
    best: dict[str, ExtractedAnswer] = {}
    locators: dict[str, list[str]] = {}

    for extraction in extractions:
        for answer in extraction.answers:
            qid = answer.question_id
            if answer.source_locator and answer.status != AnswerStatus.NOT_FOUND:
                locators.setdefault(qid, [])
                if answer.source_locator not in locators[qid]:
                    locators[qid].append(answer.source_locator)

            incumbent = best.get(qid)
            if incumbent is None or _rank(answer) > _rank(incumbent):
                best[qid] = answer.model_copy(deep=True)

    merged: list[ExtractedAnswer] = []
    for question in questionnaire.questions:
        answer = best.get(question.id)
        if answer is None:
            # The model never mentioned this id in any chunk.
            merged.append(
                ExtractedAnswer(
                    question_id=question.id,
                    status=AnswerStatus.NOT_FOUND,
                    answer_text="",
                    confidence=Confidence.HIGH,
                )
            )
            continue

        found = locators.get(question.id, [])
        if len(found) > 1:
            answer.source_locator = "; ".join(found)
        merged.append(answer)

    # Answers for ids not in the questionnaire are hallucinated -- drop them,
    # but keep them out of the way rather than failing the vendor.
    return merged


def _rank(answer: ExtractedAnswer) -> tuple[int, int]:
    return (
        _STATUS_RANK.get(answer.status, 0),
        _CONFIDENCE_RANK.get(answer.confidence, 0),
    )


def dedupe_findings(extractions: list[ChunkExtraction]) -> list[Finding]:
    """Pool chunk findings, dropping exact repeats before the assessment call."""
    pooled: list[Finding] = []
    seen: set[tuple] = set()
    for extraction in extractions:
        for finding in extraction.findings:
            key = (finding.kind, finding.category, finding.description.strip().lower())
            if key in seen:
                continue
            seen.add(key)
            pooled.append(finding)
    return pooled


# ---------------------------------------------------------------------------


def run_stage1(
    *,
    vendor_name: str,
    slug: str,
    document: Document,
    questionnaire: Questionnaire,
    provider: LLMProvider,
    model: str,
    max_chunk_tokens: int,
    max_output_tokens: int,
    store: RunStore | None = None,
    on_chunk: Callable[[int, int], None] | None = None,
    resume: bool = True,
) -> VendorSummary:
    """Evaluate one vendor. Raises ``VendorFailure`` -- callers isolate it."""
    questions_block = format_questions(questionnaire.questions)
    chunks = chunks_for(document, max_chunk_tokens, provider=provider, model=model)

    extract_prompt = load_prompt("stage1_extract")
    extract_schema = strict_schema(ChunkExtraction)

    extractions: list[ChunkExtraction] = []

    for chunk in chunks:
        if on_chunk is not None:
            on_chunk(chunk.index, chunk.total)

        # Resume: a chunk already extracted in an earlier attempt is free.
        if resume and store is not None:
            cached = store.load_chunk(slug, chunk.index)
            if cached is not None:
                extractions.append(cached)
                continue

        system, user = extract_prompt.render(
            vendor_name=vendor_name,
            questions=questions_block,
            chunk_index=chunk.index,
            chunk_total=chunk.total,
            chunk_text=chunk.text,
            filename=document.filename,
        )

        payload = provider.complete_json(
            system=system,
            user=user,
            schema=extract_schema,
            model=model,
            max_tokens=max_output_tokens,
            stage="stage1_extract",
            # The system prompt carries the question list and is byte-identical
            # for every vendor and every chunk in a run, so it is a stable cache
            # prefix worth paying the write cost for once.
            cache_system=True,
        )
        extraction = ChunkExtraction.model_validate(payload)

        if store is not None:
            store.save_chunk(slug, chunk.index, extraction)
        extractions.append(extraction)

    if not extractions:
        raise VendorFailure(vendor_name, "document produced no readable chunks")

    merged = merge_answers(extractions, questionnaire)
    pooled = dedupe_findings(extractions)

    assessment = _assess(
        vendor_name=vendor_name,
        filename=document.filename,
        questionnaire=questionnaire,
        answers=merged,
        findings=pooled,
        provider=provider,
        model=model,
        max_output_tokens=max_output_tokens,
    )

    return VendorSummary(
        vendor_name=vendor_name,
        source_file=document.filename,
        summary=assessment.summary,
        answers=merged,
        findings=assessment.findings or pooled,
        scores=assessment.scores,
        chunk_count=len(chunks),
        model=model,
    )


def _assess(
    *,
    vendor_name: str,
    filename: str,
    questionnaire: Questionnaire,
    answers: list[ExtractedAnswer],
    findings: list[Finding],
    provider: LLMProvider,
    model: str,
    max_output_tokens: int,
) -> VendorAssessment:
    """The judgment half of Stage 1: summary, findings, scores."""
    answered = sum(
        1 for a in answers if a.status in (AnswerStatus.ANSWERED, AnswerStatus.PARTIAL)
    )
    coverage_note = (
        f"Coverage: {answered} of {len(answers)} questions received a substantive answer."
    )

    prompt = load_prompt("stage1_assess")
    system, user = prompt.render(
        vendor_name=vendor_name,
        filename=filename,
        categories="\n".join(f"- {c}" for c in questionnaire.categories),
        merged_answers=json.dumps(
            [a.model_dump(mode="json") for a in answers], indent=2, ensure_ascii=False
        ),
        chunk_findings=json.dumps(
            [f.model_dump(mode="json") for f in findings], indent=2, ensure_ascii=False
        ),
        coverage_note=coverage_note,
    )

    payload = provider.complete_json(
        system=system,
        user=user,
        schema=strict_schema(VendorAssessment),
        model=model,
        max_tokens=max_output_tokens,
        stage="stage1_assess",
    )
    return VendorAssessment.model_validate(payload)
