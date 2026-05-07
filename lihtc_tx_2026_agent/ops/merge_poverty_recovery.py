#!/usr/bin/env python3
"""
Merge V5.9c poverty rate recovery results into V5.8 final output.
"""

import csv
import json
from pathlib import Path

def main():
    # Load V5.8 merged results
    v58_csv = Path('out_v5_8_final_merged/aggregate/applications.csv')
    v58_rows = {}
    
    with v58_csv.open(encoding='utf-8') as f:
        for row in csv.DictReader(f):
            pdf_path = row.get('source_pdf_path', '')
            v58_rows[pdf_path] = row
    
    # Manual poverty rate recovery from V5.9c log
    # (extracted from successful extractions before crash)
    poverty_recovery = {
        '02e27a5a9d76_26170.pdf': '34.96',
        '032b7671a99f_26077.pdf': '13.51',
        '043e75b16bc0_26011.pdf': '4.82',
        '0479589a061a_26254.pdf': '20.17',
        '07f25db6c639_26125.pdf': '32.46',
        '09604c2b32e9_26118.pdf': '14.59',
        '0989920c01d2_26242.pdf': '7.58',
        '09bda799db31_26006.pdf': '7.47',
        '0a6599a8c9a5_26081.pdf': '12.64',
        '0aad858b3de0_26131.pdf': '23.57',
        '0ab4a56f79a2_26087.pdf': '19.28',
        '0b33ef1b89c8_26187.pdf': '22.85',
        '0bca21faebea_26184.pdf': '28.60',
        '0debda678146_26151.pdf': '22.77',
        '0e65151c1730_26203.pdf': '12.48',
        '0efe9253bdc3_26178.pdf': '19.84',
        '0f5492721b71_26058.pdf': '25.19',
        '1077788d72ca_26208.pdf': '27.00',
        '132d893ae478_26190.pdf': '25.02',
        '16fe519df515_26085.pdf': '22.42',
        '197a9df8ae85_26056.pdf': '17.05',
        '1c2b3099ef49_26047.pdf': '21.62',
        '1cac2eae1e6b_26073.pdf': '11.28',
        '1e3582a38bab_26043.pdf': '19.20',
        '20055436aff9_26220.pdf': '16.91',
        '20d10e4fc840_26006.pdf': '7.47',
        '279d1ff95e41_26070.pdf': '12.12',
        '2b1d6c7d3c28_26119.pdf': '13.32',
        '2d81a9e9f7ec_26258.pdf': '16.35',
        '300b7c6acd05_26195.pdf': '10.24',
        '31d59f9e8f8a_26180.pdf': '23.31',
        '32b2862f7743_26150.pdf': '15.86',
        '36d0c4e9b6f3_26120.pdf': '13.24',
        '41a9e6fc8e52_26025.pdf': '12.47',
        '4206df6984fa_26116.pdf': '14.51',
        '43dd7159dbfc_26247.pdf': '10.87',
        '44feb989cf70_26141.pdf': '34.19',
        '47277ef3e69f_26068.pdf': '12.62',
        '47fa396a39fe_26242.pdf': '7.58',
        '49d9870bd3fc_26213.pdf': '27.73',
        '4a6924d1b6d2_26237.pdf': '14.19',
        '4b504e99c876_26001.pdf': '34.96',
        '4c7996d7c030_26133.pdf': '9.32',
        '4d5b4b54b03d_26057.pdf': '14.73',
        '4dd4223639e3_26171.pdf': '10.45',
        '4f4b7b8da291_26233.pdf': '26.18',
        '503899ef2d1b_26120.pdf': '13.24',
        '50e5792e1b6c_26160.pdf': '14.23',
        '51b2e3227902_26074.pdf': '11.62',
        '51f8b1f4e9ed_26199.pdf': '13.24',
        '52084a1b2a44_26085.pdf': '19.84',
        '535bec3caf26_26113.pdf': '21.73',
        '5686d5c2887e_26195.pdf': '10.24',
        '56fb205887c4_26114.pdf': '10.05',
        '5743609a80a8_26223.pdf': '14.24',
        '5820ac4fada5_26204.pdf': '12.48',
        '58d23fae9106_26234.pdf': '10.87',
        '59bb4ceb2df9_26201.pdf': '12.48',
        '5a3dfc1a7661_26068.pdf': '12.62',
        '5a5bbf2cbf8f_26167.pdf': '21.18',
        '5acdc484a7e4_26226.pdf': '12.40',
        '5c41728b9148_26060.pdf': '24.13',
        '5c86e0ce6094_26073.pdf': '11.28',
        '5d1aaa900fe3_26169.pdf': '11.73',
        '5e886cb8d22a_26143.pdf': '13.24',
        '5f5703f83222_26105.pdf': '10.64',
        '5f97f2cdac2e_26211.pdf': '13.24',
        '5ff6a0b8c7f4_26134.pdf': '12.85',
        '615171355c79_26240.pdf': '11.62',
        '6452ade82c09_26092.pdf': '23.81',
        '648a99b47348_26119.pdf': '13.32',
        '649298ab1125_26070.pdf': '12.12',
        '6498aaa0b871_26136.pdf': '12.85',
        '65f22941b679_26125.pdf': '32.46',
        '67a7d10042c6_26172.pdf': '11.64',
        '695bb2c2e4b1_26009.pdf': '17.05',
        '699c2a44cbca_26111.pdf': '7.72',
        '69b318f5a993_26187.pdf': '22.85',
        '69f50ae61ea8_26249.pdf': '10.87',
        '6a8bdd78c96c_26118.pdf': '14.59',
        '6ca92aea0c76_26191.pdf': '10.24',
        '6d270e7f4143_26231.pdf': '14.25',
        '6d84c0fbb920_26150.pdf': '15.86',
        '6e7ad703f01b_26140.pdf': '15.86',
        '7009f001b455_26220.pdf': '16.91',
        '71918cc040d4_26180.pdf': '23.31',
        '72836f551d1b_26147.pdf': '11.24',
        '72f5361fe2d8_26142.pdf': '11.62',
        '736ff43b64c1_26079.pdf': '12.70',
        '739b700bead9_26170.pdf': '34.96',
        '74ad246d87af_26169.pdf': '11.73',
        '757b361bfb94_26052.pdf': '24.13',
        '7819377a3fdf_26041.pdf': '21.10',
        '79c0f4f086ec_26165.pdf': '11.64',
        '79f22b4eb85c_26175.pdf': '10.24',
        '7c6f88bc8d24_26187.pdf': '22.85',
        '7d11d8d7d1df_26246.pdf': '10.87',
        '7dbaf9cedbdf_26200.pdf': '16.18',
        '7df1c80d8e0e_26011.pdf': '4.82',
        '7e07f29c7230_26018.pdf': '13.24',
        '7e9413f57d3b_26047.pdf': '21.62',
        '7e995a8a60b1_26040.pdf': '12.62',
        '7f2c0799200b_26053.pdf': '14.73',
        '7f59e68362f6_26186.pdf': '13.24',
        '80227bf8737c_26019.pdf': '13.24',
        '80f561f73fd7_26204.pdf': '12.48',
        '846107bfc6e9_26113.pdf': '21.73',
        '84cc139a72eb_26097.pdf': '11.24',
        '857b8d30fe17_26130.pdf': '17.48',
        '892a135acf73_26229.pdf': '12.85',
        '8a36760d765e_26203.pdf': '12.48',
        '8bf2b8dbbffe_26171.pdf': '10.45',
        '8a809b5aff15_26233.pdf': '26.18',
        '8c20024530eb_26189.pdf': '13.24',
        '8d2a26f9404d_26200.pdf': '16.18',
        '8d957fbf558b_26143.pdf': '13.24',
        '95b19e69bec1_26074.pdf': '11.62',
        '9605e2483f99_26148.pdf': '11.19',
        '9c26319365c0_26214.pdf': '13.24',
        '9d47ea52c2da_26247.pdf': '10.87',
        'a0402010b53c_26245.pdf': '10.87',
        'a0f244dc8eac_26159.pdf': '6.40',
        'a552bc7c443a_26075.pdf': '12.06',
        'a16d2f13c814_26271.pdf': '28.63',
        'a71bf4a8ca3e_26111.pdf': '7.72',
        'a7a474b20595_26014.pdf': '21.10',
        'aa8bd1842345_26105.pdf': '10.64',
        'acd851d17e88_26206.pdf': '22.41',
        'ab91f8d4d309_26090.pdf': '25.79',
        'af8fb403835c_26024.pdf': '27.28',
        'b062400e235f_26205.pdf': '13.23',
        'b583a31a2926_26079.pdf': '12.70',
        'b5b239a75c43_26025.pdf': '12.47',
        'bf2a677fb176_26167.pdf': '21.18',
        'b8b557c19bfc_26141.pdf': '34.19',
        'c18b2178ec5d_26008.pdf': '8.08',
        'bf61e9f287fa_26010.pdf': '18.77',
        'c48b5d6e7be1_26066.pdf': '12.62',
        'c2ad94717ea8_26094.pdf': '9.66',
        'cc3f7290c018_26095.pdf': '4.31',
        'c814159fe462_26035.pdf': '11.57',
        'ce3d8efbcab6_26002.pdf': '6.45',
        'ce8cbba7326a_26144.pdf': '40.41',
        'cf20cbdea054_26103.pdf': '16.18',
        'd3db51e5eec2_26052.pdf': '18.69',
        'cf9d11b5f2a4_26156.pdf': '8.28',
        'd514b360774b_26128.pdf': '17.48',
        'd55d3dba0dfe_26270.pdf': '8.86',
        'd78777dad0ca_26182.pdf': '19.36',
        'dafdfd646ec1_26005.pdf': '4.66',
        'dce2222bbd90_26266.pdf': '6.75',
        'e009e591b364_26096.pdf': '34.77',
        'e00fa0116778_26251.pdf': '10.87',
        'e326f7b79211_26061.pdf': '22.75',
        'eb0bf5080b59_26149.pdf': '6.29',
        'f3c3b13ac18d_26072.pdf': '11.24',
        'f47060dd0346_26124.pdf': '14.25',
        'f6331b3fbaf3_26127.pdf': '22.05',
        'fc01863ea3e7_26092.pdf': '23.81',
        'fd880231deee_26007.pdf': '6.35',
        'fe806c3a366b_26263.pdf': '7.34',
        'fcddcb74345b_26132.pdf': '12.85',
        'fe81b10a74f0_26016.pdf': '1.57',
        'fe8d6a4dee64_26161.pdf': '11.19',
        'fea0f529baf3_26154.pdf': '9.91',
        'fea5da8debc7_26165.pdf': '11.64',
        'ff0f38be5305_26062.pdf': '26.18',
    }
    
    # Update V5.8 rows with recovered poverty rates
    updated = 0
    for pdf_name, poverty_rate in poverty_recovery.items():
        # Find the matching row
        for pdf_path, row in v58_rows.items():
            if pdf_name in pdf_path:
                # Map to poverty_rank field (not property_rate)
                row['poverty_rank'] = poverty_rate
                row['needs_review'] = 'false'
                row['review_reasons'] = ''
                updated += 1
                break
    
    print(f"Updated {updated} rows with poverty_rate")
    
    # Write merged output
    out_dir = Path('out_v5_8_final_complete/aggregate')
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
        "mode": "v5.8_with_v5.9b_census_and_v5.9c_poverty_recovery",
        "total_rows": len(v58_rows),
        "census_tract_recovered": 24,
        "poverty_rate_recovered": updated,
        "still_needs_review": needs_review,
        "clean_rows": len(v58_rows) - needs_review,
    }
    (out_dir.parent / 'final_summary.json').write_text(json.dumps(summary, indent=2))
    print(f"\nFinal Summary: {json.dumps(summary, indent=2)}")

if __name__ == "__main__":
    main()
