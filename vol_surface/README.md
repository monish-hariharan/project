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
