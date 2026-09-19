"""The generated PDF: does it contain what the report structure promises."""

from __future__ import annotations

import pdfplumber
import pytest

from rfp_eval.models import (
    AnswerStatus,
    ComparisonCell,
    ComparisonRow,
    Confidence,
    ExtractedAnswer,
    Finding,
    FindingKind,
    CriterionScore,
    Recommendation,
    Severity,
    Synthesis,
    VendorProsCons,
    VendorSummary,
)
from rfp_eval.report.pdf_report import MAX_VENDOR_COLUMNS, build_report


def _summary(name: str, questionnaire) -> VendorSummary:
    return VendorSummary(
        vendor_name=name,
        source_file=f"{name.lower().replace(' ', '-')}.pdf",
        summary=f"{name} submitted a complete response.\n\nSecond paragraph about {name}.",
        answers=[
            ExtractedAnswer(
                question_id=q.id,
                status=AnswerStatus.ANSWERED,
                answer_text=f"{name} answers {q.id} in detail.",
                verbatim_quote=f"Quote from {name} for {q.id}.",
                confidence=Confidence.HIGH,
                source_locator=f"p. {i + 1}",
            )
            for i, q in enumerate(questionnaire.questions)
        ],
        findings=[
            Finding(
                kind=FindingKind.GAP,
                category="Security",
                description=f"{name} did not supply a SOC 2 report.",
                severity=Severity.HIGH,
            ),
            Finding(
                kind=FindingKind.RED_FLAG,
                category="Pricing",
                description=f"{name} left renewal pricing uncapped.",
                severity=Severity.HIGH,
            ),
        ],
        scores=[
            CriterionScore(category=c, score=4, justification=f"{name} handled {c} well.")
            for c in questionnaire.categories
        ],
    )


def _synthesis(names: list[str], questionnaire) -> Synthesis:
    return Synthesis(
        executive_summary="First paragraph of the summary.\n\nSecond paragraph.",
        per_vendor=[
            VendorProsCons(
                vendor_name=n,
                pros=[f"{n} has clear pricing."],
                cons=[f"{n} is vague on timelines."],
                red_flags=[f"{n} would not commit to an RTO."] if i == 0 else [],
            )
            for i, n in enumerate(names)
        ],
        comparison=[
            ComparisonRow(
                criterion=c,
                cells=[
                    ComparisonCell(vendor_name=n, assessment=f"{n} on {c}.", rating="Strong")
                    for n in names
                ],
            )
            for c in questionnaire.categories
        ],
        recommendation=Recommendation(
            vendor_name=names[0],
            rationale="Strongest on security and support.",
            runner_up=names[1] if len(names) > 1 else "",
            risks=["Pricing is uncapped after year one."],
            conditions=["Negotiate a renewal cap before signing."],
        ),
    )


