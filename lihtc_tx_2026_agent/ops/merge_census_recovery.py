#!/usr/bin/env python3
"""
Merge V5.9b census tract recovery results into V5.8 final output.
"""

import csv
import json
from pathlib import Path

def main():
    # Load V5.8 results
    v58_csv = Path('out_v5_8_final/aggregate/applications.csv')
    v58_rows = {}
    
    with v58_csv.open(encoding='utf-8') as f:
        for row in csv.DictReader(f):
            pdf_path = row.get('source_pdf_path', '')
            v58_rows[pdf_path] = row
    
    # Manual census tract recovery for the 24 PDFs
    # (from V5.9b scan results)
    census_recovery = {
        '0bca21faebea_26184.pdf': '48029171923',
        '1c2b3099ef49_26047.pdf': '48479001715',
        '1e3582a38bab_26043.pdf': '48309002100',
        '47277ef3e69f_26068.pdf': '48113013202',
        '6bf5cfc48b04_26032.pdf': '48029171920',
        '7d11d8d7d1df_26246.pdf': '48141010329',
        '84cc139a72eb_26097.pdf': '48283950301',
        '857b8d30fe17_26130.pdf': '48141010368',
        '8a809b5aff15_26233.pdf': '48029110100',
        '8d957fbf558b_26143.pdf': '48375010600',
        '9605e2483f99_26148.pdf': '48441011000',
        '9c26319365c0_26214.pdf': '48201231800',
        '9d47ea52c2da_26247.pdf': '48141000404',
        'a0402010b53c_26245.pdf': '48141003100',
        'af8fb403835c_26024.pdf': '48201211600',
        'bda898ee960e_26029.pdf': '48355001801',
        'c07490d037ab_26001.pdf': '48201312300',
        'c48b5d6e7be1_26066.pdf': '48201432600',
        'ce8cbba7326a_26144.pdf': '48041001702',
        'd3db51e5eec2_26052.pdf': '48215020736',
        'd514b360774b_26128.pdf': '48141010505',
        'eb0bf5080b59_26149.pdf': '48381022002',
        'f6331b3fbaf3_26127.pdf': '48141010203',
        'fcddcb74345b_26132.pdf': '48029152000',
    }
    
    # Update V5.8 rows with recovered census tracts
    updated = 0
    for pdf_name, census_tract in census_recovery.items():
        # Find the matching row
        for pdf_path, row in v58_rows.items():
            if pdf_name in pdf_path:
                row['census_tract'] = census_tract
                row['needs_review'] = 'false'
                row['review_reasons'] = ''
                updated += 1
                break
    
    print(f"Updated {updated} rows with census tract")
    
    # Write merged output
    out_dir = Path('out_v5_8_final_merged/aggregate')
    out_dir.mkdir(parents=True, exist_ok=True)
    
    # Write CSV
    fieldnames = list(v58_rows.values())[0].keys()
    with (out_dir / 'applications.csv').open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in v58_rows.values():
            writer.writerow(row)
    
    # Count stats
    needs_review = sum(1 for r in v58_rows.values() if (r.get('needs_review') or '').lower() == 'true')
    
    print(f"\nMerged output written to {out_dir}")
    print(f"Total rows: {len(v58_rows)}")
    print(f"Still needs review: {needs_review}")
    print(f"Clean rows: {len(v58_rows) - needs_review}")
    
    # Write summary
    summary = {
        "mode": "v5.8_with_v5.9b_census_recovery",
        "total_rows": len(v58_rows),
        "census_tract_recovered": updated,
        "still_needs_review": needs_review,
        "clean_rows": len(v58_rows) - needs_review,
    }
    (out_dir.parent / 'merge_summary.json').write_text(json.dumps(summary, indent=2))
    print(f"\nSummary: {json.dumps(summary, indent=2)}")

if __name__ == "__main__":
    main()
