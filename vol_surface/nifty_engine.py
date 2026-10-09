"""
NIFTY decision engine: expected range, volatility edge, positioning, regime, risk gate.

The engine answers one question each day: is the options market mispricing risk in a way
that one of two defined-risk strategies can exploit after costs?

  1. Expected range   India VIX one-day move E = S·VIX/(100·√252), the Z-score of spot vs
                      the previous close, and the range model's historical coverage.
  2. Volatility edge  R_IV = ATM IV / forecast realised vol (HAR-style blend of 5/20/60-day
                      realised vol and an EWMA), on the horizon of the trade's expiry.
  3. Positioning      estimated GEX by strike, gamma-flip level, OI change, put skew,
                      PCR and max pain. Supporting evidence only (see GEX caveat below).
  4. Strategy         Range regime + rich IV -> iron condor (delta-based and range-based
                      short strikes). Trend regime with a confirmed break -> debit spread.
  5. Risk gate        EV after costs > 0, max loss per lot within the risk budget,
                      liquidity of every leg. Anything else -> NO TRADE.

GEX caveat: open interest does not say who is long or short. The sign convention used here
(dealers long calls / short puts, so call gamma counts positive and put gamma negative) is a
modelling assumption, not an observed fact about dealer positions.

Every forecast is written to logs/predictions.jsonl and scored once the outcome is known,
so the model builds its own out-of-sample record.
"""
from __future__ import annotations

import csv
import json
import math
from datetime import date
from pathlib import Path

import numpy as np
from scipy.stats import norm

from nifty_strategies import LOT, _describe, _iv_at, _leg, _nearest, _rows

TRADING_DAYS = 252


# ------------------------------------------------------------------ data

def load_ohlc(path):
    with open(path) as f:
        rows = list(csv.DictReader(l for l in f if not l.startswith("#")))
    return dict(date=[r["date"] for r in rows],
                **{k: np.array([float(r[k]) for r in rows]) for k in ("open", "high", "low", "close")})


# ---------------------------------------------------------- 1. expected range

def expected_move(S, vix, days=1.0):
    """One-standard-deviation move in index points over `days` trading days."""
    return S * vix / 100 * math.sqrt(days / TRADING_DAYS)


def range_backtest(nifty, vix):
    """Coverage of the VIX one-day range: close, intraday high and low, by VIX tercile."""
    c, h, l = nifty["close"], nifty["high"], nifty["low"]
    v = vix["close"]
    n = min(len(c), len(v))
    rows = []
    for t in range(1, n):
        E = expected_move(c[t - 1], v[t - 1])
        rows.append(dict(date=nifty["date"][t], vix=v[t - 1], E=E,
                         ret=c[t] - c[t - 1], z=(c[t] - c[t - 1]) / E,
                         up_breach=h[t] > c[t - 1] + E, dn_breach=l[t] < c[t - 1] - E))
    if not rows:
        return None
    z = np.array([r["z"] for r in rows])
    vx = np.array([r["vix"] for r in rows])
    q = np.quantile(vx, [1 / 3, 2 / 3])

    def stats(mask, label):
        zz = z[mask]
        if len(zz) == 0:
            return None
        return dict(label=label, n=int(len(zz)),
                    close_inside=float((np.abs(zz) <= 1).mean()),
                    up_close=float((zz > 1).mean()), dn_close=float((zz < -1).mean()),
                    up_touch=float(np.mean([r["up_breach"] for r, m in zip(rows, mask) if m])),
                    dn_touch=float(np.mean([r["dn_breach"] for r, m in zip(rows, mask) if m])),
                    # realised / implied: mean |move| relative to what VIX implied (√(2/π)·E)
                    ratio=float(np.mean(np.abs(zz)) / math.sqrt(2 / math.pi)),
                    tail=float((np.abs(zz) > 2).mean()))
    by = [stats(np.ones(len(z), bool), "All"),
          stats(vx <= q[0], f"VIX ≤ {q[0]:.1f}"),
          stats((vx > q[0]) & (vx <= q[1]), f"VIX {q[0]:.1f}–{q[1]:.1f}"),
          stats(vx > q[1], f"VIX > {q[1]:.1f}")]
    return dict(rows=rows, by=[b for b in by if b], vrp=by[0]["ratio"])


# ------------------------------------------------------- 2. volatility edge

