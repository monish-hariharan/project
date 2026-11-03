#!/usr/bin/env python3
"""EMA crossover backtest on Nifty 50 15-minute candles.

Rules (as specified):
  Entry  EMA(fast) crosses EMA(slow) on a candle close
         -> fill at the NEXT candle's open (no look-ahead)
         long on a cross above, short on a cross below
  Exit   EMA(exit) on the wrong side of EMA(slow) at a candle close
         -> fill at the next candle's open
         or a stop at entry -/+ atr_mult x ATR(atr_n), ATR taken at the signal
         candle; filled at the stop, or at the open if the market gaps
         through it
  Positions are held overnight. EMAs and ATR run continuously across days.

Input: bars CSV with columns date,seq,open,high,low,close (seq = candle index
within the session, 0-based). Costs are for one lot of Nifty futures on the
Motilal Oswal plan; every rate can be overridden.
"""

import argparse
import csv
import itertools
import json
import sys
from collections import defaultdict

COSTS = {
    "lot_size": 65,
    "brokerage_per_lot": 40.0,  # per lot, per executed order
    "stt_sell": 0.0005,  # futures, sell side (Budget 2026: 0.02% -> 0.05%)
    "exchange_txn": 0.0000173,  # NSE futures, on turnover
    "sebi": 10 / 1e7,
    "stamp_buy": 0.00002,  # futures, buy side
    "gst": 0.18,  # on brokerage + exchange + SEBI
}


def load_bars(path, drop_last_bar=False):
    by_day = defaultdict(list)
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            by_day[r["date"]].append({
                "date": r["date"], "seq": int(r["seq"]),
                **{k: float(r[k]) for k in ("open", "high", "low", "close")},
            })
    bars = []
    for d in sorted(by_day):
        day = sorted(by_day[d], key=lambda b: b["seq"])
        bars.extend(day[:-1] if drop_last_bar else day)
    return bars


def ema(values, n):
    out = [None] * len(values)
    if len(values) < n:
        return out
    k = 2 / (n + 1)
    out[n - 1] = sum(values[:n]) / n
    for i in range(n, len(values)):
        out[i] = values[i] * k + out[i - 1] * (1 - k)
    return out


def atr(bars, n):
    """Wilder's ATR."""
    out = [None] * len(bars)
    trs = []
    for i, b in enumerate(bars):
        tr = b["high"] - b["low"]
        if i:
            pc = bars[i - 1]["close"]
            tr = max(tr, abs(b["high"] - pc), abs(b["low"] - pc))
        trs.append(tr)
        if i == n - 1:
            out[i] = sum(trs) / n
        elif i >= n:
            out[i] = (out[i - 1] * (n - 1) + tr) / n
    return out


def round_trip_cost(entry_px, exit_px, side, c):
    qty = c["lot_size"]
    buy, sell = (entry_px, exit_px) if side == "long" else (exit_px, entry_px)
    buy_val, sell_val = buy * qty, sell * qty
    turnover = buy_val + sell_val
    brokerage = 2 * c["brokerage_per_lot"]
    exch = c["exchange_txn"] * turnover
    sebi = c["sebi"] * turnover
    return (brokerage + exch + sebi + c["stt_sell"] * sell_val + c["stamp_buy"] * buy_val
            + c["gst"] * (brokerage + exch + sebi))


