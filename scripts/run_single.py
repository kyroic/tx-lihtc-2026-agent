#!/usr/bin/env python3
"""
Single-run benchmark — runs one independent pass (Runs 2-5 only).
"""
import json, time, sys, os, re, csv, subprocess, tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import argparse

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

def pdftotext(pdf_path: Path, first: int = 1, last: int = -1) -> str:
    with tempfile.NamedTemporaryFile(suffix='.txt', delete=False) as tmp:
        tmp_path = tmp.name
    args = ['pdftotext', '-layout']
    if first > 1: args += ['-f', str(first)]
    if last > 0: args += ['-l', str(last)]
    args += [str(pdf_path), tmp_path]
    try:
        subprocess.run(args, capture_output=True, timeout=120)
        return Path(tmp_path).read_text(errors='ignore')
    finally:
        if Path(tmp_path).exists(): Path(tmp_path).unlink()

def rcensus(text): 
    for p in [r'\b(48\d{9,13})\b', r'Census\s+Tract[:\s]+(\d{6,11})', r'Tract[:\s]+(\d{6,11})']:
        for hit in re.findall(p, text, re.IGNORECASE):
            d = re.sub(r'[^\d]', '', str(hit))
            if len(d) >= 6: return d[:15]
    return ""
def rpoverty(text):
    for p in [r'Quartile:\s*[1-4]q?\s+Poverty\s*Rate[:\s]+([0-9]{1,2}(?:\.[0-9]{1,2})?)', r'Poverty\s*Rate[:\s]+([0-9]{1,2}(?:\.[0-9]{1,2})?)']:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            try:
                v = float(m.group(1))
                if 0 <= v <= 100: return str(v)
            except: pass
    return ""
def rquartile(text):
    for p in [r'Quartile[:\s]+\s*(\d)\s*(?:q|Qualified|Census)?', r'(\d)(?:st|nd|rd|th)\s+Quartile']:
        m = re.search(p, text, re.IGNORECASE)
        if m and m.group(1) in '1234': return m.group(1)
    return ""
def rproperty(text):
    for p in [r'Property\s*Tax\s*Rate[:\s]+([0-9]{1,2}(?:\.[0-9]{1,3})?)', r'Tax\s*Rate\s*per\s*\$100[:\s]+([0-9]{1,2}(?:\.[0-9]{1,3})?)']:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            try:
                float(m.group(1))
                return m.group(1)
            except: pass
    return ""

RECOVERY = {"census_tract": rcensus, "poverty_rank": rpoverty, "quartile": rquartile, "property_rate": rproperty}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=int, required=True, choices=[2,3,4,5])
    args = ap.parse_args()
    run_id = args.run

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
            r["error"] = str(e)[:200]; r["needs_review"] = True
            r["fields"] = {}; r["review_reasons"] = [f"error:{str(e)[:80]}"]; r["time_s"] = 0
        return r

    print(f"Run {run_id}/5 started — {N} PDFs, {MAX_WORKERS} workers, model={MODEL}")
    t_start = time.time()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(process_one, pdf): pdf for pdf in pdfs}
        for f in as_completed(futures):
            r = f.result()
            results.append(r)
            with lock:
                d = done[0]
            if d % 10 == 0:
                print(f"  [{d}/{N}]", flush=True)

    elapsed = time.time() - t_start
    pre_counts = dict(counts)

    # Auto-recovery
    to_fix = [(r, [k for k in RECOVERY if not r["fields"].get(k)]) for r in results]
    to_fix = [(r, m) for r, m in to_fix if m]
    if to_fix:
        def scan_fix(item):
            r, missing = item
            text = pdftotext(PDF_DIR / r["pdf"], 1, 150)
            for k in missing:
                val = RECOVERY[k](text)
                if val:
                    r["fields"][k] = val
                    with lock: counts[k] += 1
            return True
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(scan_fix, to_fix))

    post_counts = dict(counts)
    nz = N - errors
    review_count = sum(1 for r in results if r.get("needs_review"))

    run_summary = {
        "run": f"run_{run_id:02d}", "model": MODEL, "total": N, "errors": errors,
        "needs_review": review_count, "elapsed_minutes": round(elapsed / 60, 1),
        "avg_seconds_per_pdf": round(elapsed / max(N, 1), 1),
        "coverage": {
            "standard_post": round(100 * sum(post_counts[k] for k in STANDARD_KEYS) / max(nz * len(STANDARD_KEYS), 1), 1),
            "tiebreaker_names": round(100 * sum(post_counts[k] for k in TIEBREAKER_NAMES) / max(nz * 4, 1), 1),
            "amenity_coordinates": round(100 * sum(post_counts[k] for k in COORD_KEYS) / max(nz * 8, 1), 1),
            "site_distances_score": round(100 * sum(post_counts[k] for k in OTHER_PHASE2) / max(nz * 7, 1), 1),
        },
        "per_field": {k: {"pre": pre_counts.get(k,0), "post": post_counts.get(k,0),
                           "rate": round(100 * post_counts[k] / max(nz, 1), 1)} for k in ALL_FIELDS},
        "per_pdf": results,
    }

    with open(out_dir / "run_summary.json", "w") as f:
        json.dump(run_summary, f, indent=2)

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

    print(f"✅ Run {run_id} done: {elapsed/60:.1f} min | std={run_summary['coverage']['standard_post']}% | "
          f"tb_names={run_summary['coverage']['tiebreaker_names']}% | coords={run_summary['coverage']['amenity_coordinates']}% | "
          f"site+dist={run_summary['coverage']['site_distances_score']}% | errors={errors}")

if __name__ == "__main__":
    main()
