# Daily NIFTY dashboard run (every NSE trading day, ~09:25 IST)

Data comes from the DHAN connector (the Dhan REST API is not reachable from the cloud
environment directly). Work on branch `claude/laughing-einstein-rnosdm`, in `vol_surface/`.

1. **Trading day?** If today is a weekend/NSE holiday (the intraday call for today returns no
   candles), reply "Market closed today, no dashboard" and stop.
2. **Expiries** — `market_data_agent_tool` `expirylist` `{"UnderlyingScrip":13,"UnderlyingSeg":"IDX_I"}`.
   Take the first 3 expiries plus the next two monthly (last-Tuesday-of-month style) ones,
   skipping any expiry whose OTM strikes mostly show 0 OI.
3. **Chains** — `optionchain` per expiry. Write `data/nifty_chain_<date>.csv` with header comments
   (time, spot) and rows `expiry,strike,ce_ltp,ce_oi,pe_ltp,pe_oi` for 100-point strikes within
   about ±7% of spot (keep zero-OI rows; the code ignores them). Record the underlying LTP shown.
4. **Daily candles** — copy the previous `data/nifty_daily_<prev>.csv` to `data/nifty_daily_<date>.csv`
   and append every completed session since its last row (`historical_data_agent_tool`
   `historical`, `{"securityId":"13","exchangeSegment":"IDX_I","instrument":"INDEX","expiryCode":0,
   "oi":false,"fromDate":<day after last row>,"toDate":<today>}` — toDate is exclusive and candles
   come oldest first, without dates; map them to NSE trading days).
5. **Today so far** — `intraday` 5-minute candles from 09:15 today for open/high/low.
6. **Positions** — `portfolio_agent_tool` `positions` (read-only). Convert open NIFTY option
   positions to `data/positions.csv` (`expiry,strike,type,lots,entry_price`, lots = qty/65,
   short negative, entry = costPrice). Keep any manual rows the user added there.
7. **Build**
   ```
   python nifty_dashboard.py --chain data/nifty_chain_<date>.csv --daily data/nifty_daily_<date>.csv \
     --spot <spot> --asof "<date> <HH:MM>" --today-ohlc <open>,<high>,<low> \
     [--positions data/positions.csv] --page <scratchpad>/nifty-options-desk.html
   ```
8. **Deliver** — send `output/download/nifty_dashboard_<date>.html` (works offline) and
   `output/download/nifty_dashboard_<date>.xlsx` as attachments; republish the web page; commit
   data + CSV outputs and push.
9. **Message** — 3–5 lines: spot/today's move, ATM IV vs realised, the favoured strategy idea
   and why, the positions' net Greeks/P&L if any. Then ask the user to confirm their current
   NIFTY positions (or send them if none were found).
