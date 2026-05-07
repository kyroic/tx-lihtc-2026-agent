#!/bin/bash
# Texas LIHTC 2026 Agent - Quick Setup
# Run this once to set up your environment

set -e

echo "🔧 Setting up Texas LIHTC 2026 Agent..."

# Check Python version
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 not found. Please install Python 3.10+"
    exit 1
fi

PYTHON_VERSION=$(python3 --version | cut -d' ' -f2 | cut -d'.' -f1,2)
echo "✅ Python $PYTHON_VERSION found"

# Install dependencies
echo "📦 Installing dependencies..."
pip3 install -q pdfplumber requests

# Check for pdftotext (poppler-utils)
if ! command -v pdftotext &> /dev/null; then
    echo "⚠️  pdftotext not found. Installing poppler-utils..."
    if [[ "$OSTYPE" == "darwin"* ]]; then
        brew install poppler 2>/dev/null || echo "  (brew install poppler failed, trying to continue...)"
    elif command -v apt-get &> /dev/null; then
        sudo apt-get install -y poppler-utils 2>/dev/null || echo "  (apt install failed, trying to continue...)"
    elif command -v yum &> /dev/null; then
        sudo yum install -y poppler-utils 2>/dev/null || echo "  (yum install failed, trying to continue...)"
    fi
fi

if command -v pdftotext &> /dev/null; then
    echo "✅ pdftotext found"
else
    echo "❌ pdftotext not available. Extraction will be slower."
fi

# Copy config template
if [ ! -f config.yaml ]; then
    cp lihtc_tx_2026_agent/config.yaml config.yaml
    echo "✅ Created config.yaml"
fi

# Create output directory
mkdir -p out

# Setup instructions
echo ""
echo "============================================"
echo "✅ Setup Complete!"
echo "============================================"
echo ""
echo "Next steps:"
echo ""
echo "1. Set your API key (choose ONE):"
echo "   export OPENAI_API_KEY='your-key-here'"
echo "   # OR"
echo "   export ANTHROPIC_API_KEY='your-key-here'"
echo "   # OR use local Ollama (no key needed)"
echo ""
echo "2. Run the pipeline:"
echo "   python3 -m lihtc_tx_2026_agent.ops.run_complete_pipeline"
echo ""
echo "3. Find results in:"
echo "   out/aggregate/applications.csv"
echo "   out/aggregate/applications.xlsx"
echo ""
echo "For help:"
echo "   python3 -m lihtc_tx_2026_agent.ops.run_complete_pipeline --help"
echo ""
