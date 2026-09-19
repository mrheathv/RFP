"""Parsing and chunking: the guarantees the rest of the pipeline depends on."""

from __future__ import annotations

import pytest

from rfp_eval.errors import ParseError
from rfp_eval.parsing import Document, TextUnit, parse_document
from rfp_eval.parsing.chunking import chunk_document
from tests.conftest import make_pdf, make_xlsx


def test_parse_excel_yields_row_units(xlsx_file):
    doc = parse_document(xlsx_file)
    assert doc.kind == "excel"
    assert len(doc.units) == 8
    # Headers are paired with values so column meaning survives into the prompt.
    assert "Question:" in doc.units[0].text
    assert "row" in doc.units[0].locator


def test_parse_pdf_yields_page_units(pdf_file):
    doc = parse_document(pdf_file)
    assert doc.kind == "pdf"
    assert doc.meta["pages"] == 2
    assert any("p. 1" == u.locator for u in doc.units)
    assert doc.char_count > 100


def test_missing_file_names_the_file(tmp_path):
    with pytest.raises(ParseError) as exc:
        parse_document(tmp_path / "nope.pdf")
    assert "nope.pdf" in str(exc.value)


def test_unsupported_format_is_explicit(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("hello")
    with pytest.raises(ParseError, match="unsupported format"):
        parse_document(path)


def test_legacy_xls_gets_a_useful_message(tmp_path):
    path = tmp_path / "old.xls"
    path.write_bytes(b"\xd0\xcf\x11\xe0")
    with pytest.raises(ParseError, match="re-save"):
        parse_document(path)


def test_corrupt_pdf_fails_with_the_filename(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.4\nthis is not actually a pdf\n")
    with pytest.raises(ParseError) as exc:
        parse_document(path)
    assert "broken.pdf" in str(exc.value)


def test_corrupt_xlsx_fails_with_the_filename(tmp_path):
    path = tmp_path / "broken.xlsx"
    path.write_bytes(b"not a zip archive at all")
    with pytest.raises(ParseError) as exc:
        parse_document(path)
    assert "broken.xlsx" in str(exc.value)


def test_empty_pdf_suggests_ocr(tmp_path):
    # A PDF with no text is the scanned-document case.
    make_pdf(tmp_path / "scan.pdf", pages=1, body="")
    with pytest.raises(ParseError, match="scanned"):
        parse_document(tmp_path / "scan.pdf")


def test_excel_without_headers_still_parses(tmp_path):
    path = make_xlsx(tmp_path / "raw.xlsx", rows=5, with_headers=False)
    doc = parse_document(path)
    assert len(doc.units) >= 5


# -- chunking --------------------------------------------------------------


def _doc(n: int, size: int = 100) -> Document:
    return Document(
        filename="d.pdf",
        units=[TextUnit(text="x" * size, locator=f"p. {i}") for i in range(1, n + 1)],
    )


def test_small_document_is_one_chunk():
    chunks = chunk_document(_doc(3), max_tokens=5000)
    assert len(chunks) == 1
    assert chunks[0].total == 1


def test_large_document_splits_into_several_chunks():
    chunks = chunk_document(_doc(60, size=500), max_tokens=1000)
    assert len(chunks) > 1
    assert all(c.total == len(chunks) for c in chunks)


def test_chunking_never_loses_content():
    """The load-bearing guarantee: every unit appears exactly once."""
    document = _doc(40, size=400)
    chunks = chunk_document(document, max_tokens=1000)

    recovered = [u.text for c in chunks for u in c.units]
    assert recovered == [u.text for u in document.units]


def test_oversized_single_unit_is_split_not_dropped():
    document = Document(
        filename="d.pdf",
        units=[TextUnit(text="word " * 5000, locator="p. 1")],
    )
    chunks = chunk_document(document, max_tokens=500)

    rejoined = "".join(u.text for c in chunks for u in c.units)
    assert rejoined == "word " * 5000
    assert sum(c.split_units for c in chunks) > 0
    assert "part" in chunks[0].units[0].locator


def test_verification_callback_forces_a_resplit():
    """A pessimistic real tokenizer should cause more chunks than the estimate."""
    document = _doc(8, size=300)
    loose = chunk_document(document, max_tokens=5000)
    tight = chunk_document(document, max_tokens=5000, count_tokens=lambda t: len(t) * 10)
    assert len(tight) > len(loose)


def test_failing_token_counter_does_not_break_the_run():
    def boom(_text):
        raise RuntimeError("counting service down")

    chunks = chunk_document(_doc(5), max_tokens=5000, count_tokens=boom)
    assert len(chunks) == 1
