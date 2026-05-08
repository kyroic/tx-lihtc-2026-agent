from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

# ── Texas bounds ──
TX_LAT_RANGE = (25.5, 36.5)
TX_LNG_RANGE = (-107.0, -93.0)

# ── Known garbage markers (from prior project lessons) ──
GARBAGE_MARKERS = [
    "certification", "organization chart", "information page",
    "eligibility certification", "signature staff initials",
    "without the previous", "is also affirming",
]

def _is_empty(v: str | None) -> bool:
    return not (v or "").strip()


# ═══════════════════════════════════════════════════════════════════
# 1. PHONE NORMALIZATION
# ═══════════════════════════════════════════════════════════════════

def normalize_phone(raw: str) -> str:
    """Normalize to (XXX) XXX-XXXX. Returns empty if unparseable."""
    if not raw or not raw.strip():
        return ""
    digits = re.sub(r"[^\d]", "", raw.strip())
    # Handle leading country code
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return ""  # flag as bad
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"


def phone_issues(raw: str) -> list[str]:
    """Return issues with a phone value."""
    issues: list[str] = []
    if _is_empty(raw):
        issues.append("phone_missing")
        return issues
    digits = re.sub(r"[^\d]", "", raw.strip())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        issues.append(f"phone_invalid_digits:{len(digits)}")
    normalized = normalize_phone(raw)
    if normalized and normalized != raw:
        issues.append("phone_reformatted")
    return issues


# ═══════════════════════════════════════════════════════════════════
# 2. EMAIL VALIDATION
# ═══════════════════════════════════════════════════════════════════

def email_issues(raw: str) -> list[str]:
    issues: list[str] = []
    if _is_empty(raw):
        issues.append("email_missing")
        return issues
    e = raw.strip().lower()
    if "@" not in e:
        issues.append("email_no_at")
    elif not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", e):
        issues.append("email_malformed")
    # Common typos
    for typo in [".con", ".cm", ".met", ".or"]:
        if e.endswith(typo):
            issues.append(f"email_suspicious_tld:{typo}")
            break
    return issues


# ═══════════════════════════════════════════════════════════════════
# 3. GARBAGE / ARTIFACT DETECTION (company/project names)
# ═══════════════════════════════════════════════════════════════════

def is_garbage_name(name: str) -> bool:
    """Check if a name looks like a PDF artifact, not a real name."""
    n = name.strip().lower()
    if len(n) < 3:
        return True
    for marker in GARBAGE_MARKERS:
        if marker in n:
            return True
    # All dots (like "................................................")
    if re.match(r"^[.\s]+$", n):
        return True
    # Mostly non-letter
    letters = sum(1 for c in n if c.isalpha())
    if len(n) > 3 and letters < len(n) * 0.3:
        return True
    return False


def name_issues(raw: str, *, field: str = "name") -> list[str]:
    issues: list[str] = []
    if _is_empty(raw):
        issues.append(f"{field}_missing")
        return issues
    n = raw.strip()
    if is_garbage_name(n):
        issues.append(f"{field}_garbage")
    if len(n) < 3:
        issues.append(f"{field}_too_short")
    return issues


# ═══════════════════════════════════════════════════════════════════
# 4. NUMERIC VALIDATION
# ═══════════════════════════════════════════════════════════════════

def is_numeric(v: str) -> bool:
    try:
        float(v)
        return True
    except (ValueError, TypeError):
        return False


def strip_na(v: str) -> str:
    """Replace 'N/A' variants with empty string."""
    t = v.strip()
    if t.lower() in ("n/a", "na", "n.a.", "n/a.", "none", "null"):
        return ""
    return v


def quartile_issues(raw: str) -> list[str]:
    issues: list[str] = []
    if _is_empty(raw):
        return issues  # not necessarily an issue, common
    clean = strip_na(raw)
    if not clean:
        issues.append("quartile_na_stripped")
        return issues
    if not is_numeric(clean):
        issues.append("quartile_non_numeric")
        return issues
    q = int(float(clean))
    if q not in (1, 2, 3, 4):
        issues.append(f"quartile_out_of_range:{q}")
    return issues

def poverty_issues(raw: str) -> list[str]:
    issues: list[str] = []
    if _is_empty(raw):
        return issues
    clean = strip_na(raw)
    if not clean:
        issues.append("poverty_na_stripped")
        return issues
    if not is_numeric(clean):
        issues.append("poverty_non_numeric")
        return issues
    v = float(clean)
    if v < 0 or v > 100:
        issues.append(f"poverty_out_of_range:{v}")
    return issues

