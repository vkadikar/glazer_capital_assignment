# P&L Restatement: a Bitemporal Ledger

A fund reports profit and loss to investors month by month. Once a month is
closed, its reported figures must never change, yet trade prices from closed
months are sometimes corrected later. This project keeps every original figure
and every correction, and produces two views from the same history:

- **Reporting view:** what investors were told. A late correction leaves the
  closed month untouched and is booked as an adjustment in the month the
  correction arrives.
- **Corrected view:** what each day's figure really was, as known today.

## Approach

All data lives in an **append-only, bitemporal ledger** in SQLite. Every P&L
figure is stored with two times:

- `business_date`: the day the figure is about.
- `recorded_at`: when we learned the figure.

A correction is a **new row** with the same `business_date` and a later
`recorded_at`. Nothing is ever edited or deleted: database triggers reject
every `UPDATE` and `DELETE`.

Every figure is then read with one rule:

> A day's figure, as known at time T, is the latest row recorded up to T.

Each view is that rule with a different T:

| Question | Cutoff time T |
|---|---|
| Figure as known on a date | that date |
| Reporting view of a month | the moment the month closed, plus adjustments booked in that month |
| Corrected view of a month | now |

An **adjustment** is created when a correction arrives for a month that has
already closed. It equals the new figure minus the figure held just before the
correction, and it is booked in the month the correction arrived. Because each
adjustment is measured against the latest figure, a second correction replaces
the first instead of adding to it.

Nothing calculated is stored. Adjustments and monthly figures are worked out
from the ledger each time they're needed. The schema, the algorithms, the
alternatives considered and the known limitations are in
[docs/architecture.md](docs/architecture.md).

## Repository layout

```
data/                       the five input CSVs (not committed; see Setup)
docs/
  architecture.md           design, algorithms, alternatives, limitations
src/
  data_exploration.ipynb    a first look at the input data
  pnl_ledger/
    schema.py               SQLite tables, plus triggers that make every table append-only
    ingest.py               loads the CSVs into the ledger using INSERT only
    ledger.py               as-of lookups, adjustments, monthly and year-to-date figures
    __main__.py             `python -m pnl_ledger` prints every answer
tests/
  test_ledger.py            answers on the real data, plus one test per business rule
  test_scenarios.py         ten made-up correction scenarios
  fixtures/correction_scenarios/
                            the scenario CSVs and their expected results
pyproject.toml              project metadata and pytest settings
```

## Setup

Requires Python 3.10 or later. The code uses only the standard library;
`pytest` is needed only to run the tests.

1. Put the five assignment CSVs in `data/`: `positions.csv`, `daily_pnl.csv`,
   `trade_detail.csv`, `pnl_corrections.csv` and `period_status.csv`.
2. Install pytest and run the tests:

   ```bash
   python3 -m pip install pytest
   python3 -m pytest                      # 93 tests, under a second
   ```

3. Print every answer:

   ```bash
   PYTHONPATH=src python3 -m pnl_ledger
   PYTHONPATH=src python3 -m pnl_ledger --today "2026-10-18 00:00:00"   # pin "today"
   ```

## Answers

Alpha Corp is position `P001`. Its 15 January trade was booked at 1,000 and its
execution price was corrected twice: to 49.40 on 5 March, giving 600, and to
49.20 on 12 March, giving 800. January closed on 3 February, and February closed
on 5 March at 09:00.

**Q1. Alpha Corp, 15 January 2025, as known on…**

| As of | Figure | Why |
|---|---|---|
| 31 Jan 2025 | 1,000 | Only the original booking exists |
| 6 Mar 2025 | 600 | The first correction (5 March) is the latest |
| 13 Mar 2025 | 800 | The second correction (12 March) replaces the first |
| Today | 800 | Nothing has arrived since |

**Q2. Adjustments**

| Received | Change | Adjustment | Booked in |
|---|---|---|---|
| 2025-03-05 10:15 | 1,000 → 600 | −400 | March 2025 |
| 2025-03-12 14:30 | 600 → 800 | +200 | March 2025 |

Both corrections concern January, which had already closed, so each one creates
an adjustment, booked in the month it was received. The second is measured
against 600, not 1,000.

**Q3. Alpha Corp by month**

