"""
Real-data track record for each strategy family. No simulation.

Two sources, both built from actual prices:

  1. backtest_stats.json   produced by nifty_backtest.py run on your own historical data
                           (per family: trades, win rate, expectancy, drawdowns, stop rate).
  2. logs/paper.json       every suggestion the dashboard logs is marked to market each run at
                           real Dhan last-traded prices, from the logged entry prices until its
                           take-profit, stop, spot trigger or exit date. Costs use costs.json.

Families: condor (iron condors), bull (bull call debit spread), bear (bear put debit spread).
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

LOT = 65
HERE = Path(__file__).parent


def family_of(s):
    if s.get("family"):
        return s["family"]
    n = s["name"].lower()
    return "condor" if "condor" in n else "bull" if "bull call" in n else "bear" if "bear put" in n else "other"


def _stats(pnls, dds, stops, source):
    p = np.array(pnls, dtype=float)
    if len(p) == 0:
        return None
    wins, losses = p[p > 0], p[p <= 0]
    return dict(source=source, n=int(len(p)), win_rate=float((p > 0).mean()),
                expectancy=float(p.mean()), avg_win=float(wins.mean()) if len(wins) else 0.0,
                avg_loss=float(losses.mean()) if len(losses) else 0.0,
                worst=float(p.min()), best=float(p.max()),
                profit_factor=float(wins.sum() / -losses.sum()) if len(losses) and losses.sum() < 0 else None,
                max_dd_median=float(np.median(dds)) if dds else None,
                max_dd_worst=float(max(dds)) if dds else None,
                stop_rate=float(np.mean(stops)) if stops else None)


# --------------------------------------------------------------- paper record

def paper_update(suggestions, table, S, now, log_dir):
    """Mark every open logged suggestion at today's real prices; close it when its plan says so."""
    path = Path(log_dir) / "paper.json"
    book = json.loads(path.read_text()) if path.exists() else {}
    rows = {(r["expiry"], r["strike"]): r for r in table}
    today = now.date().isoformat()
    stamp = now.strftime("%Y-%m-%d %H:%M")
    for s in suggestions:
        rec = book.setdefault(s["id"], dict(id=s["id"], name=s["name"], family=family_of(s),
                                            opened=f"{s['date']} {s['time']}", status="open",
                                            entry_net=s["net"], cost=s["cost"], series=[]))
        if rec["status"] != "open":
            continue
        if min(l["expiry"] for l in s["legs"]) < today:
            rec.update(status="closed", reason="expired without a price")
            continue
        value = 0.0
        for l in s["legs"]:
            r = rows.get((l["expiry"], l["strike"]))
            if r is None:
                value = None
                break
            value += l["lots"] * r["ce_ltp" if l["type"] == "CE" else "pe_ltp"] * LOT
        if value is None:
            continue
        pnl = s["net"] + value - s["cost"]
        if not rec["series"] or rec["series"][-1]["t"] != stamp:
            rec["series"].append(dict(t=stamp, spot=S, pnl=round(pnl, 2)))
        x = s["exit"]
        reason = None
        if x["kind"] == "credit":
            buyback = -value
            if buyback <= x["tp_close_cost"]:
                reason = "take profit"
            elif buyback >= x["sl_close_cost"]:
                reason = "stop loss"
        else:
            if value >= x["tp_value"]:
                reason = "take profit"
            elif value <= x["sl_value"]:
                reason = "stop loss"
        for K, typ in x.get("short_strikes", []):
            if (typ == "CE" and S >= K) or (typ == "PE" and S <= K):
                reason = reason or "stop loss (short strike breached)"
        if reason is None and today >= x["exit_date"]:
            reason = "exit date"
        if reason:
            rec.update(status="closed", reason=reason, closed=stamp, final_pnl=round(pnl, 2))
    for rec in book.values():
        pn = [0.0] + [p["pnl"] for p in rec["series"]]
        peak = np.maximum.accumulate(pn)
        rec["max_dd"] = round(float((peak - np.array(pn)).max()), 2)
        rec["last_pnl"] = rec["series"][-1]["pnl"] if rec["series"] else None
    path.write_text(json.dumps(book, indent=1))
    return book


def paper_stats(book):
    out = {}
    for fam in ("condor", "bull", "bear"):
        done = [r for r in book.values() if r["family"] == fam and r["status"] == "closed" and "final_pnl" in r]
        st = _stats([r["final_pnl"] for r in done], [r["max_dd"] for r in done],
                    [r["reason"].startswith("stop") for r in done], "forward record")
        if st:
            st["open"] = sum(1 for r in book.values() if r["family"] == fam and r["status"] == "open")
            out[fam] = st
    return out


# ------------------------------------------------------------------ combined

def load_backtest(path=None):
    path = Path(path or HERE / "backtest_stats.json")
    if not path.exists():
        return None
    return json.loads(path.read_text())


def history(backtest, paper, min_trades=20, regime=None):
    """Per family: the backtest record for today's regime if it has `min_trades`, else the family's
    overall backtest record, else the forward record."""
    out = {}
    fams = set((backtest or {}).get("families", {})) | set(paper)
    period = (backtest or {}).get("period", "")
    for f in fams:
        bt = (backtest or {}).get("families", {}).get(f)
        by_reg = ((backtest or {}).get("by_regime", {}).get(f) or {}).get(regime) if regime else None
        if by_reg and by_reg.get("n", 0) >= min_trades:
            bt = dict(by_reg, source=f"backtest {period}, {regime} regime".strip())
        elif bt:
            bt = dict(bt, source=f"backtest {period}".strip())
        pp = paper.get(f)
        pick = bt if bt and bt.get("n", 0) >= min_trades else pp if pp and pp["n"] >= min_trades else None
        out[f] = pick
    return {k: v for k, v in out.items() if v}
