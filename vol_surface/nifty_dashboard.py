#!/usr/bin/env python3
"""
NIFTY 50 options dashboard: IV surface + Greeks + liquidity + realised vs implied moves.

    python nifty_dashboard.py --chain data/nifty_chain_2026-10-09.csv \
        --daily data/nifty_daily_2026-10-09.csv --spot 22496.50 --asof "2026-10-09 10:44" \
        --today-ohlc 22350.05,22515.95,22294.75

Greeks are Black-76 on the parity-implied forward, using each strike's own IV:
  delta  per 1 point move in spot
  gamma  change in delta per 1 point
  theta  ₹ per day (1 calendar day of decay, spot unchanged)
  vega   ₹ per 1 vol point
Per-lot figures multiply by the lot size (65).
"""
from __future__ import annotations

import argparse
import re
import csv
import math
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.stats import norm

from nifty_costs import drawdown, load_costs, rank
import nifty_engine as eng
from nifty_risk import exit_plan, load_rules
from nifty_strategies import load_positions, suggest
from nifty_tracker import LOG_DIR, log_suggestions, match_positions, monitor, read_suggestions
from nifty_vol_surface import (IST, black76, build_grid, chain_points, load_csv,
                               time_to_expiry)

LOT = 65
TRADING_DAYS = 252


# ------------------------------------------------------------------------ greeks

def greeks(S, F, K, T, sigma, r, is_call):
    df = math.exp(-r * T)
    sd = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sd * sd) / sd
    fs = F / S                                   # dF/dS with carry fixed
    delta = df * fs * (norm.cdf(d1) if is_call else norm.cdf(d1) - 1)
    gamma = df * fs * fs * norm.pdf(d1) / (F * sd)
    vega = df * F * norm.pdf(d1) * math.sqrt(T) / 100
    dt = 1 / 365
    if T > dt:
        carry = math.log(F / S) / T              # keep the same carry rate per year
        F2 = S * math.exp(carry * (T - dt))
        p_now = black76(F, K, T, sigma, df, is_call)
        p_next = black76(F2, K, T - dt, sigma, math.exp(-r * (T - dt)), is_call)
        theta = p_next - p_now
    else:
        theta = -black76(F, K, T, sigma, df, is_call)
    return delta, gamma, theta, vega


# ---------------------------------------------------------------- realised moves

def load_daily(path):
    with open(path) as f:
        rows = list(csv.DictReader(l for l in f if not l.startswith("#")))
    d = np.array([r["date"] for r in rows])
    o, h, l, c = (np.array([float(r[k]) for r in rows]) for k in ("open", "high", "low", "close"))
    return d, o, h, l, c


def realised(o, h, l, c):
    ret = np.diff(np.log(c))
    out = {}
    for n in (5, 10, 20, 60):
        if len(ret) >= n:
            out[f"cc_{n}"] = ret[-n:].std(ddof=1) * math.sqrt(TRADING_DAYS)
    # Parkinson and Garman-Klass (range based, more efficient) over 20 days
    hl = np.log(h / l)[-20:]
    co = np.log(c / o)[-20:]
    out["park_20"] = math.sqrt((hl ** 2).mean() / (4 * math.log(2)) * TRADING_DAYS)
    out["gk_20"] = math.sqrt((0.5 * hl ** 2 - (2 * math.log(2) - 1) * co ** 2).mean() * TRADING_DAYS)
    return ret, out


# ------------------------------------------------------------------------ table

