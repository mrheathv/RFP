"""Full-run behaviour: failure isolation, resume, and the generated PDF."""

from __future__ import annotations

from rfp_eval.errors import ProviderError
from rfp_eval.llm.fake import FakeProvider
from rfp_eval.models import VendorRecord, VendorStatus
from rfp_eval.pipeline.orchestrator import (
    ProgressEvent,
    VendorInput,
    run_pipeline,
    run_stage2_only,
)
from rfp_eval.storage import create_run, slugify
from tests.conftest import make_pdf


def _vendors(tmp_path, names, *, broken: set[str] | None = None):
    broken = broken or set()
    inputs = []
    for name in names:
        slug = slugify(name)
        path = tmp_path / f"{slug}.pdf"
        if name in broken:
            path.write_bytes(b"%PDF-1.4\nnot really a pdf\n")
        else:
            make_pdf(path, pages=2)
        inputs.append(VendorInput(name=name, slug=slug, path=path))
    return inputs


def _prepare(store, vendors):
    manifest = store.load_manifest()
    manifest.vendors = [
        VendorRecord(slug=v.slug, name=v.name, filename=v.path.name) for v in vendors
    ]
    store.save_manifest(manifest)


def test_full_run_produces_a_synthesis_and_a_pdf(tmp_path, questionnaire, settings):
    store, _ = create_run(settings.data_dir)
    vendors = _vendors(tmp_path, ["Acme Corp", "Beta Ltd", "Gamma Inc"])
    _prepare(store, vendors)

    result = run_pipeline(
        questionnaire=questionnaire, vendors=vendors, settings=settings,
        store=store, provider=FakeProvider(),
    )

    assert result.ok
    assert len(result.summaries) == 3
    assert not result.failures
    assert result.report_path.exists()
    assert result.report_path.stat().st_size > 5000

    # Everything is on disk, not just in memory.
    assert store.load_synthesis() is not None
    assert len(store.load_all_summaries()) == 3
    assert store.load_manifest().synthesis_complete


def test_variable_vendor_counts(tmp_path, questionnaire, settings):
    for count in (1, 2, 6):
        store, _ = create_run(settings.data_dir)
        names = [f"Vendor {i}" for i in range(1, count + 1)]
        scratch = tmp_path / f"n{count}"
        scratch.mkdir()
        vendors = _vendors(scratch, names)
        _prepare(store, vendors)

        result = run_pipeline(
            questionnaire=questionnaire, vendors=vendors, settings=settings,
            store=store, provider=FakeProvider(),
        )
        assert result.ok, f"failed at {count} vendors"
        assert len(result.summaries) == count


def test_one_unreadable_file_does_not_sink_the_run(tmp_path, questionnaire, settings):
    store, _ = create_run(settings.data_dir)
    vendors = _vendors(
        tmp_path, ["Acme Corp", "Broken Co", "Gamma Inc"], broken={"Broken Co"}
    )
    _prepare(store, vendors)

    result = run_pipeline(
        questionnaire=questionnaire, vendors=vendors, settings=settings,
        store=store, provider=FakeProvider(),
    )

    assert result.ok
    assert len(result.summaries) == 2
    assert "Broken Co" in result.failures
    assert "broken-co.pdf" in result.failures["Broken Co"]

    manifest = store.load_manifest()
    assert manifest.vendor("broken-co").status == VendorStatus.FAILED
    assert manifest.vendor("acme-corp").status == VendorStatus.COMPLETE
    # The failure is recorded for inspection after the run.
    assert store.load_vendor_error("broken-co") is not None
    # And it reaches the report rather than disappearing.
    assert result.report_path.exists()


def test_the_user_is_told_which_vendor_failed(tmp_path, questionnaire, settings):
    store, _ = create_run(settings.data_dir)
    vendors = _vendors(tmp_path, ["Acme Corp", "Broken Co"], broken={"Broken Co"})
    _prepare(store, vendors)

    events: list[ProgressEvent] = []
    run_pipeline(
        questionnaire=questionnaire, vendors=vendors, settings=settings,
        store=store, provider=FakeProvider(), on_progress=events.append,
    )

    warnings = [e for e in events if e.level == "warning"]
    assert any("Broken Co" in e.message for e in warnings)
    assert any("continuing with the other vendors" in e.message for e in warnings)


def test_every_vendor_failing_stops_before_stage2(tmp_path, questionnaire, settings):
    store, _ = create_run(settings.data_dir)
    vendors = _vendors(tmp_path, ["A Co", "B Co"], broken={"A Co", "B Co"})
    _prepare(store, vendors)

    provider = FakeProvider()
    result = run_pipeline(
        questionnaire=questionnaire, vendors=vendors, settings=settings,
        store=store, provider=provider,
    )

    assert not result.ok
    assert len(result.failures) == 2
    # No synthesis call was attempted with nothing to compare.
    assert not any(c["stage"] == "stage2" for c in provider.calls)


