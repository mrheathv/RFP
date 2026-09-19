"""Shared fixtures. Nothing here touches the network."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rfp_eval.config import Settings  # noqa: E402
from rfp_eval.models import Question, Questionnaire  # noqa: E402

CATEGORIES = ["Security", "Support", "Pricing"]


@pytest.fixture
def questionnaire() -> Questionnaire:
    questions = [
        Question(
            id=f"Q{i}",
            category=CATEGORIES[i % len(CATEGORIES)],
            text=f"Describe your approach to {CATEGORIES[i % len(CATEGORIES)].lower()} (item {i}).",
        )
        for i in range(1, 7)
    ]
    return Questionnaire(
        rfp_name="Test RFP", questions=questions, source_file="test-rfp.xlsx"
    )


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        provider="fake",
        stage0_model="fake",
        stage1_model="fake",
        stage2_model="fake",
        max_chunk_tokens=2000,
        max_output_tokens=4000,
        data_dir=tmp_path / "data",
    )


# --------------------------------------------------------------------------
# Fixture files, generated at test time so no binaries live in the repo.
# --------------------------------------------------------------------------


def make_xlsx(path: Path, rows: int = 8, *, with_headers: bool = True) -> Path:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Questionnaire"
    if with_headers:
        ws.append(["ID", "Section", "Question", "Required"])
    for i in range(1, rows + 1):
        ws.append(
            [
                f"Q{i}",
                CATEGORIES[i % len(CATEGORIES)],
                f"Please describe in detail your approach to requirement number {i}.",
                "Yes",
            ]
        )
    wb.save(path)
    return path


def make_pdf(path: Path, pages: int = 2, *, body: str | None = None) -> Path:
    """Write a simple text PDF. ``body=""`` produces a page with no text at all,
    which is what a scanned image-only document looks like to the parser."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=A4)
    for page in range(1, pages + 1):
        if body == "":
            c.showPage()
            continue
        c.setFont("Helvetica", 11)
        y = 800
        c.drawString(60, y, f"Vendor Response - page {page}")
        y -= 24
        lines = (
            body.splitlines()
            if body
            else [
                f"{page}.{i} We provide a comprehensive approach to requirement {i}."
                for i in range(1, 12)
            ]
        )
        for line in lines:
            c.drawString(60, y, line[:95])
            y -= 16
            if y < 60:
                break
        c.showPage()
    c.save()
    return path


@pytest.fixture
def xlsx_file(tmp_path) -> Path:
    return make_xlsx(tmp_path / "test-rfp.xlsx")


@pytest.fixture
def pdf_file(tmp_path) -> Path:
    return make_pdf(tmp_path / "vendor-alpha.pdf")
