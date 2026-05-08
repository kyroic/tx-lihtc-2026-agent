#!/usr/bin/env python3
"""Full 114-PDF Phase 2 extraction scan."""
import json, time, sys, os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

os.environ["OPENAI_BASE_URL"] = "http://localhost:11434"
os.environ["OPENAI_API_KEY"] = "ollama"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lihtc_tx_2026_agent.strategies.v5_8_fast_tiebreaker import (
    extract_one_pdf_v5_8, find_tiebreaker_pages_fast
)

PDF_DIR = Path(__file__).resolve().parent.parent / "downloads_challenges"
OUT_DIR = Path(__file__).resolve().parent.parent / "test_outputs"
OUT_DIR.mkdir(parents=True, exist_ok=True)

pdfs = sorted(PDF_DIR.glob("*.pdf"))
N = len(pdfs)
MODEL = "qwen2.5:7b"

EXISTING_KEYS = [
    "tiebreaker_park", "tiebreaker_school",
    "tiebreaker_grocery", "tiebreaker_library"
]
PHASE2_COORD_KEYS = [
    "park_lat", "park_lng", "school_lat", "school_lng",
    "grocery_lat", "grocery_lng", "library_lat", "library_lng"
]
PHASE2_OTHER_KEYS = [
    "site_lat", "site_lng", "tiebreaker_score",
    "distance_to_park", "distance_to_school",
    "distance_to_grocery", "distance_to_library"
]
ALL_PHASE2 = PHASE2_COORD_KEYS + PHASE2_OTHER_KEYS

print(f"Starting Phase 2 full scan: {N} PDFs, model={MODEL}, 2 workers")
print(f"PDF dir: {PDF_DIR}")
print(f"Output: {OUT_DIR / 'phase2_114_scan_results.json'}")
print()

counts: dict[str, int] = {k: 0 for k in EXISTING_KEYS + ALL_PHASE2}
per_pdf: list[dict] = []
errors = 0
started = time.time()
done = [0]  # mutable counter for thread safety

def process_one(pdf: Path) -> dict:
    import threading
    result = {"pdf": pdf.name, "tb_pages": []}
    try:
        tb = find_tiebreaker_pages_fast(pdf)
        result["tb_pages"] = tb
        t0 = time.time()
        row = extract_one_pdf_v5_8(
            project_id="lihtc-tx-2026",
            model=MODEL,
            pdf_path=pdf,
            max_pages=10,
        )
        elapsed = time.time() - t0
        result["time_s"] = round(elapsed, 1)
        result["needs_review"] = row.needs_review
        
        existing = {k: ((getattr(row, k).value or "").strip() or None) for k in EXISTING_KEYS}
        phase2 = {k: ((getattr(row, k).value or "").strip() or None) for k in ALL_PHASE2}
        result["existing"] = existing
        result["phase2"] = phase2
        
        # Thread-safe counter update
        with threading.Lock():
            for k in EXISTING_KEYS:
                if existing.get(k): counts[k] += 1
            for k in ALL_PHASE2:
                if phase2.get(k): counts[k] += 1
            done[0] += 1
        
        ex_filled = sum(1 for v in existing.values() if v)
        p2_filled = sum(1 for v in phase2.values() if v)
        print(f"[{done[0]}/{N}] {pdf.name}: TB={len(tb)} | "
              f"existing={ex_filled}/4 | phase2={p2_filled}/15 | {elapsed:.0f}s")
        
    except Exception as e:
        result["error"] = str(e)[:200]
        with threading.Lock():
            done[0] += 1
            errors += 1
        print(f"[{done[0]}/{N}] {pdf.name}: ERROR - {e}")
    
    return result

# Process with 2 workers
with ThreadPoolExecutor(max_workers=2) as pool:
    futures = {pool.submit(process_one, pdf): pdf for pdf in pdfs}
    for future in as_completed(futures):
        result = future.result()
        per_pdf.append(result)

elapsed_total = time.time() - started

