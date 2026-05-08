#!/usr/bin/env python3
"""Full 114-PDF Phase 2 extraction via OpenAI gpt-4o-mini (fast)."""
import json, time, sys, os, re, urllib.request, urllib.error, threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── config ──────────────────────────────────────────────────
API_KEY = os.environ["OPENAI_API_KEY"]
MODEL = "gpt-4o-mini"
MAX_WORKERS = 4  # OpenAI is fast, we can parallelize more
TIMEOUT_S = 90

PDF_DIR = Path("/Users/danies/lihtc-tx-2026-agent/downloads_challenges")
OUT_DIR = Path("/Users/danies/lihtc-tx-2026-agent/test_outputs")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ── keys ─────────────────────────────────────────────────────
EXISTING_KEYS = [
    "tiebreaker_park", "tiebreaker_school",
    "tiebreaker_grocery", "tiebreaker_library"
]
COORD_KEYS = [
    "park_lat", "park_lng", "school_lat", "school_lng",
    "grocery_lat", "grocery_lng", "library_lat", "library_lng"
]
OTHER_KEYS = [
    "site_lat", "site_lng", "tiebreaker_score",
    "distance_to_park", "distance_to_school",
    "distance_to_grocery", "distance_to_library"
]
ALL_PHASE2 = COORD_KEYS + OTHER_KEYS

# ── OpenAI caller ────────────────────────────────────────────
def call_openai(messages: list[dict]) -> dict:
    body = json.dumps({
        "model": MODEL,
        "temperature": 0.0,
        "messages": messages,
        "response_format": {"type": "json_object"},
    }).encode()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}",
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=body, headers=headers, method="POST",
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
                return json.loads(r.read().decode())
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt + 1)

# ── pdfplumber extraction ────────────────────────────────────
def extract_tiebreaker_content(pdf_path: Path):
    """Returns (standard_pages, tiebreaker_pages) like the V5.8 strategy."""
    import pdfplumber

    # Find tiebreaker pages
    tiebreaker_pages = []
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for idx, page in enumerate(pdf.pages):
                try:
                    text = page.extract_text() or ""
                except Exception:
                    continue
                if re.search(
                    r"Tie-Breaker\s+Information\s*(\(Competitive\s+HTC\s+Only\))?",
                    text or "", re.IGNORECASE,
                ):
                    tiebreaker_pages.append(idx + 1)
    except Exception:
        pass

    tb_set = set(tiebreaker_pages)
    standard = []
    tiebreaker = []

    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            # First 10 standard pages
            for idx in range(min(10, len(pdf.pages))):
                pn = idx + 1
                if pn in tb_set:
                    continue
                try:
                    text = page = pdf.pages[idx]
                    text = page.extract_text() or ""
                    standard.append({"page": pn, "text": text.strip()[:4000], "tables": []})
                except Exception:
                    continue

            # Tiebreaker pages
            for pn in sorted(tb_set):
                if pn < 1 or pn > len(pdf.pages):
                    continue
                page = pdf.pages[pn - 1]
                try:
                    text = page.extract_text() or ""
                    tables = page.extract_tables() or []
                    table_text = []
                    for t in tables:
                        if t:
                            rows = [" | ".join(str(c or "") for c in row if c is not None)
                                    for row in t if row]
                            if rows:
                                table_text.append("\n".join(rows))

                    # Restructure coordinates
                    coord_summary = _build_coord_summary(tables, text)

                    tiebreaker.append({
                        "page": pn,
                        "text": text.strip()[:8000],
                        "tables": table_text,
                        "coordinate_summary": coord_summary,
                    })
                except Exception:
                    continue
    except Exception:
        pass

    return standard, tiebreaker, tiebreaker_pages


def _build_coord_summary(tables: list, full_text: str) -> str:
    coord_tables: list = []
    for t in tables:
        if not t or not t[0]:
            continue
        is_coords = (
            len(t[0]) == 2
            and all(
                re.match(r'^-?\d{1,3}\.\d+$', str(c or ''))
                for row in t if row
                for c in row if c is not None
            )
        )
        if is_coords:
            coord_tables.append(t)

    if len(coord_tables) < 2:
        return "(no coordinate tables found)"

    amenity_labels: list[str] = []
    for m in re.finditer(
        r'(Park|School|Grocery\s*Store|Library|Public\s*Library)',
        full_text, re.IGNORECASE
    ):
        label = m.group(1).strip()
        if label.lower() not in {l.lower() for l in amenity_labels}:
            amenity_labels.append(label)

    amenity_labels = amenity_labels[:4]
    if len(amenity_labels) < 4:
        amenity_labels = ["Park", "School", "Grocery", "Library"]

    lines: list[str] = []
    for i, label in enumerate(amenity_labels):
        a_idx = i * 2
        s_idx = i * 2 + 1
        a_str = s_str = ""
        if a_idx < len(coord_tables) and coord_tables[a_idx] and coord_tables[a_idx][0]:
            a_str = ", ".join(str(c or "") for c in coord_tables[a_idx][0])
        if s_idx < len(coord_tables) and coord_tables[s_idx] and coord_tables[s_idx][0]:
            s_str = ", ".join(str(c or "") for c in coord_tables[s_idx][0])
        lines.append(
            f"  {label}: amenity_lat,amenity_lng = ({a_str}) | site_lat,site_lng = ({s_str})"
        )

    if not lines:
        return "(could not parse coordinate tables)"

    return "COORDINATE TABLE (restructured):\n" + "\n".join(lines)


