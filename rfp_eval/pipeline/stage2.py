"""Stage 2 -- the reduce step.

One call, consuming only Stage 1's structured output. The raw vendor documents
never reach this stage, which is what keeps its context bounded regardless of
vendor count or document length.
"""

from __future__ import annotations

import json

from ..errors import RfpEvalError
from ..llm.base import LLMProvider
from ..models import Questionnaire, Synthesis, VendorSummary
from ..prompts import load_prompt
from ..schema_utils import strict_schema


class NoVendorsError(RfpEvalError):
    """Every vendor failed Stage 1 -- there is nothing to compare."""


def _payload_for(summary: VendorSummary, include_verbatim: bool) -> dict:
    """Trim a summary down to what Stage 2 actually needs.

    Verbatim quotes are the largest token line item and serve traceability, not
    comparison -- the appendix keeps them either way.
    """
    data = summary.model_dump(mode="json")
    data.pop("generated_at", None)
    data.pop("model", None)

    if not include_verbatim:
        for answer in data.get("answers", []):
            answer.pop("verbatim_quote", None)

    answered, total = summary.coverage()
    data["coverage"] = f"{answered}/{total} questions substantively answered"
    return data


def run_stage2(
    *,
    summaries: list[VendorSummary],
    questionnaire: Questionnaire,
    provider: LLMProvider,
    model: str,
    max_output_tokens: int,
    include_verbatim: bool = False,
    failed_vendors: list[str] | None = None,
) -> Synthesis:
    """Compare every successfully evaluated vendor and recommend one."""
    if not summaries:
        raise NoVendorsError(
            "no vendor summaries are available -- every vendor failed Stage 1, "
            "so there is nothing to synthesize."
        )

    failed = failed_vendors or []
    failed_note = ""
    if failed:
        # The model should know the comparison is incomplete, and say so.
        failed_note = (
            "\nNote: these vendors could not be evaluated and must be excluded "
            "from the comparison and the recommendation: "
            + ", ".join(failed)
            + ". Mention in the executive summary that the comparison is "
            "incomplete for this reason."
        )

    prompt = load_prompt("stage2_synthesis")
    system, user = prompt.render(
        rfp_name=questionnaire.rfp_name or "Untitled RFP",
        # `||` separated: the fake provider parses this back, and it is an
        # unambiguous delimiter for vendor names containing commas.
        vendor_names="||".join(s.vendor_name for s in summaries),
        categories="\n".join(f"- {c}" for c in questionnaire.categories),
        vendor_summaries=json.dumps(
            [_payload_for(s, include_verbatim) for s in summaries],
            indent=2,
            ensure_ascii=False,
        ),
        failed_note=failed_note,
    )

    payload = provider.complete_json(
        system=system,
        user=user,
        schema=strict_schema(Synthesis, exclude={"model", "generated_at"}),
        model=model,
        max_tokens=max_output_tokens,
        stage="stage2",
    )

    synthesis = Synthesis.model_validate(payload)
    synthesis.model = model
    return synthesis