def _text(path) -> str:
    with pdfplumber.open(str(path)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


@pytest.fixture
def rendered(tmp_path, questionnaire):
    names = ["Acme Corp", "Beta Ltd", "Gamma Inc"]
    path = build_report(
        output_path=tmp_path / "report.pdf",
        questionnaire=questionnaire,
        summaries=[_summary(n, questionnaire) for n in names],
        synthesis=_synthesis(names, questionnaire),
    )
    return path, _text(path)


def test_report_contains_every_required_section(rendered):
    _, text = rendered
    for heading in (
        "Executive summary",
        "Criteria comparison",
        "Vendor detail",
        "Recommendation",
        "Appendix: extracted answers",
    ):
        assert heading in text, f"missing section: {heading}"


def test_title_page_names_the_rfp_and_vendors(rendered):
    _, text = rendered
    assert "Test RFP" in text
    assert "3 vendors evaluated" in text
    assert "Acme Corp" in text


def test_comparison_table_uses_questionnaire_criteria(rendered, questionnaire):
    _, text = rendered
    for category in questionnaire.categories:
        assert category in text


def test_per_vendor_sections_carry_pros_cons_and_red_flags(rendered):
    _, text = rendered
    assert "Strengths" in text
    assert "Weaknesses" in text
    assert "Red flags" in text
    assert "would not commit to an RTO" in text
    assert "Gaps in the response" in text
    assert "SOC 2" in text


def test_recommendation_includes_rationale_risks_and_conditions(rendered):
    _, text = rendered
    assert "Strongest on security and support" in text
    assert "Residual risks" in text
    assert "Negotiate a renewal cap" in text
    assert "Runner-up" in text


def test_appendix_preserves_quotes_and_locators(rendered):
    _, text = rendered
    assert "Quote from Acme Corp" in text
    assert "confidence: high" in text
    assert "p. 1" in text


def test_appendix_can_be_omitted(tmp_path, questionnaire):
    names = ["Acme Corp"]
    path = build_report(
        output_path=tmp_path / "no-appendix.pdf",
        questionnaire=questionnaire,
        summaries=[_summary(n, questionnaire) for n in names],
        synthesis=_synthesis(names, questionnaire),
        include_appendix=False,
    )
    assert "Appendix" not in _text(path)


def test_failed_vendors_are_named_in_the_report(tmp_path, questionnaire):
    names = ["Acme Corp"]
    path = build_report(
        output_path=tmp_path / "with-failure.pdf",
        questionnaire=questionnaire,
        summaries=[_summary(n, questionnaire) for n in names],
        synthesis=_synthesis(names, questionnaire),
        failures={"Broken Co": "could not read PDF"},
    )
    text = _text(path)
    assert "Evaluation notes" in text
    assert "Broken Co" in text
    assert "could not read PDF" in text


def test_many_vendors_split_into_column_groups(tmp_path, questionnaire):
    """Past four vendors the table must split rather than overflow the page."""
    names = [f"Vendor {i}" for i in range(1, 8)]
    path = build_report(
        output_path=tmp_path / "many.pdf",
        questionnaire=questionnaire,
        summaries=[_summary(n, questionnaire) for n in names],
        synthesis=_synthesis(names, questionnaire),
    )
    text = _text(path)

    assert f"Vendors 1–{MAX_VENDOR_COLUMNS} of 7" in text
    assert "Vendors 5–7 of 7" in text
    for name in names:
        assert name in text


def test_single_vendor_renders(tmp_path, questionnaire):
    names = ["Solo Inc"]
    path = build_report(
        output_path=tmp_path / "solo.pdf",
        questionnaire=questionnaire,
        summaries=[_summary(n, questionnaire) for n in names],
        synthesis=_synthesis(names, questionnaire),
    )
    assert "1 vendor evaluated" in _text(path)


def test_xml_unsafe_text_does_not_break_rendering(tmp_path, questionnaire):
    """Vendor names and answers are untrusted text in a mini-XML renderer."""
    name = 'Ampersand & <Angle> "Quote" Ltd'
    summary = _summary(name, questionnaire)
    summary.summary = "Uses <b>markup</b> & ampersands."

    path = build_report(
        output_path=tmp_path / "escaped.pdf",
        questionnaire=questionnaire,
        summaries=[summary],
        synthesis=_synthesis([name], questionnaire),
    )
    text = _text(path)
    assert "Ampersand &" in text
    assert "<b>markup</b>" in text  # escaped, not interpreted as bold


def test_report_has_multiple_pages_and_page_numbers(rendered):
    path, text = rendered
    with pdfplumber.open(str(path)) as pdf:
        assert len(pdf.pages) > 3
    assert "Page 1" in text


def test_bullets_survive_text_extraction(rendered):
    """Bullets must be real characters, not symbolic glyphs.

    ReportLab's built-in fonts encode U+2022 in a way that extracts as
    "(cid:127)" — it looks right on screen but breaks copy-paste, in-PDF
    search, and screen readers.
    """
    _, text = rendered
    assert "(cid:" not in text
    assert "•" in text
