#!/usr/bin/env python3
"""Full 114-PDF Phase 2 extraction via OpenAI gpt-4o-mini."""
import os, sys
from pathlib import Path

# Unset the broken gateway; let model_client route to api.openai.com
os.environ.pop("OPENAI_BASE_URL", None)
os.environ.pop("SUPABASE_SERVICE_KEY", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lihtc_tx_2026_agent.strategies.v5_8_fast_tiebreaker import extract_one_pdf_v5_8
import json, time
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

PDF_DIR = Path(__file__).resolve().parent.parent / "downloads_challenges"
OUT_DIR = Path(__file__).resolve().parent.parent / "test_outputs"
OUT_DIR.mkdir(parents=True, exist_ok=True)
MODEL = "gpt-4o-mini"
MAX_WORKERS = 6

EXISTING = ["tiebreaker_park","tiebreaker_school","tiebreaker_grocery","tiebreaker_library"]
COORDS = ["park_lat","park_lng","school_lat","school_lng",
          "grocery_lat","grocery_lng","library_lat","library_lng"]
OTHER = ["site_lat","site_lng","tiebreaker_score",
         "distance_to_park","distance_to_school",
         "distance_to_grocery","distance_to_library"]
ALL_FIELDS = EXISTING + COORDS + OTHER

counts = {k:0 for k in ALL_FIELDS}
results = []
errors = 0
lock = threading.Lock()
done = [0]

def process_one(pdf: Path):
    global errors
    r = {"pdf": pdf.name}
    try:
        t0 = time.time()
        row = extract_one_pdf_v5_8(project_id="lihtc-tx-2026", model=MODEL, pdf_path=pdf, max_pages=10)
        elapsed = time.time() - t0
        fields = {}
        for k in ALL_FIELDS:
            v = (getattr(row, k).value or "").strip() or None
            fields[k] = v
        r["fields"] = fields
        r["time_s"] = round(elapsed, 1)
        r["needs_review"] = row.needs_review
        r["review_reasons"] = row.review_reasons
        with lock:
            for k, v in fields.items():
                if v: counts[k] += 1
            done[0] += 1
        ex = sum(1 for k in EXISTING if fields.get(k))
        p2 = sum(1 for k in COORDS+OTHER if fields.get(k))
        st = sum(1 for k in STANDARD if fields.get(k))
        print(f"[{done[0]}/{len(pdfs)}] {pdf.name}: std={st}/8 | existing={ex}/4 | phase2={p2}/15 | {elapsed:.0f}s")
    except Exception as e:
        with lock:
            done[0] += 1; errors += 1
        r["error"] = str(e)[:200]
        print(f"[{done[0]}/{len(pdfs)}] {pdf.name}: ERROR - {e}")
    return r

pdfs = sorted(PDF_DIR.glob("*.pdf"))
print(f"Phase 2: {len(pdfs)} PDFs, {MODEL}, {MAX_WORKERS} workers")
t0 = time.time()

with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
    for f in as_completed({pool.submit(process_one, p): p for p in pdfs}):
        results.append(f.result())

e = time.time() - t0
n = len(pdfs)
ex_t = sum(counts[k] for k in EXISTING)
cd_t = sum(counts[k] for k in COORDS)
ot_t = sum(counts[k] for k in OTHER)

print(f"\n{'='*60}")
print(f"DONE: {n} PDFs in {e/60:.1f}m ({e/n:.1f}s avg)")
print(f"{'='*60}")
print(f"STANDARD FIELDS: {sum(counts[k] for k in STANDARD)}/{n*len(STANDARD)} ({100*sum(counts[k] for k in STANDARD)/(n*len(STANDARD)):.0f}%)")
print(f"EXISTING NAMES:  {ex_t}/{n*4} ({100*ex_t/(n*4):.0f}%)")
print(f"AMENITY COORDS:  {cd_t}/{n*8} ({100*cd_t/(n*8):.0f}%)")
print(f"SITE+DIST+SCORE: {ot_t}/{n*7} ({100*ot_t/(n*7):.0f}%)")
print(f"Δ (names-coords): {100*ex_t/(n*4) - 100*cd_t/(n*8):.0f}pp | errors: {errors}")

for k in EXISTING: print(f"  {k}: {counts[k]}/{n}")
for k in COORDS: print(f"  {k}: {counts[k]}/{n}")
for k in OTHER: print(f"  {k}: {counts[k]}/{n}")

summary = {"model":MODEL,"pdfs":n,"errors":errors,"elapsed_m":round(e/60,1),"avg_s":round(e/n,1),
           "counts":counts,"rates":{"names":round(100*ex_t/(n*4),1),"coords":round(100*cd_t/(n*8),1),
           "site":round(100*(counts["site_lat"]+counts["site_lng"])/(n*2),1),
           "delta":round(100*ex_t/(n*4)-100*cd_t/(n*8),1)},"per_pdf":results}
with open(OUT_DIR/"phase2_114_scan_results.json","w") as f: json.dump(summary,f,indent=2)
print(f"\n→ {OUT_DIR/'phase2_114_scan_results.json'}")
