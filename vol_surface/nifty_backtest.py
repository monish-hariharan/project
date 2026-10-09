#!/usr/bin/env python3
"""
Backtest the dashboard's strategies on your own historical NIFTY option data. Real prices only.

Run it on your machine, where the data lives. Two steps:

  1. prepare  — stream your CSV/Parquet files (any size) into a compact Parquet store,
                one folder per trading day, keeping only the columns the backtest needs:
        python nifty_backtest.py prepare --config backtest_config.json --out D:/nifty_store

  2. run      — replay the strategies day by day and write the results:
        python nifty_backtest.py run --store D:/nifty_store --config backtest_config.json

Outputs (small, safe to share/commit): backtest_stats.json (read by the dashboard) and
backtest_trades.csv (every trade: legs, entry/exit prices, P&L after costs, max drawdown,
exit reason, regime).

How a trade is replayed
  * Entry at the first snapshot at/after `entry_time` on each entry day (every
    `entry_every_n_days` trading days), on the nearest expiry at least `min_days` away.
  * Strikes are chosen exactly as the dashboard does: iron condor with ~16-delta shorts and
    200-point wings (delta from the IV implied by that moment's real prices), iron condor with
    shorts at the India-VIX expected range (if a VIX file is configured), and 200-point
    bull-call / bear-put debit spreads from the ATM strike.
  * Fills: if bid/ask columns exist, buys pay the ask and sells receive the bid (entry and exit).
    Otherwise last traded price plus the slippage in costs.json is used, and the output says so.
  * The position is marked at every `check_every_minutes` snapshot until it hits its take-profit,
    stop (rules.json, including spot through a short strike) or its exit-by date.
  * Charges from costs.json (brokerage, STT, exchange, SEBI, stamp, GST) on actual entry and
    exit prices; STT on intrinsic if a leg is held to expiry in the money.
  * Regime and volatility edge at entry use the same definitions as the dashboard, computed
    from the data available up to that day, so results can be split by regime.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).parent
NEEDED = ["timestamp", "expiry", "strike", "type", "ltp"]
OPTIONAL = ["bid", "ask", "oi", "underlying"]


# ------------------------------------------------------------------ prepare

def _normalise(df, cfg):
    cols = cfg["columns"]
    keep = {v: k for k, v in cols.items() if v}
    df = df[[c for c in keep if c in df.columns]].rename(columns=keep)
    flt = cfg.get("symbol_filter")
    if flt and flt.get("column") in df.columns:
        df = df[df[flt["column"]].astype(str).str.upper() == str(flt["value"]).upper()]
    tv = cfg.get("type_values", {"CE": ["CE", "C", "CALL"], "PE": ["PE", "P", "PUT"]})
    t = df["type"].astype(str).str.upper().str.strip()
    df["type"] = np.where(t.isin([x.upper() for x in tv["CE"]]), "CE",
                          np.where(t.isin([x.upper() for x in tv["PE"]]), "PE", None))
    df = df[df["type"].notna()]
    ts = pd.to_datetime(df["timestamp"], format=cfg.get("timestamp_format"), errors="coerce")
    df["timestamp"] = ts
    df["expiry"] = pd.to_datetime(df["expiry"], format=cfg.get("expiry_format"), errors="coerce").dt.date.astype(str)
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    for c in ("ltp", "bid", "ask", "oi", "underlying"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["timestamp", "strike"])
    df["date"] = df["timestamp"].dt.date.astype(str)
    return df


def prepare(cfg, out):
    import pyarrow as pa
    import pyarrow.parquet as pq
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    files = sorted(Path(cfg["data_dir"]).rglob(cfg.get("glob", "*")))
    files = [f for f in files if f.suffix.lower() in (".csv", ".parquet")]
    n_rows, part = 0, 0
    for f in files:
        if f.suffix.lower() == ".parquet":
            pf = pq.ParquetFile(f)
            chunks = (b.to_pandas() for b in pf.iter_batches(batch_size=1_000_000))
        else:
            chunks = pd.read_csv(f, chunksize=1_000_000, low_memory=False)
        for ch in chunks:
            df = _normalise(ch, cfg)
            for d, g in df.groupby("date"):
                ddir = out / f"date={d}"
                ddir.mkdir(exist_ok=True)
                pq.write_table(pa.Table.from_pandas(g.drop(columns=["date"]), preserve_index=False),
                               ddir / f"part-{part:06d}.parquet")
                part += 1
            n_rows += len(df)
            print(f"{f.name}: {n_rows:,} rows stored", end="\r")
    print(f"\nPrepared {n_rows:,} rows into {out} ({len(list(out.glob('date=*')))} trading days)")


# ----------------------------------------------------------------- pricing

def _b76(F, K, T, sig, call):
    from scipy.stats import norm
    sd = sig * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sd * sd) / sd
    d2 = d1 - sd
    return (F * norm.cdf(d1) - K * norm.cdf(d2)) if call else (K * norm.cdf(-d2) - F * norm.cdf(-d1))


def _iv(price, F, K, T, call):
    from scipy.optimize import brentq
    intrinsic = max(F - K, 0) if call else max(K - F, 0)
    if T <= 0 or price <= intrinsic + 1e-6:
        return None
    try:
        return brentq(lambda s: _b76(F, K, T, s, call) - price, 1e-4, 5.0)
    except ValueError:
        return None


def _delta(F, K, T, sig, call):
    from scipy.stats import norm
    sd = sig * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sd * sd) / sd
    return norm.cdf(d1) if call else norm.cdf(d1) - 1


# ------------------------------------------------------------------- store

class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.dates = sorted(p.name[5:] for p in self.path.glob("date=*"))
        self._cache = {}

    def day(self, d):
        if d not in self._cache:
            self._cache = {d: pd.read_parquet(self.path / f"date={d}")}     # keep one day in memory
        return self._cache[d]


def _mid(r, has_ba):
    if has_ba and r.get("bid", 0) > 0 and r.get("ask", 0) > 0:
        return (r["bid"] + r["ask"]) / 2
    return r.get("ltp")


def _snapshot(df, t):
    snap = df[df["timestamp"] == t]
    return {(row.expiry, float(row.strike), row.type): row._asdict() for row in snap.itertuples(index=False)}


def _spot(snap, expiry, has_ba):
    """Underlying from the data if present, else put-call parity at the strike nearest ATM."""
    for v in snap.values():
        if v.get("underlying") and v["underlying"] > 0:
            return float(v["underlying"])
    best = None
    for (e, k, t), v in snap.items():
        if e != expiry or t != "CE":
            continue
        p = snap.get((e, k, "PE"))
        if not p:
            continue
        c, pp = _mid(v, has_ba), _mid(p, has_ba)
        if c and pp and c > 0 and pp > 0:
            diff = abs(c - pp)
            if best is None or diff < best[0]:
                best = (diff, k + c - pp)
    return best[1] if best else None


# ---------------------------------------------------------------- strategies

def build(snap, expiry, F, T, rules, vix, has_ba):
    strikes = sorted({k for (e, k, t) in snap if e == expiry})
    if not strikes:
        return {}

    def q(k, t):
        return snap.get((expiry, k, t))

    def near(x):
        return min(strikes, key=lambda k: abs(k - x))
    deltas = {}
    for k in strikes:
        for t in ("CE", "PE"):
            r = q(k, t)
            m = _mid(r, has_ba) if r else None
            if m and m > 0:
                iv = _iv(m, F, k, T, t == "CE")
                if iv:
                    deltas[(k, t)] = _delta(F, k, T, iv, t == "CE")
    w, dw, d = rules["wing_width"], rules["debit_width"], rules["condor_short_delta"]
    out = {}
    puts = [(abs(abs(v) - d), k) for (k, t), v in deltas.items() if t == "PE" and k < F]
    calls = [(abs(v - d), k) for (k, t), v in deltas.items() if t == "CE" and k > F]
    if puts and calls:
        sp, sc = min(puts)[1], min(calls)[1]
        out["condor"] = [(near(sp - w), "PE", 1), (sp, "PE", -1), (sc, "CE", -1), (near(sc + w), "CE", 1)]
    if vix:
        tdays = max(T * 252, 1)                     # ≈ trading days to expiry
        E = F * vix / 100 * math.sqrt(tdays / 252)
        sp, sc = near(F - E), near(F + E)
        out["condor_range"] = [(near(sp - w), "PE", 1), (sp, "PE", -1), (sc, "CE", -1), (near(sc + w), "CE", 1)]
    atm = near(F)
    out["bull"] = [(atm, "CE", 1), (near(atm + dw), "CE", -1)]
    out["bear"] = [(atm, "PE", 1), (near(atm - dw), "PE", -1)]
    return {f: legs for f, legs in out.items() if all(q(k, t) for k, t, _ in legs)}


def fill(r, lots, side, has_ba, c):
    """Executable price: buy at ask / sell at bid when available, else LTP ± modelled slippage."""
    if has_ba and r.get("bid", 0) > 0 and r.get("ask", 0) > 0:
        return (r["ask"] if side > 0 else r["bid"]), "bid/ask"
    from nifty_costs import slippage_per_unit
    p = r["ltp"]
    slip = slippage_per_unit(p, (r.get("oi") or 0) / 65, c)
    return (p + slip if side > 0 else max(p - slip, 0.05)), "ltp+slippage"


def charges(price, lots, buy, c, lot):
    from nifty_costs import order_charges
    import nifty_costs
    nifty_costs.LOT = lot
    return order_charges(price, lots, buy, c)["total"]


# ------------------------------------------------------------------- regime

def daily_bars(store):
    """Daily OHLC of the underlying from the stored snapshots (underlying column or parity)."""
    bars = []
    for d in store.dates:
        df = store.day(d)
        has_ba = "bid" in df.columns and "ask" in df.columns
        times = sorted(df["timestamp"].unique())
        sample = times[:: max(len(times) // 12, 1)] + [times[-1]]
        spots = []
        for t in sample:
            snap = _snapshot(df, t)
            exps = sorted({e for (e, _, _) in snap})
            s = _spot(snap, exps[0], has_ba) if exps else None
            if s:
                spots.append(s)
        if spots:
            bars.append(dict(date=d, open=spots[0], high=max(spots), low=min(spots), close=spots[-1]))
    return bars


def lot_size(d, cfg):
    sched = sorted(cfg.get("lot_size_schedule", [["1900-01-01", 65]]))
    size = sched[0][1]
    for start, s in sched:
        if d >= start:
            size = s
    return size


# ---------------------------------------------------------------------- run

def run(cfg, store_path, out_dir):
    import nifty_engine as eng
    from nifty_costs import load_costs
    from nifty_risk import exit_plan, load_rules
    rules = load_rules(cfg.get("rules", HERE / "rules.json"))
    c = load_costs(cfg.get("costs", HERE / "costs.json"))
    store = Store(store_path)
    print(f"{len(store.dates)} trading days in store; building daily bars...")
    bars = daily_bars(store)
    bar_by = {b["date"]: i for i, b in enumerate(bars)}
    vix = {}
    if cfg.get("vix_file"):
        vf = pd.read_csv(cfg["vix_file"])
        vix = dict(zip(pd.to_datetime(vf[cfg.get("vix_date_col", "date")]).dt.date.astype(str),
                       vf[cfg.get("vix_close_col", "close")]))
    entry_t = cfg.get("entry_time", "09:25")
    every = int(cfg.get("entry_every_n_days", 5))
    check = int(cfg.get("check_every_minutes", 15))
    trades = []
    for di, d in enumerate(store.dates):
        if di % every or d not in bar_by or bar_by[d] < 25:
            continue
        df = store.day(d)
        has_ba = "bid" in df.columns and "ask" in df.columns
        times = sorted(df["timestamp"].unique())
        t0 = next((t for t in times if pd.Timestamp(t).strftime("%H:%M") >= entry_t), None)
        if t0 is None:
            continue
        snap = _snapshot(df, t0)
        exps = sorted({e for (e, _, _) in snap if e > d})
        exp = next((e for e in exps if (pd.Timestamp(e) - pd.Timestamp(d)).days >= rules["min_days"]), None)
        if not exp:
            continue
        S = _spot(snap, exp, has_ba) or _spot(snap, exps[0], has_ba)
        if not S:
            continue
        T = ((pd.Timestamp(exp) + pd.Timedelta(hours=15, minutes=30)) - pd.Timestamp(t0)).total_seconds() / 31_536_000
        F = S                                   # forward ≈ spot at the parity strike for selection
        # regime / vol edge at entry from history up to the previous day
        i = bar_by[d]
        hist = bars[: i]
        closes = np.array([b["close"] for b in hist] + [S])
        vfc = eng.vol_forecast(closes)
        prev = hist[-1]
        v_prev = vix.get(prev["date"])
        E = eng.expected_move(prev["close"], v_prev) if v_prev else prev["close"] * vfc["forecast"] / math.sqrt(252)
        z = (S - prev["close"]) / E
        reg = eng.regime(S, prev, vfc, z, rules)
        lot = lot_size(d, cfg)
        for fam, legs in build(snap, exp, F, T, rules, vix.get(prev["date"]), has_ba).items():
            entry, src = [], None
            for k, t, n in legs:
                px, src = fill(snap[(exp, k, t)], n, n, has_ba, c)
                entry.append(dict(expiry=exp, strike=k, type=t, lots=n, price=px))
            net = -sum(l["lots"] * l["price"] for l in entry) * lot
            idea = dict(legs=[dict(l) for l in entry], net=net, eval={"cost": 0.0})
            plan = exit_plan(idea, rules, pd.Timestamp(d).date())
            entry_cost = sum(charges(l["price"], l["lots"], l["lots"] > 0, c, lot) for l in entry)
            res = replay(store, entry, plan, net, d, t0, check, has_ba, c, lot, rules)
            if res is None:
                continue
            pnl = res["pnl_gross"] - entry_cost - res["exit_cost"]
            trades.append(dict(date=d, time=pd.Timestamp(t0).strftime("%H:%M"), family=fam, expiry=exp,
                               spot=round(S, 2), regime=reg["label"], z=round(z, 3),
                               vol_edge=_edge_label(snap, exp, S, T, vfc, has_ba, rules),
                               legs="; ".join(f"{l['lots']:+d} {l['strike']:.0f}{l['type']} @ {l['price']:.2f}"
                                              for l in entry),
                               entry_net=round(net, 2), exit_reason=res["reason"], exit_time=res["t"],
                               pnl=round(pnl, 2), max_dd=round(res["max_dd"], 2), fills=src,
                               costs=round(entry_cost + res["exit_cost"], 2), lot_size=lot))
        print(f"{d}: {len(trades)} trades so far", end="\r")
    write_outputs(trades, store, out_dir, cfg)


def _edge_label(snap, exp, S, T, vfc, has_ba, rules):
    strikes = sorted({k for (e, k, t) in snap if e == exp})
    k = min(strikes, key=lambda x: abs(x - S))
    ivs = []
    for t in ("CE", "PE"):
        r = snap.get((exp, k, t))
        m = _mid(r, has_ba) if r else None
        if m:
            iv = _iv(m, S, k, T, t == "CE")
            if iv:
                ivs.append(iv)
    if not ivs:
        return None
    return __import__("nifty_engine").vol_edge(float(np.mean(ivs)), vfc["forecast"], rules)["label"]


def replay(store, legs, plan, net, d0, t0, check, has_ba, c, lot, rules):
    """Walk forward through real snapshots; return P&L before charges, exit costs and drawdown."""
    exp = legs[0]["expiry"]
    series, last_t, reason, exit_px, last_S = [0.0], None, None, None, None
    i0 = store.dates.index(d0)
    for d in store.dates[i0:]:
        if d > exp:
            break
        df = store.day(d)
        df = df[df["expiry"] == exp]
        times = sorted(df["timestamp"].unique())
        step = [t for t in times if pd.Timestamp(t) > pd.Timestamp(t0)]
        if check > 1 and step:
            keep, nxt = [], None
            for t in step:
                if nxt is None or pd.Timestamp(t) >= nxt:
                    keep.append(t)
                    nxt = pd.Timestamp(t) + pd.Timedelta(minutes=check)
            step = keep + ([step[-1]] if step[-1] not in keep else [])
        for t in step:
            snap = _snapshot(df, t)
            rows = [snap.get((exp, l["strike"], l["type"])) for l in legs]
            if any(r is None for r in rows):
                continue
            marks = [_mid(r, has_ba) for r in rows]
            if any(m is None for m in marks):
                continue
            value = sum(l["lots"] * m for l, m in zip(legs, marks)) * lot
            pnl = net + value
            series.append(pnl)
            last_t = t
            S = _spot(snap, exp, has_ba)
            last_S = S or last_S
            if plan["kind"] == "credit":
                if -value <= plan["tp_close_cost"]:
                    reason = "take profit"
                elif -value >= plan["sl_close_cost"]:
                    reason = "stop loss"
            else:
                if value >= plan["tp_value"]:
                    reason = "take profit"
                elif value <= plan["sl_value"]:
                    reason = "stop loss"
            for K, typ in plan.get("short_strikes", []):
                if S and ((typ == "CE" and S >= K) or (typ == "PE" and S <= K)):
                    reason = reason or "stop loss (short strike breached)"
            if reason is None and d >= plan["exit_date"] and pd.Timestamp(t).strftime("%H:%M") >= "15:15":
                reason = "exit date"
            if reason:
                exit_px = [fill(r, -l["lots"], -l["lots"], has_ba, c)[0] for l, r in zip(legs, rows)]
                break
        if reason:
            break
    if last_t is None:
        return None
    if exit_px is not None:
        gross = net + sum(l["lots"] * p for l, p in zip(legs, exit_px)) * lot
        ex_cost = sum(charges(p, l["lots"], l["lots"] < 0, c, lot) for l, p in zip(legs, exit_px))
    else:                                       # held to expiry: settle at intrinsic, STT on exercise
        reason = "expiry"
        S = last_S
        intr = [max(S - l["strike"], 0) if l["type"] == "CE" else max(l["strike"] - S, 0) for l in legs]
        gross = net + sum(l["lots"] * v for l, v in zip(legs, intr)) * lot
        ex_cost = sum(c.get("stt_exercise_pct", 0) / 100 * v * l["lots"] * lot
                      for l, v in zip(legs, intr) if l["lots"] > 0)
        series.append(gross)
    peak = np.maximum.accumulate(series)
    return dict(pnl_gross=gross, exit_cost=ex_cost, reason=reason,
                t=str(last_t), max_dd=float((peak - np.array(series)).max()))


def _stats(rows):
    p = np.array([r["pnl"] for r in rows])
    if len(p) == 0:
        return None
    wins, losses = p[p > 0], p[p <= 0]
    dd = [r["max_dd"] for r in rows]
    return dict(n=int(len(p)), win_rate=float((p > 0).mean()), expectancy=float(p.mean()),
                avg_win=float(wins.mean()) if len(wins) else 0.0,
                avg_loss=float(losses.mean()) if len(losses) else 0.0,
                worst=float(p.min()), best=float(p.max()),
                profit_factor=float(wins.sum() / -losses.sum()) if losses.sum() < 0 else None,
                max_dd_median=float(np.median(dd)), max_dd_worst=float(max(dd)),
                stop_rate=float(np.mean([r["exit_reason"].startswith("stop") for r in rows])),
                total_pnl=float(p.sum()))


def write_outputs(trades, store, out_dir, cfg):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if not trades:
        print("\nNo trades were generated; check the column mapping and entry_time.")
        return
    with open(out_dir / "backtest_trades.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
        w.writeheader()
        w.writerows(trades)
    fams = sorted({t["family"] for t in trades})
    stats = dict(generated=datetime.now().isoformat(timespec="seconds"),
                 period=f"{store.dates[0]} to {store.dates[-1]}",
                 fills=sorted({t["fills"] for t in trades}),
                 settings={k: cfg.get(k) for k in ("entry_time", "entry_every_n_days", "check_every_minutes")},
                 families={f: _stats([t for t in trades if t["family"] == f]) for f in fams},
                 by_regime={f: {r: _stats([t for t in trades if t["family"] == f and t["regime"] == r])
                                for r in ("Range", "Trend", "Mixed")} for f in fams},
                 by_vol_edge={f: {v: _stats([t for t in trades if t["family"] == f and t["vol_edge"] == v])
                                  for v in ("Rich", "Fair", "Cheap")} for f in fams})
    # the dashboard's "condor" family uses the delta-based condor; keep the range one separately
    (out_dir / "backtest_stats.json").write_text(json.dumps(stats, indent=2))
    print(f"\n{len(trades)} trades → {out_dir / 'backtest_stats.json'} and backtest_trades.csv")
    for f, s in stats["families"].items():
        print(f"  {f:13s} n={s['n']:4d} win {s['win_rate']*100:5.1f}%  expectancy ₹{s['expectancy']:9,.0f}"
              f"  worst ₹{s['worst']:10,.0f}  max DD median ₹{s['max_dd_median']:8,.0f}  stop {s['stop_rate']*100:4.1f}%")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--config", required=True)
    p.add_argument("--out", required=True)
    r = sub.add_parser("run")
    r.add_argument("--config", required=True)
    r.add_argument("--store", required=True)
    r.add_argument("--out", default=str(HERE))
    a = ap.parse_args()
    cfg = json.loads(Path(a.config).read_text())
    if a.cmd == "prepare":
        prepare(cfg, a.out)
    else:
        run(cfg, a.store, a.out)


if __name__ == "__main__":
    main()
