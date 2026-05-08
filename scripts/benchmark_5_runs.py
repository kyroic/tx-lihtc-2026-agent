#!/usr/bin/env python3
"""
Run 5 independent unified pipeline passes and compare results.

Each run:
- Calls extract_one_pdf_v5_8() on all 114 PDFs
- Applies auto-recovery (census_tract, poverty_rank, quartile, property_rate)
- Writes to its own output directory
- Records per-field coverage, timing, errors, and review flags

Produces a comparison report across all 5 runs.
"""
import json, time, sys, os, re, csv, subprocess, tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.pop("OPENAI_BASE_URL", None)
os.environ["OPENAI_BASE_URL"] = "https://trgggfvraglfgukxdqwn.supabase.co/functions/v1/model-proxy"

from lihtc_tx_2026_agent.strategies.v5_8_fast_tiebreaker import extract_one_pdf_v5_8

PDF_DIR = Path(__file__).resolve().parent.parent / "downloads_challenges"
BASE_OUT = Path(__file__).resolve().parent.parent / "out_benchmark"
MODEL = "gpt-4o-mini"
MAX_WORKERS = 6

STANDARD_KEYS = [
    "application_name", "contact_name", "contact_email", "contact_phone",
    "quartile", "property_rate", "poverty_rank", "census_tract"
]
TIEBREAKER_NAMES = ["tiebreaker_park", "tiebreaker_school", "tiebreaker_grocery", "tiebreaker_library"]
COORD_KEYS = ["park_lat", "park_lng", "school_lat", "school_lng",
              "grocery_lat", "grocery_lng", "library_lat", "library_lng"]
OTHER_PHASE2 = ["site_lat", "site_lng", "tiebreaker_score",
                "distance_to_park", "distance_to_school", "distance_to_grocery", "distance_to_library"]
ALL_FIELDS = STANDARD_KEYS + TIEBREAKER_NAMES + COORD_KEYS + OTHER_PHASE2

# ── Auto-recovery ────────────────────────────────────────────────
def pdftotext(pdf_path: Path, first: int = 1, last: int = -1) -> str:
    with tempfile.NamedTemporaryFile(suffix='.txt', delete=False) as tmp:
        tmp_path = tmp.name
    args = ['pdftotext', '-layout']
    if first > 1: args += ['-f', str(first)]
    if last > 0: args += ['-l', str(last)]
    args += [str(pdf_path), tmp_path]
    try:
        subprocess.run(args, capture_output=True, timeout=120)
        text = Path(tmp_path).read_text(errors='ignore')
    finally:
        if Path(tmp_path).exists(): Path(tmp_path).unlink()
    return text

def rcensus(text: str) -> str:
    for p in [r'\b(48\d{9,13})\b', r'Census\s+Tract[:\s]+(\d{6,11})', r'Tract[:\s]+(\d{6,11})']:
        for hit in re.findall(p, text, re.IGNORECASE):
            d = re.sub(r'[^\d]', '', str(hit))
            if len(d) >= 6: return d[:15]
    return ""

def rpoverty(text: str) -> str:
    for p in [r'Quartile:\s*[1-4]q?\s+Poverty\s*Rate[:\s]+([0-9]{1,2}(?:\.[0-9]{1,2})?)', r'Poverty\s*Rate[:\s]+([0-9]{1,2}(?:\.[0-9]{1,2})?)']:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            try:
                v = float(m.group(1))
                if 0 <= v <= 100: return str(v)
            except: pass
    return ""

def rquartile(text: str) -> str:
    for p in [r'Quartile[:\s]+\s*(\d)\s*(?:q|Qualified|Census)?', r'(\d)(?:st|nd|rd|th)\s+Quartile']:
        m = re.search(p, text, re.IGNORECASE)
        if m and m.group(1) in '1234': return m.group(1)
    return ""

def rproperty(text: str) -> str:
    for p in [r'Property\s*Tax\s*Rate[:\s]+([0-9]{1,2}(?:\.[0-9]{1,3})?)', r'Tax\s*Rate\s*per\s*\$100[:\s]+([0-9]{1,2}(?:\.[0-9]{1,3})?)']:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            try:
                float(m.group(1))
                return m.group(1)
            except: pass
    return ""

RECOVERY = {"census_tract": rcensus, "poverty_rank": rpoverty, "quartile": rquartile, "property_rate": rproperty}