def vol_forecast(closes, lam=0.94):
    r = np.diff(np.log(closes))
    rv = {n: float(r[-n:].std(ddof=1) * math.sqrt(TRADING_DAYS)) for n in (5, 20, 60) if len(r) >= n}
    var = float(np.var(r[:20])) if len(r) > 20 else float(np.var(r))
    for x in r:
        var = lam * var + (1 - lam) * x * x
    ewma = math.sqrt(var * TRADING_DAYS)
    har = 0.3 * rv.get(5, ewma) + 0.4 * rv.get(20, ewma) + 0.3 * rv.get(60, rv.get(20, ewma))
    return dict(rv=rv, ewma=ewma, har=har, forecast=0.5 * (har + ewma))


def vol_edge(atm_iv, forecast, rules):
    ratio = atm_iv / forecast if forecast else float("nan")
    label = ("Rich" if ratio >= rules["iv_rich_ratio"] else
             "Cheap" if ratio <= rules["iv_cheap_ratio"] else "Fair")
    return dict(ratio=ratio, label=label, atm_iv=atm_iv, forecast=forecast)


# --------------------------------------------------------- 3. positioning

def _gamma(S, F, K, T, iv):
    if T <= 0 or iv <= 0:
        return 0.0
    sd = iv * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sd * sd) / sd
    return (F / S) ** 2 * norm.pdf(d1) / (F * sd)


def gex_profile(table, S, max_days=40):
    """Estimated GEX (₹ crore per 1% move) by strike and the gamma-flip level."""
    rows = [r for r in table if r["days"] <= max_days]
    by_strike = {}
    for r in rows:
        g_ce = r["ce_gamma"] * r["ce_oi"] * LOT
        g_pe = r["pe_gamma"] * r["pe_oi"] * LOT
        val = (g_ce - g_pe) * S * S * 0.01 / 1e7
        by_strike[r["strike"]] = by_strike.get(r["strike"], 0.0) + val
    total = sum(by_strike.values())

    def total_at(s):
        tot = 0.0
        for r in rows:
            F = r["forward"] * s / S
            g = _gamma(s, F, r["strike"], r["days"] / 365, r["iv"])
            tot += g * (r["ce_oi"] - r["pe_oi"]) * LOT * s * s * 0.01 / 1e7
        return tot

    grid = np.linspace(S * 0.95, S * 1.05, 81)
    prof = np.array([total_at(s) for s in grid])
    flips = [float(grid[i - 1] + (grid[i] - grid[i - 1]) * prof[i - 1] / (prof[i - 1] - prof[i]))
             for i in range(1, len(grid)) if np.sign(prof[i]) != np.sign(prof[i - 1])]
    flip = min(flips, key=lambda f: abs(f - S)) if flips else None
    return dict(by_strike=dict(sorted(by_strike.items())), total=total, flip=flip,
                grid=grid.tolist(), profile=prof.tolist(),
                regime=("positive" if total > 0 else "negative"))


def pcr_maxpain(table, expiry):
    rows = _rows(table, expiry)
    put_oi = sum(r["pe_oi"] for r in rows)
    call_oi = sum(r["ce_oi"] for r in rows)
    strikes = [r["strike"] for r in rows]
    pain = []
    for s in strikes:
        pain.append(sum(r["ce_oi"] * max(s - r["strike"], 0) + r["pe_oi"] * max(r["strike"] - s, 0)
                        for r in rows))
    return dict(pcr=put_oi / call_oi if call_oi else float("nan"),
                max_pain=strikes[int(np.argmin(pain))] if strikes else None)


def oi_change(table, prev_table, expiry, top=5):
    if not prev_table:
        return None
    prev = {(r["expiry"], r["strike"]): r for r in prev_table}
    ch = []
    for r in _rows(table, expiry):
        p = prev.get((r["expiry"], r["strike"]))
        if p:
            ch.append(dict(strike=r["strike"], ce=r["ce_oi"] - p["ce_oi"], pe=r["pe_oi"] - p["pe_oi"]))
    return dict(rows=ch,
                top_ce=sorted(ch, key=lambda x: -x["ce"])[:top],
                top_pe=sorted(ch, key=lambda x: -x["pe"])[:top])


# --------------------------------------------------------------- 4. regime

