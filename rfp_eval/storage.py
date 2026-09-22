"""Run persistence.

Everything expensive is written to disk the moment it exists, so a Stage 2
failure never costs the Stage 1 calls again. Layout:

    <data_dir>/runs/<run_id>/
        manifest.json          run bookkeeping and vendor statuses
        questionnaire.json     the confirmed canonical questions
        synthesis.json         Stage 2 output
        report.pdf             the generated report
        inputs/                copies of the uploaded source files
        vendors/<slug>/
            summary.json       Stage 1 output for this vendor
            error.json         why this vendor failed, if it did
            chunks/000.json    per-chunk extractions (resume granularity)

Plain JSON files rather than a database: they are inspectable, diffable, and a
bad extraction can be hand-edited and Stage 2 re-run without touching code.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .models import (
    ChunkExtraction,
    Questionnaire,
    RunManifest,
    Synthesis,
    VendorRecord,
    VendorStatus,
    VendorSummary,
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """Filesystem-safe vendor identifier."""
    slug = _SLUG_RE.sub("-", name.lower()).strip("-")
    return slug or "vendor"


def unique_slugs(names: list[str]) -> list[str]:
    """Map vendor names to slugs that are unique *as a set*.

    ``slugify`` alone is not injective: "Acme Corp" and "Acme Corp." both
    reduce to ``acme-corp``. Since the slug is the directory a vendor's results
    are written to, a collision silently overwrites one vendor's evaluation
    with another's -- and Stage 2 then compares a vendor against itself. Later
    collisions get a numeric suffix.
    """
    assigned: list[str] = []
    used: set[str] = set()

    for name in names:
        base = slugify(name)
        slug, suffix = base, 2
        while slug in used:
            slug = f"{base}-{suffix}"
            suffix += 1
        used.add(slug)
        assigned.append(slug)

    return assigned


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _write_json(path: Path, payload: dict) -> None:
    """Write atomically so a crash mid-write cannot corrupt a completed result."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