def property_rate_issues(raw: str) -> list[str]:
    issues: list[str] = []
    if _is_empty(raw):
        return issues
    clean = strip_na(raw)
    if not clean:
        issues.append("property_rate_na_stripped")
        return issues
    if not is_numeric(clean):
        issues.append("property_rate_non_numeric")
        return issues
    v = float(clean)
    if v < 0 or v > 30:
        issues.append(f"property_rate_out_of_range:{v}")
    return issues

def score_issues(raw: str) -> list[str]:
    issues: list[str] = []
    if _is_empty(raw):
        issues.append("score_missing")
        return issues
    if not is_numeric(raw.strip()):
        issues.append("score_non_numeric")
    return issues


# ═══════════════════════════════════════════════════════════════════
# 5. COORDINATE VALIDATION
# ═══════════════════════════════════════════════════════════════════

# Unicode hyphens/minus that aren't ASCII 0x2D
_UNICODE_HYPHENS = re.compile("[\u2010\u2011\u2012\u2013\u2014\u2212\uff0d]")

def fix_unicode_minus(v: str) -> str:
    """Replace unicode hyphens/dashes with ASCII minus."""
    return _UNICODE_HYPHENS.sub("-", v)


def coord_in_bounds(lat: float, lng: float) -> bool:
    return (TX_LAT_RANGE[0] <= lat <= TX_LAT_RANGE[1] and
            TX_LNG_RANGE[0] <= lng <= TX_LNG_RANGE[1])


def coord_issues(lat_raw: str, lng_raw: str, label: str = "coord") -> list[str]:
    issues: list[str] = []
    lat = fix_unicode_minus(lat_raw.strip()) if lat_raw else ""
    lng = fix_unicode_minus(lng_raw.strip()) if lng_raw else ""

    # Unicode fix needed?
    if _UNICODE_HYPHENS.search(lat_raw or ""):
        issues.append(f"{label}_unicode_minus_fixed")
    if _UNICODE_HYPHENS.search(lng_raw or ""):
        issues.append(f"{label}_unicode_minus_fixed")

    if not lat and not lng:
        issues.append(f"{label}_missing")
        return issues
    if not lat or not lng:
        issues.append(f"{label}_incomplete_pair")
        return issues

    if not is_numeric(lat) or not is_numeric(lng):
        issues.append(f"{label}_non_numeric")
        return issues

    lat_f, lng_f = float(lat), float(lng)
    if not coord_in_bounds(lat_f, lng_f):
        issues.append(f"{label}_out_of_bounds:{lat_f},{lng_f}")
    return issues


# ═══════════════════════════════════════════════════════════════════
# 6. DISTANCE VALIDATION
# ═══════════════════════════════════════════════════════════════════

def distance_issues(raw: str, label: str = "distance") -> list[str]:
    issues: list[str] = []
    if _is_empty(raw):
        issues.append(f"{label}_missing")
        return issues
    if not is_numeric(raw.strip()):
        issues.append(f"{label}_non_numeric")
        return issues
    v = float(raw.strip())
    if v < 0:
        issues.append(f"{label}_negative")
    if v == 0:
        issues.append(f"{label}_zero")
    return issues


# ═══════════════════════════════════════════════════════════════════
# 7. CENSUS TRACT VALIDATION
# ═══════════════════════════════════════════════════════════════════

def census_tract_issues(raw: str) -> list[str]:
    issues: list[str] = []
    if _is_empty(raw):
        issues.append("census_tract_missing")
        return issues
    digits = re.sub(r"[^\d]", "", raw.strip())
    if len(digits) < 9:
        issues.append(f"census_tract_short:{len(digits)}digits")
    # TX GEOID starts with 48, flag if it doesn't
    if len(digits) >= 11 and not digits.startswith("48"):
        issues.append("census_tract_not_texas_geoid")
    return issues


# ═══════════════════════════════════════════════════════════════════
# 8. DUPLICATE DETECTION
# ═══════════════════════════════════════════════════════════════════

def find_duplicates(rows: list[dict[str, str]], key: str = "application_name") -> dict[str, list[int]]:
    """Return {name: [row_indexes]} for duplicates."""
    seen: dict[str, list[int]] = {}
    for i, row in enumerate(rows):
        v = (row.get(key) or "").strip()
        if v:
            seen.setdefault(v, []).append(i)
    return {k: idxs for k, idxs in seen.items() if len(idxs) > 1}


