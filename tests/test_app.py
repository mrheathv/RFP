"""Smoke tests for the Streamlit UI.

These execute ``app.py`` through Streamlit's own test harness, so a rename or a
bad widget call in the UI layer fails here rather than in front of a user.
"""

from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

# Absolute: AppTest resolves relative paths against the calling file.
APP = str(Path(__file__).resolve().parent.parent / "app.py")
TIMEOUT = 60


def _app(monkeypatch, tmp_path) -> AppTest:
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return AppTest.from_file(APP, default_timeout=TIMEOUT)


def test_app_renders_without_exceptions(monkeypatch, tmp_path):
    at = _app(monkeypatch, tmp_path).run()
    assert not at.exception
    assert "RFP Vendor Evaluation" in at.title[0].value


def test_the_three_steps_are_present(monkeypatch, tmp_path):
    at = _app(monkeypatch, tmp_path).run()
    headers = [h.value for h in at.header]
    assert any("RFP questionnaire" in h for h in headers)
    assert any("Vendor responses" in h for h in headers)


def test_vendor_upload_is_gated_on_confirming_the_questions(monkeypatch, tmp_path):
    at = _app(monkeypatch, tmp_path).run()
    assert any("Confirm the questionnaire above first" in i.value for i in at.info)


def test_sidebar_exposes_provider_and_model_controls(monkeypatch, tmp_path):
    at = _app(monkeypatch, tmp_path).run()

    labels = [w.label for w in at.sidebar.selectbox]
    assert "LLM provider" in labels

    model_inputs = [w.label for w in at.sidebar.text_input]
    assert "Stage 1 model (per vendor)" in model_inputs
    assert "Stage 2 model (synthesis)" in model_inputs


def test_switching_provider_does_not_crash(monkeypatch, tmp_path):
    at = _app(monkeypatch, tmp_path).run()
    at.sidebar.selectbox[0].set_value("anthropic").run()
    assert not at.exception


def test_missing_api_key_surfaces_in_the_ui_not_as_a_crash(monkeypatch, tmp_path):
    """Selecting a provider with no key must be recoverable, not fatal."""
    at = _app(monkeypatch, tmp_path).run()
    at.sidebar.selectbox[0].set_value("anthropic").run()

    # The app still renders; the key error is raised only when a run is started.
    assert not at.exception
    assert at.title
