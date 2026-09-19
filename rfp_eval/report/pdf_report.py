"""Render the final evaluation report as a PDF using ReportLab.

Structure, in order:
  1. Title page      -- RFP name, date generated, vendors evaluated
  2. Executive summary
  3. Comparison table -- questionnaire criteria as rows, vendors as columns
  4. Per-vendor sections -- summary, pros, cons, red flags and gaps
  5. Recommendation with rationale
  6. Appendix (optional) -- raw extracted answers per vendor, with locators

The one real layout constraint is the comparison table: criteria x vendors does
not fit portrait past about four columns. Vendors are therefore emitted in
column groups of at most four, as successive tables that repeat the criteria
column -- predictable at any vendor count, with no landscape section to manage.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
    LongTable,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    TableStyle,
)

from ..models import (
    AnswerStatus,
    FindingKind,
    Questionnaire,
    Severity,
    Synthesis,
    VendorSummary,
)

MAX_VENDOR_COLUMNS = 4


def _register_bullet_font() -> tuple[str, str]:
    """Pick a bullet glyph that survives text extraction from the finished PDF.

    The built-in Type 1 fonts encode U+2022 symbolically: it looks right on
    screen but extracts as "(cid:127)", which breaks copy-paste, in-PDF search,
    and screen readers. ReportLab bundles a Unicode TTF, so register that for
    the bullet alone; if it is ever unavailable, fall back to a middle dot,
    which is a real WinAnsi character.
    """
    try:
        import reportlab
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont

        font_path = Path(reportlab.__file__).parent / "fonts" / "Vera.ttf"
        if font_path.exists():
            if "RfpBulletFont" not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont("RfpBulletFont", str(font_path)))
            return "RfpBulletFont", "\u2022"
    except Exception:
        pass
    return "Helvetica", "\u00b7"


BULLET_FONT, BULLET = _register_bullet_font()

PAGE_SIZE = A4
MARGIN = 18 * mm

ACCENT = colors.HexColor("#1f3a5f")
MUTED = colors.HexColor("#5a6472")
RULE = colors.HexColor("#d5dae1")
HEADER_BG = colors.HexColor("#eef1f5")
FLAG_BG = colors.HexColor("#fdf1f0")
FLAG_TEXT = colors.HexColor("#9b2c2c")


# ---------------------------------------------------------------------------
# Styles
# ---------------------------------------------------------------------------


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    s = {
        "title": ParagraphStyle(
            "RfpTitle", parent=base["Title"], fontSize=26, leading=32,
            textColor=ACCENT, alignment=TA_CENTER, spaceAfter=6,
        ),
        "subtitle": ParagraphStyle(
            "RfpSubtitle", parent=base["Normal"], fontSize=12, leading=17,
            textColor=MUTED, alignment=TA_CENTER,
        ),
        "h1": ParagraphStyle(
            "RfpH1", parent=base["Heading1"], fontSize=17, leading=21,
            textColor=ACCENT, spaceBefore=4, spaceAfter=8,
        ),
        "h2": ParagraphStyle(
            "RfpH2", parent=base["Heading2"], fontSize=13, leading=17,
            textColor=ACCENT, spaceBefore=10, spaceAfter=5,
        ),
        "h3": ParagraphStyle(
            "RfpH3", parent=base["Heading3"], fontSize=10.5, leading=14,
            textColor=MUTED, spaceBefore=8, spaceAfter=3,
        ),
        "body": ParagraphStyle(
            "RfpBody", parent=base["BodyText"], fontSize=9.8, leading=14.5,
            spaceAfter=6,
        ),
        "bullet": ParagraphStyle(
            "RfpBullet", parent=base["BodyText"], fontSize=9.8, leading=14,
            leftIndent=11, bulletIndent=2, spaceAfter=3,
            bulletFontName=BULLET_FONT, bulletFontSize=9.8,
        ),
        "flag": ParagraphStyle(
            "RfpFlag", parent=base["BodyText"], fontSize=9.8, leading=14,
            leftIndent=11, bulletIndent=2, spaceAfter=3, textColor=FLAG_TEXT,
            bulletFontName=BULLET_FONT, bulletFontSize=9.8,
        ),
        "cell": ParagraphStyle(
            "RfpCell", parent=base["BodyText"], fontSize=8.2, leading=11,
            spaceAfter=0,
        ),
        "cellhead": ParagraphStyle(
            "RfpCellHead", parent=base["BodyText"], fontSize=8.6, leading=11,
            spaceAfter=0, textColor=ACCENT, fontName="Helvetica-Bold",
        ),
        "note": ParagraphStyle(
            "RfpNote", parent=base["BodyText"], fontSize=8.6, leading=12,
            textColor=MUTED, spaceAfter=6,
        ),
    }
    return s


def _esc(text: str) -> str:
    """Escape for ReportLab's mini-XML, collapsing whitespace."""
    return escape(" ".join(str(text or "").split()))


