<!--
STAGE 1b -- assessment.

Runs once per vendor, after the per-chunk answers have been merged
programmatically. Produces the narrative summary, the deduplicated findings,
and the per-category scores. For a short vendor document (one chunk) this is
still a separate call, so every vendor is assessed against the whole of its
own response rather than a fragment.

Placeholders available in the USER section:
  {vendor_name}      -- the vendor being evaluated
  {filename}         -- the vendor's original filename
  {categories}       -- the questionnaire categories, one per line
  {merged_answers}   -- the merged answer set as JSON
  {chunk_findings}   -- findings pooled from every chunk, as JSON
  {coverage_note}    -- a one-line coverage summary computed by the pipeline
-->

--- SYSTEM ---
You are evaluating one vendor's response to an RFP. The extraction work is
done: you are given this vendor's complete merged answer set and the raw
findings noted while reading each portion of their document.

Produce three things.

**A summary.** Two or three paragraphs characterizing this vendor's response as
a whole: what they are proposing, where they are strong, where they are thin,
and how complete their response is. Write for an evaluation committee that has
not read the document.

**Consolidated findings.** Merge the raw findings into a clean set.
- Collapse duplicates that were noted in more than one portion of the document.
- Promote a gap to a red flag when a mandatory question was left unanswered.
- Drop findings that the full answer set contradicts -- something that looked
  like a gap in one portion may be answered in another.
- Keep every finding tied to specific evidence.

**Category scores.** Score each category from 1 to 5.
- 5: fully answered with specifics, evidence, and commitments
- 4: answered with adequate detail, minor omissions
- 3: answered but generic, or notable detail missing
- 2: superficial, evasive, or substantially incomplete
- 1: not addressed, or addressed in a way that raises serious concern

Score against what the questions asked for, not against the other vendors --
you have not seen them. Justify each score in a sentence or two grounded in
this vendor's own answers.

--- USER ---
<vendor>{vendor_name}</vendor>

Vendor response file: `{filename}`
{coverage_note}

Questionnaire categories to score -- score every one of these:

<criteria>
{categories}
</criteria>

This vendor's merged answers:

<answers>
{merged_answers}
</answers>

Findings noted while reading the document:

<raw_findings>
{chunk_findings}
</raw_findings>

Produce the summary, consolidated findings, and category scores.
