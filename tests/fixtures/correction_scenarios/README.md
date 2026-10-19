# Correction scenarios (made-up test data)

Made-up data, in the same CSV format as `data/`, that covers correction cases
the assignment's sample data doesn't. `tests/test_scenarios.py` loads this
folder and checks every number in the table below.

## Setup

- 10 positions, `S01`–`S10`. **Each position is one scenario**, so scenarios
  can't affect each other.
- Each position has one booked day per month: 2025-01-15, 2025-02-14,
  2025-03-14 and 2025-04-15. Every day is booked at **100**, recorded at 18:00
  the same day.
- Every corrected day has trade detail: quantity 100, execution price 10.00,
  closing price 11.00 (so 100 × (11 − 10) = 100, matching the booking). A
  correction to price *p* makes the day's P&L 100 × (11 − *p*):
  10.50 → **50**, 10.20 → **80**, 10.00 → **100**.
- Months close on the same dates as the real data: January 2025-02-03 09:00,
  February 2025-03-05 09:00, March 2025-04-02 09:00. **April is never closed**,
  so it shows how an open month behaves.
- Tests treat "now" as 2025-05-01 00:00:00.

## Scenarios and expected results

Each month cell shows *reporting view / corrected view*.

| Position | Scenario | Corrections received | Adjustments | Jan | Feb | Mar | Apr |
|---|---|---|---|---|---|---|---|
| S01 | Open month correction | Jan 15 → 50, received 20 Jan (January still open) | none | 50 / 50 | 100 / 100 | 100 / 100 | 100 / 100 |
| S02 | Closed month, booked next month | Jan 15 → 50, received 10 Feb | −50 in Feb | 100 / 50 | 50 / 100 | 100 / 100 | 100 / 100 |
| S03 | Two corrections, different months | Jan 15 → 50 (10 Feb), then → 80 (10 Mar) | −50 in Feb, +30 in Mar | 100 / 80 | 50 / 100 | 130 / 100 | 100 / 100 |
| S04 | Correction later reverted | Jan 15 → 50 (10 Feb), then back to 100 (20 Feb) | −50 and +50, both in Feb | 100 / 100 | 100 / 100 | 100 / 100 | 100 / 100 |
| S05 | Same correction sent twice | Jan 15 → 50 (10 Feb), → 50 again (12 Feb) | −50 and 0, both in Feb | 100 / 50 | 50 / 100 | 100 / 100 | 100 / 100 |
| S06 | Close day, before the close | Feb 14 → 50, received 5 Mar 08:30 (February closes 09:00) | none | 100 / 100 | 50 / 50 | 100 / 100 | 100 / 100 |
| S07 | Close day, after the close | Feb 14 → 50, received 5 Mar 09:30 | −50 in Mar | 100 / 100 | 100 / 50 | 50 / 100 | 100 / 100 |
| S08 | Old month, booked in open month | Jan 15 → 50, received 10 Apr | −50 in Apr | 100 / 50 | 100 / 100 | 100 / 100 | 50 / 100 |
| S09 | Current open month day | Apr 15 → 50, received 20 Apr (April open) | none | 100 / 100 | 100 / 100 | 100 / 100 | 50 / 50 |
| S10 | No corrections (control) | — | none | 100 / 100 | 100 / 100 | 100 / 100 | 100 / 100 |

## What each scenario checks

- **S01, S09:** a correction that arrives while its month is still open is not
  an adjustment. The month just reports the corrected figure.
- **S02:** the basic prior period adjustment. The closed month stays as
  reported, and the difference goes into the month the correction was received.
- **S03:** "replace, not add." The second adjustment is measured against 50
  (the latest figure), not 100 (the original), so it is +30.
- **S04:** a correction that undoes an earlier one leaves a net zero
  adjustment, and the history keeps both events.
- **S05:** a duplicate correction books a zero adjustment instead of
  subtracting 50 a second time.
- **S06 / S07:** the close is checked by timestamp. Both corrections arrive in
  calendar March, but S06 comes before February's close (February takes it,
  no adjustment) and S07 after (adjustment in March).
- **S08:** a correction to a long-closed month, received in a month that is
  still open. The adjustment lands in April, and April's reporting figure
  includes it before April closes.
- **S10:** a control. With no corrections, the two views must be identical.

Across every scenario, year to date through April is the same in both views,
because by then every adjustment has been booked.
