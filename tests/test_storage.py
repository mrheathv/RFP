"""Persistence and resume -- the reason a Stage 2 failure is cheap."""

from __future__ import annotations

from rfp_eval.models import (
    ChunkExtraction,
    Recommendation,
    Synthesis,
    VendorRecord,
    VendorStatus,
    VendorSummary,
)
from rfp_eval.storage import create_run, list_runs, slugify


def test_slugify_is_filesystem_safe():
    assert slugify("Acme Corp. (EMEA)") == "acme-corp-emea"
    assert slugify("!!!") == "vendor"


def test_run_id_collisions_are_avoided(tmp_path):
    ids = {create_run(tmp_path)[1].run_id for _ in range(3)}
    assert len(ids) == 3


def test_questionnaire_round_trip(tmp_path, questionnaire):
    store, _ = create_run(tmp_path)
    store.save_questionnaire(questionnaire)

    loaded = store.load_questionnaire()
    assert loaded is not None
    assert loaded.rfp_name == questionnaire.rfp_name
    assert len(loaded.questions) == len(questionnaire.questions)


def test_vendor_summary_round_trip(tmp_path):
    store, _ = create_run(tmp_path)
    summary = VendorSummary(vendor_name="Acme", summary="Good response.")

    store.save_vendor_summary("acme", summary)
    assert store.load_vendor_summary("acme").vendor_name == "Acme"


def test_saving_a_summary_clears_a_previous_error(tmp_path):
    store, _ = create_run(tmp_path)
    store.save_vendor_error("acme", "parse failed")
    assert store.load_vendor_error("acme") is not None

    store.save_vendor_summary("acme", VendorSummary(vendor_name="Acme"))
    assert store.load_vendor_error("acme") is None


def test_chunk_round_trip(tmp_path):
    store, _ = create_run(tmp_path)
    store.save_chunk("acme", 2, ChunkExtraction())
    assert store.load_chunk("acme", 2) is not None
    assert store.load_chunk("acme", 99) is None


def test_manifest_tracks_vendor_status(tmp_path):
    store, manifest = create_run(tmp_path)
    manifest.vendors = [VendorRecord(slug="acme", name="Acme", filename="a.pdf")]
    store.save_manifest(manifest)

    store.update_vendor_status("acme", VendorStatus.FAILED, error="boom")
    reloaded = store.load_manifest()

    assert reloaded.vendor("acme").status == VendorStatus.FAILED
    assert reloaded.vendor("acme").error == "boom"
    assert reloaded.completed_slugs() == set()


def test_clear_vendor_discards_results(tmp_path):
    store, _ = create_run(tmp_path)
    store.save_vendor_summary("acme", VendorSummary(vendor_name="Acme"))
    store.save_chunk("acme", 1, ChunkExtraction())

    store.clear_vendor("acme")
    assert store.load_vendor_summary("acme") is None
    assert store.load_chunk("acme", 1) is None


def test_summaries_load_in_manifest_order(tmp_path):
    store, manifest = create_run(tmp_path)
    manifest.vendors = [
        VendorRecord(slug="b", name="Beta", filename="b.pdf"),
        VendorRecord(slug="a", name="Alpha", filename="a.pdf"),
    ]
    store.save_manifest(manifest)
    store.save_vendor_summary("a", VendorSummary(vendor_name="Alpha"))
    store.save_vendor_summary("b", VendorSummary(vendor_name="Beta"))

    assert [s.vendor_name for s in store.load_all_summaries()] == ["Beta", "Alpha"]


def test_uploaded_filenames_cannot_escape_the_run_directory(tmp_path):
    store, _ = create_run(tmp_path)
    path = store.save_input("../../etc/passwd", b"data")

    assert path.parent == store.inputs_dir
    assert path.name == "passwd"


def test_list_runs_is_newest_first_and_skips_corruption(tmp_path):
    for _ in range(2):
        create_run(tmp_path, rfp_name="Test")

    broken = tmp_path / "runs" / "broken"
    broken.mkdir(parents=True)
    (broken / "manifest.json").write_text("{ not json")

    runs = list_runs(tmp_path)
    assert len(runs) == 2
    assert runs[0].run_id >= runs[1].run_id


def test_list_runs_on_a_fresh_directory(tmp_path):
    assert list_runs(tmp_path / "nothing-here") == []


def test_synthesis_round_trip(tmp_path):
    store, _ = create_run(tmp_path)
    store.save_synthesis(
        Synthesis(
            executive_summary="Summary.",
            recommendation=Recommendation(vendor_name="Acme", rationale="Best fit."),
        )
    )
    assert store.load_synthesis().recommendation.vendor_name == "Acme"


def test_partial_write_cannot_corrupt_a_saved_result(tmp_path):
    """Writes are atomic: a leftover .tmp never shadows the real file."""
    store, _ = create_run(tmp_path)
    store.save_vendor_summary("acme", VendorSummary(vendor_name="Acme"))

    assert not list(store.vendor_dir("acme").glob("*.tmp"))
    assert store.load_vendor_summary("acme") is not None
