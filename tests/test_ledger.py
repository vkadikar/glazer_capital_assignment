"""Tests for the P&L ledger.

Two kinds of test:
  1. Answer tests: load the real CSVs from data/ and check the exact figures
     the assignment asks for.
  2. Rule tests: build a small made-up ledger to check one business rule at a
     time, including cases the sample data never hits (for example, a
     correction that arrives while its month is still open).

Each test's docstring says what bug would make it fail.
"""

import sqlite3
from pathlib import Path

import pytest

from pnl_ledger import ledger
from pnl_ledger.ingest import ingest, load_csv_dir
from pnl_ledger.schema import connect

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
TODAY = "2026-01-01 00:00:00"  # fixed "now", so answers don't depend on when tests run


@pytest.fixture
def real():
    """The ledger built from the assignment's sample data."""
    conn = connect()
    load_csv_dir(conn, DATA_DIR)
    return conn


def build(daily_pnl, corrections=(), period_status=(), trade_detail=None):
    """Build a small made-up ledger with one position, P1.

    daily_pnl:   [(business_date, pnl_amount, recorded_at), ...]
    corrections: [(business_date, corrected_execution_price, recorded_at), ...]
    period_status: [(period, closed_at), ...]
    trade_detail defaults to quantity 100, execution 10.00, close 11.00 for
    each corrected day, so a correction to price p gives P&L 100 × (11 − p).
    """
    corrected_days = {d for d, _, _ in corrections}
    conn = connect()
    ingest(
        conn,
        positions=[{"position_id": "P1", "security_name": "Test Co", "quantity": 100}],
        trade_detail=trade_detail or [
            {"position_id": "P1", "business_date": d, "quantity": 100,
             "execution_price": 10.0, "closing_price": 11.0}
            for d in sorted(corrected_days)
        ],
        daily_pnl=[{"position_id": "P1", "business_date": d, "pnl_amount": a, "recorded_at": r}
                   for d, a, r in daily_pnl],
        corrections=[{"position_id": "P1", "business_date": d,
                      "corrected_execution_price": p, "recorded_at": r}
                     for d, p, r in corrections],
        period_status=[{"period": p, "closed_at": c} for p, c in period_status],
    )
    return conn


# --- 1. Answers to the assignment's questions --------------------------------------------

@pytest.mark.parametrize("as_of, expected", [
    (ledger.end_of_day("2025-01-31"), 1000),  # only the original booking exists
    (ledger.end_of_day("2025-03-06"), 600),   # first correction (49.40), received 5 Mar
    (ledger.end_of_day("2025-03-13"), 800),   # second correction (49.20), received 12 Mar
    (TODAY, 800),
])
def test_alpha_15_jan_as_known_on(real, as_of, expected):
    assert ledger.figure_as_of(real, "P001", "2025-01-15", as_of) == expected


def test_adjustments(real):
    """Fails if an adjustment is measured against the original figure instead of
    the most recent one (the second would be -200, not +200), or booked in the
    month of the corrected day (January) instead of the month received (March)."""
    adjs = ledger.adjustments(real, TODAY)
    assert [(a.position_id, a.business_date, a.amount, a.booked_in) for a in adjs] == [
        ("P001", "2025-01-15", -400, "2025-03"),
        ("P001", "2025-01-15", 200, "2025-03"),
    ]


@pytest.mark.parametrize("month, reporting, corrected", [
    ("2025-01", 3000, 2800),  # reporting is frozen at what investors were told
    ("2025-02", 2500, 2500),
    ("2025-03", 1600, 1800),  # reporting includes the -400 and +200 adjustments
])
def test_alpha_monthly_both_views(real, month, reporting, corrected):
    assert ledger.reporting_month(real, month, "P001", TODAY) == reporting
    assert ledger.corrected_month(real, month, "P001", TODAY) == corrected


@pytest.mark.parametrize("position_id, through, reporting, corrected", [
    ("P001", "2025-02", 5500, 5300),
    ("P001", "2025-03", 7100, 7100),
    (None, "2025-02", 5350, 5150),  # whole fund
    (None, "2025-03", 9850, 9850),
])
def test_year_to_date_both_views(real, position_id, through, reporting, corrected):
    assert ledger.year_to_date(real, through, ledger.REPORTING, position_id, TODAY) == reporting
    assert ledger.year_to_date(real, through, ledger.CORRECTED, position_id, TODAY) == corrected


def test_views_agree_once_all_adjustments_are_booked(real):
    """Adjustments move a difference from the past into a later month, they don't
    create or lose money. So once every adjustment has been booked, year-to-date
    totals in the two views must match, for every position."""
    for position_id in ["P001", "P002", "P003", None]:
        assert (ledger.year_to_date(real, "2025-03", ledger.REPORTING, position_id, TODAY)
                == ledger.year_to_date(real, "2025-03", ledger.CORRECTED, position_id, TODAY))


# --- 2. History is never overwritten ------------------------------------------------------

