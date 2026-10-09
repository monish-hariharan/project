# Backtest v2 (2026-10-10) — debit spreads valid, condors invalid

2020-08-03 → 2026-10-06, 1,529 sessions, LTP-only data (fills = LTP ± modelled slippage),
1,142 trades (96 excluded for <50% price coverage). P&L in ₹ per lot of 65.

* **Bull call / bear put debit spreads: valid.** 589 of 592 had the intended 150–250 pt width.
  These records are loaded into the dashboard (../backtest_stats.json).
* **Iron condors: invalid.** When the 200-pt wing strike had not traded near 09:25, the
  strike chooser fell back to the short strike itself (e.g. +1 10850PE / −1 10850PE): 252 of
  298 condors and 218 of 252 range condors were degenerate (a debit, instant "stop").
  Fixed in nifty_backtest.py v3 (wings must be beyond the short strike at 0.5–1.5× the
  width, otherwise that day's condor is skipped). Rerun needed for condor results.
* The run ended with a UnicodeEncodeError on the final console print (Windows cp1252); the
  JSON/CSV had already been written. Fixed in v3 (ASCII console output).
