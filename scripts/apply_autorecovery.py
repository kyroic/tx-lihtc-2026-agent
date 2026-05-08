#!/usr/bin/env python3
"""
Auto-recovery pass: fill census_tract, poverty_rank, quartile, property_rate
from the unified scan CSV using pdftotext regex patterns.

Reads the unified CSV, patches empty fields via regex scanning of the full PDF,
and writes a completed CSV + JSONL.
"""
import csv, json, re, subprocess, sys, tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

UNIFIED_CSV = Path(__file__).resolve().parent.parent / "out_unified" / "unified_scan.csv"
PDF_DIR = Path(__file__).resolve().parent.parent / "downloads_challenges"
OUT_DIR = Path(__file__).resolve().parent.parent / "out_unified_final"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ── pdftotext helper ─────────────────────────────────────────────
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

# ── Recovery patterns ────────────────────────────────────────────
def recover_census_tract(text: str) -> str:
    for p in [r'\b(48\d{9,13})\b', r'Census\s+Tract[:\s]+(\d{6,11})', r'Tract[:\s]+(\d{6,11})']:
        m = re.findall(p, text, re.IGNORECASE)
        for hit in m:
            d = re.sub(r'[^\d]', '', str(hit))
            if len(d) >= 6:
                return d[:15]
    return ""

def recover_poverty_rate(text: str) -> str:
    for p in [
        r'Quartile:\s*[1-4]q?\s+Poverty\s*Rate[:\s]+([0-9]{1,2}(?:\.[0-9]{1,2})?)',
        r'Poverty\s*Rate[:\s]*\n\s*([0-9]{1,2}(?:\.[0-9]{1,2})?)',
        r'Poverty\s*Rate[:\s]+([0-9]{1,2}(?:\.[0-9]{1,2})?)',
    ]:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            try:
                v = float(m.group(1))
                if 0 <= v <= 100: return str(v)
            except: pass
    return ""

def recover_quartile(text: str) -> str:
    for p in [
        r'Quartile[:\s]+\s*(\d)\s*(?:q|Qualified|Census)?',
        r'(\d)(?:st|nd|rd|th)\s+Quartile',
        r'Quartile\s+(\d)',
    ]:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            v = m.group(1)
            if v in '1234': return v
    return ""

def recover_property_rate(text: str) -> str:
    for p in [
        r'Property\s*Tax\s*Rate[:\s]+([0-9]{1,2}(?:\.[0-9]{1,3})?)',
        r'Tax\s*Rate\s*per\s*\$100[:\s]+([0-9]{1,2}(?:\.[0-9]{1,3})?)',
        r'rate[:\s]+([0-9]{1,2}(?:\.[0-9]{1,3})?)\s*(?:per|/)',
    ]:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            try:
                float(m.group(1))
                return m.group(1)
            except: pass
    return ""

NEEDS = ["census_tract", "poverty_rank", "quartile", "property_rate"]
RECOVERY_FUNCS = {
    "census_tract": recover_census_tract,
    "poverty_rank": recover_poverty_rate,
    "quartile": recover_quartile,
    "property_rate": recover_property_rate,
}

# ── Load unified CSV ─────────────────────────────────────────────
with open(UNIFIED_CSV, "r") as f:
    reader = csv.DictReader(f)
    rows = list(reader)

print(f"Loaded {len(rows)} rows from unified scan")

# ── Determine which rows need recovery ───────────────────────────
to_recover = []
for row in rows:
    missing = [k for k in NEEDS if not row.get(k, "").strip()]
    if missing:
        to_recover.append({"row": row, "missing": missing, "pdf": Path(PDF_DIR) / row["pdf"]})

print(f"Rows needing recovery: {len(to_recover)}")

# ── First: one pdftotext per PDF to find everything at once ──────
def scan_pdf(item):
    pdf = item["pdf"]
    text = pdftotext(pdf, 1, 150)
    return {"row": item["row"], "missing": item["missing"], "pdf_name": pdf.name, "text": text}

t0_parallel = __import__('time').time()
results = []
with ThreadPoolExecutor(max_workers=8) as pool:
    futures = {pool.submit(scan_pdf, item): item for item in to_recover}
    for i, f in enumerate(as_completed(futures), 1):
        r = f.result()
        results.append(r)
        fixes = []
        for k in r["missing"]:
            val = RECOVERY_FUNCS[k](r["text"])
            if val:
                r["row"][k] = val
                fixes.append(f"{k}={val}")
        status = f"fixed: {', '.join(fixes)}" if fixes else "no recovery"
        print(f"[{i}/{len(to_recover)}] {r['pdf_name']}: {status}")

# ── Summary stats ────────────────────────────────────────────────
counts = {k: sum(1 for row in rows if row.get(k, "").strip()) for k in NEEDS}
print(f"\nAfter recovery:")
for k in NEEDS:
    print(f"  {k}: {counts[k]}/{len(rows)} ({100*counts[k]/len(rows):.0f}%)")

# ── Write final CSVs ─────────────────────────────────────────────
final_csv = OUT_DIR / "applications.csv"
header = list(rows[0].keys())
with open(final_csv, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=header)
    w.writeheader()
    w.writerows(rows)

# JSONL
final_jsonl = OUT_DIR / "applications.jsonl"
with open(final_jsonl, "w") as f:
    for row in rows:
        f.write(json.dumps(row) + "\n")

# Summary
summary = {
    "total": len(rows),
    "coverage": {
        k: {"count": counts[k], "rate": round(100 * counts[k] / len(rows), 1)}
        for k in NEEDS
    },
}

# Also compute Phase 2 rates
tb_names = ["tiebreaker_park", "tiebreaker_school", "tiebreaker_grocery", "tiebreaker_library"]
coord_keys = ["park_lat", "park_lng", "school_lat", "school_lng", "grocery_lat", "grocery_lng", "library_lat", "library_lng"]
other_keys = ["site_lat", "site_lng", "tiebreaker_score", "distance_to_park", "distance_to_school", "distance_to_grocery", "distance_to_library"]

for group_name, group_keys in [("tiebreaker_names", tb_names), ("amenity_coordinates", coord_keys), ("site_distances_score", other_keys)]:
    hits = sum(1 for row in rows if any(row.get(k, "").strip() for k in group_keys))
    summary["coverage"][group_name] = {"fully_extracted": hits, "rate": round(100 * hits / len(rows), 1)}

summary["files"] = {
    "csv": str(final_csv),
    "jsonl": str(final_jsonl),
}

summary_path = OUT_DIR / "summary.json"
with open(summary_path, "w") as f:
    json.dump(summary, f, indent=2)

print(f"\nFinal outputs:")
print(f"  CSV:  {final_csv}")
print(f"  JSONL: {final_jsonl}")
print(f"  Summary: {summary_path}")
print(json.dumps(summary, indent=2))