def make_table(chains, points, S, rate):
    """Per-strike rows: IV from the OTM quote, prices, OI (lots) and Greeks for call and put."""
    iv_at = {(p.expiry, p.strike): p for p in points}
    table = []
    for e in sorted(chains):
        oc = chains[e]["oc"]
        for ks in sorted(oc, key=float):
            K = float(ks)
            p = iv_at.get((e, K))
            if p is None:
                continue
            ce, pe = oc[ks]["ce"], oc[ks]["pe"]
            row = dict(expiry=e, days=p.T * 365, strike=K, forward=p.forward, iv=p.iv,
                       ce_ltp=ce["last_price"], pe_ltp=pe["last_price"],
                       ce_oi=ce["oi"] // LOT, pe_oi=pe["oi"] // LOT)
            for side, is_call in (("ce", True), ("pe", False)):
                d, g, t, v = greeks(S, p.forward, K, p.T, p.iv, rate, is_call)
                row.update({f"{side}_delta": d, f"{side}_gamma": g,
                            f"{side}_theta": t, f"{side}_vega": v})
            table.append(row)
    return table


def expiry_info(table, S, now):
    """Light per-expiry facts (forward, ATM IV, straddle move) without needing price history."""
    out = []
    for e in sorted({r["expiry"] for r in table}):
        rows = [r for r in table if r["expiry"] == e]
        F = rows[0]["forward"]
        atm = min(rows, key=lambda r: abs(r["strike"] - F))
        ks = np.array([math.log(r["strike"] / F) for r in rows])
        straddle = atm["ce_ltp"] + atm["pe_ltp"]
        out.append(dict(expiry=e, days=rows[0]["days"], forward=F, atm_strike=atm["strike"],
                        atm_iv=float(np.interp(0.0, ks, [r["iv"] for r in rows])),
                        straddle=straddle, implied_move=straddle / S,
                        tdays=max(int(np.busday_count(now.date().isoformat(), e)), 1)))
    return out


# ------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chain", required=True)
    ap.add_argument("--daily", required=True)
    ap.add_argument("--spot", type=float, required=True)
    ap.add_argument("--asof", required=True, help="'YYYY-MM-DD HH:MM' IST")
    ap.add_argument("--today-ohlc", help="open,high,low of today's session so far")
    ap.add_argument("--rate", type=float, default=0.055)
    ap.add_argument("--out", default="output")
    ap.add_argument("--page", help="also write a body-only fragment for publishing as a web page")
    ap.add_argument("--costs", default=str(Path(__file__).with_name("costs.json")),
                    help="broker charges and slippage settings (JSON)")
    ap.add_argument("--rules", default=str(Path(__file__).with_name("rules.json")),
                    help="exit and alert thresholds (JSON)")
    ap.add_argument("--positions", help="CSV of open positions (expiry,strike,type,lots,entry_price); "
                                        "only used to detect which logged suggestions you took")
    ap.add_argument("--log-dir", default=str(LOG_DIR), help="suggestion log and tracked trades")
    ap.add_argument("--vix", help="India VIX daily OHLC CSV (same sessions as --daily)")
    ap.add_argument("--vix-now", type=float, help="India VIX at --asof (default: last close)")
    ap.add_argument("--prev-chain", help="earlier chain snapshot for OI change")
    a = ap.parse_args()

    now = datetime.strptime(a.asof, "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    S = a.spot
    chains = load_csv(a.chain, S)
    points = []
    for e, data in chains.items():
        points += chain_points(e, data, now, a.rate, 0.10)
    k_grid, T_grid, IV, _, _ = build_grid(points)
    expiries = sorted(chains)

    table = make_table(chains, points, S, a.rate)

    # ---- per-expiry summary: ATM IV, straddle, implied vs realised move, liquidity
    d, o, h, l, c = load_daily(a.daily)
    ret, rv = realised(o, h, l, c)
    prev_close = c[-1]
    summary = []
    for e in expiries:
        rows = [r for r in table if r["expiry"] == e]
        F = rows[0]["forward"]
        atm = min(rows, key=lambda r: abs(r["strike"] - F))
        ks = np.array([math.log(r["strike"] / F) for r in rows])
        ivs = np.array([r["iv"] for r in rows])
        atm_iv = float(np.interp(0.0, ks, ivs))
        T = rows[0]["days"] / 365
        straddle = atm["ce_ltp"] + atm["pe_ltp"]
        tdays = max(int(np.busday_count(now.date().isoformat(), e)), 1)
        # realised: |log move| over tdays-length windows in history
        if len(c) > tdays:
            moves = np.abs(np.log(c[tdays:] / c[:-tdays]))
            med_move, p_exceed = float(np.median(moves)), float((moves > straddle / S).mean())
            n_win = len(moves)
        else:
            med_move, p_exceed, n_win = float("nan"), float("nan"), 0
        rv_move = rv["cc_20"] * math.sqrt(tdays / TRADING_DAYS) * math.sqrt(2 / math.pi)
        tot_oi = sum(r["ce_oi"] + r["pe_oi"] for r in rows)
        near = [r for r in rows if abs(r["strike"] / F - 1) <= 0.02]
        summary.append(dict(
            expiry=e, days=T * 365, tdays=tdays, forward=F, atm_strike=atm["strike"],
            atm_iv=atm_iv, straddle=straddle, implied_move=straddle / S,
            implied_1sd=atm_iv * math.sqrt(T), rv_move=rv_move, hist_median_move=med_move,
            hist_exceed=p_exceed, hist_windows=n_win, oi_lots=tot_oi,
            oi_near_lots=sum(r["ce_oi"] + r["pe_oi"] for r in near),
            atm_theta_lot=(atm["ce_theta"] + atm["pe_theta"]) * LOT,
            atm_vega_lot=(atm["ce_vega"] + atm["pe_vega"]) * LOT,
            atm_gamma_lot=(atm["ce_gamma"] + atm["pe_gamma"]) * LOT))

    today = None
    if a.today_ohlc:
        to, th, tl = map(float, a.today_ohlc.split(","))
        sigma_day = summary[0]["atm_iv"] / math.sqrt(TRADING_DAYS)
        move = math.log(S / prev_close)
        today = dict(open=to, high=th, low=tl, last=S, prev_close=prev_close,
                     move=move, range=math.log(th / tl), sigma_day=sigma_day,
                     z=move / sigma_day)

    costs = load_costs(a.costs)
    rules = load_rules(a.rules)
    nifty = eng.load_ohlc(a.daily)
    vixd = eng.load_ohlc(a.vix) if a.vix else None
    vix_prev = float(vixd["close"][-1]) if vixd else summary[0]["atm_iv"] * 100
    vix_now = a.vix_now or vix_prev
    vf = eng.vol_forecast(np.append(nifty["close"], S))
    E_prev = eng.expected_move(prev_close, vix_prev)          # yesterday's forecast for today
    Z = (S - prev_close) / E_prev
    E_next = eng.expected_move(S, vix_now)                    # forecast for the next session
    rb = eng.range_backtest(nifty, vixd) if vixd else None
    ideas, target = eng.candidates(table, summary, S, vix_now, rules)
    rank(ideas, S, vf["forecast"], costs, alt_sigma=rv["cc_5"])
    edge = eng.vol_edge(target["atm_iv"], vf["forecast"], rules)
    prev_day = dict(high=float(nifty["high"][-1]), low=float(nifty["low"][-1]))
    reg = eng.regime(S, prev_day, vf, Z, rules)
    gex = eng.gex_profile(table, S)
    pm_front = eng.pcr_maxpain(table, summary[0]["expiry"])
    pm_target = eng.pcr_maxpain(table, target["expiry"])
    prev_table = None
    if a.prev_chain:
        pch = load_csv(a.prev_chain, S)
        ppts = []
        for e, dd in pch.items():
            ppts += chain_points(e, dd, now, a.rate, 0.10)
        prev_table = make_table(pch, ppts, S, a.rate)
    oic = eng.oi_change(table, prev_table, target["expiry"])
    skew = (_skew(table, target))
    decision = eng.decide(ideas, reg, edge, gex, S, rules,
                          events_today=now.date().isoformat() in rules.get("event_dates", []))
    for i in ideas:
        i["best"] = i is decision["trade"]
        i["exit"] = exit_plan(i, rules, now.date())
        i["reasons"] = _engine_reasons(i, reg, edge, rb, gex, S)
        i["fit"] = "Pass" if i["gate"]["pass"] else "Fail"
        x = i["exit"]
        stop_pnl = (i["net"] - x["sl_close_cost"]) if x["kind"] == "credit" else (x["sl_value"] + i["net"])
        i["dd"] = drawdown(i["legs"], S, vf["forecast"], x["exit_date"], now.date(),
                           cost=i["eval"]["cost"], stop_loss=stop_pnl - i["eval"]["cost"])
    ideas.sort(key=lambda i: (not i["best"], not i["gate"]["pass"], -i["eval"]["ev_on_risk"]))
    best = decision["trade"]
    plog = Path(a.log_dir) / "predictions.jsonl"
    eng.log_prediction(plog, now.date().isoformat(), S, vix_now, E_next, reg, edge, gex, decision)
    score = eng.score_predictions(plog, nifty)
    engine = dict(vix_prev=vix_prev, vix_now=vix_now, E_prev=E_prev, Z=Z, E_next=E_next, vf=vf, rb=rb,
                  edge=edge, reg=reg, gex=gex, pm_front=pm_front, pm_target=pm_target, oic=oic,
                  skew=skew, decision=decision, target=target, score=score, prev_close=prev_close,
                  rules=rules)
    log_suggestions(ideas, now, S, a.log_dir)
    started = match_positions(load_positions(a.positions), now, a.log_dir) if a.positions else []
    tracked = monitor(table, summary, S, now, rules, a.log_dir, only_new=False)
    log = read_suggestions(a.log_dir)
    rows_by = {(r["expiry"], r["strike"]): r for r in table}
    for t, snap, alerts, _ in tracked:
        legs_now = []
        for l in t["legs"]:
            r = rows_by.get((l["expiry"], l["strike"]))
            if r is None:
                break
            side = "ce" if l["type"] == "CE" else "pe"
            legs_now.append(dict(l, price=r[f"{side}_ltp"], iv=r["iv"], F=r["forward"], T=r["days"] / 365))
        else:
            x, m = t["exit"], t["multiplier"]
            value = snap["value"]
            stop = (-(x["sl_close_cost"] * m - (-value)) if x["kind"] == "credit"
                    else (x["sl_value"] * m - value))
            t["dd"] = drawdown(legs_now, S, vf["forecast"], x["exit_date"], now.date(), 0.0, stop)

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    tag = now.strftime("%Y-%m-%d")
    write_tables(out, tag, table, summary)
    limits = limitations(engine, ideas, costs, nifty)
    extra = (engine_section(engine, S, summary) + extra_sections(ideas, tracked, log, best, costs)
             + "<section><h2>Limitations of these recommendations</h2><ol class='limits'>"
             + "".join(f"<li>{x}</li>" for x in limits) + "</ol></section>")
    html = write_dashboard(out, tag, now, S, points, k_grid, T_grid, IV, table, summary,
                           d, c, ret, rv, today, a.page, extra, engine_figs(engine, S))
    xlsx = write_workbook(out / "download" / f"nifty_dashboard_{tag}.xlsx", now, S, summary,
                          rv, table, ideas, tracked, log, costs, limits)
    for i in ideas:
        e = i["eval"]
        print(f"{'BEST ' if i['best'] else '     '}[{i['fit']}] {i['name']} {i['expiry']}: "
              f"net ₹{i['net']:,.0f}, costs ₹{e['cost']:,.0f}, EV ₹{e['ev']:,.0f} "
              f"(EV/risk {e['ev_on_risk']*100:+.1f}%, POP {e['pop']*100:.0f}%)")
    d_ = engine["decision"]
    print(f"ENGINE: regime {reg['label']}{' ' + reg['direction'] if reg['direction'] else ''} (Z {Z:+.2f}), "
          f"vol edge {edge['label']} (R_IV {edge['ratio']:.2f}), GEX {gex['regime']} flip "
          f"{gex['flip'] and round(gex['flip'])}, decision: {d_['label']}")
    for m in d_["matrix"]:
        print(f"  - {m}")
    for t in started:
        print(f"Now tracking {t['id']} {t['name']} ×{t['multiplier']} (found in your positions)")
    for t, snap, alerts, _ in tracked:
        print(f"TRACKED {t['id']} {t['name']} ×{t['multiplier']}: P&L ₹{snap['pnl']:,.0f}")
        for al in alerts:
            print(f"  ALERT [{al['level']}] {al['kind']}: {al['msg']}")
    print(f"Workbook: {xlsx.resolve()}")
    print_report(summary, rv, today)
    print(f"\nDashboard: {html.resolve()}")



# ------------------------------------------------------------------ engine

def _skew(table, target):
    rows = [r for r in table if r["expiry"] == target["expiry"]]
    F = target["forward"]
    ks = np.array([math.log(r["strike"] / F) for r in rows])
    ivs = [r["iv"] for r in rows]
    at = lambda m: float(np.interp(math.log(m), ks, ivs))
    return dict(put=at(0.96) - at(1.0), call=at(1.04) - at(1.0))


def _engine_reasons(i, reg, edge, rb, gex, S):
    r = [f"Regime {reg['label']} ({'; '.join(reg['reasons'])}).",
         f"{i['expiry']} ATM IV {edge['atm_iv']*100:.1f}% vs forecast realised {edge['forecast']*100:.1f}% "
         f"→ R_IV {edge['ratio']:.2f} ({edge['label']})."]
    if i["family"] == "condor" and rb:
        a = rb["by"][0]
        r.append(f"VIX one-day range held the close {a['close_inside']*100:.0f}% of {a['n']} sessions "
                 f"(≈68% expected); realised/implied move ratio {a['ratio']:.2f}.")
    if gex["flip"]:
        r.append(f"Estimated gamma flip {gex['flip']:,.0f}; spot is {'above' if S > gex['flip'] else 'below'} "
                 f"it ({gex['regime']} total GEX, sign convention assumed).")
    g = i["gate"]
    r.append("Risk gate: " + ("PASS" if g["pass"] else "FAIL — " + "; ".join(g["notes"])) +
             f" (budget ₹{g['budget']:,.0f} → {g['lots']} lot(s)).")
    return r


def _tile(title, value, cls, detail):
    return (f"<div class='tile'><span class='eyebrow'>{title}</span><b class='{cls}'>{value}</b>"
            f"<span class='muted'>{detail}</span></div>")


def engine_section(en, S, summary):
    d, reg, edge, gex, rb = en["decision"], en["reg"], en["edge"], en["gex"], en["rb"]
    trade = d["trade"]
    gate_val = "Fail (advisory)" if d.get("override") else "Pass" if trade else "Fail"
    tiles = "".join([
        _tile("Forecast regime", reg["label"] + (f" ({reg['direction']})" if reg["direction"] else ""),
              "warn" if reg["label"] == "Mixed" else "good",
              f"Z {en['Z']:+.2f} · 5d/20d vol {reg['expansion']:.2f}×"),
        _tile("Volatility edge", edge["label"], "good" if edge["label"] != "Fair" else "warn",
              f"R_IV {edge['ratio']:.2f} = {edge['atm_iv']*100:.1f}% / {edge['forecast']*100:.1f}%"),
        _tile("Positioning", f"{gex['regime'].title()} GEX", "warn",
              (f"flip {gex['flip']:,.0f} · " if gex['flip'] else "") +
              f"PCR {en['pm_target']['pcr']:.2f} · max pain {en['pm_target']['max_pain']:,.0f}"),
        _tile("Risk gate", gate_val, "bad" if (d.get("override") or not trade) else "good",
              f"budget ₹{d['budget']:,.0f} ({en['rules']['risk_pct']}% of ₹{en['rules']['capital']:,.0f})"),
    ])
    if trade:
        g, e, x = trade["gate"], trade["eval"], trade["exit"]
        legs = "; ".join(f"{'buy' if l['lots'] > 0 else 'sell'} {l['strike']:.0f} {l['type']} @ {l['price']:.2f}"
                         for l in trade["legs"])
        dd = trade["dd"]
        warn = ""
        if d.get("override"):
            warn = (" <span class='pill warn'>Gate failed</span> Shown because the gate is set to advisory "
                    "(ignore_gate): " + "; ".join(g["notes"]) + ".")
        final = (f"<p class='decision {'warn' if d.get('override') else 'good'}'><b>{trade['name']} — {trade['expiry']}</b> "
                 f"({trade.get('id', '')}): {legs}. Size {max(g['lots'], 1)} lot(s). EV after costs {_rs(e['ev'])}/lot, "
                 f"max loss {_rs(-g['max_loss'])}/lot, expected max drawdown {_rs(dd['mean'])} "
                 f"(worst 5%: {_rs(dd['p95'])}) over {dd['days']} trading days, chance of hitting the stop "
                 f"{dd.get('p_stop', 0)*100:.0f}%. Invalidation: {x['stop_loss']}. Exit by {x['exit_date']}.{warn}</p>")
    else:
        final = "<p class='decision bad'><b>NO TRADE.</b> No candidate passes every gate today.</p>"
    matrix = "".join(f"<li>{m}</li>" for m in d["matrix"])

    # expected range
    E, Z = en["E_next"], en["Z"]
    rng = (f"<p>India VIX {en['vix_now']:.2f} → one-day 1σ move ±{E:,.0f} pts: next-session range "
           f"<b>{S - E:,.0f} – {S + E:,.0f}</b>. Today NIFTY moved {S - en['prev_close']:+,.0f} pts vs the "
           f"±{en['E_prev']:,.0f} implied by yesterday's VIX ({en['vix_prev']:.2f}): <b>Z = {Z:+.2f}</b>.</p>")
    cov = ""
    if rb:
        cov = ("<div class='wrap'><table class='rank'><tr><th>Regime</th><th>Sessions</th><th>Close inside ±1σ</th>"
               "<th>Closed above</th><th>Closed below</th><th>High touched +1σ</th><th>Low touched −1σ</th>"
               "<th>|Z| &gt; 2</th><th>Realised / implied</th></tr>" + "".join(
                   f"<tr><td>{b['label']}</td><td>{b['n']}</td><td>{b['close_inside']*100:.0f}%</td>"
                   f"<td>{b['up_close']*100:.0f}%</td><td>{b['dn_close']*100:.0f}%</td>"
                   f"<td>{b['up_touch']*100:.0f}%</td><td>{b['dn_touch']*100:.0f}%</td>"
                   f"<td>{b['tail']*100:.0f}%</td><td>{b['ratio']:.2f}</td></tr>" for b in rb["by"]) +
               "</table></div><p class='notes'>A calibrated 1σ range holds the close ≈68% of the time; a "
               "realised/implied ratio below 1 means VIX overstated the moves (a volatility risk premium). "
               f"Only {rb['by'][0]['n']} sessions of history are available through the Dhan connector, so treat "
               "these as indicative; the prediction log below builds the out-of-sample record.</p>")
    vf = en["vf"]
    volp = (f"<p>Forecast realised vol {vf['forecast']*100:.1f}% (HAR blend {vf['har']*100:.1f}% of 5/20/60-day "
            f"{vf['rv'].get(5, 0)*100:.1f}/{vf['rv'].get(20, 0)*100:.1f}/{vf['rv'].get(60, 0)*100:.1f}%, "
            f"EWMA {vf['ewma']*100:.1f}%). {en['target']['expiry']} ATM IV {edge['atm_iv']*100:.1f}% → "
            f"R_IV {edge['ratio']:.2f}: rich ≥ {en['rules']['iv_rich_ratio']}, cheap ≤ {en['rules']['iv_cheap_ratio']}.</p>")
    pos = (f"<p>Estimated total GEX ₹{gex['total']:,.0f} cr per 1% move ({gex['regime']}); gamma flip "
           f"{'≈ ' + format(gex['flip'], ',.0f') if gex['flip'] else 'not within ±5%'}. "
           f"Put skew (4% OTM − ATM) {en['skew']['put']*100:+.1f} pts, call skew {en['skew']['call']*100:+.1f} pts. "
           f"PCR (OI) {summary[0]['expiry']} {en['pm_front']['pcr']:.2f}, {en['target']['expiry']} "
           f"{en['pm_target']['pcr']:.2f}; max pain {en['pm_front']['max_pain']:,.0f} / "
           f"{en['pm_target']['max_pain']:,.0f}.</p><p class='notes'>GEX assumes dealers are long calls and short "
           "puts; open interest does not reveal who holds which side, so the sign is a modelling convention. "
           "PCR and max pain are context only and are not used in the decision.</p>")
    if en["oic"]:
        o = en["oic"]
        pos += ("<p>OI change since the earlier snapshot (" + en["target"]["expiry"] + "): calls added at " +
                ", ".join(f"{r['strike']:.0f} ({r['ce']:+,})" for r in o["top_ce"][:3]) + "; puts added at " +
                ", ".join(f"{r['strike']:.0f} ({r['pe']:+,})" for r in o["top_pe"][:3]) + " (lots).</p>")
    sc = en["score"]
    score = ""
    if sc["rows"]:
        score = ("<div class='wrap'><table class='rank'><tr><th>Date</th><th>Spot</th><th>VIX</th><th>Range</th>"
                 "<th>Regime</th><th>Vol edge</th><th>Decision</th><th>Next close</th><th>Inside?</th></tr>" + "".join(
                     f"<tr><td>{p['date']}</td><td>{p['spot']:,.0f}</td><td>{p['vix']:.2f}</td>"
                     f"<td>{p['range_lo']:,.0f}–{p['range_hi']:,.0f}</td><td>{p['regime']}</td><td>{p['vol_edge']}</td>"
                     f"<td>{p['decision']}</td><td>{format(p['outcome']['close'], ',.0f') if p['outcome'] else 'pending'}</td>"
                     f"<td>{('yes' if p['outcome']['close_inside'] else 'no') if p['outcome'] else '—'}</td></tr>"
                     for p in reversed(sc["rows"])) + "</table></div>")
        if sc["n"]:
            score += (f"<p class='notes'>Scored predictions: {sc['n']} · close inside range "
                      f"{sc['close_inside']*100:.0f}% · range touched {sc['touched']*100:.0f}%.</p>")
    return f"""<section><h2>Decision engine</h2><div class="tiles">{tiles}</div>{final}
<ul class="matrix">{matrix}</ul></section>
<section><h2>Expected range (India VIX)</h2>{rng}{cov}</section>
<section><h2>Volatility edge</h2>{volp}</section>
<section><h2>Positioning</h2>{pos}</section>
<section><h2>Prediction log</h2>{score}</section>"""


def limitations(en, ideas, c, nifty):
    """Plain-language limits on today's recommendations; data-dependent items first."""
    n = len(nifty["close"])
    rb = en["rb"]
    out = []
    if en["decision"].get("override"):
        out.append("<b>Today's recommendation failed the risk gate</b> (" +
                   "; ".join(en["decision"]["trade"]["gate"]["notes"]) +
                   ") and is shown only because the gate is set to advisory. Treat it as the least-bad "
                   "candidate, not as a trade with a measured edge.")
    out += [
        f"<b>Short history.</b> The range model, realised-vol forecast and drawdown inputs rest on {n} daily "
        "sessions (the Dhan connector returns history 5 candles at a time). Coverage and VRP estimates "
        f"{'(' + str(rb['by'][0]['n']) + ' sessions) ' if rb else ''}have wide error bars, and 13 weeks cover one market regime.",
        "<b>No historical option data, so no strategy backtest.</b> Expected value, drawdown and stop "
        "probabilities come from a simulation, not from how these trades actually performed. The "
        "prediction log builds a real out-of-sample record from now on.",
        "<b>Model, not market, distribution.</b> Simulations use a driftless lognormal walk at the forecast "
        f"realised vol ({en['vf']['forecast']*100:.1f}%) with constant IV. Real NIFTY returns have fat tails, "
        "overnight gaps and volatility that rises when the market falls, so true drawdowns and stop-outs are "
        "likely larger than shown, especially for short-premium trades.",
        "<b>Drawdown is peak-to-trough.</b> Expected max drawdown counts open profit given back, so it can "
        "exceed the trade's maximum loss; it is a simulated average, not a cap.",
        "<b>IV held constant.</b> Mark-to-market assumes each leg keeps today's IV. A volatility spike widens "
        "losses on short options (iron condors) and helps long options, even if spot does not move.",
        "<b>Prices are last trades, not quotes.</b> The chain gives LTP and OI only, no bid/ask. Fills can "
        f"differ; slippage is an estimate ({c['slippage_pct']}% of premium or {c['slippage_min_ticks']} tick per "
        "order, more for thin strikes), not measured.",
        "<b>Stops are checked hourly at best.</b> Monitoring runs once an hour in market hours, and a stop "
        "price does not guarantee the exit price. Gaps and fast markets can exit well beyond the stop. Keep "
        "broker-side stop orders.",
        "<b>GEX sign is an assumption.</b> Open interest does not show who is long or short; the gamma flip "
        "and GEX regime use a calls-positive / puts-negative convention and have not been tested for "
        "predictive value.",
        "<b>Fixed thresholds.</b> Rich/cheap ratios, regime cut-offs, short deltas, wing widths and exit "
        "rules in rules.json are reasonable defaults, not optimised or validated values.",
        "<b>Not modelled:</b> margin requirements and margin calls, events (RBI policy, budget, results, "
        "global shocks) unless listed in event_dates, early assignment, exercise settlement details beyond "
        "STT, leg risk when orders fill one at a time, and taxes on profits.",
        "<b>Position size uses a placeholder budget</b> (capital × risk % in rules.json) until you set your own.",
        "<b>Exit dates skip weekends but not NSE holidays.</b> Check an exit date that falls on a holiday.",
        "These are model outputs for research and education, not investment advice.",
    ]
    return out


def engine_figs(en, S):
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    gex = en["gex"]
    f = make_subplots(rows=1, cols=2, column_widths=[0.55, 0.45],
                      subplot_titles=("Estimated GEX by strike (₹ cr per 1%)", "Total GEX vs spot (gamma flip)"))
    ks = list(gex["by_strike"].keys())
    vs = list(gex["by_strike"].values())
    f.add_trace(go.Bar(x=ks, y=vs, marker_color=["#2ca02c" if v >= 0 else "#d62728" for v in vs],
                       name="GEX", showlegend=False), row=1, col=1)
    f.add_trace(go.Scatter(x=gex["grid"], y=gex["profile"], mode="lines", line=dict(color="#4c78a8"),
                           name="total GEX", showlegend=False), row=1, col=2)
    for col in (1, 2):
        f.add_vline(x=S, line_dash="dot", line_color="#8a919c", row=1, col=col)
    if gex["flip"]:
        f.add_vline(x=gex["flip"], line_dash="dash", line_color="#e45756", row=1, col=2,
                    annotation_text=f"flip {gex['flip']:,.0f}", annotation_position="bottom right")
    f.update_layout(title="Positioning (sign convention: calls +, puts −; an assumption)", height=420)
    figs = [f]
    if en["rb"]:
        rows = en["rb"]["rows"]
        g = go.Figure()
        g.add_trace(go.Bar(x=[r["date"] for r in rows], y=[r["z"] for r in rows], name="Z (move / VIX 1σ)",
                           marker_color=["#d62728" if abs(r["z"]) > 1 else "#4c78a8" for r in rows]))
        for y in (1, -1):
            g.add_hline(y=y, line_dash="dash", line_color="#8a919c")
        g.update_layout(title="Daily move in units of the VIX-implied 1σ (red = outside the range)", height=380,
                        yaxis_title="Z")
        figs.append(g)
    return figs

# ------------------------------------------------------------------------ output

def _px(v):
    return "—" if v is None else f"{v:.2f}"


def _rs(v):
    if v is None:
        return "—"
    if math.isinf(v):
        return "unlimited"
    return f"−₹{-v:,.0f}" if v < 0 else f"₹{v:,.0f}"


def extra_sections(ideas, tracked, log, best, c):
    cls = {"Favoured": "good", "Neutral": "warn", "Not favoured": "bad"}
    cards = []
    for i in ideas:
        legs = "".join(
            f"<tr><td>{'Buy' if l['lots'] > 0 else 'Sell'} {abs(l['lots']):g}</td>"
            f"<td>{l['expiry']}</td><td>{l['strike']:.0f} {l['type']}</td>"
            f"<td>{l['price']:.2f}</td><td>{l['oi']:,}</td></tr>" for l in i["legs"])
        e = i["eval"]
        ch = e["charges"]
        be = ", ".join(f"{b:,.0f}" for b in e.get("breakevens_after") or i.get("breakevens") or []) or "—"
        net = f"{'Credit' if i['net'] > 0 else 'Debit'} {_rs(abs(i['net']))}"
        badge = ('<span class="pill best">Engine pick</span>' if i["best"] else
                 f'<span class="pill {"good" if i["gate"]["pass"] else "bad"}">Gate {"pass" if i["gate"]["pass"] else "fail"}</span>')
        mp = e.get("max_profit_after", i.get("max_profit"))
        ml = e.get("max_loss_after", i.get("max_loss"))
        costs_html = (
            f"<table class='costs'><tr><th>Charges per lot ({'round trip' if c['orders_per_leg'] >= 2 else 'held to expiry'})</th><th></th></tr>"
            f"<tr><td>Brokerage</td><td>{_rs(ch['brokerage'])}</td></tr>"
            f"<tr><td>STT (sell side)</td><td>{_rs(ch['stt'])}</td></tr>"
            f"<tr><td>Exchange + SEBI</td><td>{_rs(ch['exchange'] + ch['sebi'])}</td></tr>"
            f"<tr><td>Stamp duty (buy side)</td><td>{_rs(ch['stamp'])}</td></tr>"
            f"<tr><td>GST</td><td>{_rs(ch['gst'])}</td></tr>"
            f"<tr><td>Slippage (est.)</td><td>{_rs(e['slippage'])}</td></tr>"
            f"<tr class='total'><td>Total cost</td><td>{_rs(e['cost'])}</td></tr></table>")
        ev_html = (f"<p class='figs ev {'up' if e['ev'] > 0 else 'down'}'>Expected P&amp;L after costs "
                   f"{_rs(e['ev'])} at {e['sigma']*100:.1f}% vol (20d realised)"
                   + (f" · {_rs(e['ev_alt'])} at {e['alt_sigma']*100:.1f}% (5d)" if 'ev_alt' in e else "")
                   + f"<br>Chance of profit {e['pop']*100:.0f}% · EV / risk {e['ev_on_risk']*100:+.1f}%"
                   + f" · 5% worst case {_rs(e['p5'])}</p>")
        cards.append(f"""<article class="idea">
<header>{badge}<h3>{i['name']}</h3>
<span class="muted id">{i.get('id', '')}</span>
<span class="muted">{i['expiry']} · {i['view']}</span></header>
<ul>{''.join(f'<li>{r}</li>' for r in i['reasons'])}</ul>
<div class="wrap"><table><tr><th>Leg</th><th>Expiry</th><th>Strike</th><th>LTP</th><th>OI (lots)</th></tr>{legs}</table></div>
<p class="figs">{net} per lot before costs · {_rs(e['net_after'])} after costs<br>
After costs: max profit {_rs(mp)} · max loss {_rs(ml)} · breakeven {be}<br>
Δ {i['delta']:+.1f} · Γ {i['gamma']:+.3f} · Θ {_rs(i['theta'])}/day · vega {_rs(i['vega'])}/vol-pt (per lot)</p>
{ev_html}<p class="figs">Expected max drawdown {_rs(i['dd']['mean'])} · worst 5% {_rs(i['dd']['p95'])}
· over {i['dd']['days']} trading days to {i['exit']['exit_date']} · P(stop hit) {i['dd'].get('p_stop', 0)*100:.0f}%</p>
<div class="exit"><b>Exit plan</b><ul>
<li><span class="tag">Take profit</span> {i['exit']['take_profit']}</li>
<li><span class="tag">Stop loss</span> {i['exit']['stop_loss']}</li>
<li><span class="tag">Exit by</span> {i['exit']['time_exit']}</li></ul></div>
<div class="wrap">{costs_html}</div>
</article>""")
    rank_rows = "".join(
        f"<tr{' class=\'bestrow\'' if i['best'] else ''}><td>{n}</td><td>{i['name']}</td><td>{i['expiry']}</td>"
        f"<td>{_rs(i['net'])}</td><td>{_rs(i['eval']['cost'])}</td><td>{_rs(i['eval']['ev'])}</td>"
        f"<td>{_rs(i['eval'].get('ev_alt'))}</td><td>{i['eval']['pop']*100:.0f}%</td>"
        f"<td>{_rs(i['eval']['risk'])}</td><td>{i['eval']['ev_on_risk']*100:+.1f}%</td></tr>"
        for n, i in enumerate(ideas, 1))
    if best:
        verdict = (f"<p class='today'><b>Engine pick: {best['name']} ({best['expiry']}).</b> "
                   f"Expected {_rs(best['eval']['ev'])} per lot after {_rs(best['eval']['cost'])} of charges "
                   f"and slippage, {best['eval']['ev_on_risk']*100:+.1f}% of the capital at risk, "
                   f"{best['eval']['pop']*100:.0f}% chance of profit.</p>")
    else:
        verdict = ("<p class='today'><b>No candidate passes every gate today (NO TRADE).</b> "
                   "The cards show which gate each one fails.</p>")
    sigma = ideas[0]["eval"]["sigma"] if ideas else 0
    html = ('<section><h2>Strategy ideas</h2>' + verdict +
            '<div class="wrap"><table class="rank"><tr><th>Rank</th><th>Strategy</th><th>Expiry</th><th>Net ₹/lot</th>'
            '<th>Costs</th><th>EV (20d vol)</th><th>EV (5d vol)</th><th>P(profit)</th><th>Risk</th>'
            f'<th>EV / risk</th></tr>{rank_rows}</table></div>'
            f'<p class="notes">Ranked by expected P&amp;L after {c["broker"]} charges (₹{c["brokerage_per_lot_per_order"]:.0f}/lot/order, '
            f'STT {c["stt_sell_pct"]}% sell, exchange {c["exchange_pct"]}%, SEBI {c["sebi_pct"]}%, stamp {c["stamp_buy_pct"]}% buy, '
            f'GST {c["gst_pct"]:.0f}%, {c["orders_per_leg"]} order(s) per leg) and slippage '
            f'(max of {c["slippage_min_ticks"]} tick or {c["slippage_pct"]}% of premium per order, ×2 below '
            f'{c["liquid_oi_lots"]:,} lots OI, ×4 below {c["thin_oi_lots"]:,}), divided by the capital at risk. '
            f'EV assumes NIFTY moves at its 20-day realised vol ({sigma*100:.1f}%) to the first expiry; '
            'the 5-day column shows the same with recent, higher vol. Prices are last trades, not quotes; '
            'margin and events are not modelled. These are ideas to check, not advice.</p>'
            f'<div class="ideas">{"".join(cards)}</div></section>')
    lv = {"action": "bad", "warn": "warn", "info": "good"}
    blocks = []
    for t, snap, alerts, _ in tracked:
        legs = "".join(
            f"<tr><td>{'Buy' if l['lots'] > 0 else 'Sell'} {abs(l['lots']):g}</td><td>{l['expiry']}</td>"
            f"<td>{l['strike']:.0f} {l['type']}</td><td>{l['entry']:.2f}</td><td>{_px(l.get('ltp'))}</td></tr>"
            for l in [dict(l, ltp=_ltp_now(l, snap)) for l in t["legs"]])
        al = ("<ul class='alerts'>" + "".join(
            f"<li class='{lv[a['level']]}'><span class='pill {lv[a['level']]}'>{a['kind']}</span> {a['msg']}</li>"
            for a in alerts) + "</ul>") if alerts else "<p class='notes'>Inside its exit plan: no action needed.</p>"
        x = t["exit"]
        blocks.append(f"""<article class="idea"><header><h3>{t['name']} ×{t['multiplier']}</h3>
<span class="muted id">{t['id']} · taken {t['taken']} ({t['source']})</span></header>
<p class="figs {'up' if snap['pnl'] >= 0 else 'down'}">P&amp;L {_rs(snap['pnl'])} · entry {'credit' if t['entry_net'] > 0 else 'debit'} {_rs(abs(t['entry_net']))}
· net Δ {snap['delta']:+.0f} · checked {snap['time']} at spot {snap['spot']:,.2f}</p>
{al}{_dd_line(t)}<div class="wrap"><table><tr><th>Leg</th><th>Expiry</th><th>Strike</th><th>Entry</th><th>Now</th></tr>{legs}</table></div>
<p class="figs">{_levels(t)}</p></article>""")
    tracked_html = ("<div class='ideas'>" + "".join(blocks) + "</div>") if blocks else (
        "<p class='notes'>None of the logged suggestions is being tracked. When you enter one, it is "
        "picked up from your positions automatically, or tell me its ID (for example "
        f"{ideas[0].get('id', 'S20261009-1') if ideas else 'S20261009-1'}) and your fill prices.</p>")
    taken = {t["id"] for t, *_ in tracked}
    recent = sorted(log, key=lambda s: s["id"], reverse=True)[:20]
    log_rows = "".join(
        f"<tr{' class=\'bestrow\'' if s['best'] else ''}><td>{s['id']}</td><td>{s['date']} {s['time']}</td>"
        f"<td>{s['name']}</td><td>{s['expiry']}</td><td>{s['fit']}</td><td>{_rs(s['net'])}</td>"
        f"<td>{_rs(s['ev'])}</td><td>{s['exit']['exit_date']}</td>"
        f"<td>{'Tracking' if s['id'] in taken else 'Best' if s['best'] else '—'}</td></tr>" for s in recent)
    html += f"""<section><h2>Tracked trades</h2>{tracked_html}</section>
<section><h2>Suggestion log</h2><div class="wrap"><table class="rank"><tr><th>ID</th><th>Logged</th>
<th>Strategy</th><th>Expiry</th><th>Fit</th><th>Net ₹/lot</th><th>EV after costs</th><th>Exit by</th>
<th>Status</th></tr>{log_rows}</table></div>
<p class="notes">Every idea is logged with its prices and exit plan in logs/suggestions.jsonl. Only suggestions
you actually take are monitored.</p></section>"""
    return html


def _dd_line(t):
    dd = t.get("dd")
    if not dd:
        return ""
    return (f"<p class='figs'>From here to {t['exit']['exit_date']} ({dd['days']} trading days): expected max "
            f"drawdown {_rs(dd['mean'])}, worst 5% {_rs(dd['p95'])}, chance of hitting the stop "
            f"{dd.get('p_stop', 0)*100:.0f}%.</p>")


def _levels(t):
    """Exit levels for the whole tracked position, re-based on the actual fills."""
    x, m = t["exit"], t["multiplier"]
    shorts = ", ".join(f"{k:.0f} {ty}" for k, ty in x.get("short_strikes", []))
    if x["kind"] == "credit":
        txt = (f"Take profit when buy-back ≤ {_rs(x['tp_close_cost'] * m)} · warn at {_rs(x['warn_close_cost'] * m)}"
               f" · stop at {_rs(x['sl_close_cost'] * m)}")
    else:
        txt = (f"Take profit when worth ≥ {_rs(x['tp_value'] * m)} · warn at {_rs(x['warn_value'] * m)}"
               f" · stop at {_rs(x['sl_value'] * m)}")
    return txt + (f" · spot trigger: short {shorts}" if shorts else "") + f" · exit by {x['exit_date']}"


def _ltp_now(leg, snap):
    return snap.get("ltps", {}).get(f"{leg['expiry']}|{leg['strike']:.0f}|{leg['type']}")


def _xl(v):
    return None if v is None else "unlimited" if math.isinf(v) else round(v)


def write_workbook(path, now, S, summary, rv, table, ideas, tracked, log, costs, limits=()):
    from openpyxl import Workbook
    from openpyxl.styles import Font
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.append([f"NIFTY 50 options, {now:%Y-%m-%d %H:%M} IST, spot {S:,.2f}"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([])
    ws.append(["Expiry", "Days", "ATM strike", "ATM IV %", "Straddle", "Implied move %",
               "RV20 move %", "Hist median %", "Windows", "P(move>straddle) %", "OI lots"])
    for s in summary:
        ws.append([s["expiry"], round(s["days"], 1), s["atm_strike"], round(s["atm_iv"] * 100, 2),
                   s["straddle"], round(s["implied_move"] * 100, 2), round(s["rv_move"] * 100, 2),
                   round(s["hist_median_move"] * 100, 2), s["hist_windows"],
                   round(s["hist_exceed"] * 100), s["oi_lots"]])
    ws.append([])
    ws.append(["Realised vol", "Annualised %"])
    for k, v in rv.items():
        ws.append([k, round(v * 100, 2)])

    ws = wb.create_sheet("Strategies")
    ws.append(["Rank", "Best", "Fit", "Strategy", "Expiry", "View", "Net ₹/lot (+credit)",
               "Brokerage", "STT", "Exchange+SEBI", "Stamp", "GST", "Slippage", "Total cost",
               "Net after costs", "EV after costs (20d vol)", "EV after costs (5d vol)", "P(profit)",
               "Risk ₹", "EV / risk", "Max profit after costs", "Max loss after costs",
               "Breakevens after costs", "Delta", "Gamma", "Theta ₹/day", "Vega ₹/pt", "Legs", "Reasons",
               "Take profit", "Stop loss", "Exit by date", "Time exit",
               "Exp. max drawdown ₹", "Worst-5% drawdown ₹", "P(stop hit)", "Gate", "Gate notes"])
    for n, i in enumerate(ideas, 1):
        e = i["eval"]
        ch = e["charges"]
        ws.append([n, "BEST" if i["best"] else "", i["fit"], i["name"], i["expiry"], i["view"],
                   round(i["net"]), round(ch["brokerage"]), round(ch["stt"], 2),
                   round(ch["exchange"] + ch["sebi"], 2), round(ch["stamp"], 2), round(ch["gst"], 2),
                   round(e["slippage"]), round(e["cost"]), round(e["net_after"]), round(e["ev"]),
                   _xl(e.get("ev_alt")), round(e["pop"], 3), round(e["risk"]), round(e["ev_on_risk"], 4),
                   _xl(e.get("max_profit_after", i.get("max_profit"))),
                   _xl(e.get("max_loss_after", i.get("max_loss"))),
                   ", ".join(f"{b:,.0f}" for b in e.get("breakevens_after") or i.get("breakevens") or []),
                   round(i["delta"], 1), round(i["gamma"], 4), round(i["theta"]), round(i["vega"]),
                   "; ".join(f"{'Buy' if l['lots'] > 0 else 'Sell'} {l['strike']:.0f}{l['type']} @ {l['price']}"
                             for l in i["legs"]),
                   " ".join(i["reasons"]), i["exit"]["take_profit"], i["exit"]["stop_loss"],
                   i["exit"]["exit_date"], i["exit"]["time_exit"],
                   round(i["dd"]["mean"]), round(i["dd"]["p95"]), round(i["dd"].get("p_stop", 0), 3),
                   "pass" if i["gate"]["pass"] else "fail", "; ".join(i["gate"]["notes"])])

    ws = wb.create_sheet("Greeks")
    ws.append(["Expiry", "Strike", "IV %", "CE LTP", "CE OI lots", "CE delta", "CE gamma",
               "CE theta", "CE vega", "PE LTP", "PE OI lots", "PE delta", "PE gamma",
               "PE theta", "PE vega"])
    for r in table:
        ws.append([r["expiry"], r["strike"], round(r["iv"] * 100, 2), r["ce_ltp"], r["ce_oi"],
                   round(r["ce_delta"], 4), round(r["ce_gamma"], 6), round(r["ce_theta"], 2),
                   round(r["ce_vega"], 2), r["pe_ltp"], r["pe_oi"], round(r["pe_delta"], 4),
                   round(r["pe_gamma"], 6), round(r["pe_theta"], 2), round(r["pe_vega"], 2)])

    ws = wb.create_sheet("Limitations", 1)
    ws.append(["#", "Limitation"])
    for n, x in enumerate(limits, 1):
        ws.append([n, re.sub("<[^>]+>", "", x)])
    ws.column_dimensions["B"].width = 140
    ws = wb.create_sheet("Costs")
    ws.append(["Setting", "Value"])
    for k, v in costs.items():
        ws.append([k, v])

    ws = wb.create_sheet("Tracked trades", 1)
    ws.append(["ID", "Strategy", "Lots ×", "Taken", "Entry net ₹", "P&L ₹", "Net delta", "Checked",
               "Spot", "Exit by", "Alerts"])
    for t, snap, alerts, _ in tracked:
        ws.append([t["id"], t["name"], t["multiplier"], t["taken"], round(t["entry_net"]),
                   round(snap["pnl"]), round(snap["delta"]), snap["time"], snap["spot"],
                   t["exit"]["exit_date"], " | ".join(f"{a['kind']}: {a['msg']}" for a in alerts)])
    ws = wb.create_sheet("Suggestion log")
    ws.append(["ID", "Date", "Time", "Spot", "Strategy", "Expiry", "Fit", "Best", "Net ₹/lot",
               "Cost ₹", "EV ₹", "Exit by", "Take profit", "Stop loss", "Legs"])
    for s_ in sorted(log, key=lambda s: s["id"], reverse=True):
        ws.append([s_["id"], s_["date"], s_["time"], s_["spot"], s_["name"], s_["expiry"], s_["fit"],
                   "BEST" if s_["best"] else "", s_["net"], s_["cost"], s_["ev"], s_["exit"]["exit_date"],
                   s_["exit"]["take_profit"], s_["exit"]["stop_loss"],
                   "; ".join(f"{'Buy' if l['lots'] > 0 else 'Sell'} {l['expiry']} {l['strike']:.0f}{l['type']} @ {l['price']}"
                             for l in s_["legs"])])
    for sheet in wb.worksheets:
        for row in sheet.iter_rows(min_row=1, max_row=3):
            for cell in row:
                if sheet.title != "Summary" or cell.row == 3:
                    cell.font = Font(bold=True)
    wb.save(path)
    return path

def write_tables(out, tag, table, summary):
    with open(out / f"nifty_greeks_{tag}.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["expiry", "days", "strike", "iv_pct", "ce_ltp", "ce_oi_lots", "ce_delta",
                    "ce_gamma", "ce_theta_rs_day", "ce_vega_rs_volpt", "pe_ltp", "pe_oi_lots",
                    "pe_delta", "pe_gamma", "pe_theta_rs_day", "pe_vega_rs_volpt"])
        for r in table:
            w.writerow([r["expiry"], round(r["days"], 2), r["strike"], round(r["iv"] * 100, 2),
                        r["ce_ltp"], r["ce_oi"], round(r["ce_delta"], 4), f"{r['ce_gamma']:.6f}",
                        round(r["ce_theta"], 2), round(r["ce_vega"], 2),
                        r["pe_ltp"], r["pe_oi"], round(r["pe_delta"], 4), f"{r['pe_gamma']:.6f}",
                        round(r["pe_theta"], 2), round(r["pe_vega"], 2)])
    with open(out / f"nifty_expiry_summary_{tag}.csv", "w", newline="") as f:
        w = csv.writer(f)
        keys = list(summary[0])
        w.writerow(keys)
        for s in summary:
            w.writerow([round(v, 6) if isinstance(v, float) else v for v in s.values()])


def print_report(summary, rv, today):
    print("Realised vol (annualised): " + ", ".join(
        f"{k} {v*100:.1f}%" for k, v in rv.items()))
    if today:
        print(f"Today: {today['move']*100:+.2f}% vs prev close, range {today['range']*100:.2f}%, "
              f"= {today['z']:+.2f} implied daily sigma ({today['sigma_day']*100:.2f}%)")
    print(f"{'expiry':<11}{'days':>6}{'ATM IV':>8}{'straddle':>10}{'impl mv':>9}"
          f"{'RV20 mv':>9}{'hist med':>9}{'P(>str)':>8}{'n':>4}{'OI lots':>10}")
    for s in summary:
        print(f"{s['expiry']:<11}{s['days']:6.1f}{s['atm_iv']*100:7.2f}%{s['straddle']:10.1f}"
              f"{s['implied_move']*100:8.2f}%{s['rv_move']*100:8.2f}%"
              f"{s['hist_median_move']*100:8.2f}%{s['hist_exceed']*100:7.0f}%{s['hist_windows']:4}{s['oi_lots']:10,}")


def write_dashboard(out, tag, now, S, points, k_grid, T_grid, IV, table, summary,
                    dates, closes, ret, rv, today, page_path=None, extra="", extra_figs=None):
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    expiries = [s["expiry"] for s in summary]
    palette = ["#4c78a8", "#f58518", "#e45756", "#54a24b", "#b279a2", "#9d755d"]
    colour = {e: palette[i % len(palette)] for i, e in enumerate(expiries)}
    figs = []

    # 1. surface
    f = go.Figure(go.Surface(x=np.exp(k_grid) * 100, y=T_grid * 365, z=IV * 100,
                             colorscale="Viridis", colorbar=dict(title="IV %"),
                             hovertemplate="K/F %{x:.1f}%<br>%{y:.1f}d<br>IV %{z:.2f}%<extra></extra>"))
    f.update_layout(title="Implied volatility surface", height=600,
                    scene=dict(xaxis_title="K/F (%)", yaxis_title="Days", zaxis_title="IV (%)",
                               camera=dict(eye=dict(x=1.6, y=-1.6, z=0.8))))
    figs.append(f)

    # 1b. smile by expiry + ATM term structure
    f = make_subplots(rows=1, cols=2, column_widths=[0.6, 0.4],
                      subplot_titles=("Smile by expiry", "ATM term structure"))
    for e in expiries:
        ps = [p for p in points if p.expiry == e]
        f.add_trace(go.Scatter(
            x=[p.strike for p in ps], y=[p.iv * 100 for p in ps], mode="lines+markers",
            name=f"{e} ({ps[0].T*365:.1f}d)", line=dict(color=colour[e]), marker=dict(size=4),
            hovertemplate="K %{x:.0f}<br>IV %{y:.2f}%<extra>" + e + "</extra>"), row=1, col=1)
    f.add_shape(type="line", x0=S, x1=S, y0=0, y1=1, xref="x", yref="y domain",
                line=dict(dash="dot", color="#8a919c"))
    f.add_annotation(x=S, y=0.98, xref="x", yref="y domain", text=f" spot {S:,.0f}",
                     showarrow=False, xanchor="left", yanchor="top", font=dict(size=11))
    f.add_trace(go.Scatter(
        x=[s["days"] for s in summary], y=[s["atm_iv"] * 100 for s in summary],
        mode="lines+markers", name="ATM IV", showlegend=False,
        line=dict(color="#8a919c"), marker=dict(size=9, color=[colour[e] for e in expiries]),
        text=expiries, hovertemplate="%{text}<br>%{x:.1f} days<br>ATM IV %{y:.2f}%<extra></extra>"),
        row=1, col=2)
    f.update_xaxes(title_text="Strike", row=1, col=1)
    f.update_yaxes(title_text="IV (%)", row=1, col=1)
    f.update_xaxes(title_text="Days to expiry", rangemode="tozero", row=1, col=2)
    f.update_yaxes(title_text="ATM IV (%)", row=1, col=2)
    f.update_layout(title="Volatility smile and term structure", height=480,
                    legend=dict(orientation="h", y=-0.2))
    figs.append(f)
    figs.extend(extra_figs or [])

    # 2. greeks by strike (OTM side: put below forward, call above)
    f = make_subplots(rows=2, cols=2, subplot_titles=(
        "Delta (OTM option)", "Gamma per point", "Theta ₹/day per lot", "Vega ₹/vol-pt per lot"),
        horizontal_spacing=0.08, vertical_spacing=0.12)
    for e in expiries:
        rows = [r for r in table if r["expiry"] == e]
        x = [r["strike"] for r in rows]
        side = ["ce" if r["strike"] >= r["forward"] else "pe" for r in rows]
        val = lambda g, m=1: [r[f"{s}_{g}"] * m for r, s in zip(rows, side)]
        for (rr, cc), y in zip([(1, 1), (1, 2), (2, 1), (2, 2)],
                               [val("delta"), val("gamma"), val("theta", LOT), val("vega", LOT)]):
            f.add_trace(go.Scatter(x=x, y=y, mode="lines", name=e, legendgroup=e,
                                   line=dict(color=colour[e]),
                                   showlegend=(rr, cc) == (1, 1)), row=rr, col=cc)
    f.update_layout(title="Greeks by strike (Black-76, each strike's own IV)", height=700)
    f.update_xaxes(title_text="Strike")
    figs.append(f)

    # 3. liquidity
    f = make_subplots(rows=1, cols=2, column_widths=[0.65, 0.35],
                      subplot_titles=("Open interest by strike (lots)", "OI within ±2% of forward (lots)"))
    for e in expiries:
        rows = [r for r in table if r["expiry"] == e]
        f.add_trace(go.Bar(x=[r["strike"] for r in rows],
                           y=[r["ce_oi"] + r["pe_oi"] for r in rows], name=e,
                           marker_color=colour[e]), row=1, col=1)
    f.add_trace(go.Bar(x=expiries, y=[s["oi_near_lots"] for s in summary], showlegend=False,
                       marker_color=[colour[e] for e in expiries]), row=1, col=2)
    f.update_xaxes(type="category", row=1, col=2)
    f.update_layout(title="Liquidity (open interest; Dhan chain did not return volume or bid/ask)",
                    barmode="group", height=450)
    figs.append(f)

    # 4. realised vs implied
    f = make_subplots(rows=1, cols=2, column_widths=[0.6, 0.4], subplot_titles=(
        "Daily NIFTY move vs implied ±1σ day", "Expected move to expiry"))
    sig = summary[0]["atm_iv"] / math.sqrt(TRADING_DAYS) * 100
    x = list(dates[1:]) + ([now.date().isoformat()] if today else [])
    y = list(ret * 100) + ([today["move"] * 100] if today else [])
    f.add_trace(go.Bar(x=x, y=y, name="daily move %",
                       marker_color=["#d62728" if v < 0 else "#2ca02c" for v in y]), row=1, col=1)
    for s in (sig, -sig):
        f.add_trace(go.Scatter(x=[x[0], x[-1]], y=[s, s], mode="lines", showlegend=s > 0,
                               name=f"±1σ implied ({sig:.2f}%)",
                               line=dict(dash="dash", color="#8a919c")), row=1, col=1)
    lbl = [f"{s['expiry'][5:]} ({s['tdays']}td)" for s in summary]
    for key, name in (("implied_move", "Straddle (implied)"), ("rv_move", "RV20-based"),
                      ("hist_median_move", "Historical median")):
        f.add_trace(go.Bar(x=lbl, y=[s[key] * 100 for s in summary], name=name), row=1, col=2)
    f.update_yaxes(title_text="%", row=1, col=1)
    f.update_yaxes(title_text="abs move %", row=1, col=2)
    f.update_layout(title="Realised vs implied moves", height=480, barmode="group")
    figs.append(f)

    # ---- HTML
    def tr(cells, tag="td"):
        return "<tr>" + "".join(f"<{tag}>{c}</{tag}>" for c in cells) + "</tr>"

    rv_tbl = "<table>" + tr(["Measure", "Annualised"], "th") + "".join(
        tr([k.replace("cc_", "Close-to-close ").replace("park_20", "Parkinson 20d")
            .replace("gk_20", "Garman-Klass 20d") + ("d" if k.startswith("cc_") else ""),
            f"{v*100:.2f}%"]) for k, v in rv.items()) + "</table>"
    sm_tbl = "<table>" + tr(["Expiry", "Days", "ATM K", "ATM IV", "Straddle", "Implied move",
                             "RV20 move", "Hist. median", "P(move &gt; straddle)",
                             "OI (lots)", "ATM straddle θ/lot", "ATM straddle vega/lot"], "th")
    for s in summary:
        sm_tbl += tr([s["expiry"], f"{s['days']:.1f}", f"{s['atm_strike']:.0f}",
                      f"{s['atm_iv']*100:.2f}%", f"{s['straddle']:.1f}",
                      f"±{s['implied_move']*100:.2f}%", f"±{s['rv_move']*100:.2f}%",
                      f"{s['hist_median_move']*100:.2f}% (n={s['hist_windows']})",
                      f"{s['hist_exceed']*100:.0f}%",
                      f"{s['oi_lots']:,}", f"₹{s['atm_theta_lot']:,.0f}",
                      f"₹{s['atm_vega_lot']:,.0f}"])
    sm_tbl += "</table>"
    today_html = ""
    if today:
        today_html = (f"<p><b>Today so far:</b> {today['move']*100:+.2f}% vs prev close "
                      f"{today['prev_close']:,.2f} (high {today['high']:,.2f}, low {today['low']:,.2f}, "
                      f"range {today['range']*100:.2f}%). Implied 1σ day from front ATM IV is "
                      f"{today['sigma_day']*100:.2f}%, so today is a <b>{today['z']:+.2f}σ</b> move.</p>")

    # charts read on both light and dark pages: transparent ground, mid-grey ink
    ink, grid = "#8a919c", "rgba(138,145,156,0.25)"
    axis = dict(gridcolor=grid, zerolinecolor=grid, linecolor=grid)
    for fg in figs:
        fg.update_layout(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                         font=dict(color=ink, family="IBM Plex Sans, system-ui, sans-serif"),
                         legend=dict(bgcolor="rgba(0,0,0,0)"), margin=dict(l=50, r=20, t=60, b=40))
        fg.update_xaxes(**axis)
        fg.update_yaxes(**axis)
    scene_axis = dict(backgroundcolor="rgba(0,0,0,0)", gridcolor=grid, zerolinecolor=grid)
    figs[0].update_scenes(xaxis=scene_axis, yaxis=scene_axis, zaxis=scene_axis)

    body = "".join(f'<section class="chart">{fg.to_html(full_html=False, include_plotlyjs=False, config=dict(responsive=True, displaylogo=False))}</section>'
                   for fg in figs)
    page = f"""<title>NIFTY Options Desk</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<script src="https://cdn.jsdelivr.net/npm/plotly.js-dist-min@4.1.1/plotly.min.js"></script>
<style>
/* Layout: one reading column, summary tables first, charts below, each table scrolls inside itself */
:root {{
  --bg: #f7f8fa; --surface: #ffffff; --fg: #1b2230; --muted: #5d6676;
  --rule: #dde1e8; --accent: #1f6f8b; --up: #1a7f4b; --down: #b3261e; --warn: #9a6700;
  --sans: "IBM Plex Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, "SFMono-Regular", Menlo, monospace;
}}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{
  --bg: #12161d; --surface: #1a2029; --fg: #e4e8ee; --muted: #9aa3b0;
  --rule: #2c3440; --accent: #5fb3cf; --up: #4cc38a; --down: #ff7b72; --warn: #e3b341; color-scheme: dark }} }}
:root[data-theme="dark"] {{
  --bg: #12161d; --surface: #1a2029; --fg: #e4e8ee; --muted: #9aa3b0;
  --rule: #2c3440; --accent: #5fb3cf; --up: #4cc38a; --down: #ff7b72; --warn: #e3b341; color-scheme: dark }}
body {{ background: var(--bg); color: var(--fg); font-family: var(--sans); font-size: 14px; line-height: 1.5 }}
main {{ max-width: 1180px; margin: 0 auto; padding-inline: 16px; padding-block: 24px 40px;
        display: grid; gap: 20px }}
header {{ display: grid; gap: 4px }}
.eyebrow {{ font-family: var(--mono); font-size: 12px; letter-spacing: .06em; text-transform: uppercase; color: var(--accent) }}
h1 {{ font-size: clamp(22px, 3vw, 30px); font-weight: 600; margin: 0; text-wrap: balance }}
h2 {{ font-size: 16px; font-weight: 600; margin: 0 0 8px }}
.today {{ background: var(--surface); border: 1px solid var(--rule); border-radius: 6px; padding: 12px 14px; margin: 0; max-width: 75ch }}
.today b {{ font-family: var(--mono) }}
.wrap {{ overflow-x: auto; min-width: 0 }}
table {{ border-collapse: collapse; font-size: 13px; font-variant-numeric: tabular-nums; background: var(--surface) }}
th, td {{ border-bottom: 1px solid var(--rule); padding: 6px 10px; text-align: right; white-space: nowrap }}
th {{ color: var(--muted); font-weight: 500; font-size: 12px; text-align: right; border-bottom-color: var(--fg) }}
td:first-child, th:first-child {{ text-align: left; font-family: var(--mono) }}
.chart {{ background: var(--surface); border: 1px solid var(--rule); border-radius: 6px; min-width: 0; overflow: hidden }}
.notes {{ font-size: 12px; color: var(--muted); max-width: 90ch; margin: 0 0 8px }}
.muted {{ color: var(--muted); font-size: 12px }}
.ideas {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 440px), 1fr)); gap: 12px }}
.idea {{ background: var(--surface); border: 1px solid var(--rule); border-radius: 6px; padding: 12px 14px; min-width: 0 }}
.idea header {{ display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 10px }}
.idea h3 {{ margin: 0; font-size: 15px }}
.idea ul {{ margin: 8px 0; padding-left: 18px }}
.idea li {{ margin-bottom: 4px }}
.figs {{ font-family: var(--mono); font-size: 12px; margin: 8px 0 0 }}
.pill {{ font-size: 11px; font-weight: 600; padding: 1px 8px; border-radius: 10px; border: 1px solid currentColor }}
.pill.best {{ background: var(--accent); color: var(--bg); border-color: var(--accent) }}
tr.bestrow td {{ font-weight: 600; color: var(--accent) }}
.exit {{ margin-top: 8px; font-size: 13px }}
.tiles {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 220px), 1fr)); gap: 10px }}
.tile {{ background: var(--surface); border: 1px solid var(--rule); border-radius: 6px; padding: 10px 12px; display: grid; gap: 2px; min-width: 0 }}
.tile b {{ font-size: 20px }} .tile b.good {{ color: var(--up) }} .tile b.bad {{ color: var(--down) }} .tile b.warn {{ color: var(--warn) }}
.decision {{ border: 1px solid var(--rule); border-left: 4px solid var(--rule); border-radius: 6px; padding: 10px 12px; background: var(--surface); max-width: 110ch }}
.decision.good {{ border-left-color: var(--up) }} .decision.bad {{ border-left-color: var(--down) }} .decision.warn {{ border-left-color: var(--warn) }}
.limits li {{ margin-bottom: 4px; max-width: 110ch }}
.matrix {{ margin: 6px 0 0; padding-left: 18px; color: var(--muted) }}
.exit ul {{ margin: 4px 0; padding-left: 0; list-style: none }}
.tag {{ display: inline-block; min-width: 84px; font-family: var(--mono); font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em }}
.alerts {{ list-style: none; padding: 0; margin: 0 0 12px; display: grid; gap: 6px; max-width: 110ch }}
.alerts li {{ background: var(--surface); border: 1px solid var(--rule); border-left: 3px solid var(--rule); border-radius: 4px; padding: 6px 10px }}
.alerts li.bad {{ border-left-color: var(--down) }} .alerts li.warn {{ border-left-color: var(--warn) }} .alerts li.good {{ border-left-color: var(--up) }}
.alerts li .pill {{ margin-right: 6px }}
.rank td:nth-child(2), .rank th:nth-child(2) {{ text-align: left }}
table.costs {{ margin-top: 8px; font-size: 12px }}
table.costs td, table.costs th {{ padding: 2px 8px }}
.pill.good, .up {{ color: var(--up) }} .pill.bad, .down {{ color: var(--down) }} .pill.warn {{ color: var(--warn) }}
tr.total td {{ font-weight: 600; border-top: 1px solid var(--fg) }}
h3 {{ font-size: 14px; margin: 12px 0 6px }}
</style>
<main>
<header>
  <span class="eyebrow">NSE · NIFTY 50 index options · Dhan API</span>
  <h1>{now:%d %b %Y}, {now:%H:%M} IST · spot {S:,.2f}</h1>
</header>
{today_html.replace('<p>', '<p class="today">')}
{extra}
<section><h2>Expiry summary</h2><div class="wrap">{sm_tbl}</div></section>
<section><h2>Realised volatility, last {len(closes)} sessions</h2><div class="wrap">{rv_tbl}</div></section>
<p class="notes" id="nolib" hidden>The charting library did not load, so the charts below are empty. The tables above are complete.</p>
{body}
<script>if (!window.Plotly) document.getElementById("nolib").hidden = false;</script>
<p class="notes">Source: Dhan API v2 option chain (last traded price, open interest) and daily candles.
Greeks: Black-76 on the put-call-parity forward, lot size {LOT}; theta is one calendar day of decay.
Implied move = ATM straddle / spot. RV20 move = 20-day close-to-close vol × √(trading days/252) × √(2/π).
Historical median = median absolute move over past windows of the same trading-day length;
n is the number of overlapping windows, and long-dated rows rest on very few, mostly from one trend.</p>
</main>"""
    if page_path:
        Path(page_path).write_text(page)
    html = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'</head><body>{page}</body></html>')
    path = out / f"nifty_dashboard_{tag}.html"
    path.write_text(html)
    # downloadable copy with the chart library inlined, so it opens offline
    import plotly
    js = (Path(plotly.__file__).parent / "package_data" / "plotly.min.js").read_text()
    dl = out / "download"
    dl.mkdir(exist_ok=True)
    tag_src = '<script src="https://cdn.jsdelivr.net/npm/plotly.js-dist-min@4.1.1/plotly.min.js"></script>'
    (dl / f"nifty_dashboard_{tag}.html").write_text(html.replace(tag_src, f"<script>{js}</script>"))
    return path


if __name__ == "__main__":
    main()
