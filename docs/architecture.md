# Architecture: a Bitemporal P&L Ledger

This document explains how the solution is designed and how the code is
organized. It covers the data model (§1–2), the algorithms (§3), the accounting
idea behind the rules (§4), the technology and the alternatives rejected (§5),
a file-by-file walkthrough of the code (§6), and the known limitations (§7).

In short: every P&L figure is stored in an **append-only SQLite ledger** with
two timestamps, and every answer is calculated from that ledger with one rule.
Nothing calculated is stored.

---

## 1. The core idea: two times, one ledger

Every fact has two timestamps. Keeping them separate is the point of the design.

| Time | What it answers | Column | Example |
|---|---|---|---|
| **Valid time** | When was this true in the real world? | `business_date` | The trade happened on 2025-01-15. That never changes, however many times the figure is corrected. |
| **Transaction time** | When did we learn this fact? | `recorded_at` | The original figure was booked 2025-01-15 18:00. The first correction arrived 2025-03-05 10:15, and the second 2025-03-12 14:30. |

A fact is never edited. A correction is a **new fact** with the same
`business_date` and a later `recorded_at`. The ledger is the list of every fact
ever received, and the only operation the database allows on it is `INSERT`.

Because of this, both required views come from the same data:

- **Reporting view:** what was known when each month closed, plus any
  adjustments booked in that month.
- **Corrected view:** what is known now.

Both use the same lookup with a different cutoff time.

---

## 2. Schema

All tables are defined in `src/pnl_ledger/schema.py`. Dates are stored as
`'YYYY-MM-DD'` text and timestamps as `'YYYY-MM-DD HH:MM:SS'` text. In that
format, ordinary string comparison sorts chronologically, so queries can
compare times with `<=` and `<`.

### 2.1 Reference data

These tables are loaded once and used only for lookups.

```sql
CREATE TABLE positions (
    position_id   TEXT PRIMARY KEY,
    security_name TEXT NOT NULL,
    quantity      REAL NOT NULL
);

CREATE TABLE trade_detail (            -- only for days with trade-level detail
    position_id     TEXT NOT NULL REFERENCES positions(position_id),
    business_date   TEXT NOT NULL,
    quantity        REAL NOT NULL,
    execution_price REAL NOT NULL,
    closing_price   REAL NOT NULL,
    PRIMARY KEY (position_id, business_date)
);
```

### 2.2 The ledger

```sql
CREATE TABLE pnl_fact_events (
    event_id        INTEGER PRIMARY KEY AUTOINCREMENT,  -- insertion order; tie-breaker only
    position_id     TEXT NOT NULL REFERENCES positions(position_id),
    business_date   TEXT NOT NULL,                      -- valid time: the day the figure is about
    recorded_at     TEXT NOT NULL,                      -- transaction time: when we learned it
    event_type      TEXT NOT NULL CHECK (event_type IN ('original_booking', 'correction')),
    pnl_amount      REAL NOT NULL,                      -- the P&L figure this fact states
    execution_price REAL,                               -- set on corrections, for traceability
    note            TEXT
);

CREATE INDEX idx_events_bitemporal
    ON pnl_fact_events (position_id, business_date, recorded_at);
```

Each row in `daily_pnl.csv` becomes one `original_booking` event, with
`pnl_amount` copied as it is. Each row in `pnl_corrections.csv` becomes one
`correction` event. A correction only supplies a new execution price, so its
`pnl_amount` is calculated once, when it is loaded, using `trade_detail`:

```
pnl_amount = quantity × (closing_price − corrected_execution_price)
```

Calculating this at load time keeps every later query the same shape: a fact is
always `(position_id, business_date, recorded_at, pnl_amount)`, whatever its
source. The corrected `execution_price` is kept on the row so any figure can be
traced back to its inputs.

The index matches the main lookup: one position, one day, ordered by
`recorded_at`.

### 2.3 Period closures

```sql
CREATE TABLE period_closures (
    period    TEXT PRIMARY KEY,   -- 'YYYY-MM'
    closed_at TEXT NOT NULL
);
```

