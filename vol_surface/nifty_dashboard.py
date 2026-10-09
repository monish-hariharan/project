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

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    tag = now.strftime("%Y-%m-%d")
    write_tables(out, tag, table, summary)
    html = write_dashboard(out, tag, now, S, points, k_grid, T_grid, IV, table, summary,
                           d, c, ret, rv, today, a.page)
    print_report(summary, rv, today)
    print(f"\nDashboard: {html.resolve()}")


# ------------------------------------------------------------------------ output

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
                    dates, closes, ret, rv, today, page_path=None):
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
  --rule: #dde1e8; --accent: #1f6f8b; --up: #1a7f4b; --down: #b3261e;
  --sans: "IBM Plex Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, "SFMono-Regular", Menlo, monospace;
}}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{
  --bg: #12161d; --surface: #1a2029; --fg: #e4e8ee; --muted: #9aa3b0;
  --rule: #2c3440; --accent: #5fb3cf; --up: #4cc38a; --down: #ff7b72; color-scheme: dark }} }}
:root[data-theme="dark"] {{
  --bg: #12161d; --surface: #1a2029; --fg: #e4e8ee; --muted: #9aa3b0;
  --rule: #2c3440; --accent: #5fb3cf; --up: #4cc38a; --down: #ff7b72; color-scheme: dark }}
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
.notes {{ font-size: 12px; color: var(--muted); max-width: 90ch; margin: 0 }}
</style>
<main>
<header>
  <span class="eyebrow">NSE · NIFTY 50 index options · Dhan API</span>
  <h1>{now:%d %b %Y}, {now:%H:%M} IST · spot {S:,.2f}</h1>
</header>
{today_html.replace('<p>', '<p class="today">')}
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
    return path


if __name__ == "__main__":
    main()
