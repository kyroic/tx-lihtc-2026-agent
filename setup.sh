#!/usr/bin/env bash
set -euo pipefail

echo "🔧 Setting up tx-lihtc-2026-agent..."

if ! command -v python3 >/dev/null 2>&1; then
  echo "❌ Python 3.10+ is required"
  exit 1
fi

python3 -m venv .venv
source .venv/bin/activate
pip install -U pip >/dev/null
pip install -r requirements.txt >/dev/null

if ! command -v pdftotext >/dev/null 2>&1; then
  echo "⚠️ pdftotext not found (recommended for speed)"
  echo "   macOS: brew install poppler"
  echo "   Ubuntu: sudo apt-get install poppler-utils"
fi

if [ ! -f config.yaml ] && [ -f config.yaml.example ]; then
  cp config.yaml.example config.yaml
  echo "✅ Created config.yaml from template"
fi

echo "✅ Setup complete"
echo "Next:"
echo "  source .venv/bin/activate"
echo "  export OPENAI_API_KEY=..."
echo "  python run_pipeline.py"
