"""Config, prompt templates, and schema strictification."""

from __future__ import annotations

import json

import pytest

from rfp_eval.config import Settings
from rfp_eval.errors import ConfigError
from rfp_eval.llm import get_provider
from rfp_eval.llm.base import parse_json_response
from rfp_eval.errors import SchemaError
from rfp_eval.models import ChunkExtraction, Questionnaire, Synthesis, VendorSummary
from rfp_eval.prompts import PromptError, format_questions, load_prompt
from rfp_eval.schema_utils import strict_schema


# -- config ----------------------------------------------------------------


def test_missing_anthropic_key_is_caught_with_a_useful_message():
    settings = Settings(provider="anthropic", anthropic_api_key="")
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        settings.validate()


def test_missing_openai_key_is_caught():
    settings = Settings(provider="openai", openai_api_key="")
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        settings.validate()


def test_fake_provider_needs_no_key():
    Settings(provider="fake").validate()
    assert get_provider(Settings(provider="fake")).name == "fake"


def test_snapshot_never_contains_api_keys():
    settings = Settings(anthropic_api_key="sk-secret", openai_api_key="sk-also-secret")
    serialized = json.dumps(settings.snapshot())
    assert "secret" not in serialized


def test_overrides_do_not_mutate_the_original():
    base = Settings(stage1_model="claude-opus-5")
    derived = base.with_overrides(stage1_model="claude-sonnet-5", stage2_model=None)

    assert base.stage1_model == "claude-opus-5"
    assert derived.stage1_model == "claude-sonnet-5"
    assert derived.stage2_model == base.stage2_model  # None is ignored


def test_model_for_stage():
    settings = Settings(stage0_model="a", stage1_model="b", stage2_model="c")
    assert settings.model_for("stage0") == "a"
    assert settings.model_for("stage2") == "c"


# -- prompts ---------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["stage0_questionnaire", "stage1_extract", "stage1_assess", "stage2_synthesis"],
)
def test_every_shipped_prompt_loads(name):
    prompt = load_prompt(name)
    assert prompt.system
    assert prompt.user
    # Documentation comments are stripped before the model sees anything.
    assert "<!--" not in prompt.system
    assert "<!--" not in prompt.user


def test_missing_prompt_is_a_clear_error():
    with pytest.raises(PromptError, match="not found"):
        load_prompt("no_such_prompt")


def test_malformed_prompt_is_rejected(tmp_path):
    (tmp_path / "bad.md").write_text("no markers here")
    with pytest.raises(PromptError, match="markers"):
        load_prompt("bad", str(tmp_path))


def test_unsupplied_placeholder_raises_rather_than_leaking_braces(tmp_path):
    (tmp_path / "p.md").write_text("--- SYSTEM ---\nsys\n--- USER ---\n{missing}")
    with pytest.raises(PromptError, match="missing"):
        load_prompt("p", str(tmp_path)).render(other="x")


def test_question_formatting_is_machine_readable(questionnaire):
    rendered = format_questions(questionnaire.questions)
    assert "- id: Q1 | category: Support | required: yes" in rendered
    assert "  text: Describe your approach" in rendered


# -- schema ----------------------------------------------------------------


@pytest.mark.parametrize("model", [Questionnaire, ChunkExtraction, Synthesis, VendorSummary])
def test_schemas_are_strict_mode_ready(model):
    schema = strict_schema(model)
    serialized = json.dumps(schema)

    assert "$ref" not in serialized  # fully inlined
    assert "$defs" not in serialized
    assert '"default"' not in serialized
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


def test_nested_objects_are_also_strict():
    schema = strict_schema(ChunkExtraction)
    answer = schema["properties"]["answers"]["items"]
    assert answer["additionalProperties"] is False
    assert set(answer["required"]) == set(answer["properties"])


def test_excluded_fields_are_dropped_from_required_too():
    schema = strict_schema(Synthesis, exclude={"model", "generated_at"})
    assert "model" not in schema["properties"]
    assert "model" not in schema["required"]


def test_field_descriptions_survive_into_the_schema():
    """Descriptions are the model's instructions -- losing them degrades output."""
    schema = strict_schema(ChunkExtraction)
    status = schema["properties"]["answers"]["items"]["properties"]["status"]
    assert "not_found" in status["description"]


# -- response parsing ------------------------------------------------------


def test_clean_json_parses():
    assert parse_json_response('{"a": 1}', "stage1") == {"a": 1}


def test_json_wrapped_in_prose_is_recovered():
    raw = 'Here you go:\n```json\n{"a": 1}\n```\nHope that helps.'
    assert parse_json_response(raw, "stage1") == {"a": 1}


def test_empty_response_is_an_error():
    with pytest.raises(SchemaError, match="empty"):
        parse_json_response("", "stage1")


def test_non_json_response_is_an_error():
    with pytest.raises(SchemaError, match="did not return JSON"):
        parse_json_response("I cannot help with that.", "stage1")


def test_json_array_is_rejected():
    with pytest.raises(SchemaError, match="expected a JSON object"):
        parse_json_response("[1, 2, 3]", "stage1")