# ═══════════════════════════════════════════════════════════════════
# 9. ZERO-DISTANCE = SAME COORDS DETECTION
# ═══════════════════════════════════════════════════════════════════

def zero_distance_coords_check(row: dict[str, str]) -> list[str]:
    """
    Check when amenity coords identical to site coords.

    IMPORTANT: The TDHCA PDFs themselves often list amenity coords as identical
    to site coords when distance is 0ft (same parcel boundary). This is NOT an
    LLM hallucination — the LLM is faithfully extracting what's in the PDF.
    We flag these as "needs_geocode" rather than "copy_paste" since the source
    data itself needs enrichment with a real geocoder.
    """
    issues: list[str] = []
    site_lat = (row.get("site_lat") or "").strip()
    site_lng = (row.get("site_lng") or "").strip()
    if not site_lat or not site_lng:
        return issues

    for amenity in ["park", "school", "grocery", "library"]:
        alat = (row.get(f"{amenity}_lat") or "").strip()
        alng = (row.get(f"{amenity}_lng") or "").strip()
        if not alat or not alng:
            continue
        if alat == site_lat and alng == site_lng:
            dist = (row.get(f"distance_to_{amenity}") or "").strip()
            try:
                dist_ft = float(dist)
            except (ValueError, TypeError):
                dist_ft = -1
            if dist_ft == 0:
                # PDF source data says 0ft — same parcel boundary, not LLM error
                issues.append(f"needs_geocode:{amenity}")
            else:
                # Distance > 0 but coords identical — likely LLM copy-paste
                issues.append(f"coords_copy_paste:{amenity}")
    return issues


# ═══════════════════════════════════════════════════════════════════
# 10. FULL ROW SCAN (ALL CHECKS AT ONCE)
# ═══════════════════════════════════════════════════════════════════

def scan_row(row: dict[str, str]) -> dict[str, Any]:
    """Run all cleanliness checks on one row. Returns {issues, cleaned_fields, quality_score}."""

    issues: list[str] = []
    cleaned: dict[str, str] = {}

    # ── Application name ──
    raw = (row.get("application_name") or "")
    issues.extend(name_issues(raw, field="app_name"))
    cleaned["application_name"] = "" if is_garbage_name(raw) else raw.strip()

    # ── Contact name ──
    raw = (row.get("contact_name") or "")
    issues.extend(name_issues(raw, field="contact_name"))
    cleaned["contact_name"] = "" if is_garbage_name(raw) else raw.strip()

    # ── Email ──
    raw = (row.get("contact_email") or "")
    issues.extend(email_issues(raw))
    cleaned["contact_email"] = raw.strip()

    # ── Phone ──
    raw = (row.get("contact_phone") or "")
    issues.extend(phone_issues(raw))
    cleaned["contact_phone"] = normalize_phone(raw)

    # ── Numerics ──
    for field in ["quartile", "poverty_rank", "property_rate"]:
        raw = row.get(field, "")
        clean = strip_na(raw)
        cleaned[field] = clean
        if field == "quartile":
            issues.extend(quartile_issues(raw))
        elif field == "poverty_rank":
            issues.extend(poverty_issues(raw))
        else:
            issues.extend(property_rate_issues(raw))

    # ── Score ──
    raw = (row.get("tiebreaker_score") or "")
    issues.extend(score_issues(raw))
    cleaned["tiebreaker_score"] = raw.strip()

    # ── Census tract ──
    raw = (row.get("census_tract") or "")
    issues.extend(census_tract_issues(raw))
    cleaned["census_tract"] = raw.strip()

    # ── Coords (fix unicode minus + validate) ──
    for prefix in ["site", "park", "school", "grocery", "library"]:
        lat_raw = row.get(f"{prefix}_lat", "")
        lng_raw = row.get(f"{prefix}_lng", "")
        issues.extend(coord_issues(lat_raw, lng_raw, label=f"{prefix}_coords"))
        cleaned[f"{prefix}_lat"] = fix_unicode_minus(lat_raw.strip())
        cleaned[f"{prefix}_lng"] = fix_unicode_minus(lng_raw.strip())

    # ── Distances ──
    for prefix in ["distance_to_park", "distance_to_school", "distance_to_grocery", "distance_to_library"]:
        raw = row.get(prefix, "")
        issues.extend(distance_issues(raw, label=prefix))
        cleaned[prefix] = raw.strip()

    # ── Zero-distance copy-paste check ──
    issues.extend(zero_distance_coords_check(row))

    # ── All other fields pass through ──
    for k, v in row.items():
        if k not in cleaned:
            cleaned[k] = v

    # Quality score: 1.0 = no issues, subtract per issue
    n_issues = len(issues)
    severity_deductions = 0
    for iss in issues:
        if "_missing" in iss:
            severity_deductions += 0.15
        elif "_garbage" in iss or "_non_numeric" in iss or "_out_of_bounds" in iss or "_malformed" in iss:
            severity_deductions += 0.10
        elif "_copy_paste" in iss or "_negative" in iss:
            severity_deductions += 0.10  # LLM error
        elif "_zero" in iss:
            severity_deductions += 0.05  # Could be correct (0ft) but needs verification
        elif "needs_geocode" in iss:
            severity_deductions += 0.03  # PDF source data limitation, not extraction error
        else:
            severity_deductions += 0.03  # warnings (reformatting, unicode fixes)

    quality_score = round(max(0.0, 1.0 - severity_deductions), 2)

    return {
        "issues": sorted(set(issues)),
        "issue_count": len(set(issues)),
        "cleaned_fields": cleaned,
        "quality_score": quality_score,
    }


