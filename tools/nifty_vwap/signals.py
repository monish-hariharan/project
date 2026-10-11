#!/usr/bin/env python3
"""Nifty option VWAP signals on 15-minute candles.

Reads a CSV of candles (one row per bar) for every contract in the chain and
prints a BUY / SELL (short) signal per contract with an ATR stop-loss and a
target set so that the reward is 1.5x the risk *after* Motilal Oswal charges.

Input columns:
    expiry,side,strike,security_id,session,seq,open,high,low,close,volume
  expiry    YYYY-MM-DD
  side      CE | PE
  session   prev  (prior trading day's bars, used only for ATR)
            today (today's bars from 09:15, used for VWAP and ATR)
  seq       bar order within the session (0, 1, 2, ...)

Rules
  VWAP      anchored at today's open, typical price (H+L+C)/3 x volume
  Signal    last close > VWAP -> BUY,  last close < VWAP -> SELL (short)
            "fresh" when the previous bar closed on the other side of VWAP
  ATR       simple mean of the last --atr-period true ranges (prev + today)
  Stop      entry -/+ 1 x ATR
  Target    placed so net profit = RR x net loss, both including charges
"""

import argparse
import csv
import sys
from collections import defaultdict

TICK = 0.05

# Motilal Oswal F&O options plan, round trip (entry + exit order). Rates change
# with budgets and exchange circulars, so every one of them can be overridden.
DEFAULTS = {
    "lot_size": 65,
    "lots": 1,
    "brokerage_per_lot": 40.0,  # per lot, per executed order
    "stt_sell": 0.0015,  # sell side, from 1-Apr-2026 (Budget 2026)
    "exchange_txn": 0.0003503,  # NSE options, on premium turnover
    "sebi": 10 / 1e7,  # Rs 10 per crore
    "ipft": 0.0,  # NSE investor protection fund, not itemised by the broker
    "stamp_buy": 0.00003,  # buy side only
    "gst": 0.18,  # on brokerage + exchange + SEBI + IPFT
}


def charges(buy_px, sell_px, qty, c):
    buy_val, sell_val = buy_px * qty, sell_px * qty
    turnover = buy_val + sell_val
    brokerage = 2 * c["brokerage_per_lot"] * c["lots"]
    exch = c["exchange_txn"] * turnover
    sebi = c["sebi"] * turnover
    ipft = c["ipft"] * turnover
    stt = c["stt_sell"] * sell_val
    stamp = c["stamp_buy"] * buy_val
    gst = c["gst"] * (brokerage + exch + sebi + ipft)
    return brokerage + exch + sebi + ipft + stt + stamp + gst


def round_away(px, entry):
    """Round to the tick, moving away from entry so distances never shrink."""
    ticks = px / TICK
    ticks = int(ticks) + (1 if ticks > int(ticks) else 0) if px > entry else int(ticks)
    return round(ticks * TICK, 2)


def vwap(bars):
    pv = sum((b["high"] + b["low"] + b["close"]) / 3 * b["volume"] for b in bars)
    vol = sum(b["volume"] for b in bars)
    return pv / vol if vol else None


def atr(bars, period):
    trs, prev_close = [], None
    for b in bars:
        tr = b["high"] - b["low"]
        if prev_close is not None:
            tr = max(tr, abs(b["high"] - prev_close), abs(b["low"] - prev_close))
        trs.append(tr)
        prev_close = b["close"]
    trs = trs[-period:]
    return sum(trs) / len(trs) if trs else None


def levels(direction, entry, atr_val, rr, c):
    """Return (stop, target, net_loss, net_profit) in rupees for the position."""
    qty = c["lot_size"] * c["lots"]
    sign = 1 if direction == "BUY" else -1
    stop = round_away(entry - sign * atr_val, entry)

    def pnl(exit_px):
        buy, sell = (entry, exit_px) if sign == 1 else (exit_px, entry)
        return (sell - buy) * qty - charges(buy, sell, qty, c)

    net_loss = -pnl(stop)
    want = rr * net_loss
    # Charges grow with the exit price, so walk the target out until it pays.
    dist = want / qty
    for _ in range(50):
        target = entry + sign * dist
        got = pnl(target)
        if got >= want - 1e-6:
            break
        dist += (want - got) / qty
    target = round_away(entry + sign * dist, entry)
    return stop, target, net_loss, pnl(target)


def load(path):
    contracts = defaultdict(lambda: {"prev": [], "today": []})
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            key = (r["expiry"].strip(), r["side"].strip().upper(), float(r["strike"]),
                   r["security_id"].strip())
            bar = {k: float(r[k]) for k in ("open", "high", "low", "close", "volume")}
            bar["seq"] = int(r["seq"])
            contracts[key][r["session"].strip().lower()].append(bar)
    for v in contracts.values():
        v["prev"].sort(key=lambda b: b["seq"])
        v["today"].sort(key=lambda b: b["seq"])
    return contracts


