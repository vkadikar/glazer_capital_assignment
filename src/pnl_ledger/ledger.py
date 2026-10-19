"""Queries over the ledger. Every figure the system reports comes from here.

Everything is built on one rule (docs/architecture.md §3.1):

    The figure for a (position, day), as known at time T, is the pnl_amount of
    the latest event for that (position, day) with recorded_at <= T.

Taking the latest event, rather than adding events up, is what makes a later
correction replace an earlier one. Nothing is stored apart from the raw events;
adjustments and monthly totals are recomputed on every call.

Conventions:
  - `as_of` / `now` are timestamps "YYYY-MM-DD HH:MM:SS". Use `end_of_day()`
    to turn a date like "2025-03-06" into "everything known by the end of that day".
  - `month` is "YYYY-MM".
  - `position_id=None` means the whole fund (all positions).
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime

REPORTING = "reporting"
CORRECTED = "corrected"


def end_of_day(date: str) -> str:
    """'2025-03-06' -> '2025-03-06 23:59:59' (everything recorded during that day)."""
    return f"{date} 23:59:59"


def current_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# --- Point-in-time lookups ------------------------------------------------------

def figure_as_of(conn: sqlite3.Connection, position_id: str, business_date: str, as_of: str) -> float | None:
    """The figure for one position and day, as known at `as_of` (inclusive).

    Returns None if nothing had been recorded for that day by `as_of`.
    """
    row = conn.execute(
        """
        SELECT pnl_amount FROM pnl_fact_events
        WHERE position_id = ? AND business_date = ? AND recorded_at <= ?
        ORDER BY recorded_at DESC, event_id DESC
        LIMIT 1
        """,
        (position_id, business_date, as_of),
    ).fetchone()
    return row["pnl_amount"] if row else None


def _figure_before(conn: sqlite3.Connection, position_id: str, business_date: str,
                   recorded_at: str, event_id: int) -> float | None:
    """The figure for a day as it stood just before a given event.

    Events are ordered by (recorded_at, event_id), the same order every other
    query uses, so two events with the same timestamp still have a defined order.
    """
    row = conn.execute(
        """
        SELECT pnl_amount FROM pnl_fact_events
        WHERE position_id = :position_id AND business_date = :business_date
          AND (recorded_at < :recorded_at
               OR (recorded_at = :recorded_at AND event_id < :event_id))
        ORDER BY recorded_at DESC, event_id DESC
        LIMIT 1
        """,
        {"position_id": position_id, "business_date": business_date,
         "recorded_at": recorded_at, "event_id": event_id},
    ).fetchone()
    return row["pnl_amount"] if row else None


def _sum_as_of(conn: sqlite3.Connection, month: str, as_of: str, position_id: str | None) -> float:
    """Sum over every day in `month` of that day's figure as known at `as_of`.

    The same "latest event wins" rule as figure_as_of(), applied to all days
    in the month in one query.
    """
    row = conn.execute(
        """
        SELECT COALESCE(SUM(pnl_amount), 0) AS total FROM (
            SELECT pnl_amount,
                   ROW_NUMBER() OVER (
                       PARTITION BY position_id, business_date
                       ORDER BY recorded_at DESC, event_id DESC
                   ) AS newest_first
            FROM pnl_fact_events
            WHERE substr(business_date, 1, 7) = :month
              AND recorded_at <= :as_of
              AND (:position_id IS NULL OR position_id = :position_id)
        )
        WHERE newest_first = 1
        """,
        {"month": month, "as_of": as_of, "position_id": position_id},
    ).fetchone()
    return row["total"]


# --- Periods ---------------------------------------------------------------------

def closed_at(conn: sqlite3.Connection, month: str) -> str | None:
    """When `month` was closed, or None if it has no closing record."""
    row = conn.execute("SELECT closed_at FROM period_closures WHERE period = ?", (month,)).fetchone()
    return row["closed_at"] if row else None


def _is_closed(conn: sqlite3.Connection, month: str, at: str) -> bool:
    closed = closed_at(conn, month)
    return closed is not None and closed <= at


# --- Adjustments (prior period adjustments) ---------------------------------------

@dataclass(frozen=True)
class Adjustment:
    position_id: str
    business_date: str     # the day being corrected
    booked_in: str         # 'YYYY-MM': the month the correction was received
    previous_amount: float
    corrected_amount: float
    amount: float          # corrected_amount - previous_amount
    recorded_at: str       # when the correction was received
    event_id: int          # the correction event that caused this adjustment


def adjustments(conn: sqlite3.Connection, now: str | None = None) -> list[Adjustment]:
    """All adjustments caused by corrections received up to `now`, oldest first.

    A correction causes an adjustment only if the month of the day it corrects
    was already closed when the correction arrived. If that month was still
    open, the new figure goes into that month as normal and no adjustment is
    needed.
    """
    now = now or current_timestamp()
    corrections = conn.execute(
        """
        SELECT event_id, position_id, business_date, recorded_at, pnl_amount
        FROM pnl_fact_events
        WHERE event_type = 'correction' AND recorded_at <= ?
        ORDER BY recorded_at, event_id
        """,
        (now,),
    ).fetchall()

    result = []
    for c in corrections:
        corrected_month = c["business_date"][:7]
        if not _is_closed(conn, corrected_month, at=c["recorded_at"]):
            continue
        # "The figure most recently reflected for that day": the original, or
        # an earlier correction if there was one. This is why a second
        # correction replaces the first instead of adding to it.
        previous = _figure_before(conn, c["position_id"], c["business_date"],
                                  c["recorded_at"], c["event_id"])
        if previous is None:
            raise ValueError(
                f"Correction event {c['event_id']} corrects {c['position_id']} on "
                f"{c['business_date']}, but no earlier figure exists for that day."
            )
        result.append(Adjustment(
            position_id=c["position_id"],
            business_date=c["business_date"],
            booked_in=c["recorded_at"][:7],
            previous_amount=previous,
            corrected_amount=c["pnl_amount"],
            amount=round(c["pnl_amount"] - previous, 2),
            recorded_at=c["recorded_at"],
            event_id=c["event_id"],
        ))
    return result


# --- Monthly and year-to-date figures ----------------------------------------------

def reporting_month(conn: sqlite3.Connection, month: str, position_id: str | None = None,
                    now: str | None = None) -> float:
    """What investors were told for `month`.

    = each day's figure as known when the month closed
    + adjustments booked in this month.

    For a closed month the first part never changes, because events recorded
    after the close are excluded. For a month that is still open, the latest
    figures are used, since nothing has been reported yet.
    """
    now = now or current_timestamp()
    closed = closed_at(conn, month)
    cutoff = closed if closed is not None and closed <= now else now
    booked = sum(
        a.amount for a in adjustments(conn, now)
        if a.booked_in == month and (position_id is None or a.position_id == position_id)
    )
    return round(_sum_as_of(conn, month, cutoff, position_id) + booked, 2)


def corrected_month(conn: sqlite3.Connection, month: str, position_id: str | None = None,
                    now: str | None = None) -> float:
    """What `month` really was, as known at `now`: latest figures, no adjustments."""
    return round(_sum_as_of(conn, month, now or current_timestamp(), position_id), 2)


def month_figure(conn: sqlite3.Connection, month: str, view: str, position_id: str | None = None,
                 now: str | None = None) -> float:
    if view == REPORTING:
        return reporting_month(conn, month, position_id, now)
    if view == CORRECTED:
        return corrected_month(conn, month, position_id, now)
    raise ValueError(f"view must be {REPORTING!r} or {CORRECTED!r}, got {view!r}")


def year_to_date(conn: sqlite3.Connection, through_month: str, view: str,
                 position_id: str | None = None, now: str | None = None) -> float:
    """Sum of the monthly figures from January through `through_month`, in one view."""
    year, last = through_month.split("-")
    months = [f"{year}-{m:02d}" for m in range(1, int(last) + 1)]
    return round(sum(month_figure(conn, m, view, position_id, now) for m in months), 2)


def position_id_for(conn: sqlite3.Connection, security_name: str) -> str:
    row = conn.execute(
        "SELECT position_id FROM positions WHERE security_name = ?", (security_name,)
    ).fetchone()
    if row is None:
        raise KeyError(f"No position named {security_name!r}")
    return row["position_id"]