# ═══════════════════════════════════════════════════════════════════
# 11. BATCH CLEAN + QA REPORT
# ═══════════════════════════════════════════════════════════════════

def clean_csv(in_path: Path, out_dir: Path) -> dict[str, Any]:
    """
    Run all cleanliness checks on a CSV, write:
      - applications_cleaned.csv (normalized data)
      - qa_report.csv (per-row issues)
      - qa_summary.json (aggregate stats)

    Returns summary dict.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, str]] = []
    with open(in_path, newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(dict(r))

    results = [scan_row(r) for r in rows]

    # ── Check duplicates ──
    dups = find_duplicates(rows, key="application_name")
    for name, idxs in dups.items():
        for i in idxs[1:]:  # first is ok, rest flagged
            results[i]["issues"].append("duplicate_application_name")
            results[i]["issue_count"] = len(results[i]["issues"])
            results[i]["quality_score"] = max(0, results[i]["quality_score"] - 0.05)

    # ── Write cleaned CSV ──
    cleaned_csv = out_dir / "applications_cleaned.csv"
    cleaned_keys = list(results[0]["cleaned_fields"].keys())
    with open(cleaned_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=cleaned_keys)
        writer.writeheader()
        for r in results:
            writer.writerow(r["cleaned_fields"])

    # ── Write QA report ──
    qa_csv = out_dir / "qa_report.csv"
    with open(qa_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["pdf", "quality_score", "issue_count", "issues", "severity"])
        writer.writeheader()
        for r, raw in zip(results, rows):
            pdf = raw.get("pdf", raw.get("source_pdf_path", ""))
            sev = "🔴" if r["quality_score"] < 0.7 else ("🟡" if r["quality_score"] < 0.9 else "🟢")
            writer.writerow({
                "pdf": Path(pdf).name,
                "quality_score": r["quality_score"],
                "issue_count": r["issue_count"],
                "issues": "; ".join(r["issues"]),
                "severity": sev,
            })

    # ── Summary ──
    all_issues: dict[str, int] = {}
    scores = []
    for r in results:
        scores.append(r["quality_score"])
        for iss in r["issues"]:
            all_issues[iss] = all_issues.get(iss, 0) + 1

    summary = {
        "total_rows": len(rows),
        "mean_quality_score": round(sum(scores) / max(len(scores), 1), 2),
        "min_quality_score": round(min(scores), 2) if scores else 0,
        "rows_perfect": sum(1 for s in scores if s >= 1.0),
        "rows_good": sum(1 for s in scores if 0.9 <= s < 1.0),
        "rows_fair": sum(1 for s in scores if 0.7 <= s < 0.9),
        "rows_poor": sum(1 for s in scores if s < 0.7),
        "issue_frequency": dict(sorted(all_issues.items(), key=lambda x: -x[1])),
        "duplicate_groups": len(dups),
        "outputs": {
            "cleaned_csv": str(cleaned_csv),
            "qa_report": str(qa_csv),
            "qa_summary": str(out_dir / "qa_summary.json"),
        },
    }

    with open(out_dir / "qa_summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    return summary
