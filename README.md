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

