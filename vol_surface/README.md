# NIFTY 50 volatility surface (Dhan API)

Builds today's implied-volatility surface for NIFTY 50 from the Dhan HQ v2 Option Chain API.

```bash
pip install -r requirements.txt
export DHAN_CLIENT_ID=<your client id>
export DHAN_ACCESS_TOKEN=<access token from web.dhan.co → Profile → DhanHQ Trading APIs>
python nifty_vol_surface.py                # first 6 expiries
python nifty_vol_surface.py --expiries 10 --max-moneyness 0.15
python nifty_vol_surface.py --demo         # offline test with synthetic data
```

Output goes to `./output/`: an interactive HTML page (3D surface, per-expiry smiles, ATM term
structure) and CSVs with the raw IV points and the interpolated grid.

**Method:** the forward for each expiry comes from put-call parity, and IV is solved
from the bid/ask mid with Black-76 (falling back to LTP, then to Dhan's own IV). Only
OTM options are used. The surface is interpolated in total-variance space with
non-decreasing variance across expiries.
Dhan rate-limits the option chain to one request every 3 s, so 6 expiries take about 20 s.

## Greeks, liquidity and realised moves

`nifty_dashboard.py` adds per-strike Black-76 Greeks (delta, gamma, theta ₹/day, vega ₹/vol-pt;
per lot = ×65), open-interest liquidity, realised volatility (close-to-close 5/10/20/60d,
Parkinson, Garman-Klass) and implied (ATM straddle) vs realised moves per expiry:

```bash
python nifty_dashboard.py --chain data/nifty_chain_2026-10-09.csv \
  --daily data/nifty_daily_2026-10-09.csv --spot 22496.50 --asof "2026-10-09 10:44" \
  --today-ohlc 22350.05,22515.95,22294.75
```

## Costs, slippage and the best trade

`costs.json` holds the broker schedule (Motilal Oswal F&O options: ₹40/lot/order, STT 0.15% sell,
exchange 0.035%, SEBI 0.0001%, stamp 0.003% buy, GST 18%, 2 orders per leg) and slippage settings.
Each strategy idea shows its full charge breakdown, P&L limits and breakevens after costs, and an
expected P&L simulated at 20-day (and 5-day) realised vol. Ideas are ranked by EV / capital at risk;
the top one is marked "Best trade" only if its EV after costs is positive.

## Exit plans and position alerts

`rules.json` sets the exit rules and alert thresholds. Every strategy idea gets a take-profit
level, a stop loss (premium and spot trigger) and an exit-by date. With `--positions`, the
dashboard raises alerts for: book loss beyond a limit, net delta beyond ₹ per 1% (with a futures
or option hedge size), short strikes under pressure (recentre to the implied-move strike), short
premium at the stop multiple, and legs near expiry (roll to the next month at the same
moneyness, or exit long premium).
