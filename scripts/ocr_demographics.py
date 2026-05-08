#!/usr/bin/env python3
"""
Focused OCR recovery: only run on rows with missing demographic fields
(poverty_rank, quartile) where pdftotext can't extract the values.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse, base64, csv, json, re, tempfile, urllib.request
from pathlib import Path

DEMOGRAPHIC_FIELDS = {"poverty_rank", "quartile", "census_tract"}
DEMOGRAPHIC_PROMPT = """This is the Site Demographic Characteristics Report from a Texas LIHTC housing tax credit application PDF.

Extract:
- poverty_rank: The poverty rate percentage number (like 19.61 or 32.45). Look for "Poverty Rate" label.
- quartile: The census tract income quartile number (1-4). Look for "Quartile" or "Income Quartile".
- census_tract: The 11-digit census tract FIPS code (starts with "48...").

Return JSON. Only include fields clearly visible. Example: {"poverty_rank": "19.61", "quartile": "3"}"""


def find_demo_page(pdf_path: Path) -> int:
    """Find the Site Demographic Characteristics Report page using pdftotext grep."""
    import subprocess
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(pdf_path), "-"],
            capture_output=True, timeout=60,
        )
        text = result.stdout.decode("utf-8", errors="ignore")
        pages = text.split("\f")
        for i, page in enumerate(pages):
            plower = page.lower()
            if "site demographic" in plower and ("poverty" in plower or "census" in plower):
                return i + 1
    except Exception:
        pass
    return -1


def render_demo_pages(pdf_path: Path, resolution: int = 200) -> list[Path]:
    """Render demographic page + 1 page before/after."""
    import pdfplumber
    pn = find_demo_page(pdf_path)
    if pn < 1:
        return []
    images = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_num in range(max(1, pn - 1), min(len(pdf.pages), pn + 2)):
            img = pdf.pages[page_num - 1].to_image(resolution=resolution)
            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            img.save(tmp.name)
            images.append(Path(tmp.name))
    return images


def call_vision(images: list[Path], prompt: str, model: str) -> dict | None:
    """Send images to vision LLM, return parsed JSON."""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key or not images:
        return None

    content = [{"type": "text", "text": prompt}]
    for imp in images:
        with open(imp, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()
        content.append({"type": "image_url", "image_url": {
            "url": f"data:image/png;base64,{img_b64}", "detail": "high"
        }})

    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.0,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.loads(r.read())
        content_str = resp["choices"][0]["message"]["content"]
        # Parse JSON from response
        for pattern in [r'```(?:json)?\s*(\{.*?\})\s*```', r'\{.*\}']:
            m = re.search(pattern, content_str, re.DOTALL)
            if m:
                return json.loads(m.group(0) if pattern == r'\{.*\}' else m.group(1))
    except Exception as e:
        print(f"    [warn] Vision call: {e}")
    return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--in", dest="in_path", required=True)
    p.add_argument("--out", dest="out_path", required=True)
    p.add_argument("--downloads", default="downloads_challenges")
    p.add_argument("--model", default="gpt-4o")
    args = p.parse_args()

    with open(args.in_path) as f:
        rows = list(csv.DictReader(f))

    # Find rows missing poverty_rank or quartile
    targets = []
    for i, row in enumerate(rows):
        missing = set()
        for fld in DEMOGRAPHIC_FIELDS:
            v = (row.get(fld, "") or "").strip().lower()
            if v in ("", "n/a", "na", "none"):
                missing.add(fld)
        if missing:
            targets.append((i, row, missing))

    print(f"Demographic rows to OCR: {len(targets)} ({sum(len(t[2]) for t in targets)} fields)")
    sys.stdout.flush()

    filled = 0
    for ri, row, missing in targets:
        pdf_path = Path(args.downloads) / (row.get("pdf") or "").strip()
        if not pdf_path.exists():
            continue

        pdf_name = pdf_path.name
        images = render_demo_pages(pdf_path)
        if not images:
            print(f"  [{pdf_name}] no demographic page found")
            continue

        result = call_vision(images, DEMOGRAPHIC_PROMPT, args.model)
        # Clean up images
        for imp in images:
            try: imp.unlink()
            except: pass

        if result:
            row_filled = 0
            for fld in missing:
                val = str(result.get(fld, "")).strip()
                if val and val.lower() not in ("n/a", "na", "none", "null", ""):
                    row[fld] = val
                    row_filled += 1
            filled += row_filled
            if row_filled:
                print(f"  [{pdf_name}] filled {row_filled}: {', '.join(f'{k}={result[k]}' for k in missing if k in result)}")
            else:
                print(f"  [{pdf_name}] no values recovered")
        else:
            print(f"  [{pdf_name}] vision call failed")

    # Write output
    out_path = Path(args.out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nFilled {filled} demographic fields")
    print(f"Saved: {out_path}")


if __name__ == "__main__":
    main()