# ── Prompt builder ───────────────────────────────────────────
def build_extraction_prompt(std_pages, tb_pages):
    # Collect standard page text for context (app name, contact etc.)
    std_text = "\n".join(
        f"[Page {p['page']}] {p['text'][:500]}" for p in std_pages[:3]
    ) or "(no standard text)"

    # Build tiebreaker context
    tb_sections = []
    for tb in tb_pages:
        section = f"--- TIE-BREAKER PAGE {tb['page']} ---\n"
        section += f"TEXT:\n{tb['text']}\n\n"
        section += f"RESTRUCTURED COORDINATES:\n{tb.get('coordinate_summary', '(none)')}\n"
        section += f"RAW TABLES:\n"
        for i, t in enumerate(tb.get('tables', [])):
            section += f"  TABLE {i+1}:\n{t}\n"
        tb_sections.append(section)

    tb_context = "\n".join(tb_sections) if tb_sections else "(NO TIE-BREAKER PAGES FOUND)"

    system = (
        "You are an extraction agent for Texas LIHTC 2026 Full Application PDFs.\n"
        "Return ONLY valid JSON. Never invent values. If not present, use empty string and confidence 0.\n"
        "Every non-empty value MUST include pages[] and a short quote.\n"
        "Focus on tiebreaker pages for all tiebreaker_*, site_*, park_*, school_*, grocery_*, library_* fields.\n"
        "CRITICAL coordinate extraction rules:\n"
        "- site_lat / site_lng: From the 'site_lat,site_lng' column next to ANY amenity row.\n"
        "- park_lat/park_lng: From the 'amenity_lat,amenity_lng' column for the Park row.\n"
        "- school_lat/school_lng: Amenity coords for School row.\n"
        "- grocery_lat/grocery_lng: Amenity coords for Grocery row.\n"
        "- library_lat/library_lng: Amenity coords for Library row.\n"
        "- distance_to_*: ONLY extract if a distance is EXPLICITLY stated (e.g. '0.3 miles'). If not shown, leave empty. Do NOT copy coordinates into distance fields.\n"
        "- tiebreaker_score: Look for an aggregate/total score. If not on tiebreaker pages, leave empty.\n"
        "- Coordinates are decimal degrees like '29.737173', '-95.363649'. Copy exactly.\n"
    )

    user = (
        f"STANDARD PAGES (first pages for context):\n{std_text}\n\n"
        f"TIE-BREAKER PAGES:\n{tb_context}\n\n"
        f"Extract these fields as JSON. For each field: value, confidence (0-100), pages (array), quote (short).\n"
        f"Return:\n"
        f'{{"application_name":..., "census_tract":..., "poverty_rank":..., "quartile":..., '
        f'"property_rate":..., "tiebreaker_park":..., "tiebreaker_school":..., '
        f'"tiebreaker_grocery":..., "tiebreaker_library":..., "contact_email":..., '
        f'"site_lat":..., "site_lng":..., "tiebreaker_score":..., '
        f'"distance_to_park":..., "park_lat":..., "park_lng":..., '
        f'"distance_to_school":..., "school_lat":..., "school_lng":..., '
        f'"distance_to_grocery":..., "grocery_lat":..., "grocery_lng":..., '
        f'"distance_to_library":..., "library_lat":..., "library_lng":...}}'
    )

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


# ── Result parsing ───────────────────────────────────────────
def parse_result(raw: dict) -> dict[str, str | None]:
    """Extract flat string values from OpenAI response."""
    content = raw.get("choices", [{}])[0].get("message", {}).get("content", "")
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        # Try to extract JSON from markdown
        m = re.search(r'\{[\s\S]*\}', content)
        if m:
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError:
                return {}
        else:
            return {}

    result = {}
    for k in EXISTING_KEYS + ALL_PHASE2:
        field = data.get(k, {})
        if isinstance(field, dict):
            result[k] = (field.get("value") or "").strip() or None
        elif isinstance(field, str):
            result[k] = field.strip() or None
        else:
            result[k] = None
    return result


# ── Main scan ─────────────────────────────────────────────────
counts = {k: 0 for k in EXISTING_KEYS + ALL_PHASE2}
results = []
errors = 0
lock = threading.Lock()
done = [0]