# Compute summary
print(f"\n{'='*70}")
print(f"FULL SCAN COMPLETE: {N} PDFs in {elapsed_total/60:.1f} min")
print(f"{'='*70}")

n = N
ex_total = sum(counts[k] for k in EXISTING_KEYS)
p2_coord_total = sum(counts[k] for k in PHASE2_COORD_KEYS)
p2_other_total = sum(counts[k] for k in PHASE2_OTHER_KEYS)
p2_total = p2_coord_total + p2_other_total

print(f"\n--- EXISTING TIEBREAKER NAMES ---")
for k in EXISTING_KEYS:
    print(f"  {k}: {counts[k]}/{n} ({100*counts[k]/n:.0f}%)")
print(f"  TOTAL: {ex_total}/{n*4} ({100*ex_total/(n*4):.0f}%)")

print(f"\n--- PHASE 2 AMENITY COORDINATES ---")
for k in PHASE2_COORD_KEYS:
    print(f"  {k}: {counts[k]}/{n} ({100*counts[k]/n:.0f}%)")
print(f"  TOTAL: {p2_coord_total}/{n*8} ({100*p2_coord_total/(n*8):.0f}%)")

print(f"\n--- PHASE 2 SITE + DISTANCES + SCORE ---")
for k in PHASE2_OTHER_KEYS:
    print(f"  {k}: {counts[k]}/{n} ({100*counts[k]/n:.0f}%)")
print(f"  TOTAL: {p2_other_total}/{n*7} ({100*p2_other_total/(n*7):.0f}%)")

print(f"\n--- COMPARISON ---")
print(f"  Baseline tiebreaker names:   {100*ex_total/(n*4):.0f}%")
print(f"  Phase 2 amenity coordinates: {100*p2_coord_total/(n*8):.0f}%")
print(f"  Phase 2 site coords:         {100*(counts['site_lat']+counts['site_lng'])/(n*2):.0f}%")
print(f"  Delta (names vs coords):     {100*ex_total/(n*4) - 100*p2_coord_total/(n*8):.0f}pp")
print(f"  Errors: {errors}")

# Write full results
summary = {
    "model": MODEL,
    "total_pdfs": N,
    "errors": errors,
    "elapsed_minutes": round(elapsed_total / 60, 1),
    "existing_keys": EXISTING_KEYS,
    "phase2_coord_keys": PHASE2_COORD_KEYS,
    "phase2_other_keys": PHASE2_OTHER_KEYS,
    "counts": counts,
    "rates": {
        "existing_names": round(100 * ex_total / (n * 4), 1),
        "phase2_coordinates": round(100 * p2_coord_total / (n * 8), 1),
        "phase2_site_coords": round(100 * (counts["site_lat"] + counts["site_lng"]) / (n * 2), 1),
        "delta_pp": round(100 * ex_total / (n * 4) - 100 * p2_coord_total / (n * 8), 1),
    },
    "per_pdf": per_pdf,
}

out_path = OUT_DIR / "phase2_114_scan_results.json"
with open(out_path, "w") as f:
    json.dump(summary, f, indent=2)
print(f"\nResults written to {out_path}")

# Also write a flat CSV
csv_path = OUT_DIR / "phase2_114_scan.csv"
with open(csv_path, "w") as f:
    all_keys = ["pdf"] + EXISTING_KEYS + ALL_PHASE2 + ["tb_pages", "time_s", "error"]
    f.write(",".join(all_keys) + "\n")
    for r in per_pdf:
        vals = [r["pdf"]]
        for k in EXISTING_KEYS:
            vals.append(r.get("existing", {}).get(k) or "")
        for k in ALL_PHASE2:
            vals.append(r.get("phase2", {}).get(k) or "")
        vals.append(str(len(r.get("tb_pages", []))))
        vals.append(str(r.get("time_s", "")))
        vals.append(r.get("error", "").replace(",", ";"))
        f.write(",".join(f'"{v}"' for v in vals) + "\n")
print(f"CSV written to {csv_path}")
