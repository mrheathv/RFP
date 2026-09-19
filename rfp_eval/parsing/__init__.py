"""Document parsing: PDF and Excel in, locator-tagged text units out.

A ``TextUnit`` is the atom of this layer. It carries text plus enough of a
locator (page number, sheet + row) that an extracted answer can be traced back
to the vendor's own document in the report appendix. Chunking only ever splits
*between* units, so a spreadsheet row or a table row is never torn in half.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..errors import ParseError

# Below this many characters of extracted text we assume the file is a scanned
# image, a stub, or otherwise unusable -- better to say so than to send an
# empty document to the model and bill the user for a hallucinated summary.
MIN_USABLE_CHARS = 40


@dataclass
class TextUnit:
    """One atomic piece of a source document."""

    text: str
    locator: str = ""
    # Structural grouping hint (page number, sheet name) so the chunker can
    # prefer to break where the document itself breaks.
    section: str = ""

    def rendered(self) -> str:
        """The form the LLM sees: locator-prefixed so quotes stay traceable."""
        return f"[{self.locator}] {self.text}" if self.locator else self.text


@dataclass
class Document:
    """A parsed source document."""

    filename: str
    units: list[TextUnit] = field(default_factory=list)
    kind: str = ""  # "pdf" | "excel"
    meta: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "\n".join(u.rendered() for u in self.units)

    @property
    def char_count(self) -> int:
        return sum(len(u.text) for u in self.units)


def parse_document(path: str | Path, *, display_name: str | None = None) -> Document:
    """Parse a PDF or Excel file into a ``Document``.

    Raises ``ParseError`` naming the file if it cannot be read, is an
    unsupported format, or yields effectively no text.
    """
    path = Path(path)
    name = display_name or path.name
    suffix = path.suffix.lower()

    if not path.exists():
        raise ParseError(name, "file not found")

    if suffix == ".pdf":
        from .pdf import parse_pdf

        doc = parse_pdf(path, name)
    elif suffix in (".xlsx", ".xlsm", ".xltx"):
        from .excel import parse_excel

        doc = parse_excel(path, name)
    elif suffix == ".xls":
        raise ParseError(
            name,
            "legacy .xls is not supported -- re-save the file as .xlsx and retry",
        )
    else:
        raise ParseError(
            name,
            f"unsupported format '{suffix or 'none'}' -- this tool reads .pdf and .xlsx",
        )

    if doc.char_count < MIN_USABLE_CHARS:
        hint = (
            " Is this a scanned PDF? Image-only documents need OCR, which this "
            "tool does not perform."
            if suffix == ".pdf"
            else " The file appears to be empty."
        )
        raise ParseError(name, f"no extractable text found.{hint}")

    return doc


__all__ = ["TextUnit", "Document", "parse_document", "ParseError", "MIN_USABLE_CHARS"]
