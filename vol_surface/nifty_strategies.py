"""
Rule-based strategy ideas and position analytics for the NIFTY dashboard.

Every idea is built from the day's chain (last traded prices) and states the
numbers that drive it, so the reader can check the reasoning. These are
screening ideas, not advice: they ignore costs, slippage, margin and events.
"""
from __future__ import annotations

import csv
import math

import numpy as np

LOT = 65


# --------------------------------------------------------------------- helpers

def _rows(table, expiry):
    return sorted((r for r in table if r["expiry"] == expiry), key=lambda r: r["strike"])


def _nearest(rows, K):
    return min(rows, key=lambda r: abs(r["strike"] - K))


def _iv_at(rows, F, moneyness):
    ks = np.array([math.log(r["strike"] / F) for r in rows])
    return float(np.interp(math.log(moneyness), ks, [r["iv"] for r in rows]))


def _leg(row, kind, lots):
    side = "ce" if kind == "CE" else "pe"
    return dict(expiry=row["expiry"], strike=row["strike"], type=kind, lots=lots,
                price=row[f"{side}_ltp"], oi=row[f"{side}_oi"],
                delta=row[f"{side}_delta"], gamma=row[f"{side}_gamma"],
                theta=row[f"{side}_theta"], vega=row[f"{side}_vega"])


def _payoff(legs, S_T):
    v = 0.0
    for l in legs:
        intrinsic = max(S_T - l["strike"], 0) if l["type"] == "CE" else max(l["strike"] - S_T, 0)
        v += l["lots"] * (intrinsic - l["price"])
    return v * LOT


def _describe(legs, same_expiry=True):
    net = -sum(l["lots"] * l["price"] for l in legs) * LOT          # + = credit received
    g = {k: sum(l["lots"] * l[k] for l in legs) * LOT for k in ("delta", "gamma", "theta", "vega")}
    out = dict(legs=legs, net=net, **g)
    if same_expiry:
        lo = min(l["strike"] for l in legs) * 0.85
        hi = max(l["strike"] for l in legs) * 1.15
        grid = np.linspace(lo, hi, 4001)
        pnl = np.array([_payoff(legs, s) for s in grid])
        out["max_profit"], out["max_loss"] = float(pnl.max()), float(pnl.min())
        # a payoff still rising at the edge of the grid is open-ended
        if pnl[-1] >= pnl.max() - 1e-6 and pnl[-1] > pnl[-2]:
            out["max_profit"] = float("inf")
        if pnl[-1] <= pnl.min() + 1e-6 and pnl[-1] < pnl[-2]:
            out["max_loss"] = float("-inf")
        sign = np.sign(pnl)
        out["breakevens"] = [float(grid[i]) for i in range(1, len(grid)) if sign[i] != sign[i - 1]]
    return out


# ------------------------------------------------------------------ strategies