def backtest(bars, fast=20, slow=50, exit_n=10, atr_n=14, atr_mult=1.5, mode="both",
             slippage=0.0, costs=COSTS, warmup=None):
    """mode: long | short | both. atr_mult None disables the stop."""
    closes = [b["close"] for b in bars]
    ef, es, ex = ema(closes, fast), ema(closes, slow), ema(closes, exit_n)
    at = atr(bars, atr_n)
    warmup = warmup if warmup is not None else 3 * max(fast, slow, exit_n, atr_n)

    trades, pos, pending = [], None, None
    equity, realized = [], 0.0

    def close_pos(i, px, reason):
        nonlocal pos, realized
        side = pos["side"]
        px = px - slippage if side == "long" else px + slippage
        pts = (px - pos["entry"]) if side == "long" else (pos["entry"] - px)
        cost = round_trip_cost(pos["entry"], px, side, costs)
        net = pts * costs["lot_size"] - cost
        realized += net
        trades.append({**pos, "exit_i": i, "exit_date": bars[i]["date"], "exit": px,
                       "reason": reason, "points": pts, "cost": cost, "net": net})
        pos = None

    def open_pos(i, side, signal_i):
        nonlocal pos
        px = bars[i]["open"]
        px = px + slippage if side == "long" else px - slippage
        stop = None
        if atr_mult is not None:
            d = atr_mult * at[signal_i]
            stop = px - d if side == "long" else px + d
        pos = {"side": side, "entry_i": i, "entry_date": bars[i]["date"], "entry": px,
               "stop": stop}

    for i, b in enumerate(bars):
        # 1. Orders decided at the previous close fill at this open.
        if pending:
            action, signal_i = pending
            pending = None
            if pos and action in ("exit", "long", "short"):
                close_pos(i, b["open"], "ema_exit" if action == "exit" else "reverse")
            if action in ("long", "short"):
                open_pos(i, action, signal_i)

        # 2. Intrabar stop.
        if pos and pos["stop"] is not None:
            if pos["side"] == "long" and b["low"] <= pos["stop"]:
                close_pos(i, min(b["open"], pos["stop"]), "stop")
            elif pos["side"] == "short" and b["high"] >= pos["stop"]:
                close_pos(i, max(b["open"], pos["stop"]), "stop")

        # 3. Signals on this close, filled at the next open.
        if i >= warmup and i + 1 < len(bars):
            up = ef[i - 1] <= es[i - 1] and ef[i] > es[i]
            dn = ef[i - 1] >= es[i - 1] and ef[i] < es[i]
            if up and mode in ("long", "both") and not (pos and pos["side"] == "long"):
                pending = ("long", i)
            elif dn and mode in ("short", "both") and not (pos and pos["side"] == "short"):
                pending = ("short", i)
            elif pos and pos["side"] == "long" and ex[i] < es[i]:
                pending = ("exit", i)
            elif pos and pos["side"] == "short" and ex[i] > es[i]:
                pending = ("exit", i)

        mtm = 0.0
        if pos:
            pts = (b["close"] - pos["entry"]) if pos["side"] == "long" else (pos["entry"] - b["close"])
            mtm = pts * costs["lot_size"]
        equity.append(realized + mtm)

    if pos:
        close_pos(len(bars) - 1, bars[-1]["close"], "end_of_data")
        equity[-1] = realized
    return trades, equity


def stats(trades, equity, bars):
    n = len(trades)
    wins = [t for t in trades if t["net"] > 0]
    losses = [t for t in trades if t["net"] <= 0]
    gross_win = sum(t["net"] for t in wins)
    gross_loss = -sum(t["net"] for t in losses)
    peak, mdd = 0.0, 0.0
    for e in equity:
        peak = max(peak, e)
        mdd = max(mdd, peak - e)
    net = sum(t["net"] for t in trades)
    reasons = defaultdict(int)
    for t in trades:
        reasons[t["reason"]] += 1
    held = sum(t["exit_i"] - t["entry_i"] for t in trades)
    return {
        "trades": n,
        "win_rate": len(wins) / n if n else 0.0,
        "net_rs": net,
        "gross_points": sum(t["points"] for t in trades),
        "costs_rs": sum(t["cost"] for t in trades),
        "avg_win_rs": gross_win / len(wins) if wins else 0.0,
        "avg_loss_rs": -gross_loss / len(losses) if losses else 0.0,
        "profit_factor": gross_win / gross_loss if gross_loss else float("inf"),
        "expectancy_rs": net / n if n else 0.0,
        "max_dd_rs": mdd,
        "net_over_dd": net / mdd if mdd else float("inf"),
        "exits": dict(reasons),
        "avg_bars_held": held / n if n else 0.0,
        "time_in_market": held / len(bars) if bars else 0.0,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("bars")
    p.add_argument("--fast", type=int, default=20)
    p.add_argument("--slow", type=int, default=50)
    p.add_argument("--exit", dest="exit_n", type=int, default=10)
    p.add_argument("--atr-n", type=int, default=14)
    p.add_argument("--atr-mult", type=float, default=1.5)
    p.add_argument("--no-stop", action="store_true")
    p.add_argument("--mode", choices=["long", "short", "both"], default="both")
    p.add_argument("--slippage", type=float, default=0.0, help="index points per side")
    p.add_argument("--drop-last-bar", action="store_true")
    p.add_argument("--trades-out")
    args = p.parse_args()

    bars = load_bars(args.bars, args.drop_last_bar)
    trades, equity = backtest(bars, args.fast, args.slow, args.exit_n, args.atr_n,
                              None if args.no_stop else args.atr_mult, args.mode, args.slippage)
    json.dump(stats(trades, equity, bars), sys.stdout, indent=2)
    print()
    if args.trades_out:
        with open(args.trades_out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(trades[0].keys()) if trades else ["none"])
            w.writeheader()
            w.writerows(trades)


if __name__ == "__main__":
    main()
