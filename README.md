# LIHTC TX 2026 Extraction Agent (standalone)

Standalone repo to **run and evaluate** an AI agent that extracts structured data from Texas 2026 LIHTC “Full Application” PDFs.

> The repo is now root-runnable: you do **not** need to open any subfolder. Use `setup.sh` and `run_pipeline.py` from the repo root.

## Current Pipeline Status (tested)

Latest validated runs on the 2026 full-application cohort (`/imaged/2026-9-challenges/`, 114 PDFs):

- PDFs processed: **114 / 114**
- Fast extraction runtime (V5.8 path): **~12.7 min**
- Independent-source runtime (isolated copy): **~19.3 min**
- `application_name` / contact fields: **100%**
- `census_tract`: **100%** after recovery
- `poverty_rank`: **~97%** after recovery
- `tiebreaker_*`: **~93%**

These are empirical results from local runs in this workspace.

## Discovery: OpenClaw or not?

Default discovery is **not OpenClaw browser automation**.

By default the pipeline uses local Python code in `lihtc_tx_2026_agent/discover.py`:
- starts from TDHCA seed pages,
- crawls links,
- collects PDF URLs,
- filters by year/pattern and source folder (default `2026-9-challenges`).

OpenClaw-based orchestration is optional and documented later in this README (`openclaw_orchestrator`).

## Quickstart

1) One-command setup:

```bash
bash setup.sh
source .venv/bin/activate
```

2) Configure model access

This agent supports:
- **Direct OpenAI** via `OPENAI_API_KEY`
- **Any OpenAI-compatible gateway** via `OPENAI_BASE_URL` (optional)

Direct OpenAI:

```bash
export OPENAI_API_KEY="..."
```

Optional gateway:

```bash
export OPENAI_BASE_URL="http://localhost:11435"   # or your gateway base URL
```

3) Run the full pipeline (discover → download → extract):

```bash
python run_pipeline.py
```

(You can still run module commands directly; `run_pipeline.py` is just a top-level convenience wrapper.)

### Variants (multiple strategies)

List implemented strategies:

```bash
python3 -m lihtc_tx_2026_agent.run --list-strategies --out-dir ./out --pdf-dir ./fixtures/pdfs
```

Run a specific strategy:

```bash
python3 -m lihtc_tx_2026_agent.run \
  --strategy llm_single_pass \
  --pdf-dir "/path/to/tx_2026_full_app_pdfs" \
  --out-dir "./out"
```

## Website mode (discover + download)

If you want the agent to **find and download PDFs from the website** automatically:

```bash
python3 -m lihtc_tx_2026_agent.run \
  --out-dir "./out" \
  --download-dir "./downloads" \
  --seed-url "https://www.tdhca.texas.gov/competitive-9-housing-tax-credits" \
  --seed-url "https://www.tdhca.texas.gov/apply-funds"
```

To try to find **all possible PDFs** reachable from the seed pages (less selective), add:

```bash
python3 -m lihtc_tx_2026_agent.run \
  --out-dir "./out" \
  --download-dir "./downloads" \
  --seed-url "https://www.tdhca.texas.gov/competitive-9-housing-tax-credits" \
  --discover-all-hosts \
  --crawl-max-pages 200
```

To keep discovery focused, use regex filters:

```bash
python3 -m lihtc_tx_2026_agent.run \
  --out-dir "./out" \
  --download-dir "./downloads" \
  --seed-url "https://www.tdhca.texas.gov/competitive-9-housing-tax-credits" \
  --include-pdf-regex "2026" \
  --exclude-pdf-regex "Appraisals"
```

## Agentic run (discover → download → classify → extract)

To have the system identify *which PDFs are actually 2026 full applications* before extracting:

```bash
python3 -m lihtc_tx_2026_agent.ops.agentic_run \
  --out-dir ./out_agentic \
  --download-dir ./downloads_agentic
```

With `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` set, each run writes one `audit_log` row (see Supabase section below). Use `--no-supabase-log` to skip, or `--require-supabase-log` to fail the run if logging cannot occur.

Outputs:
- `out/applications.csv` (Excel-ready)
- `out/applications.jsonl` (rich evidence per field)
- `out/review_queue.csv`
- `out/run_summary.json`

### Multi-run website sweep (TDHCA **harvest cycles**, not eval “runs”)

This driver repeats **harvest cycles**: discover → download (new files only, merged manifest) → classify (cached per file) → extract **only not-yet-extracted** full 2026 applications, then merges everything under `aggregate/`.