# ── Run one pass ─────────────────────────────────────────────────
def run_one_pass(run_id: int) -> dict:
    out_dir = BASE_OUT / f"run_{run_id:02d}"
    out_dir.mkdir(parents=True, exist_ok=True)

    pdfs = sorted(PDF_DIR.glob("*.pdf"))
    N = len(pdfs)

    counts = {k: 0 for k in ALL_FIELDS}
    results = []
    errors = 0
    lock = threading.Lock()
    done = [0]

    def process_one(pdf: Path):
        nonlocal errors
        r = {"pdf": pdf.name}
        try:
            t0 = time.time()
            row = extract_one_pdf_v5_8(project_id="lihtc-tx-2026", model=MODEL, pdf_path=pdf, max_pages=15)
            r["time_s"] = round(time.time() - t0, 1)
            r["fields"] = {}
            for k in ALL_FIELDS:
                v = (getattr(row, k).value or "").strip() or None
                r["fields"][k] = v
            r["needs_review"] = row.needs_review
            r["review_reasons"] = row.review_reasons or []
            with lock:
                for k, v in r["fields"].items():
                    if v: counts[k] += 1
                done[0] += 1
        except Exception as e:
            with lock:
                done[0] += 1; errors += 1
            r["error"] = str(e)[:200]
            r["needs_review"] = True
            r["fields"] = {}
            r["review_reasons"] = [f"error:{str(e)[:80]}"]
            r["time_s"] = 0
        return r

    t_start = time.time()
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(process_one, pdf): pdf for pdf in pdfs}
        for f in as_completed(futures):
            results.append(f.result())

    elapsed = time.time() - t_start

    # Count pre-recovery
    pre_counts = dict(counts)

    # Auto-recovery: compute missing per-PDF once
    to_fix = []
    for r in results:
        missing = [k for k in RECOVERY if not r["fields"].get(k)]
        if missing:
            to_fix.append((r, missing))

    if to_fix:
        def scan_fix(item):
            r, missing = item
            pdf_path = PDF_DIR / r["pdf"]
            text = pdftotext(pdf_path, 1, 150)
            fixed = []
            for k in missing:
                val = RECOVERY[k](text)
                if val:
                    r["fields"][k] = val
                    with lock: counts[k] += 1
                    fixed.append(k)
            return r["pdf"], fixed

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(scan_fix, item): item for item in to_fix}
            for f in as_completed(futures):
                f.result()

    post_counts = dict(counts)

    # Build coverage stats
    nz = N - errors  # non-error PDFs
    n_std = len(STANDARD_KEYS)
    n_tb = len(TIEBREAKER_NAMES)
    n_coord = len(COORD_KEYS)
    n_other = len(OTHER_PHASE2)

    def rate(key_list, total_fields, ref_n):
        hits = sum(post_counts[k] for k in key_list)
        return round(100 * hits / max(ref_n * len(key_list), 1), 1) if ref_n > 0 else 0.0

    review_count = sum(1 for r in results if r.get("needs_review"))

    per_field = {}
    for k in ALL_FIELDS:
        per_field[k] = {"pre": pre_counts.get(k, 0), "post": post_counts.get(k, 0),
                         "rate": round(100 * post_counts[k] / max(nz, 1), 1) if nz > 0 else 0}

    run_summary = {
        "run": f"run_{run_id:02d}",
        "model": MODEL,
        "total": N,
        "errors": errors,
        "needs_review": review_count,
        "elapsed_minutes": round(elapsed / 60, 1),
        "avg_seconds_per_pdf": round(elapsed / max(N, 1), 1),
        "coverage": {
            "standard_pre": rate(STANDARD_KEYS, n_std, nz),
            "standard_post": rate(STANDARD_KEYS, n_std, nz),
            "tiebreaker_names": rate(TIEBREAKER_NAMES, n_tb, nz),
            "amenity_coordinates": rate(COORD_KEYS, n_coord, nz),
            "site_distances_score": rate(OTHER_PHASE2, n_other, nz),
        },
        "per_field": per_field,
        "per_pdf": results,
    }

    # Save JSON
    with open(out_dir / "run_summary.json", "w") as f:
        json.dump(run_summary, f, indent=2)

    # Save CSV
    csv_path = out_dir / "applications.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["pdf"] + ALL_FIELDS + ["needs_review", "review_reasons", "time_s", "error"])
        writer.writeheader()
        for r in results:
            row_data = {"pdf": r["pdf"]}
            for k in ALL_FIELDS:
                row_data[k] = (r.get("fields", {}).get(k) or "").replace(",", ";")
            row_data["needs_review"] = "true" if r.get("needs_review") else "false"
            row_data["review_reasons"] = ";".join(r.get("review_reasons", []) or []).replace(",", ";")
            row_data["time_s"] = str(r.get("time_s", ""))
            row_data["error"] = (r.get("error", "") or "").replace(",", ";")
            writer.writerow(row_data)

    return run_summary


# ── Main: 5 runs ─────────────────────────────────────────────────
print("╔══════════════════════════════════════════════════════════════╗")
print("║    5 INDEPENDENT UNIFIED PIPELINE RUNS — BENCHMARK          ║")
print("╠══════════════════════════════════════════════════════════════╣")
print(f"║  PDFs: 114   Model: {MODEL}   Workers: {MAX_WORKERS}                ║")
print("║  Output: out_benchmark/run_01 → run_05                      ║")
print("╚══════════════════════════════════════════════════════════════╝")
print()