def regime(S, prev, vf, z, rules):
    """Range / Trend / Mixed from Z, the previous day's range and vol expansion."""
    expansion = vf["rv"].get(5, vf["ewma"]) / vf["rv"].get(20, vf["ewma"])
    broke_up = S > prev["high"]
    broke_dn = S < prev["low"]
    reasons = [f"Z = {z:+.2f} vs previous close",
               f"5d/20d realised vol {expansion:.2f}×",
               ("closed above yesterday's high" if broke_up else
                "closed below yesterday's low" if broke_dn else "inside yesterday's range")]
    if (broke_up or broke_dn) and (abs(z) >= rules["trend_z"] or expansion >= rules["expansion_ratio"]):
        lab = "Trend"
    elif abs(z) < rules["range_z"] and expansion < rules["expansion_ratio"] and not (broke_up or broke_dn):
        lab = "Range"
    else:
        lab = "Mixed"
    direction = "up" if (broke_up or (lab == "Trend" and z > 0)) else "down" if (broke_dn or (lab == "Trend" and z < 0)) else None
    return dict(label=lab, direction=direction, expansion=expansion, reasons=reasons)


# -------------------------------------------------------------- 5. strategies

def candidates(table, info, S, vix, rules):
    """Iron condors (delta- and range-based) and directional debit spreads."""
    out = []
    target = next((e for e in info if e["days"] >= rules["min_days"]), info[-1])
    rows = _rows(table, target["expiry"])
    F = target["forward"]
    w = rules["wing_width"]

    # A1: iron condor, short strikes by delta
    d = rules["condor_short_delta"]
    sp = min(rows, key=lambda r: abs(abs(r["pe_delta"]) - d))
    sc = min(rows, key=lambda r: abs(r["ce_delta"] - d))
    legs = [_leg(_nearest(rows, sp["strike"] - w), "PE", 1), _leg(sp, "PE", -1),
            _leg(sc, "CE", -1), _leg(_nearest(rows, sc["strike"] + w), "CE", 1)]
    out.append(dict(name=f"Iron condor (≈{d*100:.0f}Δ shorts)", family="condor", expiry=target["expiry"],
                    view="Range-bound; IV rich vs forecast", reasons=[], **_describe(legs)))

    # A2: iron condor, short strikes at the VIX expected range to expiry
    E = expected_move(S, vix, max(target["tdays"], 1))
    sp = _nearest(rows, F - E)
    sc = _nearest(rows, F + E)
    if (sp["strike"], sc["strike"]) != (out[0]["legs"][1]["strike"], out[0]["legs"][2]["strike"]):
        legs = [_leg(_nearest(rows, sp["strike"] - w), "PE", 1), _leg(sp, "PE", -1),
                _leg(sc, "CE", -1), _leg(_nearest(rows, sc["strike"] + w), "CE", 1)]
        out.append(dict(name="Iron condor (VIX range shorts)", family="condor", expiry=target["expiry"],
                        view=f"Shorts at ±{E:,.0f} pts (1σ to expiry)", reasons=[], **_describe(legs)))

    # B: debit spreads (buy near-ATM, sell further out)
    atm = _nearest(rows, F)
    dw = rules["debit_width"]
    legs = [_leg(atm, "CE", 1), _leg(_nearest(rows, atm["strike"] + dw), "CE", -1)]
    out.append(dict(name="Bull call debit spread", family="bull", expiry=target["expiry"],
                    view="Confirmed upside break with expanding vol", reasons=[], **_describe(legs)))
    legs = [_leg(atm, "PE", 1), _leg(_nearest(rows, atm["strike"] - dw), "PE", -1)]
    out.append(dict(name="Bear put debit spread", family="bear", expiry=target["expiry"],
                    view="Confirmed downside break with expanding vol", reasons=[], **_describe(legs)))
    return out, target


