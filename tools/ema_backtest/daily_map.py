#!/usr/bin/env python3
"""Assign dates to Dhan daily candles fetched one week per call.

The daily tool returns candles without dates. A week's candles are its trading
days in order, so a date is assigned only for weeks where every weekday is
known from the intraday fetch (session or holiday) and the trading-day count
equals the week's data_points. Other weeks are skipped and reported.
"""

import argparse
import csv
import datetime as dt
from collections import defaultdict


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("daily_weeks")
    p.add_argument("windows", nargs="+")
    p.add_argument("--out", required=True)
    args = p.parse_args()

    status = {}  # date -> True (session) / False (holiday)
    for path in args.windows:
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                d = r["date"].strip()
                if r["window_start"].strip() == "09:15":
                    status[d] = int(r["data_points"]) > 0

    weeks = defaultdict(list)
    points = {}
    with open(args.daily_weeks, newline="") as f:
        for r in csv.DictReader(f):
            w = r["week_start"].strip()
            points[w] = int(r["data_points"])
            if r["idx"].strip():
                weeks[w].append((int(r["idx"]), r))

    out, skipped = [], []
    for w in sorted(weeks):
        start = dt.date.fromisoformat(w)
        days = [(start + dt.timedelta(i)).isoformat() for i in range(5)]
        days = [d for d in days if d <= max(status)] if status else []
        if not days or any(d not in status for d in days):
            skipped.append(f"{w}: intraday status incomplete")
            continue
        trading = [d for d in days if status[d]]
        if len(trading) != points[w]:
            skipped.append(f"{w}: {len(trading)} intraday sessions vs {points[w]} daily candles")
            continue
        for (i, r), d in zip(sorted(weeks[w], key=lambda x: x[0]), trading):
            out.append({"date": d, **{k: r[k] for k in ("open", "high", "low", "close")}})

    with open(args.out, "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=["date", "open", "high", "low", "close"])
        wr.writeheader()
        wr.writerows(out)
    print(f"daily candles dated: {len(out)}  weeks skipped: {len(skipped)}")
    for s in skipped:
        print("  " + s)


if __name__ == "__main__":
    main()