def suggest(table, summary, rv, today, S):
    """Return a list of strategy ideas with a fit verdict and the reasons behind it."""
    front, ideas = summary[0], []
    # the nearest expiry with at least ~2 weeks left, for premium-selling structures
    monthly = next((s for s in summary if s["days"] >= 12), summary[-1])
    m_rows = _rows(table, monthly["expiry"])
    F = monthly["forward"]
    iv_m, rv20, rv5 = monthly["atm_iv"], rv["cc_20"], rv["cc_5"]
    skew = _iv_at(m_rows, F, 0.96) - _iv_at(m_rows, F, 1.0)          # 4% OTM put vs ATM
    call_skew = _iv_at(m_rows, F, 1.04) - _iv_at(m_rows, F, 1.0)
    z = today["z"] if today else 0.0

    # 1. Iron condor on the monthly: harvest IV over realised vol
    mv = monthly["implied_move"]
    sp = _nearest(m_rows, F * (1 - mv))["strike"]
    sc = _nearest(m_rows, F * (1 + mv))["strike"]
    legs = [_leg(_nearest(m_rows, sp - 200), "PE", 1), _leg(_nearest(m_rows, sp), "PE", -1),
            _leg(_nearest(m_rows, sc), "CE", -1), _leg(_nearest(m_rows, sc + 200), "CE", 1)]
    spread = iv_m - rv20
    reasons = [f"{monthly['expiry']} ATM IV {iv_m*100:.1f}% vs 20-day realised {rv20*100:.1f}% "
               f"(spread {spread*100:+.1f} pts).",
               f"Short strikes sit at the straddle-implied move (±{mv*100:.1f}%, about 0.8σ), "
               f"so roughly 40% of outcomes finish beyond one of them; the wings 200 points "
               f"out cap the loss.",
               f"5-day realised is {rv5*100:.1f}%" + (" — recent moves are bigger than implied, "
               "so short gamma is exposed if that continues." if rv5 > iv_m else ".")]
    fit = ("Favoured" if spread > 0.005 and rv5 < iv_m + 0.02 and abs(z) < 1.5
           else "Neutral" if spread > 0 else "Not favoured")
    if abs(z) >= 1.5:
        reasons.append(f"Today's {z:+.1f}σ move argues for waiting before selling premium.")
    ideas.append(dict(name="Short iron condor", expiry=monthly["expiry"], fit=fit,
                      view="Range-bound; implied vol stays above realised",
                      reasons=reasons, **_describe(legs)))

    # 2. Bull put spread: sell the rich put skew
    short_put = _nearest(m_rows, F * 0.97)
    legs = [_leg(short_put, "PE", -1), _leg(_nearest(m_rows, short_put["strike"] - 300), "PE", 1)]
    reasons = [f"Put skew on {monthly['expiry']}: 4% OTM put IV is {skew*100:+.1f} pts over ATM "
               f"(4% OTM call {call_skew*100:+.1f} pts), so downside protection is expensive to buy "
               "and relatively rich to sell.",
               f"Short {short_put['strike']:.0f} PE is about {abs(short_put['pe_delta'])*100:.0f} delta; "
               "the long put 300 lower caps the loss."]
    fit = "Favoured" if skew > 0.025 and z > -1.5 else "Neutral" if skew > 0.01 else "Not favoured"
    ideas.append(dict(name="Bull put spread", expiry=monthly["expiry"], fit=fit,
                      view="Neutral to mildly bullish; sell expensive downside",
                      reasons=reasons, **_describe(legs)))

    # 3. Long front-week straddle: buy gamma when realised runs above implied
    f_rows = _rows(table, front["expiry"])
    atm = _nearest(f_rows, front["forward"])
    legs = [_leg(atm, "CE", 1), _leg(atm, "PE", 1)]
    reasons = [f"5-day realised {rv5*100:.1f}% and 10-day {rv['cc_10']*100:.1f}% vs front-week "
               f"ATM IV {front['atm_iv']*100:.1f}%.",
               f"Breakevens need a move of about ±{front['implied_move']*100:.2f}% by "
               f"{front['expiry']}; historically {front['hist_exceed']*100:.0f}% of "
               f"{front['tdays']}-day windows moved that much.",
               f"Theta cost about ₹{abs(sum(l['theta'] for l in legs))*LOT:,.0f} per lot per day."]
    fit = ("Favoured" if rv5 > front["atm_iv"] + 0.02 and front["hist_exceed"] > 0.35
           else "Neutral" if rv5 > front["atm_iv"] else "Not favoured")
    ideas.append(dict(name="Long ATM straddle", expiry=front["expiry"], fit=fit,
                      view="Expect a large move either way before expiry",
                      reasons=reasons, **_describe(legs)))

    # 4. Calendar: sell front, buy next monthly at the same strike
    if len(summary) > 1:
        K = atm["strike"]
        back_rows = _rows(table, monthly["expiry"])
        if any(r["strike"] == K for r in back_rows) and monthly["expiry"] != front["expiry"]:
            legs = [_leg(atm, "CE", -1), _leg(_nearest(back_rows, K), "CE", 1)]
            slope = monthly["atm_iv"] - front["atm_iv"]
            reasons = [f"Term structure: {front['expiry']} ATM {front['atm_iv']*100:.2f}% vs "
                       f"{monthly['expiry']} {monthly['atm_iv']*100:.2f}% ({slope*100:+.2f} pts).",
                       "Front-week decay is faster than the back month's; the trade earns "
                       "theta if spot stays near the strike, and gains if back-month IV rises.",
                       "Max loss is roughly the net debit if spot moves far from the strike."]
            fit = "Favoured" if slope < -0.003 else "Neutral" if slope < 0.004 else "Not favoured"
            d = _describe(legs, same_expiry=False)
            d.update(max_profit=None, max_loss=d["net"], breakevens=[])
            ideas.append(dict(name="Call calendar", expiry=f"{front['expiry']} / {monthly['expiry']}",
                              fit=fit, view="Spot pins near the strike; front vol overpriced",
                              reasons=reasons, **d))
    order = {"Favoured": 0, "Neutral": 1, "Not favoured": 2}
    return sorted(ideas, key=lambda i: order[i["fit"]])


# -------------------------------------------------------------------- positions

def load_positions(path):
    """CSV: expiry,strike,type(CE/PE),lots(+long/-short),entry_price"""
    with open(path) as f:
        rows = [r for r in csv.DictReader(l for l in f if not l.startswith("#")) if r.get("expiry")]
    return [dict(expiry=r["expiry"].strip(), strike=float(r["strike"]), type=r["type"].strip().upper(),
                 lots=float(r["lots"]), entry=float(r["entry_price"])) for r in rows]


def analyse_positions(positions, table, S, greeks_fn, rate):
    out, missing = [], []
    for p in positions:
        rows = _rows(table, p["expiry"])
        if not rows:
            missing.append(p)
            continue
        F, T = rows[0]["forward"], rows[0]["days"] / 365
        iv = _iv_at(rows, F, p["strike"] / F)
        exact = [r for r in rows if r["strike"] == p["strike"]]
        side = "ce" if p["type"] == "CE" else "pe"
        ltp = exact[0][f"{side}_ltp"] if exact else None
        d, g, t, v = greeks_fn(S, F, p["strike"], T, iv, rate, p["type"] == "CE")
        q = p["lots"] * LOT
        out.append(dict(p, iv=iv, ltp=ltp, F=F, T=T,
                        pnl=(ltp - p["entry"]) * q if ltp is not None else None,
                        delta=d * q, gamma=g * q, theta=t * q, vega=v * q))
    return out, missing


def scenario_pnl(analysed, S, black76, rate, moves=(-0.03, -0.02, -0.01, 0, 0.01, 0.02, 0.03)):
    """Instant spot shock, IV and time unchanged, P&L vs today's model value (₹)."""
    res = []
    for m in moves:
        tot = 0.0
        for p in analysed:
            F1 = p["F"] * (1 + m)
            df = math.exp(-rate * p["T"])
            now = black76(p["F"], p["strike"], p["T"], p["iv"], df, p["type"] == "CE")
            new = black76(F1, p["strike"], p["T"], p["iv"], df, p["type"] == "CE")
            tot += (new - now) * p["lots"] * LOT
        res.append((m, S * (1 + m), tot))
    return res
