#!/usr/bin/env python3
"""Stitch Dhan MCP window fetches into a validated 15-minute bars file.

The Dhan MCP intraday tool returns at most the last 5 candles of a request and
no timestamps, and both ends of the requested window are exclusive (a window
starting 09:15 does not include the 09:15 candle). A session (25 candles,
09:15-15:15) is fetched as windows starting 09:00 (5 candles), 10:15, 11:15,
12:15, 13:15, 14:15 (4 each). Older fetches used a 09:15 window (4 candles,
09:30-10:15) plus a separate 09:00-09:30 window for the 09:15 candle; both
layouts stitch in window order.

Input (windows CSV): date,window_start,data_points,bar_idx,open,high,low,close
Optional daily CSV (Dhan daily candles): date,open,high,low,close

Checks, all reported:
  - each window's rows match its reported data_points
  - every day has 25 candles
  - OHLC consistency (high >= open/close >= low)
  - intraday gaps: a candle opening far from the previous close
  - stitched day high/low/open vs the daily candle, when given
"""

import argparse
import csv
import sys
from collections import defaultdict

WINDOWS = ["09:00", "09:15", "10:15", "11:15", "12:15", "13:15", "14:15"]
REQUIRED = ["09:00", "10:15", "11:15", "12:15", "13:15", "14:15"]
SESSION_CANDLES = 25


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("windows", nargs="+")
    p.add_argument("--daily")
    p.add_argument("--out", required=True)
    p.add_argument("--gap-pct", type=float, default=0.3, help="flag intraday opens this far from prev close")
    args = p.parse_args()

    rows = defaultdict(lambda: defaultdict(dict))  # date -> window -> idx -> bar
    reported = defaultdict(dict)
    for path in args.windows:
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                d, w = r["date"].strip(), r["window_start"].strip()
                reported[d][w] = int(r["data_points"])
                if r["bar_idx"].strip() == "":
                    continue
                rows[d][w][int(r["bar_idx"])] = {k: float(r[k]) for k in ("open", "high", "low", "close")}

    problems, bars, counts = [], [], defaultdict(int)
    for d in sorted(reported):
        if all(reported[d].get(w, 0) == 0 for w in WINDOWS):
            continue  # holiday
        day = []
        for w in WINDOWS:
            if w not in reported[d]:
                if w in REQUIRED:
                    problems.append(f"{d} {w}: window missing")
                continue
            got = rows[d][w]
            if len(got) != reported[d][w] or sorted(got) != list(range(len(got))):
                problems.append(f"{d} {w}: {len(got)} rows for {reported[d][w]} data points")
            day.extend(got[i] for i in sorted(got))
        counts[len(day)] += 1
        if len(day) != SESSION_CANDLES:
            problems.append(f"{d}: {len(day)} candles, expected {SESSION_CANDLES}")
        for i, b in enumerate(day):
            if not (b["high"] >= max(b["open"], b["close"]) and b["low"] <= min(b["open"], b["close"])):
                problems.append(f"{d} candle {i}: OHLC inconsistent {b}")
            if i and abs(b["open"] / day[i - 1]["close"] - 1) * 100 > args.gap_pct:
                problems.append(f"{d} candle {i}: opens {b['open']} vs prev close {day[i - 1]['close']}")
        for i, b in enumerate(day):
            bars.append({"date": d, "seq": i, **b})

    if args.daily:
        by_day = defaultdict(list)
        for b in bars:
            by_day[b["date"]].append(b)
        with open(args.daily, newline="") as f:
            for r in csv.DictReader(f):
                d = r["date"].strip()
                if d not in by_day:
                    problems.append(f"{d}: in daily data but no intraday candles")
                    continue
                day = by_day[d]
                hi, lo = max(b["high"] for b in day), min(b["low"] for b in day)
                for name, mine, theirs in (("high", hi, float(r["high"])), ("low", lo, float(r["low"])),
                                           ("open", day[0]["open"], float(r["open"]))):
                    if abs(mine - theirs) > 0.051:
                        problems.append(f"{d}: stitched {name} {mine} vs daily {theirs}")

    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "seq", "open", "high", "low", "close"])
        w.writeheader()
        w.writerows(bars)

    print(f"sessions: {sum(counts.values())}  candles/session: {dict(counts)}  candles: {len(bars)}")
    print(f"problems: {len(problems)}")
    for line in problems:
        print("  " + line)


if __name__ == "__main__":
    main()
