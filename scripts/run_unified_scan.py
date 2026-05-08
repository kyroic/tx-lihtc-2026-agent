#!/usr/bin/env python3
"""
Unified 114-PDF scan — all 27 fields from a single extract_one_pdf_v5_8() call.

Uses the library's integrated function (standard pages + tiebreaker pages
+ coordinate/distance restructuring + post-extraction cleaning).
"""
import json, time, sys, os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Clear any broken gateway env vars so model_client routes correctly
os.environ.pop("OPENAI_BASE_URL", None)
os.environ.pop("SUPABASE_SERVICE_KEY", None)

from lihtc_tx_2026_agent.strategies.v5_8_fast_tiebreaker import extract_one_pdf_v5_8

PDF_DIR = Path(__file__).resolve().parent.parent / "downloads_challenges"
OUT_DIR = Path(__file__).resolve().parent.parent / "out_unified"
OUT_DIR.mkdir(parents=True, exist_ok=True)

MODEL = "gpt-4o-mini"
MAX_WORKERS = 6

STANDARD_KEYS = [
    "application_name", "contact_name", "contact_email", "contact_phone",
    "quartile", "property_rate", "poverty_rank", "census_tract"
]
TIEBREAKER_NAMES = [
    "tiebreaker_park", "tiebreaker_school",
    "tiebreaker_grocery", "tiebreaker_library"
]
COORD_KEYS = [
    "park_lat", "park_lng", "school_lat", "school_lng",
    "grocery_lat", "grocery_lng", "library_lat", "library_lng"
]
OTHER_PHASE2 = [
    "site_lat", "site_lng", "tiebreaker_score",
    "distance_to_park", "distance_to_school",
    "distance_to_grocery", "distance_to_library"
]
ALL_FIELDS = STANDARD_KEYS + TIEBREAKER_NAMES + COORD_KEYS + OTHER_PHASE2

counts = {k: 0 for k in ALL_FIELDS}
results = []
errors = 0
lock = threading.Lock()
done = [0]

def process_one(pdf: Path):
    global errors
    r = {"pdf": pdf.name}
    try:
        t0 = time.time()
        row = extract_one_pdf_v5_8(
            project_id="lihtc-tx-2026",
            model=MODEL,
            pdf_path=pdf,
            max_pages=15,
        )
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
        std_f = sum(1 for k in STANDARD_KEYS if fields.get(k))
        ex_f = sum(1 for k in TIEBREAKER_NAMES if fields.get(k))
        cd_f = sum(1 for k in COORD_KEYS if fields.get(k))
        ot_f = sum(1 for k in OTHER_PHASE2 if fields.get(k))
        print(f"[{done[0]}/{len(pdfs)}] {pdf.name}: "
              f"std={std_f}/{len(STANDARD_KEYS)} | tb_names={ex_f}/{len(TIEBREAKER_NAMES)} | "
              f"coords={cd_f}/{len(COORD_KEYS)} | other={ot_f}/{len(OTHER_PHASE2)} | {elapsed:.0f}s")
    except Exception as e:
        with lock:
            done[0] += 1
            errors += 1
        r["error"] = str(e)[:200]
        r["needs_review"] = True
        print(f"[{done[0]}/{len(pdfs)}] {pdf.name}: ERROR - {e}")

    return r

pdfs = sorted(PDF_DIR.glob("*.pdf"))
N = len(pdfs)

print(f"Unified scan: {N} PDFs | model={MODEL} | {MAX_WORKERS} workers")
print(f"PDF dir: {PDF_DIR}")
print(f"Output:  {OUT_DIR}")
print()

t_start = time.time()

with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
    futures = {pool.submit(process_one, pdf): pdf for pdf in pdfs}
    for f in as_completed(futures):
        results.append(f.result())

elapsed = time.time() - t_start

# ── Summary ───────────────────────────────────────────────────
print(f"\n{'='*70}")
print(f"DONE: {N} PDFs in {elapsed/60:.1f} min ({elapsed/N:.1f}s avg)")
print(f"{'='*70}")

n = N
std_t = sum(counts[k] for k in STANDARD_KEYS)
ex_t = sum(counts[k] for k in TIEBREAKER_NAMES)
cd_t = sum(counts[k] for k in COORD_KEYS)
ot_t = sum(counts[k] for k in OTHER_PHASE2)

print(f"\n--- STANDARD FIELDS (8) ---")
for k in STANDARD_KEYS:
    print(f"  {k}: {counts[k]}/{n} ({100*counts[k]/n:.0f}%)")
print(f"  TOTAL: {std_t}/{n*len(STANDARD_KEYS)} ({100*std_t/(n*len(STANDARD_KEYS)):.0f}%)")

print(f"\n--- TIEBREAKER NAMES (4) ---")
for k in TIEBREAKER_NAMES:
    print(f"  {k}: {counts[k]}/{n} ({100*counts[k]/n:.0f}%)")
print(f"  TOTAL: {ex_t}/{n*4} ({100*ex_t/(n*4):.0f}%)")

print(f"\n--- AMENITY COORDINATES (8) ---")
for k in COORD_KEYS:
    print(f"  {k}: {counts[k]}/{n} ({100*counts[k]/n:.0f}%)")

print(f"\n--- SITE + DISTANCES + SCORE (7) ---")
for k in OTHER_PHASE2:
    print(f"  {k}: {counts[k]}/{n} ({100*counts[k]/n:.0f}%)")

review_count = sum(1 for r in results if r.get("needs_review"))
print(f"\nErrors: {errors}")
print(f"Needs review: {review_count}/{N}")

# ── Save ──────────────────────────────────────────────────────
summary = {
    "model": MODEL,
    "total_pdfs": N,
    "errors": errors,
    "needs_review": review_count,
    "elapsed_minutes": round(elapsed / 60, 1),
    "avg_seconds_per_pdf": round(elapsed / N, 1),
    "counts": counts,
    "rates": {
        "standard_fields": round(100 * std_t / (n * len(STANDARD_KEYS)), 1),
        "tiebreaker_names": round(100 * ex_t / (n * 4), 1),
        "amenity_coordinates": round(100 * cd_t / (n * 8), 1),
        "site_distances_score": round(100 * ot_t / (n * 7), 1),
    },
    "per_pdf": results,
}

out_json = OUT_DIR / "unified_scan_results.json"
with open(out_json, "w") as f:
    json.dump(summary, f, indent=2)

# CSV
csv_path = OUT_DIR / "unified_scan.csv"
with open(csv_path, "w") as f:
    header = ["pdf"] + STANDARD_KEYS + TIEBREAKER_NAMES + COORD_KEYS + OTHER_PHASE2 + ["needs_review", "review_reasons", "time_s", "error"]
    f.write(",".join(header) + "\n")
    for r in results:
        vals = [r["pdf"]]
        for k in STANDARD_KEYS + TIEBREAKER_NAMES + COORD_KEYS + OTHER_PHASE2:
            vals.append((r.get("fields", {}).get(k) or "").replace(",", ";"))
        vals.append("true" if r.get("needs_review") else "false")
        vals.append(";".join(r.get("review_reasons", []) or []).replace(",", ";"))
        vals.append(str(r.get("time_s", "")))
        vals.append((r.get("error", "") or "").replace(",", ";"))
        f.write(",".join(f'"{v}"' for v in vals) + "\n")

print(f"\nJSON: {out_json}")
print(f"CSV:  {csv_path}")
