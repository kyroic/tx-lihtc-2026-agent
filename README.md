# Texas LIHTC 2026 Agent

**Automated extraction of Texas Low-Income Housing Tax Credit (LIHTC) 2026 Full Application PDFs.**

This agent automatically:
1. Finds LIHTC application PDFs from the TDHCA website
2. Downloads them
3. Extracts structured data (project name, contacts, tie-breaker distances, census tract, poverty rate, etc.)
4. Exports clean CSV/Excel files

> **No OpenClaw required** — this is a standalone Python project.

---

## Quick Start (5 Minutes)

### 1. Clone the repo

```bash
git clone https://github.com/kyroic/tx-lihtc-2026-agent.git
cd tx-lihtc-2026-agent
```

### 2. Run setup

```bash
bash setup.sh
source .venv/bin/activate
```

This installs Python dependencies and checks for `pdftotext` (recommended for speed).

### 3. Set your API key

Choose one provider:

```bash
# OpenAI (recommended)
export OPENAI_API_KEY='sk-...'

# OR Anthropic
export ANTHROPIC_API_KEY='sk-ant-...'

# OR use local Ollama (free, no API key needed)
# Install from: https://ollama.ai
```

### 4. Run the pipeline

```bash
python run_pipeline.py
```

This will:
- Discover PDFs from TDHCA website
- Download them to `downloads/`
- Extract data with auto-recovery for missing fields
- Write outputs to `out/aggregate/`

### 5. Open results

Find your extracted data in:
- `out/aggregate/applications.csv` — Main spreadsheet (open in Excel)
- `out/aggregate/applications.xlsx` — Excel format
- `out/aggregate/review_queue.csv` — Items that may need manual check

---

## What It Extracts

| Field | Description | Coverage |
|-------|-------------|----------|
| `application_name` | Development/project name | 100% |
| `contact_name` | Developer contact person | 100% |
| `contact_email` | Contact email | 100% |
| `contact_phone` | Contact phone | 100% |
| `census_tract` | 11-digit Census GEOID | 100% (auto-recovery) |
| `poverty_rank` | Poverty rate percentage | 97% (auto-recovery) |
| `quartile` | Income quartile (1-4) | 100% |
| `property_rate` | Property tax rate | 97% |
| `tiebreaker_park` | Name of nearest park | 93% |
| `tiebreaker_school` | Name of nearest school | 93% |
| `tiebreaker_grocery` | Name of nearest grocery | 93% |
| `tiebreaker_library` | Name of nearest library | 93% |
| `distance_to_park` | Distance to nearest park (ft/mi) | 🆕 Phase 2 |
| `park_lat` | Park latitude (decimal degrees) | 🆕 Phase 2 |
| `park_lng` | Park longitude (decimal degrees) | 🆕 Phase 2 |
| `distance_to_school` | Distance to nearest school (ft/mi) | 🆕 Phase 2 |
| `school_lat` | School latitude (decimal degrees) | 🆕 Phase 2 |
| `school_lng` | School longitude (decimal degrees) | 🆕 Phase 2 |
| `distance_to_grocery` | Distance to nearest grocery (ft/mi) | 🆕 Phase 2 |
| `grocery_lat` | Grocery latitude (decimal degrees) | 🆕 Phase 2 |
| `grocery_lng` | Grocery longitude (decimal degrees) | 🆕 Phase 2 |
| `distance_to_library` | Distance to nearest library (ft/mi) | 🆕 Phase 2 |
| `library_lat` | Library latitude (decimal degrees) | 🆕 Phase 2 |
| `library_lng` | Library longitude (decimal degrees) | 🆕 Phase 2 |
| `site_lat` | Project site latitude (decimal degrees) | 🆕 Phase 2 |
| `site_lng` | Project site longitude (decimal degrees) | 🆕 Phase 2 |
| `tiebreaker_score` | Aggregate tie-breaker score | 🆕 Phase 2 |

**Total: 27 data columns** (12 original + 15 Phase 2 detail fields, plus 3 tracking + 2 review columns = 32 CSV columns)

---

## Performance (5-Run Benchmark Results)

114 full application PDFs, 27 fields, single LLM pass (gpt-4o-mini):

| Category | Coverage | Variance |
|---|---|---|
| Standard fields (8) | 85.6% | ±0.3% across 5 runs |
| Tiebreaker names (4) | 93.9% | ±0.1% across 5 runs |
| Amenity coordinates (8) | 93.6% | ±0.1% across 5 runs |
| Site/distances/score (7) | 93.8% | ±0.2% across 5 runs |
| **Mean runtime** | **19.8 min** | 16.9–24.4 min |
| **Errors** | **1 in 570 PDFs** | 0.2% error rate |