**Terminology:** `--harvest-cycles` counts **website resume passes**. For **agent improvement iterations** on fixtures (repeat eval + optional OpenClaw coaching), use `python3 -m lihtc_tx_2026_agent.ops.progression_run --runs …` instead.

```bash
python3 -m lihtc_tx_2026_agent.ops.progression_agentic \
  --harvest-cycles 20 \
  --out-root ~/Desktop/parcell/agent \
  --include-pdf-regex "2026" \
  --exclude-pdf-regex "Appraisals"
```

**Defaults (no download flags):** **50** new PDFs per harvest cycle (not 10), **adaptive download sizing on** (raises the cap when a cycle finds no `full_application` PDFs, up to **150** by default), then resets after successful extractions. Override with `--max-new-downloads-per-cycle`, `--adaptive-download-max`, or **`--no-adaptive-downloads`** for a fixed cap.

`--runs` and `--max-new-downloads-per-run` still work but print a **deprecation warning** (flushed to stderr); prefer `--harvest-cycles` / `--max-new-downloads-per-cycle`. Use **`--dry-config`** to print resolved counts and exit without crawling.

State lives under `--out-root` (`download_manifest.json`, `classification_cache.json`, `extracted_hashes.json`, `discover_state.json`, optional `adaptive_download_state.json`). Per-cycle JSON summaries use `harvest_cycle` / `of_harvest_cycles` (and keep `run` / `of_runs` as mirrors for older tooling).

### OpenClaw orchestrator (planner per iteration)

Same `--out-root` workspace as above, but **each iteration** sends a **monitor** payload (last cycle metrics, cumulative extract count, last plan) to OpenClaw and expects JSON with `crawl_max_pages`, `max_new_downloads`, `include_pdf_regex`, `exclude_pdf_regex`, optional `seed_urls` / `replace_seeds`, and `rationale`. Python clamps caps, runs one discover → download → classify → extract cycle, and writes `openclaw_orchestrator/run_KKK/` plus `openclaw_orchestrator_state.json`.

Requires the `openclaw` CLI in `PATH`. Agent name: `--openclaw-agent` or env `LIHTC_OPENCLAW_ORCHESTRATOR_AGENT` (fallback: `LIHTC_OPENCLAW_COACHING_AGENT`, then `default`). Use **`--fallback-heuristic`** to exercise the loop without OpenClaw (local adaptive-style plans only).

```bash
python3 -m lihtc_tx_2026_agent.ops.openclaw_orchestrator \
  --iterations 12 \
  --out-root ~/Desktop/parcell/agent \
  --default-max-new-downloads 15 \
  --cap-max-new-downloads 80
```

## Evaluation loop (fixtures)

Place a small set of **local-only** PDFs under `fixtures/pdfs/` and corresponding labels under `fixtures/labels/`.
Then run:

```bash
python3 -m lihtc_tx_2026_agent.eval --pdf-dir fixtures/pdfs --labels-dir fixtures/labels --out-dir ./out_eval \
  --strategy llm_single_pass
```

This produces a per-field accuracy report and a `diffs.jsonl` file for fast iteration.

## Self-run improvement loop (eval → log → enqueue)

Run evaluation, write an `improvements.json` summary, log to Supabase when credentials are set, and optionally enqueue a “fix the top issues” task:

```bash
python3 -m lihtc_tx_2026_agent.ops.improve_loop \
  --pdf-dir fixtures/pdfs \
  --labels-dir fixtures/labels \
  --out-dir ./out_eval \
  --model gpt-4o-mini \
  --strategy llm_single_pass \
  --strategy llm_two_pass \
  --strategy regex_then_llm \
  --strategy focused_pages_llm \
  --strategy self_consistency_vote \
  --enqueue-fix-task
```

## Supabase project log (continuous improvement)

Extraction, eval, benchmark, agentic, and improve-loop commands **post to `audit_log` automatically** when these env vars are set:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_KEY`

Each row is scoped by the CLI `--project-id` (default `lihtc-tx-2026`). The JSON `payload` includes **`run_id`**, **`pipeline`**, **`project_context`**, and **`audit_agent_version`** so you can correlate runs and build dashboards.

- **Opt out:** `--no-supabase-log` or `LIHTC_SKIP_SUPABASE_LOG=1`
- **Strict CI (must log):** `--require-supabase-log`

`improve_loop` passes `--no-supabase-log` to the inner `eval` subprocess so you get **one** combined `lihtc_eval_run` row from the parent (not duplicate `lihtc_eval` rows).

## Dashboard

Open `dashboard/index.html` in a browser and load an eval `report.json` file to view accuracy by strategy.

