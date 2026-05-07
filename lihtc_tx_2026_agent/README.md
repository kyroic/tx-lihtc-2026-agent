# Texas LIHTC 2026 Agent

Automated extraction of Texas Low-Income Housing Tax Credit (LIHTC) 2026 Full Application PDFs.

## Quick Start (No OpenClaw Required)

### 1. Install Dependencies

```bash
# Run the setup script
bash lihtc_tx_2026_agent/setup.sh

# Or manually:
pip3 install pdfplumber requests
```

**Required:**
- Python 3.10+
- `pdftotext` (from poppler-utils) — for fast PDF text extraction

### 2. Set API Key

Choose **ONE** provider:

```bash
# OpenAI (recommended)
export OPENAI_API_KEY='sk-...'

# OR Anthropic
export ANTHROPIC_API_KEY='sk-ant-...'

# OR use local Ollama (free, no API key)
# Install from: https://ollama.ai
```

### 3. Run the Pipeline

```bash
# Full end-to-end (discover → download → extract)
python3 -m lihtc_tx_2026_agent.ops.run_complete_pipeline

# Or use existing PDFs
python3 -m lihtc_tx_2026_agent.ops.run_complete_pipeline \
  --pdf-dir /path/to/your/pdfs \
  --out-dir out

# Or just extract (skip discovery/download)
python3 -m lihtc_tx_2026_agent.ops.run_complete_pipeline \
  --skip-discover \
  --pdf-dir out_v5_6_full/downloads
```

### 4. Get Results

Find outputs in `out/aggregate/`:
- `applications.csv` — Main spreadsheet
- `applications.xlsx` — Excel format
- `applications.jsonl` — JSON lines for processing
- `review_queue.csv` — Items needing manual review

## Configuration

Edit `config.yaml` to customize:

```yaml
extraction:
  model: "gpt-4o-mini"  # Change model
  parallel_workers: 4    # Adjust parallelism
  
api:
  provider: "openai"     # or "anthropic", "ollama"
```

## What It Extracts

| Field | Description | Recovery |
|-------|-------------|----------|
| `application_name` | Development name | ✅ |
| `contact_*` | Developer contact info | ✅ |
| `census_tract` | 11-digit GEOID | ✅ Auto-recovery |
| `poverty_rank` | Poverty rate % | ✅ Auto-recovery |
| `quartile` | Income quartile (1-4) | ✅ |
| `tiebreaker_park` | Distance to park | ✅ Broad search |
| `tiebreaker_school` | Distance to school | ✅ Broad search |
| `tiebreaker_grocery` | Distance to grocery | ✅ Broad search |
| `tiebreaker_library` | Distance to library | ✅ Broad search |

## Without OpenClaw

This pipeline works **standalone** — no OpenClaw runtime needed.

**What OpenClaw provided:**
- Session management
- Built-in model routing
- Message channel integration

**What this standalone version uses:**
- Direct API calls (OpenAI/Anthropic/Ollama)
- Local file I/O
- Standard Python logging

## Troubleshooting

### "pdftotext not found"
```bash
# macOS
brew install poppler

# Ubuntu/Debian
sudo apt-get install poppler-utils

# RHEL/CentOS
sudo yum install poppler-utils
```

### "No module named 'pdfplumber'"
```bash
pip3 install pdfplumber
```

### "API key not set"
```bash
export OPENAI_API_KEY='your-key-here'
# Or switch to Ollama in config.yaml
```

### Slow extraction
- Increase `parallel_workers` in `config.yaml`
- Use `gpt-4o-mini` (faster than `gpt-4o`)
- Ensure `pdftotext` is installed (10x faster than pdfplumber alone)

## Advanced Usage

### Custom Model
```bash
python3 -m lihtc_tx_2026_agent.ops.run_complete_pipeline \
  --model claude-sonnet-4-20250514
```

### Different Output Location
```bash
python3 -m lihtc_tx_2026_agent.ops.run_complete_pipeline \
  --out-dir /path/to/output
```

### Process Specific PDFs Only
```bash
python3 -m lihtc_tx_2026_agent.ops.run_complete_pipeline \
  --skip-discover \
  --pdf-dir /path/to/specific/pdfs
```

## Data Quality

- **census_tract:** 100% (auto-recovery via regex)
- **poverty_rank:** ~97% (auto-recovery near quartile)
- **tiebreaker_*:** ~93% (broad heading search)
- **quartile:** ~79% (structured field)

Items flagged for review can be found in `review_queue.csv`.

## License

MIT

## Support

For issues or questions, open an issue on GitHub.
