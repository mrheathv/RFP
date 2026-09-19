# RFP Vendor Evaluation

An internal Streamlit tool for evaluating vendor responses to an RFP. Upload the
questionnaire you sent out plus each vendor's response, and it produces a
downloadable PDF report comparing them, with a recommendation.

It handles any number of vendors and documents of any length, because the work
is split into two stages rather than crammed into one prompt.

---

## How it works

```
Upload RFP ──► Stage 0 ──► canonical questions ──► [you review and edit them]
                                  │
                                  ▼
Upload vendors ──► parse ──► chunk ──► Stage 1, once per vendor, independently
                                         │   long document ⇒ N extract calls + 1 assess call
                                         ▼
                                  VendorSummary JSON ──[saved to disk immediately]
                                         │
                                         ▼
                             Stage 2 ── one call, summaries only
                                         │
                                         ▼
                             Synthesis JSON ──► PDF report
```

**Stage 1 (the map)** reads one vendor's document per call and returns structured
JSON: an answer for every question, plus strengths, weaknesses, gaps and red
flags. Each vendor is independent, so one vendor's failure never touches the
others.

**Stage 2 (the reduce)** takes only those JSON summaries — never the raw
documents — and produces the executive summary, per-vendor pros and cons, the
comparison table, and the recommendation. Because it consumes summaries rather
than source files, its context stays bounded no matter how many vendors you
evaluate or how long their responses are.

Two properties worth knowing:

- **Nothing is truncated.** A vendor response too long for one call is split on
  document boundaries (pages, spreadsheet rows) and every piece is sent. A long
  document costs more calls, not less content.
- **Nothing expensive is lost.** Every per-vendor result is written to disk the
  moment it exists. If synthesis fails, "Retry synthesis" costs one call, not
  one per vendor.

---

## Setup

Requires Python 3.10+.

```bash
git clone <this repo>
cd rfp

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env               # then add your API key
```

Run it:

```bash
streamlit run app.py
```

It opens at <http://localhost:8501>.

### Try it without an API key

```bash
LLM_PROVIDER=fake streamlit run app.py
```

The `fake` provider returns schema-valid placeholder data, so you can walk the
entire flow — upload, edit questions, run several vendors, download the PDF —
before spending anything. It is also what the test suite runs against.

---

## Using it

1. **Upload the RFP questionnaire** (PDF or `.xlsx`) and click *Parse
   questionnaire*. The tool tries heuristics first — an Excel questionnaire with
   a question column, or a PDF with numbered items — and falls back to an LLM
   call only if those come up short. Heuristics cost nothing.
2. **Review the questions.** They appear in an editable grid. Fix anything that
   came out wrong *before* running vendors: these IDs are what every vendor
   answer is keyed on, and the categories become the comparison criteria in the
   report. Click *Confirm questions*.
3. **Upload vendor responses**, one file per vendor, and check the vendor names
   (defaulted from filenames — they appear in the report).
4. **Run the evaluation.** Progress shows each step: *Summarizing vendor 2 of 4:
   Acme Corp → Synthesizing → Generating PDF*.
5. **Download the PDF.**

A failed vendor does not stop the run. It is named in the UI, in the manifest,
and in the report's *Evaluation notes* section, and the other vendors complete
normally.

### Resuming

The sidebar lists previous runs. Load one and re-run: vendors already completed
are reused rather than re-called. This also covers the case where you fix one
malformed vendor file and re-run — only that vendor costs another call.

---

## Configuration

Everything lives in `.env` (see `.env.example`), and every value can be
overridden from the sidebar for a single run.

| Variable | Default | What it does |
|---|---|---|
| `LLM_PROVIDER` | `anthropic` | `anthropic`, `openai`, or `fake` |
| `ANTHROPIC_API_KEY` | — | Required when provider is `anthropic` |
| `OPENAI_API_KEY` | — | Required when provider is `openai` |
| `STAGE0_MODEL` | `claude-opus-5` | Questionnaire normalization |
| `STAGE1_MODEL` | `claude-opus-5` | Per-vendor reading — the bulk-token stage |
| `STAGE2_MODEL` | `claude-opus-5` | Synthesis |
| `OPENAI_STAGE{0,1,2}_MODEL` | `gpt-4o` | Used when provider is `openai` |
| `ANTHROPIC_EFFORT` | `high` | `low` … `max` |
| `MAX_CHUNK_TOKENS` | `60000` | Vendor-document tokens per Stage 1 call |
| `MAX_OUTPUT_TOKENS` | `32000` | Generation ceiling per call |
| `STAGE2_INCLUDE_VERBATIM` | `false` | Send verbatim quotes to Stage 2 |
| `INCLUDE_APPENDIX` | `true` | Append raw extracted answers to the PDF |
| `DATA_DIR` | `data` | Where run artifacts are written |

### Switching LLM provider

Set `LLM_PROVIDER` and the matching key:

```bash
# Anthropic
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
STAGE1_MODEL=claude-opus-5

# OpenAI
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
OPENAI_STAGE1_MODEL=gpt-4o
```

Or switch in the sidebar without restarting. Both providers are driven through
the same `LLMProvider` interface (`rfp_eval/llm/base.py`) and both receive the
same strict JSON Schema, so Stage 2 gets identical structured input either way —
which is what makes A/B comparison meaningful.

To add a third provider, implement `complete_json` and `count_tokens`, then
register it in `rfp_eval/llm/__init__.py`. Nothing else changes.

### Controlling cost

Stage 1 dominates: it reads whole vendor documents, and input tokens are most of
the bill. Two levers, in order:

1. **Per-stage models.** `STAGE1_MODEL=claude-sonnet-5` with
   `STAGE2_MODEL=claude-opus-5` keeps the judgment-heavy synthesis on the
   stronger model while cutting the bulk-token stage substantially.
2. **`ANTHROPIC_EFFORT`.** `medium` is often enough for extraction.

Prompt caching is on for Stage 1: the system prompt and question list are
byte-identical for every vendor in a run, so after the first vendor that prefix
is served from cache.

---

## Tuning the prompts

The evaluation criteria are **not** in the code. They live in `prompts/`, as
Markdown you can edit without touching Python:

| File | Role |
|---|---|
| `stage0_questionnaire.md` | How the RFP is turned into canonical questions |
| `stage1_extract.md` | How one chunk of a vendor document is read |
| `stage1_assess.md` | The scoring rubric and what counts as a red flag |
| `stage2_synthesis.md` | How vendors are compared and a recommendation formed |

Each file has a `--- SYSTEM ---` and a `--- USER ---` section — those two markers
are the only structural requirement. An HTML comment at the top lists the
placeholders available in that template. Everything else is prose you can
rewrite.

The 1–5 scoring rubric, the definition of a red flag, and the instruction to
distrust marketing language all live in `stage1_assess.md` and
`stage1_extract.md`. Change them there.

Prompt templates are cached in-process; restart Streamlit after editing one.

---

## Where results are stored

```
data/runs/<run_id>/
  manifest.json          run bookkeeping, vendor statuses, failures
  questionnaire.json     the confirmed canonical questions
  synthesis.json         Stage 2 output
  report.pdf             the generated report
  inputs/                copies of the uploaded files
  vendors/<slug>/
    summary.json         Stage 1 output for this vendor
    error.json           why this vendor failed, if it did
    chunks/000.json      per-chunk extractions (resume granularity)
```

Plain JSON, deliberately: you can read it, diff it, and hand-edit a bad
extraction then hit *Retry synthesis* without touching code.

`data/` is gitignored. Vendor responses are commercially sensitive — keep it
that way.

---

## Supported formats

| | Input | Notes |
|---|---|---|
| ✅ | `.pdf` | Text and tables are both extracted |
| ✅ | `.xlsx`, `.xlsm`, `.xltx` | Every sheet; a row is never split across chunks |
| ❌ | `.xls` | Re-save as `.xlsx` |
| ❌ | `.docx` | Not supported |
| ❌ | Scanned PDFs | No OCR. An image-only PDF is rejected with a clear message rather than silently producing an empty summary. |

---

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The suite runs entirely offline against the `fake` provider — no API key, no
network. It covers parsing and the no-content-loss guarantee in chunking, the
per-vendor failure isolation, resume, schema strictness, the rendered PDF's
contents, and a smoke test of the Streamlit UI itself.

### Layout

```
app.py                   Streamlit UI — the only file that imports Streamlit
prompts/                 editable prompt templates
rfp_eval/
  config.py              settings from .env
  models.py              the Pydantic contract between stages
  schema_utils.py        Pydantic → strict JSON Schema for both providers
  prompts.py             template loading
  storage.py             run directories, save/load, resume
  parsing/               pdf.py, excel.py, chunking.py
  llm/                   base.py (protocol), anthropic_provider, openai_provider, fake
  pipeline/              stage0, stage1, stage2, orchestrator
  report/pdf_report.py   ReportLab renderer
tests/
```

The pipeline never imports Streamlit. Progress is reported through a callback
the caller supplies, so swapping the frontend — or moving to a background job
queue — means replacing `app.py`, not rewriting the core.

---

## Deployment

The pipeline is a slow multi-step sequence: parse, one LLM call per vendor, a
synthesis call, then a PDF render. Streamlit runs as a persistent process, which
is why this is not a serverless app — on Vercel or Cloudflare Workers a run with
several vendors would exceed the request timeout.

### Streamlit Community Cloud

Quickest path. Push the repo, connect it at
[share.streamlit.io](https://share.streamlit.io), set `app.py` as the entrypoint,
and put your keys in **Settings → Secrets**:

```toml
ANTHROPIC_API_KEY = "sk-ant-..."
LLM_PROVIDER = "anthropic"
STAGE1_MODEL = "claude-opus-5"
```

Two caveats worth weighing before you use it for real vendor responses:

- **Storage is ephemeral.** `data/` is wiped when the app sleeps or redeploys,
  so completed runs and their resume state do not survive. Download reports
  promptly. For durable runs, point `DATA_DIR` at a mounted volume — which means
  the container option below.
- **Access control is the viewer allowlist.** Adequate for a small team;
  everything else about the deployment is on Streamlit's infrastructure.

### Container or VM behind your own domain

The right option if vendor responses shouldn't leave your infrastructure.

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV DATA_DIR=/data
VOLUME /data
EXPOSE 8501
HEALTHCHECK CMD curl --fail http://localhost:8501/_stcore/health || exit 1
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
```

```bash
docker build -t rfp-evaluator .
docker run -d -p 8501:8501 \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -v rfp-data:/data \
  --name rfp-evaluator rfp-evaluator
```

No extra system packages are needed — the report renderer is pure Python.

A small VM (2 vCPU, 4 GB) is ample; the process spends nearly all its time
waiting on API calls.

**Put authentication in front of it.** Streamlit has no built-in auth, and this
app holds vendor pricing and commercial terms. Terminate TLS and enforce SSO at
a reverse proxy — nginx with `auth_request`, Caddy with `forward_auth`,
oauth2-proxy, Cloudflare Access, or your existing ingress. Do not expose port
8501 directly.

Also set `--server.maxUploadSize` to something sensible for your documents (the
default is 200 MB) and keep the container's volume on encrypted storage.
