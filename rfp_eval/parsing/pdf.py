"""PDF parsing via pdfplumber.

Both narrative text and tables are extracted. Tables matter here: RFP responses
routinely put the actual answers in a two-column question/answer table, and
``extract_text()`` alone flattens those into ambiguous runs of words.
"""

from __future__ import annotations

from pathlib import Path

from ..errors import ParseError
from . import Document, TextUnit


def _render_table(rows: list[list[str | None]]) -> str:
    """Render a table as pipe-delimited lines, which survive tokenization well."""
    lines = []
    for row in rows:
        cells = [(c or "").replace("\n", " ").strip() for c in row]
        if any(cells):
            lines.append(" | ".join(cells))
    return "\n".join(lines)


def parse_pdf(path: Path, display_name: str) -> Document:
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - environment problem, not data
        raise ParseError(display_name, f"pdfplumber is not installed: {exc}") from exc

    units: list[TextUnit] = []
    page_count = 0

    try:
        with pdfplumber.open(str(path)) as pdf:
            page_count = len(pdf.pages)
            for index, page in enumerate(pdf.pages, start=1):
                locator = f"p. {index}"

                try:
                    text = page.extract_text() or ""
                except Exception as exc:
                    # One malformed page should not lose the other 99.
                    text = ""
                    units.append(
                        TextUnit(
                            text=f"(page text could not be extracted: {exc})",
                            locator=locator,
                            section=locator,
                        )
                    )

                if text.strip():
                    units.append(
                        TextUnit(text=text.strip(), locator=locator, section=locator)
                    )

                try:
                    tables = page.extract_tables() or []
                except Exception:
                    tables = []

                for t_index, table in enumerate(tables, start=1):
                    rendered = _render_table(table)
                    if rendered.strip():
                        units.append(
                            TextUnit(
                                text=f"Table {t_index}:\n{rendered}",
                                locator=f"{locator}, table {t_index}",
                                section=locator,
                            )
                        )
    except ParseError:
        raise
    except Exception as exc:
        raise ParseError(
            display_name,
            f"could not read PDF ({type(exc).__name__}: {exc}). "
            "It may be corrupt or password-protected.",
        ) from exc

    return Document(
        filename=display_name,
        units=units,
        kind="pdf",
        meta={"pages": page_count},
    )