There is one row per month, loaded from `period_status.csv`. The closing time
decides two things: whether a correction needs an adjustment, and the cutoff
for a month's reporting view.

### 2.4 Append-only enforcement

**All four tables** get two triggers that abort any change:

```sql
CREATE TRIGGER pnl_fact_events_no_update BEFORE UPDATE ON pnl_fact_events
BEGIN SELECT RAISE(ABORT, 'pnl_fact_events is append-only: UPDATE forbidden'); END;

CREATE TRIGGER pnl_fact_events_no_delete BEFORE DELETE ON pnl_fact_events
BEGIN SELECT RAISE(ABORT, 'pnl_fact_events is append-only: DELETE forbidden'); END;
```

The guarantee lives in the database rather than in coding discipline. An
accidental `UPDATE` fails with an error instead of silently changing history.

---

## 3. Algorithms

Everything is built from the ledger. Nothing in this section stores any results.
All functions are in `src/pnl_ledger/ledger.py`.

### 3.1 The one rule: point-in-time lookup

> A day's figure, as known at time T, is the latest row for that day with
> `recorded_at <= T`.

```sql
SELECT pnl_amount
FROM pnl_fact_events
WHERE position_id = :position_id
  AND business_date = :business_date
  AND recorded_at <= :as_of
ORDER BY recorded_at DESC, event_id DESC
LIMIT 1;
```

Taking the **single latest** row, rather than adding rows together, is what
makes a later correction replace an earlier one. No special case is needed.
`event_id` breaks ties if two facts share a timestamp.

"As of date D" means the end of that day (`D 23:59:59`). The helper
`end_of_day()` makes this conversion.

### 3.2 Adjustments

Corrections are processed in `(recorded_at, event_id)` order. For each one:

1. Find the month of its `business_date`. Check whether that month had
   **already closed when the correction arrived** (`closed_at <= recorded_at`).
   - **No:** the month hasn't been reported yet, so no adjustment is needed.
     The new figure simply becomes part of that month (see §3.3).
   - **Yes:** this is a prior period adjustment. Calculate:

     ```
     previous   = the latest figure for that day before this correction
     adjustment = correction.pnl_amount − previous
     booked_in  = the calendar month of correction.recorded_at
     ```

"Before this correction" uses the same `(recorded_at, event_id)` ordering as
everywhere else. So a second correction to the same day is measured against
the first correction, not against the original figure. This is the "replace,
do not add" rule.

Adjustments are recalculated each time they're needed. They can't drift out of
step with the facts, because there is no stored copy to update.

### 3.3 Monthly and year-to-date figures

```
reporting_month(position, month)
  = Σ figure as known at the month's close    for each business date in the month
  + Σ adjustments booked in the month

corrected_month(position, month)
  = Σ figure as known now                      for each business date in the month

year_to_date(position, view, through_month)
  = Σ monthly figures from January through through_month, in that view
```

Passing `position_id=None` gives the whole fund (all positions together). For a
month that hasn't closed yet, the reporting view uses "now" as its cutoff.

The monthly sum is a single query that applies the §3.1 rule to every day at
once. It numbers each day's rows newest first and keeps row 1:

```sql
ROW_NUMBER() OVER (PARTITION BY position_id, business_date
                   ORDER BY recorded_at DESC, event_id DESC)  -- keep where = 1
```

**Why "as known at close" rather than "original figures":** the brief defines
the reporting view as the sum of a month's original daily figures plus its
adjustments. For all of the sample data, the two readings give the same
numbers. They differ only when a correction arrives **before** its month
closes. In that case investors were told the corrected figure and no adjustment
is booked (§3.2), so summing the original bookings would lose the correction.
Using each day's figure as known at close handles both cases.

---

## 4. The accounting idea: prior period adjustments

A prior period adjustment is the accounting idea of correcting an error that
belongs to an earlier, already-reported period. The business rules apply it in
a specific way:

