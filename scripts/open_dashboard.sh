#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${1:-8000}"
# JSON paths are resolved relative to /dashboard/, so default to one level up.
BENCH="${2:-../out_bench_demo/benchmark.json}"

cd "${ROOT_DIR}"

echo "[dashboard] serving on http://localhost:${PORT}"
echo "[dashboard] opening benchmark: ${BENCH}"

python3 -m http.server "${PORT}" >/dev/null 2>&1 &
PID=$!

open "http://localhost:${PORT}/dashboard/index.html?bench=${BENCH}"

echo "[dashboard] http.server pid=${PID} (Ctrl+C won't stop it since it's backgrounded)"
echo "[dashboard] to stop: kill ${PID}"

