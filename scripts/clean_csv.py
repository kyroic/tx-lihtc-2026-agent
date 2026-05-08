#!/usr/bin/env python3
"""
Post-extraction cleanliness pass.

Reads an applications.csv (from any pipeline step), runs all quality checks,
normalizes values, and outputs cleaned CSV + QA report.

Usage:
  python scripts/clean_csv.py --in out_verify/applications.csv --out out_verify_clean
  python scripts/clean_csv.py --in out_benchmark/run_05/applications.csv --out out_benchmark/run_05_clean
"""
import argparse, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lihtc_tx_2026_agent.cleanliness import clean_csv


def main():
    ap = argparse.ArgumentParser(description="Clean & validate extracted CSV")
    ap.add_argument("--in", dest="in_csv", required=True, help="Input applications.csv")
    ap.add_argument("--out", dest="out_dir", required=True, help="Output directory for cleaned files")
    args = ap.parse_args()

    in_path = Path(args.in_csv).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve()

    if not in_path.exists():
        print(f"ERROR: input file not found: {in_path}")
        return 1

    summary = clean_csv(in_path, out_dir)

    print(f"\n{'='*60}")
    print(f"✅ CLEANLINESS PASS COMPLETE")
    print(f"{'='*60}")
    print(f"Rows:           {summary['total_rows']}")
    print(f"Mean quality:   {summary['mean_quality_score']:.2f}")
    print(f"Perfect (1.0):  {summary['rows_perfect']}")
    print(f"Good (0.9+):    {summary['rows_good']}")
    print(f"Fair (0.7+):    {summary['rows_fair']}")
    print(f"Poor (<0.7):    {summary['rows_poor']}")
    print(f"Duplicates:     {summary['duplicate_groups']} groups")
    print(f"\nTop issues:")
    for iss, n in list(summary['issue_frequency'].items())[:10]:
        print(f"  {iss}: {n}")
    print(f"\nOutputs:")
    for name, path in summary['outputs'].items():
        print(f"  {name}: {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
