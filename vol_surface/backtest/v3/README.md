# Backtest v3 (2026-10-10) — loaded into the dashboard

Same period and settings as v2; condor wing selection fixed (no degenerate legs: verified).
629 trades, 42 excluded for <50% price coverage.

* Bull call / bear put debit spreads: identical to v2 (292 / 290 trades, −₹1,720 / −₹1,202 per lot of 65).
* Condors: only 4 (+1 range) trades could be built — the stored data rarely contains the far-OTM
  wing strikes around 09:25. Too few to judge; see strike_coverage.py to measure data reach.