def decide(cands, reg, edge, gex, S, rules, events_today=False):
    """Apply the regime/vol matrix and the risk gate; returns (decision, per-candidate gates)."""
    budget = rules["capital"] * rules["risk_pct"] / 100
    near_flip = gex["flip"] is not None and abs(S - gex["flip"]) / S * 100 < rules["flip_buffer_pct"]
    allowed = set()
    matrix = []
    if events_today:
        matrix.append("Event day flagged: no short premium.")
    if reg["label"] == "Range" and edge["label"] == "Rich" and not events_today:
        allowed.add("condor")
        matrix.append("Range regime + rich IV → iron condor.")
    elif reg["label"] == "Range":
        matrix.append(f"Range regime but IV is {edge['label'].lower()} → no premium edge; wait.")
    if reg["label"] == "Trend":
        fam = "bull" if reg["direction"] == "up" else "bear"
        allowed.add(fam)
        matrix.append(f"Trend regime ({reg['direction']}) → {'bull call' if fam == 'bull' else 'bear put'} debit spread"
                      + (" (IV rich: check the breakeven move)" if edge["label"] == "Rich" else "") + ".")
    if reg["label"] == "Mixed":
        matrix.append("Mixed signals (no clean range or confirmed break) → stand aside.")
    if near_flip:
        matrix.append(f"Spot within {rules['flip_buffer_pct']}% of the estimated gamma flip "
                      f"({gex['flip']:,.0f}) → hedging regime uncertain; reduce risk.")
    for c in cands:
        e = c["eval"]
        ml = e.get("max_loss_after", c.get("max_loss"))
        ml_abs = abs(ml) if ml is not None and not math.isinf(ml) else float("inf")
        lots = int(budget // ml_abs) if ml_abs not in (0, float("inf")) else 0
        thin = [l for l in c["legs"] if l["oi"] < rules["min_leg_oi_lots"]]
        gate = dict(
            regime_ok=c["family"] in allowed,
            ev_ok=e["ev"] > 0,
            size_ok=lots >= 1,
            liquidity_ok=not thin,
            lots=lots, budget=budget, max_loss=ml_abs,
            notes=[])
        if not gate["regime_ok"]:
            gate["notes"].append("not the strategy this regime calls for")
        if not gate["ev_ok"]:
            gate["notes"].append(f"EV after costs ₹{e['ev']:,.0f} ≤ 0")
        if not gate["size_ok"]:
            gate["notes"].append(f"max loss ₹{ml_abs:,.0f}/lot exceeds the ₹{budget:,.0f} risk budget")
        if thin:
            gate["notes"].append("thin leg(s): " + ", ".join(f"{l['strike']:.0f}{l['type']}" for l in thin))
        gate["pass"] = gate["regime_ok"] and gate["ev_ok"] and gate["size_ok"] and gate["liquidity_ok"]
        c["gate"] = gate
    passing = [c for c in cands if c["gate"]["pass"]]
    if near_flip:
        passing = []
    best = max(passing, key=lambda c: c["eval"]["ev_on_risk"]) if passing else None
    override = False
    if best is None and rules.get("ignore_gate", False) and cands:
        # gate failed: still name the best candidate, preferring the family the regime calls for
        pool = [c for c in cands if c["family"] in allowed] or cands
        best = max(pool, key=lambda c: c["eval"]["ev_on_risk"])
        override = True
    return dict(trade=best, matrix=matrix, budget=budget, near_flip=near_flip, override=override,
                label=(best["name"] + (" (gate failed)" if override else "") if best else "NO TRADE"))


# ----------------------------------------------------------- prediction log

def log_prediction(path, d, S, vix, E, reg, edge, gex, decision):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    keep = []
    if path.exists():
        keep = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
        keep = [p for p in keep if p["date"] != d]
    keep.append(dict(date=d, spot=S, vix=vix, E_day=round(E, 2),
                     range_lo=round(S - E, 2), range_hi=round(S + E, 2),
                     regime=reg["label"], direction=reg["direction"], vol_edge=edge["label"],
                     r_iv=round(edge["ratio"], 4), gex_regime=gex["regime"],
                     gex_flip=gex["flip"], decision=decision["label"], outcome=None))
    path.write_text("".join(json.dumps(p) + "\n" for p in keep))
    return keep


def score_predictions(path, nifty):
    """Fill outcomes for past predictions from the next session's OHLC; return scorecard."""
    path = Path(path)
    if not path.exists():
        return dict(n=0, rows=[])
    preds = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    dates = nifty["date"]
    for p in preds:
        if p["outcome"] is None and p["date"] in dates:
            i = dates.index(p["date"])
            if i + 1 < len(dates):
                c, h, l = nifty["close"][i + 1], nifty["high"][i + 1], nifty["low"][i + 1]
                p["outcome"] = dict(date=dates[i + 1], close=float(c), high=float(h), low=float(l),
                                    close_inside=bool(p["range_lo"] <= c <= p["range_hi"]),
                                    touched_hi=bool(h > p["range_hi"]), touched_lo=bool(l < p["range_lo"]))
    path.write_text("".join(json.dumps(p) + "\n" for p in preds))
    done = [p for p in preds if p["outcome"]]
    return dict(n=len(done), rows=preds[-15:],
                close_inside=(np.mean([p["outcome"]["close_inside"] for p in done]) if done else None),
                touched=(np.mean([p["outcome"]["touched_hi"] or p["outcome"]["touched_lo"] for p in done])
                         if done else None))
