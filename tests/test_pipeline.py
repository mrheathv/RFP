"""Pipeline behaviour, driven entirely by the offline fake provider."""

from __future__ import annotations

import pytest

from rfp_eval.llm.fake import FakeProvider
from rfp_eval.models import (
    AnswerStatus,
    ChunkExtraction,
    Confidence,
    ExtractedAnswer,
    Finding,
    FindingKind,
)
from rfp_eval.parsing import parse_document
from rfp_eval.pipeline.stage0 import build_questionnaire, heuristic_questionnaire
from rfp_eval.pipeline.stage1 import dedupe_findings, merge_answers, run_stage1
from rfp_eval.pipeline.stage2 import NoVendorsError, run_stage2
from rfp_eval.storage import create_run
from tests.conftest import make_pdf


# -- Stage 0 ---------------------------------------------------------------


def test_heuristics_read_an_excel_questionnaire(xlsx_file):
    doc = parse_document(xlsx_file)
    q = heuristic_questionnaire(doc, xlsx_file)

    assert q is not None
    assert q.parsed_by == "heuristic"
    assert len(q.questions) == 8
    assert set(q.categories) == {"Security", "Support", "Pricing"}


def test_heuristics_read_numbered_pdf_questions(tmp_path):
    body = "\n".join(
        [
            "Security",
            "1. Describe your approach to data encryption at rest and in transit.",
            "2. How do you handle security incident disclosure to customers?",
            "Support",
            "3. What are your guaranteed response times for critical issues?",
        ]
    )
    path = make_pdf(tmp_path / "rfp.pdf", pages=1, body=body)
    q = heuristic_questionnaire(parse_document(path), path)

    assert q is not None
    assert len(q.questions) == 3
    assert q.questions[0].category == "Security"
    assert q.questions[2].category == "Support"


def test_heuristics_decline_when_there_is_no_questionnaire(tmp_path):
    path = make_pdf(tmp_path / "prose.pdf", pages=1, body="Just some narrative prose.")
    assert heuristic_questionnaire(parse_document(path), path) is None


def test_stage0_falls_back_to_the_llm(tmp_path):
    path = make_pdf(tmp_path / "prose.pdf", pages=1, body="Just some narrative prose.")
    provider = FakeProvider()

    q = build_questionnaire(
        parse_document(path),
        provider,
        path=path,
        model="fake",
        max_output_tokens=4000,
        max_chunk_tokens=5000,
    )

    assert q.parsed_by == "llm"
    assert len(q.questions) > 0
    assert [c["stage"] for c in provider.calls] == ["stage0"]


def test_stage0_skips_the_llm_when_heuristics_succeed(xlsx_file):
    provider = FakeProvider()
    build_questionnaire(
        parse_document(xlsx_file),
        provider,
        path=xlsx_file,
        model="fake",
        max_output_tokens=4000,
        max_chunk_tokens=5000,
    )
    assert provider.calls == []  # no tokens spent


# -- Stage 1 merging -------------------------------------------------------


def _answer(qid, status, confidence=Confidence.MEDIUM, text="", locator=""):
    return ExtractedAnswer(
        question_id=qid,
        status=status,
        answer_text=text,
        confidence=confidence,
        source_locator=locator,
    )


def test_merge_prefers_a_real_answer_over_not_found(questionnaire):
    chunks = [
        ChunkExtraction(answers=[_answer("Q1", AnswerStatus.NOT_FOUND)]),
        ChunkExtraction(
            answers=[_answer("Q1", AnswerStatus.ANSWERED, text="Here is the answer.")]
        ),
    ]
    merged = {a.question_id: a for a in merge_answers(chunks, questionnaire)}

    assert merged["Q1"].status == AnswerStatus.ANSWERED
    assert merged["Q1"].answer_text == "Here is the answer."


def test_merge_breaks_status_ties_on_confidence(questionnaire):
    chunks = [
        ChunkExtraction(
            answers=[_answer("Q2", AnswerStatus.ANSWERED, Confidence.LOW, "weak")]
        ),
        ChunkExtraction(
            answers=[_answer("Q2", AnswerStatus.ANSWERED, Confidence.HIGH, "strong")]
        ),
    ]
    merged = {a.question_id: a for a in merge_answers(chunks, questionnaire)}
    assert merged["Q2"].answer_text == "strong"


def test_merge_collects_locators_from_every_chunk(questionnaire):
    chunks = [
        ChunkExtraction(
            answers=[_answer("Q3", AnswerStatus.PARTIAL, locator="p. 2", text="a")]
        ),
        ChunkExtraction(
            answers=[_answer("Q3", AnswerStatus.ANSWERED, locator="p. 9", text="b")]
        ),
    ]
    merged = {a.question_id: a for a in merge_answers(chunks, questionnaire)}
    assert merged["Q3"].source_locator == "p. 2; p. 9"


