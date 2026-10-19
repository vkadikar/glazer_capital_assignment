"""Bitemporal, append-only P&L ledger for the Glazer Capital restatement case study.

Modules:
  schema   - SQLite tables, plus the triggers that make every table append-only
  ingest   - loads the assignment CSVs into the ledger (INSERT only)
  ledger   - point-in-time lookups, adjustments, monthly and year-to-date figures
  __main__ - `python -m pnl_ledger` prints the answers to the assignment's questions
"""
