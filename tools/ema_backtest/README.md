# Nifty 15m EMA crossover backtest

Strategy: enter when EMA20 crosses EMA50 on a 15-minute close (long on a cross
up, short on a cross down), filled at the next candle's open. Exit at the next
open after EMA10 closes on the wrong side of EMA50, or at a 1.5 x ATR(14) stop
(filled at the stop, or at the open on a gap through it). Positions are held
overnight. Costs: 1 lot (65) of Nifty futures on the Motilal Oswal plan.

Data (`data/`): Nifty 50 index (Dhan security 13, IDX_I) 15-minute candles,
3 Nov 2025 – 2 Nov 2026, from the Dhan MCP.

- `windows_*.csv` raw transcriptions of each Dhan request.
- `bars.csv` stitched and validated: 246 sessions x 25 candles (09:15–15:15).
- `daily.csv` Dhan daily candles, used to cross-check every session's
  open/high/low (7 differences, all <= 2.05 points, at the opening print).
- `results.json` robustness suite output; `trades_<mode>.csv` trade lists.

The Dhan MCP returns at most 5 candles per request, without timestamps, and
both ends of the requested window are exclusive; see `stitch.py`.

Reproduce:

    python3 stitch.py data/windows_*.csv --daily data/daily.csv --out data/bars.csv
    python3 robustness.py data/bars.csv --out data/results.json
    python3 -m unittest test_backtest