def analyse(contracts, rr, min_volume, atr_period, c):
    rows = []
    for (expiry, side, strike, sid), s in contracts.items():
        today = [b for b in s["today"] if b["volume"] > 0]
        row = {"expiry": expiry, "side": side, "strike": strike, "security_id": sid}
        if not today:
            rows.append({**row, "signal": "NO DATA"})
            continue
        vw = vwap(today)
        a = atr(s["prev"] + s["today"], atr_period)
        entry = today[-1]["close"]
        vol = sum(b["volume"] for b in today)
        if vw is None or not a:
            rows.append({**row, "signal": "NO DATA", "entry": entry, "volume": vol})
            continue
        if entry == vw:
            rows.append({**row, "signal": "NEUTRAL", "entry": entry, "vwap": vw, "atr": a, "volume": vol})
            continue
        direction = "BUY" if entry > vw else "SELL"
        fresh = len(today) > 1 and ((today[-2]["close"] > vw) != (entry > vw))
        stop, target, net_loss, net_profit = levels(direction, entry, a, rr, c)
        notes = []
        if vol < min_volume:
            notes.append("thin")
        if direction == "SELL" and target <= TICK:
            notes.append("target below tick: not viable")
        rows.append({
            **row, "signal": direction, "fresh": fresh, "entry": entry,
            "vwap": vw, "atr": a, "stop": stop, "target": target,
            "net_loss": net_loss, "net_profit": net_profit,
            "net_rr": net_profit / net_loss if net_loss > 0 else None,
            "volume": vol, "notes": ", ".join(notes),
        })
    return rows


def mark_changes(rows, path):
    with open(path, newline="") as f:
        before = {(r["expiry"], r["side"], r["security_id"]): r["signal"] for r in csv.DictReader(f)}
    for r in rows:
        old = before.get((r["expiry"], r["side"], r["security_id"]))
        if r["signal"] not in ("BUY", "SELL") or old == r["signal"]:
            continue
        r["change"] = "FLIP" if old in ("BUY", "SELL") else "NEW"


def fmt(v, nd=2):
    return "" if v is None or v == "" else f"{v:,.{nd}f}"


def print_table(rows, out):
    hdr = ["Expiry", "Strike", "Side", "Signal", "Change", "Fresh", "Entry", "VWAP", "ATR", "SL", "Target",
           "Net loss Rs", "Net profit Rs", "Net RR", "Volume", "Notes"]
    out.write("| " + " | ".join(hdr) + " |\n")
    out.write("|" + "---|" * len(hdr) + "\n")
    for r in rows:
        out.write("| " + " | ".join([
            r["expiry"], f"{r['strike']:.0f}", r["side"], r["signal"], r.get("change", ""),
            "yes" if r.get("fresh") else "",
            fmt(r.get("entry")), fmt(r.get("vwap")), fmt(r.get("atr")),
            fmt(r.get("stop")), fmt(r.get("target")),
            fmt(r.get("net_loss"), 0), fmt(r.get("net_profit"), 0),
            fmt(r.get("net_rr")), fmt(r.get("volume"), 0), r.get("notes", ""),
        ]) + " |\n")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("candles", help="CSV of 15m candles (see module docstring)")
    p.add_argument("--rr", type=float, default=1.5)
    p.add_argument("--min-volume", type=float, default=50_000,
                   help="today's volume below this is flagged 'thin'")
    p.add_argument("--atr-period", type=int, default=14)
    p.add_argument("--prev-results", help="previous run's --csv-out, to mark NEW / FLIP")
    p.add_argument("--csv-out", help="also write results to this CSV")
    for k, v in DEFAULTS.items():
        p.add_argument("--" + k.replace("_", "-"), type=type(v), default=v)
    args = p.parse_args()
    c = {k: getattr(args, k) for k in DEFAULTS}

    rows = analyse(load(args.candles), args.rr, args.min_volume, args.atr_period, c)
    if args.prev_results:
        mark_changes(rows, args.prev_results)
    order = {"BUY": 0, "SELL": 0, "NEUTRAL": 1, "NO DATA": 2}
    rows.sort(key=lambda r: (r["expiry"], order[r["signal"]], r.get("notes", "") != "",
                             -r.get("volume", 0)))
    print_table(rows, sys.stdout)

    if args.csv_out:
        keys = ["expiry", "strike", "side", "change", "security_id", "signal", "fresh", "entry", "vwap", "atr",
                "stop", "target", "net_loss", "net_profit", "net_rr", "volume", "notes"]
        with open(args.csv_out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)


if __name__ == "__main__":
    main()
