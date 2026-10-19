"""Database schema for the bitemporal P&L ledger.

Every table here is append-only: SQLite triggers reject any UPDATE or DELETE,
so history can only grow. See docs/architecture.md §2 for the reasoning.

Timestamps are stored as ISO-8601 text ("YYYY-MM-DD HH:MM:SS") and dates as
"YYYY-MM-DD". In that format, plain string comparison sorts chronologically,
which is what lets the queries in ledger.py compare times with `<=` and `<`.
"""

import sqlite3

TABLES_DDL = """
-- Reference data: the positions the fund holds.
CREATE TABLE IF NOT EXISTS positions (
    position_id   TEXT PRIMARY KEY,
    security_name TEXT NOT NULL,
    quantity      REAL NOT NULL
);

-- Reference data: trade inputs, used to recompute P&L when a price is corrected.
CREATE TABLE IF NOT EXISTS trade_detail (
    position_id     TEXT NOT NULL REFERENCES positions(position_id),
    business_date   TEXT NOT NULL,
    quantity        REAL NOT NULL,
    execution_price REAL NOT NULL,
    closing_price   REAL NOT NULL,
    PRIMARY KEY (position_id, business_date)
);

-- The ledger. One row per fact we learned, in the order we learned it.
--   business_date = valid time       (the day the figure is about)
--   recorded_at   = transaction time (when we learned the figure)
CREATE TABLE IF NOT EXISTS pnl_fact_events (
    event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id     TEXT NOT NULL REFERENCES positions(position_id),
    business_date   TEXT NOT NULL,
    recorded_at     TEXT NOT NULL,
    event_type      TEXT NOT NULL CHECK (event_type IN ('original_booking', 'correction')),
    pnl_amount      REAL NOT NULL,
    execution_price REAL,  -- set on corrections only, so the figure can be traced back
    note            TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_bitemporal
    ON pnl_fact_events (position_id, business_date, recorded_at);

-- When each month was closed and reported to investors.
CREATE TABLE IF NOT EXISTS period_closures (
    period    TEXT PRIMARY KEY,  -- 'YYYY-MM'
    closed_at TEXT NOT NULL
);
"""

APPEND_ONLY_TABLES = ("positions", "trade_detail", "pnl_fact_events", "period_closures")


def _append_only_triggers(table: str) -> str:
    return f"""
CREATE TRIGGER IF NOT EXISTS {table}_no_update BEFORE UPDATE ON {table}
BEGIN SELECT RAISE(ABORT, '{table} is append-only: UPDATE forbidden'); END;

CREATE TRIGGER IF NOT EXISTS {table}_no_delete BEFORE DELETE ON {table}
BEGIN SELECT RAISE(ABORT, '{table} is append-only: DELETE forbidden'); END;
"""


def connect(path: str = ":memory:") -> sqlite3.Connection:
    """Open (or create) a ledger database and make sure the schema exists.

    The default ":memory:" gives a throwaway database, which is what the CLI
    and tests use: they rebuild the ledger from the CSVs on every run.
    """
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(TABLES_DDL)
    for table in APPEND_ONLY_TABLES:
        conn.executescript(_append_only_triggers(table))
    return conn
