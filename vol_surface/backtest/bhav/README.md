# Backtest on NSE bhavcopy (end-of-day), 2019-02 to 2026-10

Run on the user's machine with `nse_bhavcopy.py` + `nifty_backtest.py --config backtest_config_bhav.json`
(1,918 trading days, entry every 5th day at the close, nearest expiry ≥ 12 days, all prices real NSE closes).

Checks done here
- 0 degenerate structures; condor wings 200 pts (100-300 where 200 was not traded)
- condor shorts median 2.9% OTM on both sides (≈16Δ at 15 DTE), credit median ≈ 39 pts
- 7 trades had stale closing prices at entry (a vertical priced at ≤ 0 or ≥ its width); excluded in
  `backtest_stats_filtered.json` and in the dashboard. The backtest now drops these itself.
- Regime labels are not meaningful here: with one price per day the previous day's high = low = close,
  so "Range" can never occur. The dashboard does not use a regime split for condors. Fixed in the
  backtest (label "n/a" for end-of-day data).

Result (₹ per 65-unit lot, after Motilal charges + slippage), filtered
| family | n | win | expectancy | worst | profit factor |
|---|---|---|---|---|---|
| condor (16Δ) | 333 | 61.3% | −762 | −8,948 | 0.45 |
| condor (VIX range) | 240 | 62.9% | −781 | −8,785 | 0.42 |
| bull call (daily) | 366 | 41.3% | −1,024 | −9,673 | 0.56 |
| bear put (daily) | 357 | 39.5% | −702 | −6,701 | 0.62 |

Condors lost money in every year 2019-2026 and in every vol-edge bucket (Rich −878, Fair −543,
Cheap −862). Average win ≈ ₹1,000 vs average loss ≈ ₹3,500.

Limitation: stops/take-profits are checked only at the daily close, so stop-outs fill at the close
after the breach (worse than an intraday stop on most days, better on none). Debit spreads in the
dashboard still use the intraday v3 record; this run's debit results agree in sign.
