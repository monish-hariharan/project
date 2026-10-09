# Backtest v1 (2026-10-09) — not used by the dashboard

Run on 2020-08-03 → 2026-10-06 (1,529 sessions, LTP-only data, 614 trades). Kept for reference.
Invalid because of bugs in nifty_backtest.py v1, fixed in v2:

1. When legs were not all traded in the same 15-minute bar, the replay skipped the check; if no
   later complete snapshot existed it "settled at expiry" using the last seen spot (often the entry
   day). 12 of 23 condors and 22 debit spreads exited this way, booking P&L without testing stops.
2. Condors were only built when all four legs printed in the exact 09:25 bar → 23 condors in six
   years, biased to quiet days.
3. The `"expiry"` flag in lot_size_schedule was ignored → 33 trades used the wrong lot size.
4. P&L was in ₹ per lot of that era's lot size (25/50/75/65), so years were not comparable.

Directional reading that is unlikely to change: 200-pt debit spreads (bull call / bear put, ATM
long, 40% stop / 50% target) lost after costs across 588 trades and every regime.
