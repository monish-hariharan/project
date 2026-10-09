# NIFTY dashboard runbook

Data comes from the DHAN connector (the Dhan REST API is not reachable from the cloud
environment directly). Work on branch `claude/laughing-einstein-rnosdm`, in `vol_surface/`.
Never place, modify or cancel orders.

## Morning run (every NSE trading day, ~09:25 IST)

1. **Trading day?** If the intraday call for today returns no candles (weekend/NSE holiday),
   reply "Market closed today, no dashboard" and stop.
2. **Expiries** — `market_data_agent_tool` `expirylist` `{"UnderlyingScrip":13,"UnderlyingSeg":"IDX_I"}`.
   Take the first 3 expiries plus the next two monthly ones, skipping any expiry whose OTM
   strikes mostly show 0 OI. Also include every expiry used by an open tracked trade
   (`python nifty_tracker.py list`).
3. **Chains** — `optionchain` per expiry. Write `data/nifty_chain_<date>.csv` with header comments
   (time, spot) and rows `expiry,strike,ce_ltp,ce_oi,pe_ltp,pe_oi` for 100-point strikes within
   about ±7% of spot, plus every strike an open tracked trade uses.
4. **Daily candles** — copy the previous `data/nifty_daily_<prev>.csv` to `data/nifty_daily_<date>.csv`
   and append every completed session since its last row (`historical_data_agent_tool`
   `historical`, `{"securityId":"13","exchangeSegment":"IDX_I","instrument":"INDEX","expiryCode":0,
   "oi":false,"fromDate":<day after last row>,"toDate":<today>}` — toDate is exclusive, candles
   come oldest first without dates; map them to NSE trading days).
5. **Today so far** — `intraday` 5-minute candles from 09:15 today for open/high/low.
6. **Taken suggestions** — `portfolio_agent_tool` `positions` (read-only) into a scratch CSV
   (`expiry,strike,type,lots,entry_price`, lots = qty/65, short negative, entry = costPrice).
   It is only used to detect which logged suggestions were entered; other positions are ignored
   and not reported.
7. **Build**
   ```
   python nifty_dashboard.py --chain data/nifty_chain_<date>.csv --daily data/nifty_daily_<date>.csv \
     --spot <spot> --asof "<date> <HH:MM>" --today-ohlc <open>,<high>,<low> \
     --positions <scratch>/positions.csv --page <scratchpad>/nifty-options-desk.html
   ```
   This logs today's suggestions (`logs/suggestions.jsonl`), starts tracking any suggestion found
   in the positions, and checks every tracked trade against its exit plan.
8. **Deliver** — send `output/download/nifty_dashboard_<date>.html` and `.xlsx` as attachments;
   republish the web page; commit `data/`, `logs/` and the CSV outputs, and push.
9. **Message** — tracked-trade alerts first (take profit, stop loss, strike breached/tested,
   recentre, hedge, exit date, roll — with the exact trade proposed). Then 3–5 lines: spot and
   today's move, ATM IV vs realised, the best trade after costs (or "stay flat") with its ID and
   exit plan. Do not ask for positions.

## Hourly monitor (10:15–15:15 IST, trading days)

1. `git pull`; `python nifty_tracker.py list`. If no trade is `[open]`, reply "No tracked trades"
   and stop.
2. Fetch the option chain for each expiry the open trades use, plus the next monthly expiry
   (for roll quotes). Write `data/monitor/nifty_chain_<date>_<HHMM>.csv` in the chain format with
   the trades' strikes and ±1,000 points around spot; note the underlying LTP.
3. `python nifty_tracker.py monitor --chain <file> --spot <ltp> --asof "<date> <HH:MM>"`
4. Commit `logs/` and the chain file, push.
5. Message only if there are NEW alerts: each alert with the trade it proposes. At the 15:15
   check also send a one-line P&L per tracked trade. Otherwise stay silent.

## Taking or closing a suggestion by hand

```
python nifty_tracker.py take S20261009-1 --lots 2 --prices 85,48   # fills per leg, in leg order
python nifty_tracker.py close S20261009-1 --reason "took profit"
```
