"""Print the answers to the assignment's questions.

Usage (from the repository root):
    PYTHONPATH=src python3 -m pnl_ledger [--data-dir data] [--today "YYYY-MM-DD HH:MM:SS"]

The ledger is rebuilt in memory from the CSVs on every run.
"""

import argparse
from pathlib import Path

from pnl_ledger import ledger
from pnl_ledger.ingest import load_csv_dir
from pnl_ledger.schema import connect

REPO_ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", default=REPO_ROOT / "data", type=Path)
    parser.add_argument("--today", default=None, help="treat this timestamp as 'now' (default: the real current time)")
    args = parser.parse_args()

    now = args.today or ledger.current_timestamp()
    conn = connect()
    load_csv_dir(conn, args.data_dir)
    alpha = ledger.position_id_for(conn, "Alpha Corp")

    print(f"'Today' is taken as {now}\n")

    print("Q1. Alpha Corp, 15 January 2025, as known on:")
    for label, as_of in [
        ("31 Jan 2025", ledger.end_of_day("2025-01-31")),
        ("6 Mar 2025", ledger.end_of_day("2025-03-06")),
        ("13 Mar 2025", ledger.end_of_day("2025-03-13")),
        ("today", now),
    ]:
        print(f"    {label:<12} {ledger.figure_as_of(conn, alpha, '2025-01-15', as_of):>10,.2f}")

    print("\nQ2. Adjustments:")
    for a in ledger.adjustments(conn, now):
        print(f"    {a.position_id} {a.business_date}: {a.previous_amount:,.2f} -> {a.corrected_amount:,.2f}"
              f" = {a.amount:+,.2f}, booked in {a.booked_in} (received {a.recorded_at})")

    print("\nQ3. Alpha Corp by month:")
    print(f"    {'month':<8} {'reporting':>10} {'corrected':>10}")
    for month in ["2025-01", "2025-02", "2025-03"]:
        print(f"    {month:<8} {ledger.reporting_month(conn, month, alpha, now):>10,.2f}"
              f" {ledger.corrected_month(conn, month, alpha, now):>10,.2f}")

    print("\nQ4. Year to date:")
    print(f"    {'scope':<12} {'through':<8} {'reporting':>10} {'corrected':>10}")
    for scope, position_id in [("Alpha Corp", alpha), ("Whole fund", None)]:
        for month in ["2025-02", "2025-03"]:
            print(f"    {scope:<12} {month:<8}"
                  f" {ledger.year_to_date(conn, month, ledger.REPORTING, position_id, now):>10,.2f}"
                  f" {ledger.year_to_date(conn, month, ledger.CORRECTED, position_id, now):>10,.2f}")


if __name__ == "__main__":
    main()