def test_ledger_rejects_update(real):
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        real.execute("UPDATE pnl_fact_events SET pnl_amount = 0")


def test_ledger_rejects_delete(real):
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        real.execute("DELETE FROM pnl_fact_events")


@pytest.mark.parametrize("table", ["positions", "trade_detail", "period_closures"])
def test_reference_tables_reject_update_and_delete(real, table):
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        real.execute(f"DELETE FROM {table}")


def test_every_original_and_correction_is_kept(real):
    rows = real.execute(
        "SELECT event_type, pnl_amount FROM pnl_fact_events "
        "WHERE position_id = 'P001' AND business_date = '2025-01-15' ORDER BY recorded_at"
    ).fetchall()
    assert [(r["event_type"], r["pnl_amount"]) for r in rows] == [
        ("original_booking", 1000), ("correction", 600), ("correction", 800),
    ]


# --- 3. Business rules, one at a time, on small made-up ledgers ---------------------------

JAN_CLOSED = [("2025-01", "2025-02-03 09:00:00")]


def test_later_correction_replaces_earlier_one():
    """Fails if corrections are added together instead of the latest one winning."""
    conn = build(
        daily_pnl=[("2025-01-10", 100, "2025-01-10 18:00:00")],
        corrections=[("2025-01-10", 10.50, "2025-01-20 12:00:00"),   # 100 × (11 − 10.50) = 50
                     ("2025-01-10", 10.80, "2025-01-25 12:00:00")],  # 100 × (11 − 10.80) = 20
    )
    assert ledger.figure_as_of(conn, "P1", "2025-01-10", TODAY) == 20
    assert ledger.corrected_month(conn, "2025-01", "P1", TODAY) == 20


def test_correction_in_open_month_needs_no_adjustment():
    """A correction that arrives before its month closes isn't a prior period
    adjustment: the month simply hasn't been reported yet. The sample data
    never hits this case. Fails if every correction is treated as an adjustment."""
    conn = build(
        daily_pnl=[("2025-01-10", 100, "2025-01-10 18:00:00")],
        corrections=[("2025-01-10", 10.50, "2025-01-20 12:00:00")],  # -> 50, January still open
        period_status=JAN_CLOSED,
    )
    assert ledger.adjustments(conn, TODAY) == []
    # January closes with the corrected figure, so both views say 50.
    assert ledger.reporting_month(conn, "2025-01", "P1", TODAY) == 50
    assert ledger.corrected_month(conn, "2025-01", "P1", TODAY) == 50


def test_closed_month_reporting_figure_never_changes():
    """Fails if a correction received after the close leaks back into the closed month."""
    conn = build(
        daily_pnl=[("2025-01-10", 100, "2025-01-10 18:00:00")],
        corrections=[("2025-01-10", 10.50, "2025-02-10 12:00:00")],  # -> 50, after January closed
        period_status=JAN_CLOSED,
    )
    before = ledger.reporting_month(conn, "2025-01", "P1", now="2025-02-03 10:00:00")
    after = ledger.reporting_month(conn, "2025-01", "P1", TODAY)
    assert before == after == 100
    # The -50 lands in February instead.
    assert ledger.reporting_month(conn, "2025-02", "P1", TODAY) == -50


@pytest.mark.parametrize("received, expect_adjustment", [
    ("2025-02-03 08:59:00", False),  # one minute before January closes
    ("2025-02-03 09:01:00", True),   # one minute after
])
def test_close_is_compared_by_timestamp_not_date(received, expect_adjustment):
    """The sample's first correction arrives 75 minutes after February closed, on
    the same day. Fails if the close check compares dates instead of timestamps."""
    conn = build(
        daily_pnl=[("2025-01-10", 100, "2025-01-10 18:00:00")],
        corrections=[("2025-01-10", 10.50, received)],
        period_status=JAN_CLOSED,
    )
    assert bool(ledger.adjustments(conn, TODAY)) == expect_adjustment


def test_as_of_ignores_facts_learned_later():
    conn = build(
        daily_pnl=[("2025-01-10", 100, "2025-01-10 18:00:00")],
        corrections=[("2025-01-10", 10.50, "2025-02-10 12:00:00")],
    )
    assert ledger.figure_as_of(conn, "P1", "2025-01-10", "2025-01-09 23:59:59") is None
    assert ledger.figure_as_of(conn, "P1", "2025-01-10", "2025-02-10 11:59:59") == 100
    assert ledger.figure_as_of(conn, "P1", "2025-01-10", "2025-02-10 12:00:00") == 50


def test_correction_without_trade_detail_is_rejected():
    with pytest.raises(ValueError, match="no matching trade_detail"):
        build(
            daily_pnl=[("2025-01-10", 100, "2025-01-10 18:00:00")],
            corrections=[("2025-01-10", 10.50, "2025-02-10 12:00:00")],
            trade_detail=[{"position_id": "P1", "business_date": "2025-01-11", "quantity": 1,
                           "execution_price": 1, "closing_price": 1}],
        )