class RunStore:
    """Read/write access to a single run's directory."""

    def __init__(self, data_dir: Path | str, run_id: str):
        self.run_id = run_id
        self.root = Path(data_dir) / "runs" / run_id
        self.root.mkdir(parents=True, exist_ok=True)

    # -- paths ---------------------------------------------------------

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    @property
    def questionnaire_path(self) -> Path:
        return self.root / "questionnaire.json"

    @property
    def synthesis_path(self) -> Path:
        return self.root / "synthesis.json"

    @property
    def report_path(self) -> Path:
        return self.root / "report.pdf"

    @property
    def inputs_dir(self) -> Path:
        return self.root / "inputs"

    def vendor_dir(self, slug: str) -> Path:
        return self.root / "vendors" / slug

    # -- inputs --------------------------------------------------------

    def save_input(self, filename: str, data: bytes) -> Path:
        """Persist an uploaded file so a resumed run can re-read it."""
        self.inputs_dir.mkdir(parents=True, exist_ok=True)
        # Basename only -- an uploaded filename is untrusted input.
        safe = Path(filename).name or "upload"
        path = self.inputs_dir / safe
        path.write_bytes(data)
        return path

    def input_path(self, filename: str) -> Path:
        return self.inputs_dir / Path(filename).name

    # -- manifest ------------------------------------------------------

    def save_manifest(self, manifest: RunManifest) -> None:
        manifest.updated_at = datetime.now(timezone.utc).isoformat()
        _write_json(self.manifest_path, manifest.model_dump(mode="json"))

    def load_manifest(self) -> RunManifest | None:
        data = _read_json(self.manifest_path)
        return RunManifest.model_validate(data) if data else None

    def update_vendor_status(
        self,
        slug: str,
        status: VendorStatus,
        *,
        error: str = "",
        chunk_count: int | None = None,
    ) -> None:
        """Patch one vendor's status in the manifest and flush it."""
        manifest = self.load_manifest()
        if manifest is None:
            return
        record = manifest.vendor(slug)
        if record is None:
            return
        record.status = status
        record.error = error
        if chunk_count is not None:
            record.chunk_count = chunk_count
        self.save_manifest(manifest)

    # -- questionnaire -------------------------------------------------

    def save_questionnaire(self, questionnaire: Questionnaire) -> None:
        _write_json(self.questionnaire_path, questionnaire.model_dump(mode="json"))

    def load_questionnaire(self) -> Questionnaire | None:
        data = _read_json(self.questionnaire_path)
        return Questionnaire.model_validate(data) if data else None

    # -- per-vendor ----------------------------------------------------

    def save_vendor_summary(self, slug: str, summary: VendorSummary) -> None:
        _write_json(self.vendor_dir(slug) / "summary.json", summary.model_dump(mode="json"))
        # A vendor that now succeeds should not keep a stale error file around.
        (self.vendor_dir(slug) / "error.json").unlink(missing_ok=True)

    def load_vendor_summary(self, slug: str) -> VendorSummary | None:
        data = _read_json(self.vendor_dir(slug) / "summary.json")
        return VendorSummary.model_validate(data) if data else None

    def save_vendor_error(self, slug: str, message: str, detail: str = "") -> None:
        _write_json(
            self.vendor_dir(slug) / "error.json",
            {
                "message": message,
                "detail": detail,
                "at": datetime.now(timezone.utc).isoformat(),
            },
        )

    def load_vendor_error(self, slug: str) -> dict | None:
        return _read_json(self.vendor_dir(slug) / "error.json")

    def save_chunk(self, slug: str, index: int, extraction: ChunkExtraction) -> None:
        path = self.vendor_dir(slug) / "chunks" / f"{index:03d}.json"
        _write_json(path, extraction.model_dump(mode="json"))

    def load_chunk(self, slug: str, index: int) -> ChunkExtraction | None:
        data = _read_json(self.vendor_dir(slug) / "chunks" / f"{index:03d}.json")
        return ChunkExtraction.model_validate(data) if data else None

    def clear_vendor(self, slug: str) -> None:
        """Discard a vendor's results so it re-runs from scratch."""
        shutil.rmtree(self.vendor_dir(slug), ignore_errors=True)

    def load_all_summaries(self) -> list[VendorSummary]:
        """Every completed vendor summary, in manifest order."""
        manifest = self.load_manifest()
        if manifest is None:
            return []
        summaries = []
        for record in manifest.vendors:
            summary = self.load_vendor_summary(record.slug)
            if summary is not None:
                summaries.append(summary)
        return summaries

    # -- synthesis / report --------------------------------------------

    def save_synthesis(self, synthesis: Synthesis) -> None:
        _write_json(self.synthesis_path, synthesis.model_dump(mode="json"))

    def load_synthesis(self) -> Synthesis | None:
        data = _read_json(self.synthesis_path)
        return Synthesis.model_validate(data) if data else None


# ---------------------------------------------------------------------------
# Run discovery (for the resume selector in the UI)
# ---------------------------------------------------------------------------


def list_runs(data_dir: Path | str) -> list[RunManifest]:
    """Every run on disk, newest first. Unreadable runs are skipped."""
    runs_dir = Path(data_dir) / "runs"
    if not runs_dir.exists():
        return []

    manifests = []
    for entry in sorted(runs_dir.iterdir(), reverse=True):
        if not entry.is_dir():
            continue
        data = _read_json(entry / "manifest.json")
        if data:
            try:
                manifests.append(RunManifest.model_validate(data))
            except Exception:
                continue  # a corrupt manifest should not hide the other runs
    return manifests


def create_run(
    data_dir: Path | str,
    *,
    rfp_name: str = "",
    rfp_file: str = "",
    settings_snapshot: dict | None = None,
) -> tuple[RunStore, RunManifest]:
    """Start a new run, collision-proofed against same-second creation."""
    base = new_run_id()
    run_id, suffix = base, 1
    while (Path(data_dir) / "runs" / run_id).exists():
        run_id = f"{base}-{suffix}"
        suffix += 1

    store = RunStore(data_dir, run_id)
    manifest = RunManifest(
        run_id=run_id,
        rfp_name=rfp_name,
        rfp_file=rfp_file,
        settings_snapshot=settings_snapshot or {},
    )
    store.save_manifest(manifest)
    return store, manifest


__all__ = [
    "RunStore",
    "VendorRecord",
    "create_run",
    "list_runs",
    "new_run_id",
    "slugify",
    "unique_slugs",
]