def _paragraphs(text: str, style: ParagraphStyle) -> list:
    """Split on blank lines so the model's paragraphing survives into the PDF."""
    blocks = [b.strip() for b in str(text or "").split("\n\n") if b.strip()]
    if not blocks:
        return []
    return [Paragraph(_esc(b), style) for b in blocks]


def _bullets(items: list[str], style: ParagraphStyle) -> list:
    return [Paragraph(_esc(i), style, bulletText=BULLET) for i in items if str(i).strip()]


# ---------------------------------------------------------------------------
# Page furniture
# ---------------------------------------------------------------------------


class _Doc(BaseDocTemplate):
    """Adds a footer with page numbers to every page after the title page."""

    def __init__(self, path: str, rfp_name: str, **kwargs):
        super().__init__(path, pagesize=PAGE_SIZE, **kwargs)
        self.rfp_name = rfp_name

        frame = Frame(
            MARGIN, MARGIN, PAGE_SIZE[0] - 2 * MARGIN, PAGE_SIZE[1] - 2 * MARGIN,
            id="body",
        )
        self.addPageTemplates(
            [
                PageTemplate(id="title", frames=[frame]),
                PageTemplate(id="content", frames=[frame], onPage=self._footer),
            ]
        )

    def _footer(self, canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(MUTED)
        y = MARGIN - 6 * mm
        canvas.drawString(MARGIN, y, self.rfp_name[:90])
        canvas.drawRightString(PAGE_SIZE[0] - MARGIN, y, f"Page {doc.page - 1}")
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(0.5)
        canvas.line(MARGIN, y + 4 * mm, PAGE_SIZE[0] - MARGIN, y + 4 * mm)
        canvas.restoreState()


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def _title_page(st, questionnaire, summaries, failures) -> list:
    story = [Spacer(1, 55 * mm)]
    story.append(Paragraph(_esc(questionnaire.rfp_name or "RFP Evaluation"), st["title"]))
    story.append(Paragraph("Vendor Response Evaluation", st["subtitle"]))
    story.append(Spacer(1, 14 * mm))
    story.append(
        Paragraph(
            f"Generated {datetime.now().strftime('%d %B %Y')}", st["subtitle"]
        )
    )
    story.append(Spacer(1, 10 * mm))

    names = ", ".join(s.vendor_name for s in summaries) or "None"
    story.append(
        Paragraph(
            f"<b>{len(summaries)}</b> vendor{'s' if len(summaries) != 1 else ''} "
            f"evaluated: {_esc(names)}",
            st["subtitle"],
        )
    )

    if failures:
        story.append(Spacer(1, 6 * mm))
        story.append(
            Paragraph(
                "Not evaluated: " + _esc(", ".join(failures)) + " — see Evaluation notes.",
                ParagraphStyle("failnote", parent=st["subtitle"], textColor=FLAG_TEXT),
            )
        )

    story.append(Spacer(1, 12 * mm))
    story.append(
        Paragraph(
            f"{len(questionnaire.questions)} questions across "
            f"{len(questionnaire.categories)} criteria",
            st["subtitle"],
        )
    )
    return story


def _failures_section(st, failures: dict[str, str]) -> list:
    if not failures:
        return []
    story = [Paragraph("Evaluation notes", st["h1"])]
    story.append(
        Paragraph(
            "The following vendor responses could not be processed and are "
            "excluded from the comparison and the recommendation below.",
            st["body"],
        )
    )
    for name, reason in failures.items():
        story.append(
            Paragraph(f"<b>{_esc(name)}</b> — {_esc(reason)}", st["flag"], bulletText=BULLET)
        )
    story.append(Spacer(1, 4 * mm))
    return story


def _comparison_section(st, synthesis: Synthesis, vendor_names: list[str]) -> list:
    if not synthesis.comparison:
        return []

    story = [Paragraph("Criteria comparison", st["h1"])]

    groups = [
        vendor_names[i : i + MAX_VENDOR_COLUMNS]
        for i in range(0, len(vendor_names), MAX_VENDOR_COLUMNS)
    ]

    available = PAGE_SIZE[0] - 2 * MARGIN

    for group_index, group in enumerate(groups):
        if len(groups) > 1:
            story.append(
                Paragraph(
                    f"Vendors {group_index * MAX_VENDOR_COLUMNS + 1}–"
                    f"{group_index * MAX_VENDOR_COLUMNS + len(group)} "
                    f"of {len(vendor_names)}",
                    st["h3"],
                )
            )

        header = [Paragraph("Criterion", st["cellhead"])] + [
            Paragraph(_esc(v), st["cellhead"]) for v in group
        ]
        data = [header]

        for row in synthesis.comparison:
            by_vendor = {c.vendor_name: c for c in row.cells}
            line = [Paragraph(f"<b>{_esc(row.criterion)}</b>", st["cell"])]
            for vendor in group:
                cell = by_vendor.get(vendor)
                if cell is None:
                    line.append(Paragraph("—", st["cell"]))
                    continue
                rating = f"<b>{_esc(cell.rating)}</b><br/>" if cell.rating else ""
                line.append(Paragraph(rating + _esc(cell.assessment), st["cell"]))
            data.append(line)

        criterion_width = available * 0.22
        vendor_width = (available - criterion_width) / max(len(group), 1)

        table = LongTable(
            data,
            colWidths=[criterion_width] + [vendor_width] * len(group),
            repeatRows=1,
        )
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), HEADER_BG),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("GRID", (0, 0), (-1, -1), 0.4, RULE),
                    ("LEFTPADDING", (0, 0), (-1, -1), 5),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, HEADER_BG]),
                ]
            )
        )
        story.append(table)
        story.append(Spacer(1, 6 * mm))

    return story