def process_one(pdf: Path) -> dict:
    global errors
    result = {"pdf": pdf.name, "tb_pages": []}

    try:
        std, tie, tb_nums = extract_tiebreaker_content(pdf)
        result["tb_pages"] = tb_nums

        t0 = time.time()
        messages = build_extraction_prompt(std, tie)
        raw = call_openai(messages)
        elapsed = time.time() - t0

        fields = parse_result(raw)
        result["fields"] = fields
        result["time_s"] = round(elapsed, 1)

        with lock:
            for k in EXISTING_KEYS + ALL_PHASE2:
                if fields.get(k):
                    counts[k] += 1
            done[0] += 1

        ex_f = sum(1 for k in EXISTING_KEYS if fields.get(k))
        p2_f = sum(1 for k in ALL_PHASE2 if fields.get(k))
        print(f"[{done[0]}/{len(pdfs)}] {pdf.name}: TB={len(tb_nums)} | "
              f"existing={ex_f}/4 | phase2={p2_f}/15 | {elapsed:.0f}s")

    except Exception as e:
        with lock:
            done[0] += 1
            errors += 1
        result["error"] = str(e)[:200]
        print(f"[{done[0]}/{len(pdfs)}] {pdf.name}: ERROR - {e}")

    return result


# ── Go ────────────────────────────────────────────────────────
pdfs = sorted(PDF_DIR.glob("*.pdf"))
N = len(pdfs)

print(f"Phase 2 full scan: {N} PDFs, model={MODEL}, {MAX_WORKERS} workers (OpenAI)")
print(f"Output: {OUT_DIR / 'phase2_114_scan_results.json'}")
print()

t_start = time.time()

with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
    futures = {pool.submit(process_one, pdf): pdf for pdf in pdfs}
    for f in as_completed(futures):
        results.append(f.result())

elapsed = time.time() - t_start

# ── Summary ───────────────────────────────────────────────────
print(f"\n{'='*70}")
print(f"COMPLETE: {N} PDFs in {elapsed/60:.1f} min ({elapsed/N:.1f}s avg per PDF)")
print(f"{'='*70}")

n = N
ex_t = sum(counts[k] for k in EXISTING_KEYS)
cd_t = sum(counts[k] for k in COORD_KEYS)
ot_t = sum(counts[k] for k in OTHER_KEYS)

print(f"\n--- EXISTING TIEBREAKER NAMES ---")
for k in EXISTING_KEYS:
    print(f"  {k}: {counts[k]}/{n} ({100*counts[k]/n:.0f}%)")
print(f"  TOTAL: {ex_t}/{n*4} ({100*ex_t/(n*4):.0f}%)")

print(f"\n--- PHASE 2 AMENITY COORDINATES ---")
for k in COORD_KEYS:
    print(f"  {k}: {counts[k]}/{n} ({100*counts[k]/n:.0f}%)")
print(f"  TOTAL: {cd_t}/{n*8} ({100*cd_t/(n*8):.0f}%)")

print(f"\n--- PHASE 2 SITE + DISTANCES + SCORE ---")
for k in OTHER_KEYS:
    print(f"  {k}: {counts[k]}/{n} ({100*counts[k]/n:.0f}%)")
print(f"  TOTAL: {ot_t}/{n*7} ({100*ot_t/(n*7):.0f}%)")

print(f"\n--- DELTA ---")
print(f"  Names:            {100*ex_t/(n*4):.0f}%")
print(f"  Amenity coords:   {100*cd_t/(n*8):.0f}%")
print(f"  Site coords:      {100*(counts['site_lat']+counts['site_lng'])/(n*2):.0f}%")
print(f"  Δ (names-coords): {100*ex_t/(n*4) - 100*cd_t/(n*8):.0f}pp")
print(f"  Errors:           {errors}")

summary = {
    "model": MODEL,
    "total_pdfs": N,
    "errors": errors,
    "elapsed_minutes": round(elapsed / 60, 1),
    "avg_seconds_per_pdf": round(elapsed / N, 1),
    "counts": counts,
    "rates": {
        "existing_names": round(100 * ex_t / (n * 4), 1),
        "amenity_coordinates": round(100 * cd_t / (n * 8), 1),
        "site_coordinates": round(100 * (counts["site_lat"] + counts["site_lng"]) / (n * 2), 1),
        "delta_coords_vs_names_pp": round(100 * ex_t / (n * 4) - 100 * cd_t / (n * 8), 1),
    },
    "per_pdf": results,
}

out_path = OUT_DIR / "phase2_114_scan_results.json"
with open(out_path, "w") as f:
    json.dump(summary, f, indent=2)

# CSV
csv_path = OUT_DIR / "phase2_114_scan.csv"
with open(csv_path, "w") as f:
    all_k = ["pdf"] + EXISTING_KEYS + ALL_PHASE2 + ["tb_pages", "time_s", "error"]
    f.write(",".join(all_k) + "\n")
    for r in results:
        v = [r["pdf"]]
        for k in EXISTING_KEYS + ALL_PHASE2:
            v.append((r.get("fields", {}).get(k) or "").replace(",", ";"))
        v.append(str(len(r.get("tb_pages", []))))
        v.append(str(r.get("time_s", "")))
        v.append((r.get("error", "") or "").replace(",", ";"))
        f.write(",".join(f'"{x}"' for x in v) + "\n")

print(f"\nJSON: {out_path}")
print(f"CSV:  {csv_path}")
