"""The data contract between pipeline stages.

These models are the whole reason the map-reduce split works: Stage 2 consumes
``VendorSummary`` objects, never raw documents, so its context stays bounded no
matter how many vendors there are or how long their responses run.

Every model here is also a JSON Schema handed to the LLM (see
``rfp_eval.schema_utils``), so field descriptions are load-bearing -- they are
the instructions the model reads about each field.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Stage 0 -- the canonical questionnaire
# --------------------------------------------------------------------------


class Question(BaseModel):
    """One question from the original RFP, as sent to vendors."""

    id: str = Field(description="Stable short identifier, e.g. 'Q1' or 'SEC-3'.")
    category: str = Field(
        description="Section or theme this question belongs to, e.g. 'Security'. "
        "These become the comparison criteria in the final report."
    )
    text: str = Field(description="The full question text, verbatim from the RFP.")
    required: bool = Field(
        default=True,
        description="Whether the RFP marked this question as mandatory.",
    )


class Questionnaire(BaseModel):
    """The full set of questions the organization sent to vendors."""

    rfp_name: str = Field(description="Title of the RFP, as it appears in the document.")
    questions: list[Question] = Field(description="Every question found, in document order.")
    source_file: str = Field(default="", description="Filename the questionnaire came from.")
    parsed_by: str = Field(
        default="heuristic",
        description="'heuristic' or 'llm' -- how this questionnaire was extracted.",
    )

    @property
    def categories(self) -> list[str]:
        """Unique categories in first-appearance order -- the report's criteria rows."""
        seen: list[str] = []
        for q in self.questions:
            if q.category not in seen:
                seen.append(q.category)
        return seen


# --------------------------------------------------------------------------
# Stage 1 -- per-vendor extraction and assessment
# --------------------------------------------------------------------------


class AnswerStatus(str, Enum):
    ANSWERED = "answered"
    PARTIAL = "partial"
    UNANSWERED = "unanswered"
    NOT_FOUND = "not_found"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class FindingKind(str, Enum):
    STRENGTH = "strength"
    WEAKNESS = "weakness"
    GAP = "gap"
    RED_FLAG = "red_flag"


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ExtractedAnswer(BaseModel):
    """A vendor's answer to one questionnaire question."""

    question_id: str = Field(description="The Question.id this answers.")
    status: AnswerStatus = Field(
        description="'answered' if fully addressed, 'partial' if only some of the "
        "question is addressed, 'unanswered' if the vendor acknowledged the "
        "question but gave no substantive answer, 'not_found' if this chunk of "
        "the document contains nothing relevant."
    )
    answer_text: str = Field(
        default="",
        description="The vendor's answer, summarized faithfully. Do not editorialize "
        "or evaluate here -- that belongs in findings.",
    )
    verbatim_quote: str = Field(
        default="",
        description="A short direct quote from the vendor document supporting this "
        "answer. Empty if status is 'not_found'.",
    )
    confidence: Confidence = Field(
        default=Confidence.MEDIUM,
        description="How confident you are that this text actually answers this question.",
    )
    source_locator: str = Field(
        default="",
        description="Where in the document this came from, e.g. 'p. 12' or "
        "\"sheet 'Security', row 17\".",
    )


class Finding(BaseModel):
    """An evaluative observation about a vendor's response."""

    kind: FindingKind
    category: str = Field(description="Questionnaire category this relates to.")
    description: str = Field(description="What was observed and why it matters.")
    severity: Severity = Field(
        default=Severity.MEDIUM,
        description="Impact on the evaluation. Strengths use severity as magnitude.",
    )
    evidence: str = Field(
        default="",
        description="Quote or specific reference from the vendor document.",
    )


class CriterionScore(BaseModel):
    """A 1-5 score for one questionnaire category."""

    category: str
    score: int = Field(ge=1, le=5, description="1 = poor/absent, 5 = excellent.")
    justification: str = Field(description="One or two sentences grounded in the response.")


