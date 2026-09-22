"""Streamlit UI for the RFP vendor evaluation tool.

This is the only module in the project that imports Streamlit. It handles
uploads, settings, progress display, and downloads; every piece of actual work
lives in ``rfp_eval`` and is reachable from a script or a job queue without
touching this file.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from rfp_eval.config import ANTHROPIC, FAKE, OPENAI, VALID_EFFORTS, Settings
from rfp_eval.errors import ConfigError, ParseError, RfpEvalError
from rfp_eval.llm import get_provider
from rfp_eval.models import Question, Questionnaire, VendorRecord, VendorStatus
from rfp_eval.parsing import parse_document
from rfp_eval.pipeline import ProgressEvent, run_pipeline, run_stage2_only
from rfp_eval.pipeline.orchestrator import VendorInput
from rfp_eval.pipeline.stage0 import build_questionnaire
from rfp_eval.storage import RunStore, create_run, list_runs, unique_slugs

st.set_page_config(page_title="RFP Vendor Evaluation", page_icon="📋", layout="wide")

SUPPORTED = ["pdf", "xlsx", "xlsm", "xltx"]


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------


def _init_state() -> None:
    defaults = {
        "run_id": None,
        "questionnaire": None,
        "questions_confirmed": False,
        "result": None,
        "vendor_names": {},
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def _base_settings() -> Settings:
    try:
        return Settings.from_env()
    except ConfigError as exc:
        st.error(f"Configuration problem: {exc}")
        st.stop()


def _store(settings: Settings) -> RunStore | None:
    if not st.session_state.run_id:
        return None
    return RunStore(settings.data_dir, st.session_state.run_id)


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------


def _sidebar(base: Settings) -> Settings:
    st.sidebar.title("Settings")

    providers = [ANTHROPIC, OPENAI, FAKE]
    provider = st.sidebar.selectbox(
        "LLM provider",
        providers,
        index=providers.index(base.provider) if base.provider in providers else 0,
        help="`fake` returns canned data with no API key — use it to try the UI offline.",
    )

    if provider == FAKE:
        st.sidebar.info("Fake provider: output is placeholder text, not a real evaluation.")

    stage1_model = st.sidebar.text_input(
        "Stage 1 model (per vendor)",
        value=base.stage1_model,
        help="Reads whole vendor documents — the bulk-token stage.",
    )
    stage2_model = st.sidebar.text_input(
        "Stage 2 model (synthesis)",
        value=base.stage2_model,
        help="Reads only the Stage 1 summaries.",
    )
    stage0_model = st.sidebar.text_input(
        "Stage 0 model (questionnaire)", value=base.stage0_model
    )

    effort = st.sidebar.select_slider(
        "Reasoning effort (Anthropic)",
        options=list(VALID_EFFORTS),
        value=base.effort if base.effort in VALID_EFFORTS else "high",
    )

    max_chunk_tokens = st.sidebar.number_input(
        "Max tokens per Stage 1 call",
        min_value=1_000,
        max_value=500_000,
        value=base.max_chunk_tokens,
        step=5_000,
        help="Documents larger than this are split across several calls and merged. "
        "Nothing is ever truncated.",
    )
    max_output_tokens = st.sidebar.number_input(
        "Max output tokens per call",
        min_value=1_000,
        max_value=128_000,
        value=base.max_output_tokens,
        step=2_000,
    )

    st.sidebar.divider()
    include_appendix = st.sidebar.checkbox(
        "Include answer appendix in the PDF",
        value=base.include_appendix,
        help="Raw extracted answers per vendor, with source locations, for traceability.",
    )
    include_verbatim = st.sidebar.checkbox(
        "Send verbatim quotes to Stage 2",
        value=base.stage2_include_verbatim,
        help="Off by default: quotes are the biggest token cost and the appendix "
        "keeps them regardless.",
    )

    settings = base.with_overrides(
        provider=provider,
        stage0_model=stage0_model,
        stage1_model=stage1_model,
        stage2_model=stage2_model,
        effort=effort,
        max_chunk_tokens=int(max_chunk_tokens),
        max_output_tokens=int(max_output_tokens),
        include_appendix=include_appendix,
        stage2_include_verbatim=include_verbatim,
    )

    _resume_selector(settings)
    return settings


def _resume_selector(settings: Settings) -> None:
    st.sidebar.divider()
    st.sidebar.subheader("Previous runs")

    runs = list_runs(settings.data_dir)
    if not runs:
        st.sidebar.caption("No previous runs yet.")
        return

    labels = {
        f"{r.run_id} — {r.rfp_name or 'untitled'} ({len(r.vendors)} vendors)": r.run_id
        for r in runs[:25]
    }
    choice = st.sidebar.selectbox(
        "Load a run", ["—"] + list(labels), label_visibility="collapsed"
    )

    if choice != "—" and st.sidebar.button("Load", use_container_width=True):
        run_id = labels[choice]
        store = RunStore(settings.data_dir, run_id)
        st.session_state.run_id = run_id
        st.session_state.questionnaire = store.load_questionnaire()
        st.session_state.questions_confirmed = st.session_state.questionnaire is not None
        st.session_state.result = None
        st.rerun()


# ---------------------------------------------------------------------------
# Step 1 -- the questionnaire
# ---------------------------------------------------------------------------


def _step_questionnaire(settings: Settings) -> None:
    st.header("1 · The RFP questionnaire")
    st.caption(
        "Upload the questionnaire you sent to vendors. Its questions become the keys "
        "every vendor answer is mapped onto, and its sections become the comparison "
        "criteria in the report."
    )

    upload = st.file_uploader(
        "Original RFP (PDF or Excel)", type=SUPPORTED, key="rfp_upload"
    )

    col1, col2 = st.columns([1, 3])
    with col1:
        force_llm = st.checkbox(
            "Always use the LLM",
            value=False,
            help="By default the questionnaire is parsed with heuristics first, "
            "which costs nothing. Tick this to skip straight to the LLM.",
        )

    if upload is not None and st.button("Parse questionnaire", type="primary"):
        _parse_questionnaire(settings, upload, force_llm)

    if st.session_state.questionnaire is not None:
        _review_questions(settings)


def _parse_questionnaire(settings: Settings, upload, force_llm: bool) -> None:
    try:
        provider = get_provider(settings)
    except ConfigError as exc:
        st.error(str(exc))
        return

    with st.status("Parsing the RFP…", expanded=True) as status:
        try:
            store, manifest = create_run(
                settings.data_dir,
                rfp_file=upload.name,
                settings_snapshot=settings.snapshot(),
            )
            st.session_state.run_id = manifest.run_id
            st.write(f"Run `{manifest.run_id}` created.")

            path = store.save_input(upload.name, upload.getvalue())
            document = parse_document(path, display_name=upload.name)
            st.write(
                f"Read {len(document.units)} sections from **{upload.name}**."
            )

            st.write("Extracting questions…")
            questionnaire = build_questionnaire(
                document,
                provider,
                path=path,
                model=settings.stage0_model,
                max_output_tokens=settings.max_output_tokens,
                max_chunk_tokens=settings.max_chunk_tokens,
                force_llm=force_llm,
            )

            store.save_questionnaire(questionnaire)
            manifest.rfp_name = questionnaire.rfp_name
            store.save_manifest(manifest)

            st.session_state.questionnaire = questionnaire
            st.session_state.questions_confirmed = False

            how = (
                "heuristics — no API call needed"
                if questionnaire.parsed_by == "heuristic"
                else "an LLM call"
            )
            status.update(
                label=f"Found {len(questionnaire.questions)} questions using {how}",
                state="complete",
            )
        except ParseError as exc:
            status.update(label="Could not read the file", state="error")
            st.error(f"**{exc.filename}** — {exc.reason}")
        except RfpEvalError as exc:
            status.update(label="Parsing failed", state="error")
            st.error(str(exc))


def _review_questions(settings: Settings) -> None:
    questionnaire: Questionnaire = st.session_state.questionnaire

    st.subheader("Review the questions")
    st.caption(
        "Edit anything that came out wrong before running the vendors — these ids and "
        "categories drive every later step. Add or delete rows as needed."
    )

    name = st.text_input("RFP name (used on the report title page)", questionnaire.rfp_name)

    frame = pd.DataFrame(
        [
            {
                "id": q.id,
                "category": q.category,
                "text": q.text,
                "required": q.required,
            }
            for q in questionnaire.questions
        ]
    )

    edited = st.data_editor(
        frame,
        num_rows="dynamic",
        use_container_width=True,
        height=380,
        column_config={
            "id": st.column_config.TextColumn("ID", width="small", required=True),
            "category": st.column_config.TextColumn("Category", width="medium"),
            "text": st.column_config.TextColumn("Question", width="large"),
            "required": st.column_config.CheckboxColumn("Required", width="small"),
        },
        key="question_editor",
    )

    if st.button("Confirm questions", type="primary"):
        updated = _questionnaire_from_frame(edited, name, questionnaire)
        if updated is None:
            return

        store = _store(settings)
        if store is not None:
            store.save_questionnaire(updated)
        st.session_state.questionnaire = updated
        st.session_state.questions_confirmed = True
        st.success(
            f"{len(updated.questions)} questions confirmed across "
            f"{len(updated.categories)} criteria."
        )


def _questionnaire_from_frame(frame, name: str, original: Questionnaire) -> Questionnaire | None:
    questions: list[Question] = []
    seen: set[str] = set()

    for _, row in frame.iterrows():
        text = str(row.get("text") or "").strip()
        if not text:
            continue  # a blank row from the editor is not a question

        qid = str(row.get("id") or "").strip() or f"Q{len(questions) + 1}"
        if qid in seen:
            st.error(
                f"Duplicate question id `{qid}`. Ids must be unique — every vendor "
                "answer is keyed on them."
            )
            return None
        seen.add(qid)

        questions.append(
            Question(
                id=qid,
                category=str(row.get("category") or "General").strip() or "General",
                text=text,
                required=bool(row.get("required", True)),
            )
        )

    if not questions:
        st.error("At least one question is needed before vendors can be evaluated.")
        return None

    return Questionnaire(
        rfp_name=name.strip() or original.rfp_name,
        questions=questions,
        source_file=original.source_file,
        parsed_by=original.parsed_by,
    )


# ---------------------------------------------------------------------------
# Step 2 -- vendors
# ---------------------------------------------------------------------------


def _step_vendors(settings: Settings) -> None:
    st.header("2 · Vendor responses")

    if not st.session_state.questions_confirmed:
        st.info("Confirm the questionnaire above first.")
        return

    st.caption(
        "One file per vendor. Each is read by its own LLM call, so a slow or "
        "unreadable response never blocks the others."
    )

    uploads = st.file_uploader(
        "Vendor responses (PDF or Excel)",
        type=SUPPORTED,
        accept_multiple_files=True,
        key="vendor_uploads",
    )

    if not uploads:
        return

    st.subheader(f"{len(uploads)} vendor{'s' if len(uploads) != 1 else ''} to evaluate")
    st.caption("Names appear in the report — adjust any the filename got wrong.")

    names: list[str] = []
    for index, upload in enumerate(uploads):
        col1, col2 = st.columns([3, 2])
        default = st.session_state.vendor_names.get(upload.name, Path(upload.name).stem)
        with col1:
            name = st.text_input(
                f"Vendor name for `{upload.name}`",
                value=default,
                key=f"vendor_name_{index}",
                label_visibility="collapsed",
            )
        with col2:
            st.caption(f"📄 {upload.name} · {upload.size / 1024:.0f} KB")
        st.session_state.vendor_names[upload.name] = name
        names.append(name.strip() or Path(upload.name).stem)

    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        st.error(
            f"Vendor names must be unique. Duplicated: {', '.join(sorted(duplicates))}"
        )
        return

    col1, col2 = st.columns([1, 3])
    with col1:
        run = st.button("Run evaluation", type="primary", use_container_width=True)
    with col2:
        reuse = st.checkbox(
            "Reuse saved results where available",
            value=True,
            help="Skips vendors already evaluated in this run — the point of saving "
            "each vendor's output as it is produced.",
        )

    if run:
        _run(settings, uploads, names, resume=reuse)


def _run(settings: Settings, uploads, names: list[str], *, resume: bool) -> None:
    store = _store(settings)
    if store is None:
        st.error("No active run. Parse a questionnaire first.")
        return

    try:
        provider = get_provider(settings)
    except ConfigError as exc:
        st.error(str(exc))
        return

    # Persist the uploads so a resumed run can re-read them without re-uploading.
    vendors: list[VendorInput] = []
    records: list[VendorRecord] = []
    # Slugs are the on-disk identity of a vendor, so they must be unique even
    # when two distinct names reduce to the same string.
    for upload, name, slug in zip(uploads, names, unique_slugs(names)):
        path = store.save_input(upload.name, upload.getvalue())
        vendors.append(VendorInput(name=name, slug=slug, path=path))
        records.append(VendorRecord(slug=slug, name=name, filename=upload.name))

    manifest = store.load_manifest()
    manifest.vendors = records
    manifest.settings_snapshot = settings.snapshot()
    store.save_manifest(manifest)

    with st.status("Running the evaluation…", expanded=True) as status:
        progress = st.progress(0.0)
        log = st.container()

        def on_progress(event: ProgressEvent) -> None:
            _render_event(event, log)
            if event.stage == "stage1" and event.total:
                # Stage 1 is most of the wall clock; leave headroom for stage 2.
                progress.progress(min(event.current / event.total * 0.8, 0.8))
            elif event.stage == "stage2":
                progress.progress(0.9)
            elif event.stage in ("report", "done"):
                progress.progress(1.0)

        result = run_pipeline(
            questionnaire=st.session_state.questionnaire,
            vendors=vendors,
            settings=settings,
            store=store,
            provider=provider,
            on_progress=on_progress,
            resume=resume,
        )

        if result.ok:
            label = f"Evaluated {len(result.summaries)} vendors"
            if result.failures:
                label += f" · {len(result.failures)} failed"
            status.update(label=label, state="complete")
        else:
            status.update(label="Evaluation did not complete", state="error")

    st.session_state.result = result


def _render_event(event: ProgressEvent, container) -> None:
    with container:
        if event.level == "error":
            st.error(event.message)
        elif event.level == "warning":
            st.warning(event.message)
        else:
            st.write(event.message)


# ---------------------------------------------------------------------------
# Step 3 -- results
# ---------------------------------------------------------------------------


def _step_results(settings: Settings) -> None:
    store = _store(settings)
    if store is None:
        return

    manifest = store.load_manifest()
    if manifest is None or not manifest.vendors:
        return

    st.header("3 · Results")
    _vendor_status_table(manifest)

    synthesis = store.load_synthesis()
    report = store.report_path

    if report.exists():
        st.download_button(
            "⬇ Download the PDF report",
            data=report.read_bytes(),
            file_name=f"{(manifest.rfp_name or 'rfp').replace(' ', '-').lower()}-evaluation.pdf",
            mime="application/pdf",
            type="primary",
        )

    col1, col2 = st.columns(2)
    with col1:
        if store.load_all_summaries() and st.button("Retry synthesis and report"):
            _retry_stage2(settings, store)
    with col2:
        failed = [v for v in manifest.vendors if v.status == VendorStatus.FAILED]
        if failed:
            st.caption(
                f"{len(failed)} vendor(s) failed. Fix the file and re-run — completed "
                "vendors are reused, so only the failures cost another call."
            )

    if synthesis is not None:
        _preview(synthesis)

    st.caption(f"All artifacts for this run: `{store.root}`")


def _vendor_status_table(manifest) -> None:
    icons = {
        VendorStatus.COMPLETE: "✅ complete",
        VendorStatus.FAILED: "❌ failed",
        VendorStatus.RUNNING: "⏳ running",
        VendorStatus.PENDING: "· pending",
    }
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Vendor": v.name,
                    "File": v.filename,
                    "Status": icons.get(v.status, str(v.status)),
                    "Parts": v.chunk_count or "—",
                    "Error": v.error or "",
                }
                for v in manifest.vendors
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )


def _retry_stage2(settings: Settings, store: RunStore) -> None:
    questionnaire = st.session_state.questionnaire or store.load_questionnaire()
    if questionnaire is None:
        st.error("The questionnaire for this run could not be loaded.")
        return

    with st.status("Re-running synthesis…", expanded=True) as status:
        log = st.container()
        result = run_stage2_only(
            questionnaire=questionnaire,
            settings=settings,
            store=store,
            on_progress=lambda e: _render_event(e, log),
        )
        status.update(
            label="Synthesis complete" if result.ok else "Synthesis failed",
            state="complete" if result.ok else "error",
        )

    if result.ok:
        st.rerun()


def _preview(synthesis) -> None:
    with st.expander("Executive summary", expanded=True):
        st.write(synthesis.executive_summary)

    with st.expander("Recommendation"):
        rec = synthesis.recommendation
        st.subheader(rec.vendor_name)
        st.write(rec.rationale)
        if rec.runner_up:
            st.caption(f"Runner-up: {rec.runner_up}")
        if rec.risks:
            st.markdown("**Residual risks**")
            for risk in rec.risks:
                st.markdown(f"- {risk}")
        if rec.conditions:
            st.markdown("**Clarify before committing**")
            for condition in rec.conditions:
                st.markdown(f"- {condition}")

    if synthesis.comparison:
        with st.expander("Comparison table"):
            vendors = [c.vendor_name for c in synthesis.comparison[0].cells]
            rows = []
            for row in synthesis.comparison:
                by_vendor = {c.vendor_name: c for c in row.cells}
                record = {"Criterion": row.criterion}
                for vendor in vendors:
                    cell = by_vendor.get(vendor)
                    record[vendor] = (
                        f"{cell.rating} — {cell.assessment}" if cell else "—"
                    )
                rows.append(record)
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    for vendor in synthesis.per_vendor:
        with st.expander(f"{vendor.vendor_name} — pros and cons"):
            col1, col2 = st.columns(2)
            with col1:
                st.markdown("**Strengths**")
                for pro in vendor.pros:
                    st.markdown(f"- {pro}")
            with col2:
                st.markdown("**Weaknesses**")
                for con in vendor.cons:
                    st.markdown(f"- {con}")
            if vendor.red_flags:
                st.markdown("**🚩 Red flags**")
                for flag in vendor.red_flags:
                    st.markdown(f"- {flag}")


# ---------------------------------------------------------------------------


def main() -> None:
    _init_state()
    base = _base_settings()
    settings = _sidebar(base)

    st.title("RFP Vendor Evaluation")
    st.caption(
        "Upload the RFP questionnaire and each vendor's response. Every vendor is "
        "summarized by its own LLM call, then a single synthesis call compares them "
        "and produces a downloadable PDF report."
    )

    if st.session_state.run_id:
        st.caption(f"Active run: `{st.session_state.run_id}`")

    _step_questionnaire(settings)
    st.divider()
    _step_vendors(settings)
    st.divider()
    _step_results(settings)


if __name__ == "__main__":
    main()