def test_merge_covers_every_question_even_when_unmentioned(questionnaire):
    merged = merge_answers([ChunkExtraction(answers=[])], questionnaire)
    assert len(merged) == len(questionnaire.questions)
    assert all(a.status == AnswerStatus.NOT_FOUND for a in merged)


def test_merge_drops_hallucinated_question_ids(questionnaire):
    chunks = [
        ChunkExtraction(answers=[_answer("Q999", AnswerStatus.ANSWERED, text="invented")])
    ]
    merged = merge_answers(chunks, questionnaire)
    assert "Q999" not in {a.question_id for a in merged}


def test_findings_are_deduplicated_across_chunks():
    finding = Finding(
        kind=FindingKind.GAP, category="Security", description="No SOC 2 report."
    )
    pooled = dedupe_findings(
        [
            ChunkExtraction(findings=[finding]),
            ChunkExtraction(findings=[finding.model_copy()]),
        ]
    )
    assert len(pooled) == 1


# -- Stage 1 end to end ----------------------------------------------------


def test_stage1_single_chunk_uses_two_calls(questionnaire, pdf_file):
    provider = FakeProvider()
    summary = run_stage1(
        vendor_name="Acme Corp",
        slug="acme-corp",
        document=parse_document(pdf_file),
        questionnaire=questionnaire,
        provider=provider,
        model="fake",
        max_chunk_tokens=50_000,
        max_output_tokens=4000,
    )

    assert summary.vendor_name == "Acme Corp"
    assert summary.chunk_count == 1
    assert len(summary.answers) == len(questionnaire.questions)
    # One extraction + one assessment.
    assert [c["stage"] for c in provider.calls] == ["stage1_extract", "stage1_assess"]


def test_long_document_is_chunked_not_truncated(questionnaire, tmp_path):
    path = make_pdf(tmp_path / "long.pdf", pages=12)
    provider = FakeProvider()

    summary = run_stage1(
        vendor_name="Verbose Inc",
        slug="verbose-inc",
        document=parse_document(path),
        questionnaire=questionnaire,
        provider=provider,
        model="fake",
        max_chunk_tokens=1000,
        max_output_tokens=4000,
    )

    extracts = [c for c in provider.calls if c["stage"] == "stage1_extract"]
    assert summary.chunk_count > 1
    assert len(extracts) == summary.chunk_count
    assert sum(1 for c in provider.calls if c["stage"] == "stage1_assess") == 1


def test_stage1_resumes_from_saved_chunks(questionnaire, tmp_path, settings):
    path = make_pdf(tmp_path / "long.pdf", pages=12)
    document = parse_document(path)
    store, _ = create_run(settings.data_dir)

    first = FakeProvider()
    run_stage1(
        vendor_name="Acme", slug="acme", document=document, questionnaire=questionnaire,
        provider=first, model="fake", max_chunk_tokens=1000, max_output_tokens=4000,
        store=store,
    )
    extracted_first = sum(1 for c in first.calls if c["stage"] == "stage1_extract")
    assert extracted_first > 1

    # Second run reuses every saved chunk; only the assessment call repeats.
    second = FakeProvider()
    run_stage1(
        vendor_name="Acme", slug="acme", document=document, questionnaire=questionnaire,
        provider=second, model="fake", max_chunk_tokens=1000, max_output_tokens=4000,
        store=store, resume=True,
    )
    assert [c["stage"] for c in second.calls] == ["stage1_assess"]


# -- Stage 2 ---------------------------------------------------------------


def test_stage2_refuses_an_empty_vendor_list(questionnaire):
    with pytest.raises(NoVendorsError):
        run_stage2(
            summaries=[], questionnaire=questionnaire, provider=FakeProvider(),
            model="fake", max_output_tokens=4000,
        )


def test_stage2_receives_summaries_not_documents(questionnaire, pdf_file):
    provider = FakeProvider()
    summary = run_stage1(
        vendor_name="Acme", slug="acme", document=parse_document(pdf_file),
        questionnaire=questionnaire, provider=provider, model="fake",
        max_chunk_tokens=50_000, max_output_tokens=4000,
    )

    provider.calls.clear()
    synthesis = run_stage2(
        summaries=[summary], questionnaire=questionnaire, provider=provider,
        model="fake", max_output_tokens=4000,
    )

    assert len(provider.calls) == 1
    assert synthesis.recommendation.vendor_name
    assert len(synthesis.comparison) == len(questionnaire.categories)


def test_verbatim_quotes_are_excluded_from_stage2_by_default(questionnaire):
    from rfp_eval.models import VendorSummary
    from rfp_eval.pipeline.stage2 import _payload_for

    summary = VendorSummary(
        vendor_name="Acme",
        answers=[_answer("Q1", AnswerStatus.ANSWERED, text="yes")],
    )
    summary.answers[0].verbatim_quote = "a long quote from the document"

    trimmed = _payload_for(summary, include_verbatim=False)
    kept = _payload_for(summary, include_verbatim=True)

    assert "verbatim_quote" not in trimmed["answers"][0]
    assert "verbatim_quote" in kept["answers"][0]