all_runs = []
grand_start = time.time()

for run_id in range(1, 6):
    print(f"\n{'='*60}")
    print(f"RUN {run_id}/5")
    print(f"{'='*60}")
    summary = run_one_pass(run_id)
    all_runs.append(summary)
    print(f"  ✅ Elapsed: {summary['elapsed_minutes']} min | "
          f"std={summary['coverage']['standard_post']}% | "
          f"tb_names={summary['coverage']['tiebreaker_names']}% | "
          f"coords={summary['coverage']['amenity_coordinates']}% | "
          f"site+dist={summary['coverage']['site_distances_score']}% | "
          f"errors={summary['errors']}")

grand_elapsed = time.time() - grand_start

# ── Comparison report ────────────────────────────────────────────
print(f"\n\n{'='*80}")
print(f"COMPARISON REPORT — 5 runs, total elapsed: {grand_elapsed/60:.1f} min")
print(f"{'='*80}")

categories = ["standard_post", "tiebreaker_names", "amenity_coordinates", "site_distances_score"]
cat_labels = {"standard_post": "Standard (post-recovery)", "tiebreaker_names": "Tiebreaker names (4)",
              "amenity_coordinates": "Amenity coords (8)", "site_distances_score": "Site + dist + score (7)"}

print(f"\n{'Category':<30} {'Run 1':>7} {'Run 2':>7} {'Run 3':>7} {'Run 4':>7} {'Run 5':>7} {'Mean':>7} {'StDev':>7}")
print("-" * 80)

for cat in categories:
    vals = [r["coverage"][cat] for r in all_runs]
    mean = sum(vals) / len(vals)
    stdev = (sum((v - mean)**2 for v in vals) / len(vals)) ** 0.5
    row = f"{cat_labels[cat]:<30}"
    for v in vals:
        row += f" {v:>6.1f}%"
    row += f" {mean:>6.1f}%" if mean == int(mean) else f" {mean:>6.1f}%"
    row += f" {stdev:>6.2f}"
    print(row)

# Timing
timings = [r["elapsed_minutes"] for r in all_runs]
print(f"\n{'Elapsed (min)':<30}", end="")
for t in timings:
    print(f" {t:>6.1f} ", end="")
mean_t = sum(timings) / len(timings)
stdev_t = (sum((t - mean_t)**2 for t in timings) / len(timings)) ** 0.5
print(f" {mean_t:>6.1f}  {stdev_t:>6.2f}")

# Errors
errs = [r["errors"] for r in all_runs]
print(f"{'Errors':<30}", end="")
for e in errs:
    print(f" {e:>6} ", end="")
print(f" {sum(errs)/len(errs):>6.1f}  {0:>6}")

# Needs review
revs = [r["needs_review"] for r in all_runs]
print(f"{'Needs review':<30}", end="")
for r in revs:
    print(f" {r:>6} ", end="")
mean_r = sum(revs) / len(revs)
stdev_r = (sum((r - mean_r)**2 for r in revs) / len(revs)) ** 0.5
print(f" {mean_r:>6.1f}  {stdev_r:>6.2f}")

# ── Per-field cross-run agreement ────────────────────────────────
print(f"\n{'─'*80}")
print("PER-FIELD CROSS-RUN STABILITY (rate % across 5 runs)")
print(f"{'─'*80}")
print(f"{'Field':<25}", end="")
for i in range(1, 6):
    print(f" {'R'+str(i):>6}", end="")
print(f" {'Range':>7} {'μ':>6}")
print("-" * 65)

for k in ALL_FIELDS:
    rates = [r["per_field"][k]["rate"] for r in all_runs]
    rng = max(rates) - min(rates)
    mean = sum(rates) / len(rates)
    print(f"{k:<25}", end="")
    for v in rates:
        print(f" {v:>5.1f}%", end="")
    print(f" {rng:>6.1f} {mean:>5.1f}%")

# ── Save comparison ──────────────────────────────────────────────
comparison = {
    "runs": [r["run"] for r in all_runs],
    "categories": {cat: [r["coverage"][cat] for r in all_runs] for cat in categories},
    "elapsed_minutes": timings,
    "errors": errs,
    "needs_review": revs,
    "per_field_stability": {
        k: {
            "rates": [r["per_field"][k]["rate"] for r in all_runs],
            "range": max([r["per_field"][k]["rate"] for r in all_runs]) - min([r["per_field"][k]["rate"] for r in all_runs]),
        }
        for k in ALL_FIELDS
    },
    "grand_elapsed_minutes": round(grand_elapsed / 60, 1),
}

with open(BASE_OUT / "comparison_report.json", "w") as f:
    json.dump(comparison, f, indent=2)

print(f"\n\nComparison report: {BASE_OUT / 'comparison_report.json'}")
print("Done.")
