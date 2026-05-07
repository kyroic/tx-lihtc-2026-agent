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
git clone https://github.com/Doculy-AI/qc-agent.git
cd qc-agent
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
| `tiebreaker_park` | Distance to nearest park | 93% |
| `tiebreaker_school` | Distance to nearest school | 93% |
| `tiebreaker_grocery` | Distance to nearest grocery | 93% |
| `tiebreaker_library` | Distance to nearest library | 93% |

---

## Performance (Tested Results)

Latest runs on 114 full application PDFs (`2026-9-challenges` folder):

| Metric | Result |
|--------|--------|
| PDFs processed | 114 / 114 |
| Runtime (extraction only) | ~13 minutes |
| Runtime (full pipeline) | ~20 minutes |
| Clean extractions (no review) | 89 / 114 (78%) |
| Items needing review | 25 / 114 (22%) |

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

## Configuration (Optional)

Copy the example config and customize:

```bash
cp config.yaml.example config.yaml
```

Edit `config.yaml` to change:
- Default model
- Parallel workers
- Output formats
- API provider

---

## Project Structure

```
qc-agent/
├── setup.sh                    # One-time setup script
├── run_pipeline.py            # Main entry point
├── config.yaml.example        # Configuration template
├── requirements.txt           # Python dependencies
├── lihtc_tx_2026_agent/       # Core code
│   ├── discover.py            # PDF discovery (web crawler)
│   ├── download.py            # PDF downloader
│   ├── extract.py             # Data extraction
│   ├── model_client.py        # AI model interface
│   ├── strategies/            # Extraction strategies
│   └── ops/                   # Pipeline operations
└── out/                       # Output folder (created on run)
    └── aggregate/
        ├── applications.csv
        ├── applications.xlsx
        └── review_queue.csv
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
