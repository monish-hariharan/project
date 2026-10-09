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
import csv
import math
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.stats import norm

from nifty_costs import load_costs, rank
from nifty_strategies import analyse_positions, load_positions, scenario_pnl, suggest
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
    ap.add_argument("--positions", help="CSV of open positions: expiry,strike,type,lots,entry_price")
    a = ap.parse_args()

    now = datetime.strptime(a.asof, "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    S = a.spot
    chains = load_csv(a.chain, S)
    points = []
    for e, data in chains.items():
        points += chain_points(e, data, now, a.rate, 0.10)
    k_grid, T_grid, IV, _, _ = build_grid(points)
    expiries = sorted(chains)

    # ---- per-strike table: IV from the OTM quote, Greeks for both call and put
    iv_at = {(p.expiry, p.strike): p for p in points}
    table = []
    for e in expiries:
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
                d, g, t, v = greeks(S, p.forward, K, p.T, p.iv, a.rate, is_call)
                row.update({f"{side}_delta": d, f"{side}_gamma": g,
                            f"{side}_theta": t, f"{side}_vega": v})
            table.append(row)

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

    ideas = suggest(table, summary, rv, today, S)
    costs = load_costs(a.costs)
    best = rank(ideas, S, rv["cc_20"], costs, alt_sigma=rv["cc_5"])
    book = None
    if a.positions:
        analysed, missing = analyse_positions(load_positions(a.positions), table, S, greeks, a.rate)
        book = dict(rows=analysed, missing=missing,
                    scen=scenario_pnl(analysed, S, black76, a.rate) if analysed else [])

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    tag = now.strftime("%Y-%m-%d")
    write_tables(out, tag, table, summary)
    extra = extra_sections(ideas, book, best, costs)
    html = write_dashboard(out, tag, now, S, points, k_grid, T_grid, IV, table, summary,
                           d, c, ret, rv, today, a.page, extra)
    xlsx = write_workbook(out / "download" / f"nifty_dashboard_{tag}.xlsx", now, S, summary,
                          rv, table, ideas, book, costs)
    for i in ideas:
        e = i["eval"]
        print(f"{'BEST ' if i['best'] else '     '}[{i['fit']}] {i['name']} {i['expiry']}: "
              f"net ₹{i['net']:,.0f}, costs ₹{e['cost']:,.0f}, EV ₹{e['ev']:,.0f} "
              f"(EV/risk {e['ev_on_risk']*100:+.1f}%, POP {e['pop']*100:.0f}%)")
    print(f"Workbook: {xlsx.resolve()}")
    print_report(summary, rv, today)
    print(f"\nDashboard: {html.resolve()}")


# ------------------------------------------------------------------------ output

def _px(v):
    return "—" if v is None else f"{v:.2f}"


def _rs(v):
    if v is None:
        return "—"
    if math.isinf(v):
        return "unlimited"
    return f"−₹{-v:,.0f}" if v < 0 else f"₹{v:,.0f}"


def extra_sections(ideas, book, best, c):
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
        badge = '<span class="pill best">Best trade</span>' if i["best"] else ""
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
<header>{badge}<span class="pill {cls[i['fit']]}">{i['fit']}</span><h3>{i['name']}</h3>
<span class="muted">{i['expiry']} · {i['view']}</span></header>
<ul>{''.join(f'<li>{r}</li>' for r in i['reasons'])}</ul>
<div class="wrap"><table><tr><th>Leg</th><th>Expiry</th><th>Strike</th><th>LTP</th><th>OI (lots)</th></tr>{legs}</table></div>
<p class="figs">{net} per lot before costs · {_rs(e['net_after'])} after costs<br>
After costs: max profit {_rs(mp)} · max loss {_rs(ml)} · breakeven {be}<br>
Δ {i['delta']:+.1f} · Γ {i['gamma']:+.3f} · Θ {_rs(i['theta'])}/day · vega {_rs(i['vega'])}/vol-pt (per lot)</p>
{ev_html}<div class="wrap">{costs_html}</div>
</article>""")
    rank_rows = "".join(
        f"<tr{' class=\'bestrow\'' if i['best'] else ''}><td>{n}</td><td>{i['name']}</td><td>{i['expiry']}</td>"
        f"<td>{_rs(i['net'])}</td><td>{_rs(i['eval']['cost'])}</td><td>{_rs(i['eval']['ev'])}</td>"
        f"<td>{_rs(i['eval'].get('ev_alt'))}</td><td>{i['eval']['pop']*100:.0f}%</td>"
        f"<td>{_rs(i['eval']['risk'])}</td><td>{i['eval']['ev_on_risk']*100:+.1f}%</td></tr>"
        for n, i in enumerate(ideas, 1))
    if best:
        verdict = (f"<p class='today'><b>Best trade today: {best['name']} ({best['expiry']}).</b> "
                   f"Expected {_rs(best['eval']['ev'])} per lot after {_rs(best['eval']['cost'])} of charges "
                   f"and slippage, {best['eval']['ev_on_risk']*100:+.1f}% of the capital at risk, "
                   f"{best['eval']['pop']*100:.0f}% chance of profit.</p>")
    else:
        verdict = ("<p class='today'><b>No idea has a positive expected value after costs today.</b> "
                   "Staying flat is the cost-adjusted best choice.</p>")
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
    if book is None:
        html += ('<section><h2>Your positions</h2><p class="notes">No positions supplied. '
                 'Reply with your open NIFTY option positions (expiry, strike, CE/PE, lots, '
                 'entry price) and the next dashboard will show their Greeks, P&amp;L and '
                 'spot-shock scenarios.</p></section>')
        return html
    rows = "".join(
        f"<tr><td>{p['expiry']}</td><td>{p['strike']:.0f} {p['type']}</td><td>{p['lots']:+g}</td>"
        f"<td>{p['entry']:.2f}</td><td>{_px(p['ltp'])}</td>"
        f"<td>{_rs(p['pnl'])}</td><td>{p['iv']*100:.1f}%</td><td>{p['delta']:+.1f}</td>"
        f"<td>{p['gamma']:+.3f}</td><td>{_rs(p['theta'])}</td><td>{_rs(p['vega'])}</td></tr>"
        for p in book["rows"])
    tot = {k: sum(p[k] for p in book["rows"]) for k in ("delta", "gamma", "theta", "vega")}
    pnl = sum(p["pnl"] or 0 for p in book["rows"])
    rows += (f"<tr class='total'><td>Total</td><td></td><td></td><td></td><td></td><td>{_rs(pnl)}</td>"
             f"<td></td><td>{tot['delta']:+.1f}</td><td>{tot['gamma']:+.3f}</td>"
             f"<td>{_rs(tot['theta'])}</td><td>{_rs(tot['vega'])}</td></tr>")
    scen = "".join(f"<td>{m*100:+.0f}%<br><span class='muted'>{s:,.0f}</span></td>" for m, s, _ in book["scen"])
    scen_v = "".join(f"<td class='{'up' if v >= 0 else 'down'}'>{_rs(v)}</td>" for *_, v in book["scen"])
    miss = ""
    if book["missing"]:
        miss = ("<p class='notes'>Not priced (expiry not in today's chain): " +
                ", ".join(f"{m['expiry']} {m['strike']:.0f} {m['type']}" for m in book["missing"]) + "</p>")
    html += f"""<section><h2>Your positions</h2>
<div class="wrap"><table><tr><th>Expiry</th><th>Option</th><th>Lots</th><th>Entry</th><th>LTP</th>
<th>P&amp;L</th><th>IV</th><th>Δ (units)</th><th>Γ</th><th>Θ/day</th><th>Vega/pt</th></tr>{rows}</table></div>
<h3>Spot shock, IV unchanged</h3><div class="wrap"><table><tr><th>Move</th>{scen}</tr><tr><td>P&amp;L</td>{scen_v}</tr></table></div>{miss}</section>"""
    return html


def _xl(v):
    return None if v is None else "unlimited" if math.isinf(v) else round(v)


def write_workbook(path, now, S, summary, rv, table, ideas, book, costs):
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
               "Breakevens after costs", "Delta", "Gamma", "Theta ₹/day", "Vega ₹/pt", "Legs", "Reasons"])
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
                   " ".join(i["reasons"])])

    ws = wb.create_sheet("Greeks")
    ws.append(["Expiry", "Strike", "IV %", "CE LTP", "CE OI lots", "CE delta", "CE gamma",
               "CE theta", "CE vega", "PE LTP", "PE OI lots", "PE delta", "PE gamma",
               "PE theta", "PE vega"])
    for r in table:
        ws.append([r["expiry"], r["strike"], round(r["iv"] * 100, 2), r["ce_ltp"], r["ce_oi"],
                   round(r["ce_delta"], 4), round(r["ce_gamma"], 6), round(r["ce_theta"], 2),
                   round(r["ce_vega"], 2), r["pe_ltp"], r["pe_oi"], round(r["pe_delta"], 4),
                   round(r["pe_gamma"], 6), round(r["pe_theta"], 2), round(r["pe_vega"], 2)])

    ws = wb.create_sheet("Costs")
    ws.append(["Setting", "Value"])
    for k, v in costs.items():
        ws.append([k, v])

    if book and book["rows"]:
        ws = wb.create_sheet("Positions")
        ws.append(["Expiry", "Strike", "Type", "Lots", "Entry", "LTP", "P&L ₹", "IV %",
                   "Delta", "Gamma", "Theta ₹/day", "Vega ₹/pt"])
        for p in book["rows"]:
            ws.append([p["expiry"], p["strike"], p["type"], p["lots"], p["entry"], p["ltp"],
                       None if p["pnl"] is None else round(p["pnl"]), round(p["iv"] * 100, 2),
                       round(p["delta"], 1), round(p["gamma"], 4), round(p["theta"]), round(p["vega"])])
        ws.append([])
        ws.append(["Spot shock", "Spot", "P&L ₹"])
        for m, s_, v in book["scen"]:
            ws.append([f"{m*100:+.0f}%", round(s_), round(v)])
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
                    dates, closes, ret, rv, today, page_path=None, extra=""):
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