| Month | Reporting | Corrected |
|---|---|---|
| January | 3,000 | 2,800 |
| February | 2,500 | 2,500 |
| March | 1,600 | 1,800 |

The reporting view keeps January as reported. March's own days total 1,800, and
the reporting view adds the two March adjustments (−400 and +200) to give 1,600.

**Q4. Year to date**

| Scope | Through | Reporting | Corrected |
|---|---|---|---|
| Alpha Corp | 28 Feb 2025 | 5,500 | 5,300 |
| Alpha Corp | 31 Mar 2025 | 7,100 | 7,100 |
| Whole fund | 28 Feb 2025 | 5,350 | 5,150 |
| Whole fund | 31 Mar 2025 | 9,850 | 9,850 |

The views differ by 200 through February. They agree by the end of March, once
the adjustments have been booked.

### How each answer is produced

These are the exact calls, run from the repository root with
`PYTHONPATH=src python3`. `python -m pnl_ledger` makes the same calls.

```python
from pnl_ledger import ledger
from pnl_ledger.ingest import load_csv_dir
from pnl_ledger.schema import connect

conn = connect()              # new in-memory SQLite database with append-only tables
load_csv_dir(conn, "data")    # insert the five CSVs into the ledger
NOW = "2026-10-18 00:00:00"   # what "today" means

# Q1: the latest row for that day recorded by the end of the given date
ledger.figure_as_of(conn, "P001", "2025-01-15", ledger.end_of_day("2025-01-31"))  # 1000.0
ledger.figure_as_of(conn, "P001", "2025-01-15", ledger.end_of_day("2025-03-06"))  # 600.0
ledger.figure_as_of(conn, "P001", "2025-01-15", ledger.end_of_day("2025-03-13"))  # 800.0
ledger.figure_as_of(conn, "P001", "2025-01-15", NOW)                               # 800.0

# Q2: every adjustment caused by corrections received up to NOW
for a in ledger.adjustments(conn, NOW):
    print(a.business_date, a.previous_amount, a.corrected_amount, a.amount, a.booked_in)
# 2025-01-15 1000.0 600.0 -400.0 2025-03
# 2025-01-15 600.0 800.0 200.0 2025-03

# Q3: one call per month and view (position_id="P001")
ledger.reporting_month(conn, "2025-01", "P001", NOW)   # 3000.0
ledger.corrected_month(conn, "2025-01", "P001", NOW)   # 2800.0
ledger.reporting_month(conn, "2025-03", "P001", NOW)   # 1600.0
ledger.corrected_month(conn, "2025-03", "P001", NOW)   # 1800.0

# Q4: position_id=None means the whole fund
ledger.year_to_date(conn, "2025-02", ledger.REPORTING, "P001", NOW)  # 5500.0
ledger.year_to_date(conn, "2025-02", ledger.CORRECTED, "P001", NOW)  # 5300.0
ledger.year_to_date(conn, "2025-03", ledger.REPORTING, None, NOW)    # 9850.0
ledger.year_to_date(conn, "2025-03", ledger.CORRECTED, None, NOW)    # 9850.0
```

What each function does:

| Function | Returns |
|---|---|
| `figure_as_of(conn, position, day, as_of)` | The figure for one position and day, as known at `as_of` |
| `adjustments(conn, now)` | Every adjustment from corrections to already-closed months, with the month each is booked in |
| `reporting_month(conn, month, position, now)` | Each day's figure at the month's close, plus that month's adjustments |
| `corrected_month(conn, month, position, now)` | Each day's latest figure, with no adjustments |
| `year_to_date(conn, through_month, view, position, now)` | The sum of monthly figures from January through `through_month`, in one view |

## Tests

`python3 -m pytest` runs 93 tests:

- **Answers on the real data:** every figure above.
- **History is never overwritten:** `UPDATE` and `DELETE` are rejected.
- **One test per business rule:** for example, replace rather than add, an open
  month needing no adjustment, and the close being checked to the minute.
- **Ten made-up correction scenarios:** described in
  [tests/fixtures/correction_scenarios/README.md](tests/fixtures/correction_scenarios/README.md).
- **A consistency check:** once all adjustments are booked, both views give the
  same year-to-date total.

To check that the tests can fail, bugs were deliberately added to a throwaway
copy of the code, such as measuring adjustments against the original figure or
adding corrections together. Each bug made between 5 and 24 tests fail.
