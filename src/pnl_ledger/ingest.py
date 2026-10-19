"""Load the source CSVs into the ledger, using INSERT only.

`ingest()` takes plain lists of dicts (one dict per CSV row, keyed by the
column names given in the assignment). `load_csv_dir()` reads those dicts from
the CSV files. Keeping the two separate lets tests build small made-up
ledgers without writing files.
"""

import csv
import sqlite3
from pathlib import Path


def _read_csv(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def load_csv_dir(conn: sqlite3.Connection, data_dir: str | Path) -> None:
    """Read the five assignment CSVs from `data_dir` and ingest them."""
    data_dir = Path(data_dir)
    ingest(
        conn,
        positions=_read_csv(data_dir / "positions.csv"),
        trade_detail=_read_csv(data_dir / "trade_detail.csv"),
        daily_pnl=_read_csv(data_dir / "daily_pnl.csv"),
        corrections=_read_csv(data_dir / "pnl_corrections.csv"),
        period_status=_read_csv(data_dir / "period_status.csv"),
    )


def ingest(
    conn: sqlite3.Connection,
    positions: list[dict],
    trade_detail: list[dict],
    daily_pnl: list[dict],
    corrections: list[dict],
    period_status: list[dict],
) -> None:
    """Append all source rows to the database in one transaction."""
    with conn:
        conn.executemany(
            "INSERT INTO positions VALUES (:position_id, :security_name, :quantity)",
            positions,
        )
        conn.executemany(
            "INSERT INTO trade_detail VALUES "
            "(:position_id, :business_date, :quantity, :execution_price, :closing_price)",
            trade_detail,
        )
        conn.executemany(
            "INSERT INTO period_closures VALUES (:period, :closed_at)",
            period_status,
        )

        # Bookings and corrections go into one ledger, sorted by when we learned
        # them, so event_id order matches the real order of events.
        events = [_booking_event(row) for row in daily_pnl]
        events += [_correction_event(conn, row) for row in corrections]
        events.sort(key=lambda e: e["recorded_at"])
        conn.executemany(
            "INSERT INTO pnl_fact_events "
            "(position_id, business_date, recorded_at, event_type, pnl_amount, execution_price, note) "
            "VALUES (:position_id, :business_date, :recorded_at, :event_type, :pnl_amount, "
            ":execution_price, :note)",
            events,
        )


def _booking_event(row: dict) -> dict:
    """A daily_pnl.csv row becomes an 'original_booking' event, amount copied as-is."""
    return {
        "position_id": row["position_id"],
        "business_date": row["business_date"],
        "recorded_at": row["recorded_at"],
        "event_type": "original_booking",
        "pnl_amount": float(row["pnl_amount"]),
        "execution_price": None,
        "note": None,
    }


def _correction_event(conn: sqlite3.Connection, row: dict) -> dict:
    """A pnl_corrections.csv row becomes a 'correction' event.

    The correction only gives a new execution price, so the day's P&L is
    recomputed here, once, with the assignment's formula:
        quantity × (closing_price − execution_price)
    using quantity and closing price from trade_detail.
    """
    trade = conn.execute(
        "SELECT quantity, closing_price FROM trade_detail "
        "WHERE position_id = ? AND business_date = ?",
        (row["position_id"], row["business_date"]),
    ).fetchone()
    if trade is None:
        raise ValueError(
            f"Correction for {row['position_id']} on {row['business_date']} has no "
            "matching trade_detail row, so its P&L cannot be recomputed."
        )
    execution_price = float(row["corrected_execution_price"])
    return {
        "position_id": row["position_id"],
        "business_date": row["business_date"],
        "recorded_at": row["recorded_at"],
        "event_type": "correction",
        # Rounded to cents: binary floats give 1000 × (50 − 49.4) = 600.0000000000014.
        "pnl_amount": round(trade["quantity"] * (trade["closing_price"] - execution_price), 2),
        "execution_price": execution_price,
        "note": "recomputed from trade_detail with corrected execution price",
    }