def test_a_stage1_provider_error_is_isolated(tmp_path, questionnaire, settings):
    store, _ = create_run(settings.data_dir)
    vendors = _vendors(tmp_path, ["Acme Corp", "Flaky Co"])
    _prepare(store, vendors)

    class FlakyProvider(FakeProvider):
        # Fails only this vendor's Stage 1 calls. Stage 2 must still run: it
        # names the failed vendor in its prompt, so the check is stage-scoped.
        def complete_json(self, **kwargs):
            if kwargs["stage"].startswith("stage1") and "Flaky Co" in kwargs["user"]:
                raise ProviderError("fake", kwargs["stage"], "upstream exploded")
            return super().complete_json(**kwargs)

    result = run_pipeline(
        questionnaire=questionnaire, vendors=vendors, settings=settings,
        store=store, provider=FlakyProvider(),
    )

    assert result.ok
    assert list(result.failures) == ["Flaky Co"]
    assert "upstream exploded" in result.failures["Flaky Co"]


def test_synthesis_failure_preserves_every_stage1_result(tmp_path, questionnaire, settings):
    store, _ = create_run(settings.data_dir)
    vendors = _vendors(tmp_path, ["Acme Corp", "Beta Ltd"])
    _prepare(store, vendors)

    class Stage2Fails(FakeProvider):
        def complete_json(self, **kwargs):
            if kwargs["stage"] == "stage2":
                raise ProviderError("fake", "stage2", "synthesis exploded")
            return super().complete_json(**kwargs)

    result = run_pipeline(
        questionnaire=questionnaire, vendors=vendors, settings=settings,
        store=store, provider=Stage2Fails(),
    )

    assert not result.ok
    # The expensive half survived.
    assert len(store.load_all_summaries()) == 2

    # Retrying costs one call, not one per vendor.
    retry_provider = FakeProvider()
    retry = run_stage2_only(
        questionnaire=questionnaire, settings=settings, store=store,
        provider=retry_provider,
    )

    assert retry.ok
    assert [c["stage"] for c in retry_provider.calls] == ["stage2"]
    assert retry.report_path.exists()


def test_resume_skips_vendors_already_completed(tmp_path, questionnaire, settings):
    store, _ = create_run(settings.data_dir)
    vendors = _vendors(tmp_path, ["Acme Corp", "Beta Ltd"])
    _prepare(store, vendors)

    run_pipeline(
        questionnaire=questionnaire, vendors=vendors, settings=settings,
        store=store, provider=FakeProvider(),
    )

    second = FakeProvider()
    result = run_pipeline(
        questionnaire=questionnaire, vendors=vendors, settings=settings,
        store=store, provider=second, resume=True,
    )

    assert result.ok
    # Only the synthesis ran again; no vendor was re-read.
    assert [c["stage"] for c in second.calls] == ["stage2"]


def test_stage2_only_reports_when_there_is_nothing_saved(tmp_path, questionnaire, settings):
    store, _ = create_run(settings.data_dir)
    events: list[ProgressEvent] = []

    result = run_stage2_only(
        questionnaire=questionnaire, settings=settings, store=store,
        provider=FakeProvider(), on_progress=events.append,
    )

    assert not result.ok
    assert any(e.level == "error" for e in events)


def test_progress_narrates_the_pipeline(tmp_path, questionnaire, settings):
    store, _ = create_run(settings.data_dir)
    vendors = _vendors(tmp_path, ["Acme Corp", "Beta Ltd"])
    _prepare(store, vendors)

    events: list[ProgressEvent] = []
    run_pipeline(
        questionnaire=questionnaire, vendors=vendors, settings=settings,
        store=store, provider=FakeProvider(), on_progress=events.append,
    )

    stages = [e.stage for e in events]
    assert stages.index("stage1") < stages.index("stage2") < stages.index("report")
    assert stages[-1] == "done"
    assert any("vendor 1 of 2" in e.message for e in events)
    assert any("vendor 2 of 2" in e.message for e in events)


def test_a_long_vendor_document_reports_part_progress(tmp_path, questionnaire, settings):
    store, _ = create_run(settings.data_dir)
    path = make_pdf(tmp_path / "long.pdf", pages=14)
    vendors = [VendorInput(name="Verbose Inc", slug="verbose-inc", path=path)]
    _prepare(store, vendors)

    settings = settings.with_overrides(max_chunk_tokens=1000)
    events: list[ProgressEvent] = []
    result = run_pipeline(
        questionnaire=questionnaire, vendors=vendors, settings=settings,
        store=store, provider=FakeProvider(), on_progress=events.append,
    )

    assert result.ok
    assert result.summaries[0].chunk_count > 1
    assert any("part 1 of" in e.message for e in events)