class VendorSummary(BaseModel):
    """Stage 1 output for a single vendor. This is all Stage 2 ever sees."""

    vendor_name: str
    source_file: str = ""
    summary: str = Field(
        default="",
        description="A few paragraphs characterizing this vendor's response overall.",
    )
    answers: list[ExtractedAnswer] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    scores: list[CriterionScore] = Field(default_factory=list)

    # Provenance -- not model-generated, filled in by the pipeline.
    chunk_count: int = 1
    model: str = ""
    generated_at: str = Field(default_factory=_utcnow)

    def findings_of(self, kind: FindingKind) -> list[Finding]:
        return [f for f in self.findings if f.kind == kind]

    def coverage(self) -> tuple[int, int]:
        """(substantively answered, total) -- drives the 'gaps' story in the report."""
        answered = sum(
            1
            for a in self.answers
            if a.status in (AnswerStatus.ANSWERED, AnswerStatus.PARTIAL)
        )
        return answered, len(self.answers)


# --- Intermediate: what a single chunk extraction returns ------------------


class ChunkExtraction(BaseModel):
    """Stage 1 output for ONE chunk of one vendor document.

    Kept separate from VendorSummary because a chunk has no business producing
    an overall summary or scores -- it has only seen part of the document.
    """

    answers: list[ExtractedAnswer] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)


class VendorAssessment(BaseModel):
    """The judgment half of Stage 1, produced once per vendor over merged answers."""

    summary: str
    findings: list[Finding] = Field(default_factory=list)
    scores: list[CriterionScore] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Stage 2 -- cross-vendor synthesis
# --------------------------------------------------------------------------


class VendorProsCons(BaseModel):
    vendor_name: str
    pros: list[str] = Field(default_factory=list)
    cons: list[str] = Field(default_factory=list)
    red_flags: list[str] = Field(
        default_factory=list,
        description="Serious concerns or unanswered critical questions. Empty list if none.",
    )


class ComparisonCell(BaseModel):
    """One cell of the criteria x vendor comparison table."""

    vendor_name: str
    assessment: str = Field(description="A terse phrase, not a paragraph -- this is a table cell.")
    rating: str = Field(
        default="",
        description="A short comparable label, e.g. 'Strong', 'Adequate', 'Weak', 'Not addressed'.",
    )


class ComparisonRow(BaseModel):
    criterion: str = Field(description="A category from the original questionnaire.")
    cells: list[ComparisonCell] = Field(description="One cell per vendor, same order throughout.")


class Recommendation(BaseModel):
    vendor_name: str = Field(description="The vendor recommended for selection.")
    rationale: str = Field(description="Why this vendor, referencing specific criteria.")
    runner_up: str = Field(default="", description="Next-best vendor, or empty if none.")
    risks: list[str] = Field(
        default_factory=list,
        description="Residual risks of choosing the recommended vendor.",
    )
    conditions: list[str] = Field(
        default_factory=list,
        description="What to clarify or negotiate before committing.",
    )


class Synthesis(BaseModel):
    """Stage 2 output -- everything the final PDF report renders."""

    executive_summary: str
    per_vendor: list[VendorProsCons] = Field(default_factory=list)
    comparison: list[ComparisonRow] = Field(default_factory=list)
    recommendation: Recommendation

    model: str = ""
    generated_at: str = Field(default_factory=_utcnow)


# --------------------------------------------------------------------------
# Run bookkeeping (never sent to an LLM)
# --------------------------------------------------------------------------


class VendorStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


class VendorRecord(BaseModel):
    slug: str
    name: str
    filename: str
    status: VendorStatus = VendorStatus.PENDING
    error: str = ""
    chunk_count: int = 0


class RunManifest(BaseModel):
    run_id: str
    created_at: str = Field(default_factory=_utcnow)
    updated_at: str = Field(default_factory=_utcnow)
    rfp_name: str = ""
    rfp_file: str = ""
    vendors: list[VendorRecord] = Field(default_factory=list)
    synthesis_complete: bool = False
    report_path: str = ""
    settings_snapshot: dict = Field(default_factory=dict)

    def vendor(self, slug: str) -> VendorRecord | None:
        return next((v for v in self.vendors if v.slug == slug), None)

    def completed_slugs(self) -> set[str]:
        return {v.slug for v in self.vendors if v.status == VendorStatus.COMPLETE}
