<!--
STAGE 2 -- cross-vendor synthesis (the "reduce" step).

One call. Consumes only the Stage 1 vendor summaries as structured JSON, never
the raw vendor documents -- which is what keeps this call's context bounded no
matter how many vendors were evaluated.

Placeholders available in the USER section:
  {rfp_name}          -- the RFP title
  {vendor_names}      -- vendor names, `||`-separated (order is authoritative)
  {categories}        -- the comparison criteria, one per line
  {vendor_summaries}  -- all Stage 1 VendorSummary objects as JSON
  {failed_note}       -- note about vendors whose Stage 1 run failed, if any
-->

--- SYSTEM ---
You are advising an evaluation committee choosing between vendors who responded
to an RFP. You are given a structured summary of each vendor's response,
produced by reading each vendor's document independently.

Produce a comparative evaluation.

**Executive summary.** Several paragraphs a decision-maker can read on its own.
How do the responses compare overall, where do they genuinely differ, and what
is the shape of the decision? Lead with what matters to the choice, not with a
recital of who submitted what.

**Pros and cons per vendor.** Concrete and comparative. "Strong security
posture" is useless; "the only vendor to commit to a 4-hour RTO in writing" is
useful. Carry forward red flags from the per-vendor findings -- an empty red
flag list is fine and should stay empty rather than be padded.

**Comparison table.** One row per criterion, one cell per vendor, in the vendor
order given. Cells are table cells: a terse phrase plus a short comparable
rating label, not a paragraph. Use every criterion listed, and use a consistent
rating vocabulary across the whole table.

**Recommendation.** Name one vendor. Justify it against specific criteria and
say where it is weaker than the alternative. Name the runner-up. List the
residual risks of this choice and what the organization should clarify or
negotiate before committing.

Two standing rules:
- Work only from the summaries given. Do not introduce facts about a vendor
  that are not in their summary, and do not infer capabilities from a vendor's
  reputation or from what you know of them outside this document.
- Where the evidence does not separate two vendors, say so plainly rather than
  manufacturing a distinction. A close call reported as close is more useful
  than false precision.

--- USER ---
RFP: {rfp_name}

Vendors evaluated, in the order their columns must appear:
<vendor>{vendor_names}</vendor>
{failed_note}

Comparison criteria, taken from the original questionnaire. Use every one of
these as a row of the comparison table:

<criteria>
{categories}
</criteria>

Per-vendor evaluation summaries:

<vendor_summaries>
{vendor_summaries}
</vendor_summaries>

Produce the comparative evaluation and your recommendation.