- A closed month's reported figures never change, so a report that has been
  sent is never altered.
- An adjustment is recorded in the month the correction was received, so the
  effect lands in the current period.
- An adjustment is the new figure minus the figure most recently reflected, so
  only the change is booked.

**A note on terminology.** Under the formal standards (US GAAP ASC 250 and IFRS
IAS 8), a material error is normally corrected by restating the prior period.
Smaller errors are typically corrected in the current period. The brief's rules
follow the current-period approach. "Prior period adjustment" names the right
family of ideas, but this design doesn't claim to implement the standards'
treatment of every error.

---

## 5. Technology and alternatives

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.10+, standard library only | `sqlite3` and `csv` are built in. `pytest` is the only extra dependency, for tests. |
| Database | SQLite, in memory by default | Zero setup and runs the same on any machine. It is a real relational database with window functions and triggers. |
| Append-only | `BEFORE UPDATE` and `BEFORE DELETE` triggers | The database enforces the rule rather than relying on the code. |
| Testing | `pytest` | Tests check exact figures, not just that the code runs. |

### Rejected: SCD Type 2

Slowly Changing Dimension Type 2 keeps history by adding a new row for each
change, with start and end dates and a current flag. It was rejected for two
reasons. First, adding a version means **updating the previous row** to set its
end date and clear its flag, which breaks the rule that nothing is overwritten.
Second, it has a **single pair of dates**, which must mean either when a figure
applies or when we learned it. This design stores both, as separate columns,
and needs no end date: a version ends when the next one is recorded. If
something downstream needs an SCD2-style table, one can be generated from the
ledger with `LEAD(recorded_at)`.

### Rejected: SQL temporal tables

SQL:2011 system-versioned tables save old row versions automatically. They were
rejected for three reasons:

- The database stamps each version with the time **it** made the change. The
  time that matters here is when the business learned the fact, which comes
  from the source files. Loading the files today would date every correction
  today.
- They work by updating rows, while this design forbids updates outright.
- Only some databases support them, and SQLite doesn't.

---

## 6. Code walkthrough

```
src/pnl_ledger/
  __init__.py    package description
  schema.py      creates the tables and append-only triggers
  ingest.py      loads the CSVs into the database using INSERT only
  ledger.py      every query: as-of lookup, adjustments, monthly and YTD figures
  __main__.py    command line entry point that prints the answers
tests/
  test_ledger.py
  test_scenarios.py
  fixtures/correction_scenarios/
src/data_exploration.ipynb
```

Data flows in one direction: **CSV files → `ingest.py` → SQLite tables
(`schema.py`) → `ledger.py` queries → `__main__.py` or tests.**

### `schema.py`: the database

| Name | What it does |
|---|---|
| `TABLES_DDL` | The `CREATE TABLE` and `CREATE INDEX` statements from §2. |
| `APPEND_ONLY_TABLES` | The four tables that get the append-only triggers. |
| `_append_only_triggers(table)` | Builds the no-update and no-delete triggers for one table. |
| `connect(path=":memory:")` | Opens a database, turns on foreign keys, and creates the tables and triggers if they are missing. Rows come back as `sqlite3.Row`, so columns can be read by name. |

### `ingest.py`: loading the data

| Name | What it does |
|---|---|
| `load_csv_dir(conn, data_dir)` | Reads the five CSVs from a folder and passes their rows to `ingest`. |
| `ingest(conn, positions, trade_detail, daily_pnl, corrections, period_status)` | Inserts everything in one transaction. Bookings and corrections are merged and **sorted by `recorded_at`** before inserting, so `event_id` order matches the order facts were learned. |
| `_booking_event(row)` | Turns a `daily_pnl.csv` row into an `original_booking` event. |
| `_correction_event(conn, row)` | Turns a `pnl_corrections.csv` row into a `correction` event. It looks up quantity and closing price in `trade_detail` and calculates the P&L, rounded to cents. It raises an error if there is no matching trade. |

