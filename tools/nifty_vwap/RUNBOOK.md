# Nifty options 15m VWAP signals — daily runbook

Instructions for the Claude session that runs this every trading day from
10:00 to 15:15 IST, using the Dhan MCP (`mcp__DHAN__*`) and `signals.py`.

## Scope

- Underlying NIFTY (`UnderlyingScrip` 13, `UnderlyingSeg` `IDX_I`).
- **10:00 cycle:** every expiry from `expirylist` up to 31 December of the
  current year.
- **15-minute refreshes (10:15–15:15):** only the 2 nearest expiries (fixed
  at the 10:00 cycle for the whole day).
- Every strike where the CE or PE has OI > 0 or LTP > 0 in that expiry's
  option chain, both CE and PE.
- 15-minute candles only. Lot size 65, 1 lot, Motilal Oswal charges (defaults
  in `signals.py`).

## Dhan MCP quirks (verified)

- `historical_data_agent_tool` `intraday` returns a summary plus **only the
  last 5 candles**, without timestamps. **Both ends of the window are
  exclusive on the candle start time**: a window from 09:15 does NOT include
  the 09:15 candle. To get the candles starting at S, S+15, ... up to E, use
  `fromDate` = S − 15 min and `toDate` = E + 15 min (verified against Dhan's
  daily open: 09:00–10:30 returns the 09:15–10:15 candles). A window that
  returns fewer `Data Points` than expected has a gap, and the position of
  the missing bar is unknown, so refetch that window one 15-minute bar at a
  time to place each bar correctly.
- `optionchain` has no security IDs. Resolve them with `search_agent_tool`,
  query `NIFTY-<Mon><YYYY>-<strike>` (e.g. `NIFTY-Oct2026-22500`), exchange
  `NSE`. One search returns CE and PE for every expiry in that month.
- `optionchain` is limited to one request per 3 seconds.
- Candle payload: `{"securityId": "<id>", "exchangeSegment": "NSE_FNO",
  "instrument": "OPTIDX", "interval": 15, "oi": false, "fromDate":
  "YYYY-MM-DD HH:MM:SS", "toDate": "YYYY-MM-DD HH:MM:SS"}`.

## Files

Work in `tools/nifty_vwap/data/<YYYY-MM-DD>/` (git-ignored):

- `ids.csv`: `expiry,side,strike,security_id`
- `candles.csv`: the `signals.py` input. Columns
  `expiry,side,strike,security_id,session,seq,open,high,low,close,volume`,
  where `session` is `prev` or `today` and today's `seq` is the bar index from
  09:15 (09:15 = 0, 09:30 = 1, ... 15:15 = 24).
- `results_<HHMM>.csv`: each cycle's `--csv-out`.

## First cycle (10:00)

1. Check that the market is open: fetch today's 09:00–10:00 candles for one
   near-the-money contract. If it returns 0 data points, today is a holiday.
   Report that in one line and stop without scheduling anything.
2. `expirylist`, then `optionchain` for each expiry in scope. Build the
   contract list.
3. Resolve IDs (one search per month and strike) into `ids.csv`.
4. Split the contracts into up to 8 equal chunks and give each chunk to its
   own subagent (Agent tool, `general-purpose`) in parallel. Each subagent:
   - **prev**: the previous trading day's last 5 bars (14:15–15:15 candles),
     window 14:00–15:30. If
     there are 0 points, step back one more weekday, up to 5 days. Store them
     as `session=prev` with `seq` 0.. in order.
   - **today**: window 09:00–10:00, giving the 09:15, 09:30 and 09:45 bars,
     `seq` 0–2. (Do not extend `toDate` past 10:00: the 10:00 bar is still
     forming.)
   - Writes `data/<date>/part_<k>.csv` with the header above. It transcribes
     the O/H/L/C/V numbers exactly, removing thousands separators.
   - Writes a contract with no candles as no rows. It does not invent bars.
5. Merge the parts into `candles.csv`, keeping one header and de-duplicating
   on `expiry,side,security_id,session,seq`.
6. Run
   `python3 tools/nifty_vwap/signals.py data/<date>/candles.csv --csv-out data/<date>/results_1000.csv`.
7. Report (see below), then schedule the next cycle.

## Later cycles (10:15, 10:30, ... 15:15)

At cycle time T, the bar that just closed starts at T−15 minutes and has
`seq = (T − 15min − 09:15) / 15min`.

1. If `candles.csv` or `ids.csv` is missing (the container was restarted),
   rebuild them for the 2 nearest expiries as in the first cycle, fetching
   today's closed bars in hourly windows 09:00–10:15, 10:00–11:15,
   11:00–12:15, ... (each returns 4 candles: 09:15–10:00, 10:15–11:00, ...),
   with the last window ending at T so the forming bar is excluded.
2. Fetch the window `fromDate` = T − 30 min, `toDate` = T (returns just the
   candle starting at T − 15) for every contract of the 2 nearest
   expiries, split across up to 8 subagents as above, and append the bars to
   `candles.csv`. Run `signals.py` on those 2 expiries only: filter
   `candles.csv` into `candles_near.csv` first, so the other expiries'
   10:00 signals are not reported as current.
3. Run `signals.py` with `--prev-results` pointing at the previous cycle's
   results file and `--csv-out data/<date>/results_<HHMM>.csv`.
4. Report, then schedule the next cycle. Stop after the 15:15 cycle.

Schedule each cycle with the `send_later` tool (claude-code-remote MCP), one
minute after the quarter hour (10:16, 10:31, ...) so the bar has closed.
Message: `Run the next Nifty VWAP cycle per tools/nifty_vwap/RUNBOOK.md`.

## Report

Each cycle, post in chat:

- The time and the number of contracts analysed per expiry, plus any that
  came back with no data.
- A table of the **NEW and FLIP signals** this cycle, liquid ones first
  (no `thin` note).
- At 10:00, the top 10 BUY and top 10 SELL signals by volume for each expiry.
- The path of the full results CSV.

Signals are mechanical rules, not advice. Shorts carry unlimited risk.