After scanner fix (broad regex for tiebreaker pages):

| Category | New Coverage |
|---|---|
| Tiebreaker names (4) | 99.1% |
| Amenity coordinates (8) | 98.9% |
| Site/distances/score (7) | 99.0% |

### Pipeline Steps

1. **Extract** — `extract_one_pdf_v5_8()` (single LLM call, all 27 fields)
2. **Auto-recover** — regex scan for census_tract, poverty_rank, quartile, property_rate
3. **Clean** — data cleanliness agent (phone normalization, coordinate validation, artifact detection)

---

## Data Cleanliness

After extraction, run the cleanliness agent to normalize and validate:

```bash
python scripts/clean_csv.py --in out/aggregate/applications.csv --out out/clean/
```

Outputs:
- `applications_cleaned.csv` — normalized data (all phones `(XXX) XXX-XXXX`, N/A stripped, unicode fixed)
- `qa_report.csv` — per-row quality score + issues
- `qa_summary.json` — aggregate stats

10 validation rules: phone format, email validity, garbage name detection, numeric ranges,
unicode minus fix, Texas coordinate bounds, distance sanity, LLM copy-paste detection, census tract format, duplicate names.

---

## Common Commands

### Run with existing PDFs (skip download)

If you already have PDFs in a folder:

```bash
python run_pipeline.py --skip-discover --pdf-dir /path/to/your/pdfs
```

### Change output location

```bash
python run_pipeline.py --out-dir /path/to/output
```

### Adjust parallelism (speed vs. memory)

```bash
# Faster (more parallel workers)
python run_pipeline.py --parallel 8

# Slower but uses less memory
python run_pipeline.py --parallel 2
```

### Use a different AI model

```bash
# Faster/cheaper
python run_pipeline.py --model gpt-4o-mini

# More accurate (costs more)
python run_pipeline.py --model gpt-4o
```

---

## Troubleshooting

### "pdftotext not found"

Install poppler-utils for faster PDF processing:

```bash
# macOS
brew install poppler

# Ubuntu/Debian
sudo apt-get install poppler-utils

# RHEL/CentOS
sudo yum install poppler-utils
```

The pipeline will work without it, but extraction will be slower.

### "No module named 'pdfplumber'"

Run setup again:

```bash
bash setup.sh
source .venv/bin/activate
```

### "API key not set"

Set your API key before running:

```bash
export OPENAI_API_KEY='sk-...'
```

### Slow extraction

- Increase parallel workers: `--parallel 8`
- Use `gpt-4o-mini` model (faster than `gpt-4o`)
- Ensure `pdftotext` is installed (10x faster)

### No PDFs downloaded

- Check internet connection
- Verify TDHCA website is accessible
- Check `out/download_manifest.json` for error details

---

---

## Project Structure

```
tx-lihtc-2026-agent/
├── setup.sh                    # One-time setup script
├── run_pipeline.py             # Main entry point
├── requirements.txt            # Python dependencies
├── scripts/
│   ├── run_single.py           # Single-run benchmark
│   ├── clean_csv.py            # Data cleanliness pass
│   └── benchmark_5_runs.py     # 5-run comparison
├── lihtc_tx_2026_agent/        # Core code
│   ├── discover.py             # PDF discovery (web crawler)
│   ├── download.py             # PDF downloader
│   ├── extract.py              # Data extraction + data model
│   ├── model_client.py         # AI model interface (OpenAI + Supabase proxy)
│   ├── cleanliness.py          # Data validation & normalization
│   ├── strategies/
│   │   ├── registry.py         # Strategy registry
│   │   ├── base.py             # ExtractStrategy protocol
│   │   └── v5_8_fast_tiebreaker.py  # Active extraction strategy
│   └── ops/                    # Pipeline operations
│       ├── supabase.py         # Audit logging
│       ├── run_complete_pipeline.py  # Full pipeline orchestrator
│       └── classify_pdfs.py    # PDF classification
└── archive/                    # Deprecated code (35 files, v1-v5.7)
    ├── strategies/
    ├── ops/
    └── scripts/
```

---

## How Discovery Works

The discovery layer (`discover.py`) is a **local Python web crawler**:

1. Starts from TDHCA seed pages (e.g., competitive 9% HTC page)
2. Follows links within the TDHCA domain
3. Collects PDF URLs matching the year pattern (default: includes "2026")
4. Filters to target folder (default: `2026-9-challenges` for full applications)

**No browser automation or OpenClaw is required** for standard operation.

---

## License

MIT

## Support

For issues or questions, open an issue on GitHub or contact the team.