def _scores_table(st, summary: VendorSummary) -> list:
    if not summary.scores:
        return []

    available = PAGE_SIZE[0] - 2 * MARGIN
    data = [
        [
            Paragraph("Criterion", st["cellhead"]),
            Paragraph("Score", st["cellhead"]),
            Paragraph("Justification", st["cellhead"]),
        ]
    ]
    for score in summary.scores:
        data.append(
            [
                Paragraph(_esc(score.category), st["cell"]),
                Paragraph(f"<b>{score.score}</b> / 5", st["cell"]),
                Paragraph(_esc(score.justification), st["cell"]),
            ]
        )

    table = LongTable(
        data,
        colWidths=[available * 0.26, available * 0.10, available * 0.64],
        repeatRows=1,
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), HEADER_BG),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("GRID", (0, 0), (-1, -1), 0.4, RULE),
                ("ALIGN", (1, 1), (1, -1), "CENTER"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return [Paragraph("Scores by criterion", st["h3"]), table, Spacer(1, 4 * mm)]


def _vendor_sections(st, synthesis: Synthesis, summaries: list[VendorSummary]) -> list:
    story = [PageBreak(), Paragraph("Vendor detail", st["h1"])]
    pros_cons = {p.vendor_name: p for p in synthesis.per_vendor}

    for index, summary in enumerate(summaries):
        if index:
            story.append(PageBreak())

        story.append(Paragraph(_esc(summary.vendor_name), st["h2"]))

        answered, total = summary.coverage()
        detail = f"Source: {_esc(summary.source_file)}" if summary.source_file else ""
        chunks = (
            f" · processed in {summary.chunk_count} parts"
            if summary.chunk_count > 1
            else ""
        )
        story.append(
            Paragraph(
                f"{detail}{' · ' if detail else ''}"
                f"{answered} of {total} questions substantively answered{chunks}",
                st["note"],
            )
        )

        story.extend(_paragraphs(summary.summary, st["body"]))

        pc = pros_cons.get(summary.vendor_name)
        if pc:
            if pc.pros:
                story.append(Paragraph("Strengths", st["h3"]))
                story.extend(_bullets(pc.pros, st["bullet"]))
            if pc.cons:
                story.append(Paragraph("Weaknesses", st["h3"]))
                story.extend(_bullets(pc.cons, st["bullet"]))
            if pc.red_flags:
                story.append(Paragraph("Red flags", st["h3"]))
                story.extend(_bullets(pc.red_flags, st["flag"]))

        gaps = summary.findings_of(FindingKind.GAP)
        if gaps:
            story.append(Paragraph("Gaps in the response", st["h3"]))
            story.extend(
                _bullets(
                    [f"{g.category}: {g.description}" for g in gaps], st["bullet"]
                )
            )

        # Red flags found during Stage 1 that the synthesis did not carry up.
        stage1_flags = summary.findings_of(FindingKind.RED_FLAG)
        carried = {f.lower() for f in (pc.red_flags if pc else [])}
        extra = [
            f for f in stage1_flags
            if f.description.lower() not in carried
        ]
        if extra:
            story.append(Paragraph("Additional concerns from the response", st["h3"]))
            story.extend(
                _bullets(
                    [
                        f"[{f.severity.value if isinstance(f.severity, Severity) else f.severity}] "
                        f"{f.category}: {f.description}"
                        for f in extra
                    ],
                    st["flag"],
                )
            )

        story.extend(_scores_table(st, summary))

    return story


def _recommendation_section(st, synthesis: Synthesis) -> list:
    rec = synthesis.recommendation
    story = [PageBreak(), Paragraph("Recommendation", st["h1"])]
    story.append(
        Paragraph(
            f"Recommended vendor: <b>{_esc(rec.vendor_name)}</b>",
            ParagraphStyle("rec", parent=st["body"], fontSize=13, leading=18,
                           textColor=ACCENT, spaceAfter=8),
        )
    )
    story.extend(_paragraphs(rec.rationale, st["body"]))

    if rec.runner_up:
        story.append(Paragraph("Runner-up", st["h3"]))
        story.append(Paragraph(_esc(rec.runner_up), st["body"]))

    if rec.risks:
        story.append(Paragraph("Residual risks", st["h3"]))
        story.extend(_bullets(rec.risks, st["bullet"]))

    if rec.conditions:
        story.append(Paragraph("Clarify or negotiate before committing", st["h3"]))
        story.extend(_bullets(rec.conditions, st["bullet"]))

    return story


def _appendix(st, questionnaire: Questionnaire, summaries: list[VendorSummary]) -> list:
    story = [PageBreak(), Paragraph("Appendix: extracted answers", st["h1"])]
    story.append(
        Paragraph(
            "Each vendor's answers as extracted from their response document, with "
            "the location they were found. Use these to verify any statement in "
            "this report against the original submission.",
            st["note"],
        )
    )

    by_id = {q.id: q for q in questionnaire.questions}

    for summary in summaries:
        story.append(PageBreak())
        story.append(Paragraph(_esc(summary.vendor_name), st["h2"]))

        for answer in summary.answers:
            question = by_id.get(answer.question_id)
            question_text = question.text if question else answer.question_id
            category = question.category if question else ""

            status = (
                answer.status.value
                if isinstance(answer.status, AnswerStatus)
                else str(answer.status)
            )
            confidence = getattr(answer.confidence, "value", answer.confidence)

            block = [
                Paragraph(
                    f"<b>{_esc(answer.question_id)}</b> — {_esc(question_text)}",
                    st["h3"],
                )
            ]

            meta = " · ".join(
                part
                for part in (
                    category,
                    f"status: {status}",
                    f"confidence: {confidence}",
                    answer.source_locator,
                )
                if part
            )
            block.append(Paragraph(_esc(meta), st["note"]))

            if answer.answer_text:
                block.append(Paragraph(_esc(answer.answer_text), st["body"]))
            else:
                block.append(
                    Paragraph(
                        "<i>No answer found in this vendor's response.</i>", st["body"]
                    )
                )

            if answer.verbatim_quote:
                block.append(
                    Paragraph(
                        f"“{_esc(answer.verbatim_quote)}”",
                        ParagraphStyle(
                            "quote", parent=st["note"], leftIndent=10, fontName="Helvetica-Oblique"
                        ),
                    )
                )

            # Keep a question and its answer on the same page where possible.
            story.append(KeepTogether(block))

    return story


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_report(
    *,
    output_path: Path | str,
    questionnaire: Questionnaire,
    summaries: list[VendorSummary],
    synthesis: Synthesis,
    include_appendix: bool = True,
    failures: dict[str, str] | None = None,
) -> Path:
    """Render the report and return the path it was written to."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    st = _styles()
    failures = failures or {}
    # The synthesis's vendor ordering is authoritative for table columns, but
    # fall back to summary order if Stage 2 returned nothing usable.
    vendor_names = [s.vendor_name for s in summaries]

    doc = _Doc(
        str(output_path),
        rfp_name=questionnaire.rfp_name or "RFP Evaluation",
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=MARGIN,
        title=f"{questionnaire.rfp_name or 'RFP'} — Vendor Evaluation",
        author="RFP Evaluator",
    )

    story: list = []
    story.extend(_title_page(st, questionnaire, summaries, list(failures)))

    # Switch to the footer-bearing template for everything after the cover.
    story.append(NextPageTemplate("content"))
    story.append(PageBreak())
    story.append(Paragraph("Executive summary", st["h1"]))
    story.extend(_paragraphs(synthesis.executive_summary, st["body"]))
    story.append(Spacer(1, 4 * mm))

    story.extend(_failures_section(st, failures))
    story.extend(_comparison_section(st, synthesis, vendor_names))
    story.extend(_vendor_sections(st, synthesis, summaries))
    story.extend(_recommendation_section(st, synthesis))

    if include_appendix:
        story.extend(_appendix(st, questionnaire, summaries))

    doc.build(story)
    return output_path
