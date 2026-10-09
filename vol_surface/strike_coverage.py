#!/usr/bin/env python3
"""
How far from the money does your stored option data reach? (Run on your machine.)

    python strike_coverage.py --store "D:/nifty_store"

For every 5th trading day it looks at 09:15-10:30, takes the nearest expiry at least 12 days
out, and reports which strikes traded: lowest/highest strike as % from spot, strike step,
and whether an iron condor (≈2-3% OTM shorts + 200-pt wings) could have been built.
Writes strike_coverage.txt (a few KB) — send that file.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", required=True)
    ap.add_argument("--every", type=int, default=5)
    a = ap.parse_args()
    days = sorted(p.name[5:] for p in Path(a.store).glob("date=*"))
    rows = []
    for d in days[:: a.every]:
        df = pd.read_parquet(Path(a.store) / f"date={d}")
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        hm = df["timestamp"].dt.strftime("%H:%M")
        win = df[(hm >= "09:15") & (hm <= "10:30")]
        if win.empty:
            continue
        exps = sorted(e for e in win["expiry"].unique() if (pd.Timestamp(e) - pd.Timestamp(d)).days >= 12)
        if not exps:
            continue
        e = exps[0]
        w = win[win["expiry"] == e]
        spot = w["underlying"].dropna()
        spot = float(spot[spot > 0].iloc[0]) if len(spot[spot > 0]) else None
        if not spot:
            continue
        puts = np.sort(w.loc[w["type"] == "PE", "strike"].unique())
        calls = np.sort(w.loc[w["type"] == "CE", "strike"].unique())
        allk = np.sort(w["strike"].unique())
        step = float(np.median(np.diff(allk))) if len(allk) > 1 else None
        lo = (spot - puts.min()) / spot * 100 if len(puts) else 0.0
        hi = (calls.max() - spot) / spot * 100 if len(calls) else 0.0
        # could a 2.5%-OTM short with a 200-pt wing exist on both sides?
        need_p, need_c = spot * 0.975 - 200, spot * 1.025 + 200
        rows.append(dict(date=d, expiry=e, spot=round(spot), puts=len(puts), calls=len(calls),
                         put_reach_pct=round(lo, 2), call_reach_pct=round(hi, 2), step=step,
                         condor_possible=bool(len(puts) and len(calls) and puts.min() <= need_p
                                              and calls.max() >= need_c),
                         rows_in_window=len(w)))
    t = pd.DataFrame(rows)
    lines = [f"{len(t)} sample days from {days[0]} to {days[-1]}",
             "", "Per year (median):",
             t.assign(year=t.date.str[:4]).groupby("year")[["puts", "calls", "put_reach_pct", "call_reach_pct",
                                                            "step", "rows_in_window"]].median().to_string(),
             "", f"Condor (2.5% OTM shorts + 200-pt wings) possible on {t.condor_possible.mean()*100:.0f}% of days",
             "", "Distribution of reach (% from spot):",
             t[["put_reach_pct", "call_reach_pct"]].describe().round(2).to_string(),
             "", "First 15 rows:", t.head(15).to_string(index=False)]
    Path("strike_coverage.txt").write_text("\n".join(lines))
    print("\n".join(lines))
    print("\nWrote strike_coverage.txt - send that file.")


if __name__ == "__main__":
    main()
