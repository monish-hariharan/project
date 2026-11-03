#!/usr/bin/env python3
"""Robustness suite for the EMA crossover backtest. Writes results JSON."""

import argparse
import itertools
import json
import random
from collections import defaultdict

from backtest import backtest, load_bars, stats

MODES = ("long", "short", "both")
BASE = {"fast": 20, "slow": 50, "exit_n": 10, "atr_n": 14, "atr_mult": 1.5}


def run(bars, mode, **kw):
    params = {**BASE, **kw}
    trades, equity = backtest(bars, mode=mode, **params)
    return trades, stats(trades, equity, bars)


def bootstrap(trades, n=5000, seed=7):
    if not trades:
        return None
    rnd = random.Random(seed)
    nets = [t["net"] for t in trades]
    sums = sorted(sum(rnd.choice(nets) for _ in nets) for _ in range(n))
    return {"p05": sums[int(0.05 * n)], "p50": sums[n // 2], "p95": sums[int(0.95 * n)],
            "prob_loss": sum(s <= 0 for s in sums) / n}


def monthly(trades):
    m = defaultdict(float)
    for t in trades:
        m[t["exit_date"][:7]] += t["net"]
    return dict(sorted(m.items()))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("bars")
    p.add_argument("--out", required=True)
    args = p.parse_args()

    bars = load_bars(args.bars)
    days = sorted({b["date"] for b in bars})
    res = {"data": {"first_day": days[0], "last_day": days[-1], "sessions": len(days),
                    "candles": len(bars)}}
    lot = 65
    res["buy_hold"] = {"points": bars[-1]["close"] - bars[0]["open"],
                       "rs_per_lot": (bars[-1]["close"] - bars[0]["open"]) * lot}

    res["base"], res["bootstrap"], res["monthly"] = {}, {}, {}
    for mode in MODES:
        trades, s = run(bars, mode)
        res["base"][mode] = s
        res["bootstrap"][mode] = bootstrap(trades)
        res["monthly"][mode] = monthly(trades)

    res["no_stop"] = {m: run(bars, m, atr_mult=None)[1] for m in MODES}
    res["slippage"] = {str(sl): {m: run(bars, m, slippage=sl)[1]["net_rs"] for m in MODES}
                       for sl in (0, 1, 2)}
    dropped = load_bars(args.bars, drop_last_bar=True)
    res["drop_last_bar"] = {m: stats(*backtest(dropped, mode=m, **BASE), dropped) for m in MODES}

    # Split by session: first half vs second half, each with its own EMA warm-up.
    half = days[len(days) // 2]
    h1 = [b for b in bars if b["date"] < half]
    h2 = [b for b in bars if b["date"] >= half]
    res["halves"] = {"split_at": half,
                     "h1": {m: stats(*backtest(h1, mode=m, **BASE), h1) for m in MODES},
                     "h2": {m: stats(*backtest(h2, mode=m, **BASE), h2) for m in MODES}}

    grid = []
    for fast, slow, ex, am in itertools.product((15, 20, 25), (40, 50, 60), (8, 10, 12),
                                                (1.0, 1.5, 2.0, None)):
        row = {"fast": fast, "slow": slow, "exit": ex, "atr_mult": am}
        for m in MODES:
            row[m] = run(bars, m, fast=fast, slow=slow, exit_n=ex, atr_mult=am)[1]["net_rs"]
        grid.append(row)
    res["grid"] = grid
    summary = {}
    for m in MODES:
        vals = sorted(r[m] for r in grid)
        base_val = res["base"][m]["net_rs"]
        summary[m] = {"combos": len(vals), "profitable": sum(v > 0 for v in vals),
                      "median": vals[len(vals) // 2], "best": vals[-1], "worst": vals[0],
                      "base_rank_pct": sum(v <= base_val for v in vals) / len(vals)}
    res["grid_summary"] = summary

    with open(args.out, "w") as f:
        json.dump(res, f, indent=2)

    print(f"{res['data']}")
    print(f"buy & hold: {res['buy_hold']['points']:.1f} pts, Rs {res['buy_hold']['rs_per_lot']:,.0f}/lot")
    for m in MODES:
        s = res["base"][m]
        print(f"{m:5s} trades {s['trades']:4d} win {s['win_rate']:.0%} net Rs {s['net_rs']:>10,.0f} "
              f"PF {s['profit_factor']:.2f} maxDD Rs {s['max_dd_rs']:,.0f} costs Rs {s['costs_rs']:,.0f} "
              f"| grid profitable {summary[m]['profitable']}/{summary[m]['combos']}")


if __name__ == "__main__":
    main()