`ingest` takes plain lists of dictionaries rather than file paths, so tests can
build small made-up ledgers without writing files.

### `ledger.py`: the queries

| Name | What it does |
|---|---|
| `figure_as_of(conn, position_id, business_date, as_of)` | The one rule (§3.1). Returns the figure, or `None` if nothing was known yet. |
| `_figure_before(...)` | The figure just before a given event. Used to measure adjustments. |
| `_sum_as_of(conn, month, as_of, position_id)` | The one rule applied to every day of a month in one query, then summed. |
| `closed_at(conn, month)` / `_is_closed(conn, month, at)` | When a month closed, and whether it had closed by a given time. |
| `Adjustment` | One adjustment: the day corrected, previous and corrected figures, the amount, the month it is booked in, and the correction that caused it. |
| `adjustments(conn, now)` | All adjustments from corrections received up to `now` (§3.2). |
| `reporting_month(conn, month, position_id, now)` | Reporting view of one month (§3.3). |
| `corrected_month(conn, month, position_id, now)` | Corrected view of one month (§3.3). |
| `month_figure(conn, month, view, ...)` | Calls one of the two above, chosen by `REPORTING` or `CORRECTED`. |
| `year_to_date(conn, through_month, view, position_id, now)` | Sums `month_figure` from January through `through_month`. |
| `end_of_day(date)`, `current_timestamp()` | Helpers for "as of date D" and "now". |
| `position_id_for(conn, security_name)` | Looks up a position by name, for example `"Alpha Corp"` gives `"P001"`. |

Every function that depends on the current time takes a `now` argument. Tests
pass a fixed time, so results don't change from day to day. When `now` is left
out, the real current time is used.

### `__main__.py`: printing the answers

`PYTHONPATH=src python3 -m pnl_ledger` builds a fresh in-memory database, loads
`data/`, and prints the answers to the four questions using the functions
above. Two options are available: `--data-dir` points at another folder of
CSVs, and `--today "YYYY-MM-DD HH:MM:SS"` fixes what "today" means.

### Tests

| File | What it covers |
|---|---|
| `tests/test_ledger.py` | The exact answers on the real data; `UPDATE` and `DELETE` being rejected on every table; every original and correction being kept; and one test per business rule on small made-up ledgers, built with the `build()` helper. The rules include replace not add, an open month needing no adjustment, a closed month staying frozen, and the close being checked to the minute. |
| `tests/test_scenarios.py` | Loads `tests/fixtures/correction_scenarios/`, where each of ten made-up positions is one correction scenario. It checks every adjustment and every month in both views against expected values, and that both views agree on year to date once all adjustments are booked. |

To confirm the tests can fail, bugs were deliberately added to a throwaway copy
of the code: measuring adjustments against the original figure, booking them in
the wrong month, adding corrections together, and removing the triggers. Each
bug made between 5 and 24 of the 93 tests fail.

### `data_exploration.ipynb`

A notebook that describes each input file, its columns, and how the files
relate. The package doesn't use it.

---

## 7. Known limitations

- **No tamper-proofing.** Every figure can be recalculated and traced back to
  the facts it came from. However, the facts are not hash-chained, so someone
  with direct access to the database file could still alter them.
- **Closed months never reopen.** The logic assumes a month stays closed once
  it has closed.
- **Only execution-price corrections are modeled.** A correction to quantity or
  closing price would need each correction to record which field it changes, so
  that corrections to different fields of the same trade combine instead of
  replacing each other.
- **Single writer.** There is no concurrency control beyond what SQLite
  provides by default.
- **One time zone.** Timestamps are used exactly as they appear in the CSVs.
- **Calendar month of receipt.** An adjustment is booked in the calendar month
  of `recorded_at`. If a month ever closed before its own calendar month ended,
  a correction arriving after that close would be booked into a month that had
  already been reported. With the sample's close dates, each a few days into the
  following month, this can't happen.
- **Floating-point amounts.** Amounts are rounded to cents. A production ledger
  would use `Decimal` or integer cents throughout.
