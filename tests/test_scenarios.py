"""Correction scenarios on made-up data.

Loads tests/fixtures/correction_scenarios/, where each position (S01–S10) is
one correction scenario, and checks every expected figure in that folder's
README.md. Keep the table here and the README table in step.
"""

from pathlib import Path

import pytest

from pnl_ledger import ledger
from pnl_ledger.ingest import load_csv_dir
from pnl_ledger.schema import connect

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "correction_scenarios"
NOW = "2025-05-01 00:00:00"
MONTHS = ["2025-01", "2025-02", "2025-03", "2025-04"]

# position: (adjustments as [(amount, booked_in)], {month: (reporting, corrected)})
EXPECTED = {
    "S01": ([], {"2025-01": (50, 50)}),
    "S02": ([(-50, "2025-02")], {"2025-01": (100, 50), "2025-02": (50, 100)}),
    "S03": ([(-50, "2025-02"), (30, "2025-03")],
            {"2025-01": (100, 80), "2025-02": (50, 100), "2025-03": (130, 100)}),
    "S04": ([(-50, "2025-02"), (50, "2025-02")], {}),
    "S05": ([(-50, "2025-02"), (0, "2025-02")], {"2025-01": (100, 50), "2025-02": (50, 100)}),
    "S06": ([], {"2025-02": (50, 50)}),
    "S07": ([(-50, "2025-03")], {"2025-02": (100, 50), "2025-03": (50, 100)}),
    "S08": ([(-50, "2025-04")], {"2025-01": (100, 50), "2025-04": (50, 100)}),
    "S09": ([], {"2025-04": (50, 50)}),
    "S10": ([], {}),
}
UNCHANGED = (100, 100)  # any month not listed above


@pytest.fixture(scope="module")
def conn():
    c = connect()
    load_csv_dir(c, FIXTURE_DIR)
    return c


@pytest.mark.parametrize("position_id", EXPECTED)
def test_adjustments(conn, position_id):
    expected_adjustments, _ = EXPECTED[position_id]
    actual = [(a.amount, a.booked_in) for a in ledger.adjustments(conn, NOW) if a.position_id == position_id]
    assert actual == expected_adjustments


@pytest.mark.parametrize("position_id", EXPECTED)
@pytest.mark.parametrize("month", MONTHS)
def test_monthly_both_views(conn, position_id, month):
    reporting, corrected = EXPECTED[position_id][1].get(month, UNCHANGED)
    assert ledger.reporting_month(conn, month, position_id, NOW) == reporting
    assert ledger.corrected_month(conn, month, position_id, NOW) == corrected


@pytest.mark.parametrize("position_id", [*EXPECTED, None])
def test_views_agree_once_every_adjustment_is_booked(conn, position_id):
    assert (ledger.year_to_date(conn, "2025-04", ledger.REPORTING, position_id, NOW)
            == ledger.year_to_date(conn, "2025-04", ledger.CORRECTED, position_id, NOW))


@pytest.mark.parametrize("as_of, expected", [
    ("2025-02-10 11:59:59", 100),  # before either correction
    ("2025-02-10 12:00:00", 50),   # first correction
    ("2025-03-10 11:59:59", 50),
    ("2025-03-10 12:00:00", 80),   # second correction replaces the first
])
def test_two_corrections_as_known_over_time(conn, as_of, expected):
    assert ledger.figure_as_of(conn, "S03", "2025-01-15", as_of) == expected


def test_reverted_correction_keeps_full_history(conn):
    """S04 ends where it started, but all three facts must still be in the ledger."""
    rows = conn.execute(
        "SELECT event_type, pnl_amount FROM pnl_fact_events "
        "WHERE position_id = 'S04' AND business_date = '2025-01-15' ORDER BY event_id"
    ).fetchall()
    assert [(r["event_type"], r["pnl_amount"]) for r in rows] == [
        ("original_booking", 100), ("correction", 50), ("correction", 100),
    ]


def test_reporting_view_of_closed_month_is_frozen(conn):
    """January as reported at close must equal January as reported today, even
    though five scenarios corrected a January day after the close."""
    at_close = ledger.reporting_month(conn, "2025-01", now="2025-02-03 09:00:00")
    assert ledger.reporting_month(conn, "2025-01", now=NOW) == at_close
