"""Drives the full pipeline and reports progress through a callback.

The callback is the only concession to the UI, and it is a plain function --
``app.py`` passes one that writes to ``st.status``, a script could pass
``print``. Nothing here knows Streamlit exists.

Failure policy: one vendor failing never aborts the others. Each vendor runs in
its own try/except, its error is written to the run directory and recorded in
the manifest, and the run proceeds to Stage 2 with whatever succeeded.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..config import Settings
from ..errors import ParseError, ProviderError, RfpEvalError, SchemaError
from ..llm import get_provider
from ..llm.base import LLMProvider
from ..models import Questionnaire, Synthesis, VendorStatus, VendorSummary
from ..parsing import parse_document
from ..storage import RunStore
from .stage1 import run_stage1
from .stage2 import NoVendorsError, run_stage2


@dataclass
class ProgressEvent:
    """A step of the pipeline, for display."""

    stage: str  # "parse" | "stage1" | "stage2" | "report" | "done"
    message: str
    current: int = 0
    total: int = 0
    level: str = "info"  # "info" | "warning" | "error"


ProgressCallback = Callable[[ProgressEvent], None]


@dataclass
class VendorInput:
    """One vendor to evaluate."""

    name: str
    slug: str
    path: Path


@dataclass
class PipelineResult:
    summaries: list[VendorSummary] = field(default_factory=list)
    synthesis: Synthesis | None = None
    report_path: Path | None = None
    failures: dict[str, str] = field(default_factory=dict)  # vendor name -> reason

    @property
    def ok(self) -> bool:
        return self.synthesis is not None


def _noop(_: ProgressEvent) -> None:
    return None


def run_pipeline(
    *,
    questionnaire: Questionnaire,
    vendors: list[VendorInput],
    settings: Settings,
    store: RunStore,
    provider: LLMProvider | None = None,
    on_progress: ProgressCallback | None = None,
    generate_report: bool = True,
    resume: bool = True,
) -> PipelineResult:
    """Run Stage 1 for every vendor, then Stage 2, then render the report."""
    report = on_progress or _noop
    provider = provider or get_provider(settings)
    result = PipelineResult()

    completed = store.load_manifest().completed_slugs() if resume else set()

    # ---- Stage 1: one independent pass per vendor ---------------------
    for index, vendor in enumerate(vendors, start=1):
        label = f"vendor {index} of {len(vendors)}: {vendor.name}"

        if resume and vendor.slug in completed:
            cached = store.load_vendor_summary(vendor.slug)
            if cached is not None:
                result.summaries.append(cached)
                report(
                    ProgressEvent(
                        "stage1", f"Reusing saved results for {vendor.name}",
                        index, len(vendors),
                    )
                )
                continue

        store.update_vendor_status(vendor.slug, VendorStatus.RUNNING)
        report(ProgressEvent("stage1", f"Summarizing {label}", index, len(vendors)))

        try:
            document = parse_document(vendor.path, display_name=vendor.path.name)

            def on_chunk(chunk_index: int, chunk_total: int, _label=label) -> None:
                if chunk_total > 1:
                    report(
                        ProgressEvent(
                            "stage1",
                            f"Summarizing {_label} (part {chunk_index} of {chunk_total})",
                            index,
                            len(vendors),
                        )
                    )

            summary = run_stage1(
                vendor_name=vendor.name,
                slug=vendor.slug,
                document=document,
                questionnaire=questionnaire,
                provider=provider,
                model=settings.stage1_model,
                max_chunk_tokens=settings.max_chunk_tokens,
                max_output_tokens=settings.max_output_tokens,
                store=store,
                on_chunk=on_chunk,
                resume=resume,
            )

            store.save_vendor_summary(vendor.slug, summary)
            store.update_vendor_status(
                vendor.slug, VendorStatus.COMPLETE, chunk_count=summary.chunk_count
            )
            result.summaries.append(summary)

        except (ParseError, ProviderError, SchemaError, RfpEvalError) as exc:
            _record_failure(store, vendor, result, report, str(exc))
        except Exception as exc:  # unexpected -- still isolated to this vendor
            _record_failure(
                store,
                vendor,
                result,
                report,
                f"unexpected error: {type(exc).__name__}: {exc}",
                detail=traceback.format_exc(),
            )

    # ---- Stage 2: one call over the Stage 1 output --------------------
    if not result.summaries:
        report(
            ProgressEvent(
                "stage2",
                "Every vendor failed, so there is nothing to compare. "
                "Fix the errors above and re-run.",
                level="error",
            )
        )
        return result

    report(
        ProgressEvent(
            "stage2", f"Synthesizing across {len(result.summaries)} vendor summaries"
        )
    )

    try:
        synthesis = run_stage2(
            summaries=result.summaries,
            questionnaire=questionnaire,
            provider=provider,
            model=settings.stage2_model,
            max_output_tokens=settings.max_output_tokens,
            include_verbatim=settings.stage2_include_verbatim,
            failed_vendors=list(result.failures.keys()),
        )
    except (NoVendorsError, ProviderError, SchemaError, RfpEvalError) as exc:
        # Stage 1 output is already on disk, so this is cheap to retry alone.
        report(
            ProgressEvent(
                "stage2",
                f"Synthesis failed: {exc}. Every vendor summary is saved -- "
                "use 'Retry synthesis' to re-run only this step.",
                level="error",
            )
        )
        return result

    store.save_synthesis(synthesis)
    result.synthesis = synthesis

    manifest = store.load_manifest()
    if manifest is not None:
        manifest.synthesis_complete = True
        store.save_manifest(manifest)

    # ---- Report -------------------------------------------------------
    if generate_report:
        report(ProgressEvent("report", "Generating PDF report"))
        result.report_path = _render_report(
            store=store,
            questionnaire=questionnaire,
            summaries=result.summaries,
            synthesis=synthesis,
            settings=settings,
            failures=result.failures,
            report=report,
        )

    report(ProgressEvent("done", "Evaluation complete"))
    return result


def _record_failure(
    store: RunStore,
    vendor: VendorInput,
    result: PipelineResult,
    report: ProgressCallback,
    message: str,
    detail: str = "",
) -> None:
    store.save_vendor_error(vendor.slug, message, detail)
    store.update_vendor_status(vendor.slug, VendorStatus.FAILED, error=message)
    result.failures[vendor.name] = message
    report(
        ProgressEvent(
            "stage1",
            f"{vendor.name} failed: {message} -- continuing with the other vendors.",
            level="warning",
        )
    )


def _render_report(
    *,
    store: RunStore,
    questionnaire: Questionnaire,
    summaries: list[VendorSummary],
    synthesis: Synthesis,
    settings: Settings,
    failures: dict[str, str],
    report: ProgressCallback,
) -> Path | None:
    from ..report.pdf_report import build_report

    try:
        path = build_report(
            output_path=store.report_path,
            questionnaire=questionnaire,
            summaries=summaries,
            synthesis=synthesis,
            include_appendix=settings.include_appendix,
            failures=failures,
        )
    except Exception as exc:
        # The synthesis is saved; only the rendering failed.
        report(
            ProgressEvent(
                "report",
                f"PDF generation failed ({type(exc).__name__}: {exc}). "
                "The synthesis is saved and can be re-rendered.",
                level="error",
            )
        )
        return None

    manifest = store.load_manifest()
    if manifest is not None:
        manifest.report_path = str(path)
        store.save_manifest(manifest)
    return path


def run_stage2_only(
    *,
    questionnaire: Questionnaire,
    settings: Settings,
    store: RunStore,
    provider: LLMProvider | None = None,
    on_progress: ProgressCallback | None = None,
) -> PipelineResult:
    """Re-run synthesis and rendering from saved Stage 1 output.

    This is the whole point of persisting per-vendor results: a synthesis
    failure costs one call to fix, not one per vendor.
    """
    report = on_progress or _noop
    provider = provider or get_provider(settings)

    result = PipelineResult(summaries=store.load_all_summaries())
    manifest = store.load_manifest()
    if manifest is not None:
        result.failures = {
            v.name: v.error for v in manifest.vendors if v.status == VendorStatus.FAILED
        }

    if not result.summaries:
        report(
            ProgressEvent(
                "stage2", "No saved vendor summaries found for this run.", level="error"
            )
        )
        return result

    report(
        ProgressEvent(
            "stage2", f"Synthesizing across {len(result.summaries)} saved vendor summaries"
        )
    )

    try:
        synthesis = run_stage2(
            summaries=result.summaries,
            questionnaire=questionnaire,
            provider=provider,
            model=settings.stage2_model,
            max_output_tokens=settings.max_output_tokens,
            include_verbatim=settings.stage2_include_verbatim,
            failed_vendors=list(result.failures.keys()),
        )
    except RfpEvalError as exc:
        report(ProgressEvent("stage2", f"Synthesis failed: {exc}", level="error"))
        return result

    store.save_synthesis(synthesis)
    result.synthesis = synthesis
    if manifest is not None:
        manifest.synthesis_complete = True
        store.save_manifest(manifest)

    report(ProgressEvent("report", "Generating PDF report"))
    result.report_path = _render_report(
        store=store,
        questionnaire=questionnaire,
        summaries=result.summaries,
        synthesis=synthesis,
        settings=settings,
        failures=result.failures,
        report=report,
    )
    report(ProgressEvent("done", "Evaluation complete"))
    return result
