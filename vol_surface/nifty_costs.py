"""
Transaction costs, slippage and cost-adjusted ranking for NIFTY option strategies.

Charges follow the broker schedule in costs.json (Motilal Oswal F&O options by default):
  brokerage  ₹ per lot per executed order
  STT        % of premium, sell side
  exchange   % of premium, both sides
  SEBI fee   % of premium, both sides
  stamp duty % of premium, buy side
  GST        on brokerage + exchange + SEBI
orders_per_leg = 1 (hold to expiry) or 2 (enter and exit). The exit order is costed at the
entry premium, since the exit price is unknown. With orders_per_leg = 1, STT on the intrinsic
value of exercised long options (stt_exercise_pct) is charged in the simulation.

Slippage: with only last-traded prices available, each order is assumed to fill
max(min_ticks × ₹0.05, pct × premium) worse than LTP, multiplied by a liquidity factor
from the strike's open interest (deep book ×1, thin ×2, very thin ×4).

Ranking: each idea's P&L is simulated to its first expiry with spot following a lognormal
walk at a chosen "real-world" volatility (20-day realised by default). Expiring legs settle
at intrinsic; later legs are revalued with Black-76 at today's IV. Expected P&L after all
costs, divided by the maximum loss, ranks the ideas. Positive EV means the option prices
look rich or cheap relative to how much NIFTY has actually been moving, after costs.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
from scipy.stats import norm

LOT = 65
TICK = 0.05

DEFAULT_COSTS = {
    "broker": "Motilal Oswal",
    "brokerage_per_lot_per_order": 40.0,
    "stt_sell_pct": 0.15,
    "exchange_pct": 0.035,
    "sebi_pct": 0.0001,
    "stamp_buy_pct": 0.003,
    "gst_pct": 18.0,
    "orders_per_leg": 2,
    "slippage_min_ticks": 1,
    "slippage_pct": 0.5,
    "liquid_oi_lots": 10000,
    "thin_oi_lots": 1000,
}


def load_costs(path=None):
    c = dict(DEFAULT_COSTS)
    if path and Path(path).exists():
        c.update(json.loads(Path(path).read_text()))
    return c


def order_charges(price, lots, buy, c):
    """Statutory + broker charges (₹) for one order of `lots` lots at `price` per unit."""
    turnover = price * abs(lots) * LOT
    brokerage = c["brokerage_per_lot_per_order"] * abs(lots)
    exch = turnover * c["exchange_pct"] / 100
    sebi = turnover * c["sebi_pct"] / 100
    stt = 0.0 if buy else turnover * c["stt_sell_pct"] / 100
    stamp = turnover * c["stamp_buy_pct"] / 100 if buy else 0.0
    gst = (brokerage + exch + sebi) * c["gst_pct"] / 100
    return dict(brokerage=brokerage, stt=stt, exchange=exch, sebi=sebi, stamp=stamp, gst=gst,
                total=brokerage + stt + exch + sebi + stamp + gst)


def slippage_per_unit(price, oi_lots, c):
    base = max(c["slippage_min_ticks"] * TICK, price * c["slippage_pct"] / 100)
    factor = 1 if oi_lots >= c["liquid_oi_lots"] else 2 if oi_lots >= c["thin_oi_lots"] else 4
    return base * factor


def trade_costs(legs, c):
    """Round-trip (or hold-to-expiry) charges and slippage for a strategy, ₹."""
    charges = dict(brokerage=0.0, stt=0.0, exchange=0.0, sebi=0.0, stamp=0.0, gst=0.0, total=0.0)
    slip = 0.0
    for l in legs:
        buy = l["lots"] > 0
        orders = [buy] + ([not buy] if c["orders_per_leg"] >= 2 else [])
        for is_buy in orders:
            for k, v in order_charges(l["price"], l["lots"], is_buy, c).items():
                charges[k] += v
            slip += slippage_per_unit(l["price"], l["oi"], c) * abs(l["lots"]) * LOT
    return charges, slip


def _b76(F, K, T, sigma, is_call):
    F = np.asarray(F, dtype=float)
    if T <= 0:
        return np.maximum(F - K, 0) if is_call else np.maximum(K - F, 0)
    sd = sigma * math.sqrt(T)
    d1 = (np.log(F / K) + 0.5 * sd * sd) / sd
    d2 = d1 - sd
    return F * norm.cdf(d1) - K * norm.cdf(d2) if is_call else K * norm.cdf(-d2) - F * norm.cdf(-d1)


def simulate(legs, S, sigma, n=40000, seed=7, exercise_stt_pct=0.0):
    """P&L per path (₹, before costs) at the first leg expiry, spot lognormal at `sigma`."""
    T1 = min(l["T"] for l in legs)
    rng = np.random.default_rng(seed)
    z = rng.standard_normal(n)
    # drift-free in forward terms: centre on the first expiry's forward
    F1 = min(legs, key=lambda l: l["T"])["F"]
    ST = F1 * np.exp(-0.5 * sigma * sigma * T1 + sigma * math.sqrt(T1) * z)
    pnl = np.zeros(n)
    for l in legs:
        is_call = l["type"] == "CE"
        rem = l["T"] - T1
        if rem <= 1e-9:
            val = np.maximum(ST - l["strike"], 0) if is_call else np.maximum(l["strike"] - ST, 0)
            if exercise_stt_pct and l["lots"] > 0:      # STT on intrinsic of exercised long options
                pnl -= exercise_stt_pct / 100 * val * l["lots"] * LOT
        else:
            carry = l["F"] / F1           # keep the forward spread between the two expiries
            val = _b76(ST * carry, l["strike"], rem, l["iv"], is_call)
        pnl += l["lots"] * (val - l["price"]) * LOT
    return pnl


def evaluate(idea, S, sigma, c, alt_sigma=None):
    legs = idea["legs"]
    charges, slip = trade_costs(legs, c)
    cost = charges["total"] + slip
    ex = c.get("stt_exercise_pct", 0.0) if c["orders_per_leg"] < 2 else 0.0
    pnl = simulate(legs, S, sigma, exercise_stt_pct=ex) - cost
    ev = float(pnl.mean())
    max_loss = idea.get("max_loss")
    if max_loss is None or math.isinf(max_loss):
        risk = float(-np.percentile(pnl, 1))            # 1st-percentile loss as a risk proxy
    else:
        risk = -(max_loss) + cost
    out = dict(charges=charges, slippage=slip, cost=cost, ev=ev, pop=float((pnl > 0).mean()),
               p5=float(np.percentile(pnl, 5)), risk=max(risk, 1.0), sigma=sigma,
               net_after=idea["net"] - cost)
    out["ev_on_risk"] = ev / out["risk"]
    out["cost_pct_of_max_profit"] = (cost / idea["max_profit"]
                                     if idea.get("max_profit") not in (None, float("inf"))
                                     and idea["max_profit"] > 0 else None)
    if alt_sigma:
        out["ev_alt"] = float((simulate(legs, S, alt_sigma, exercise_stt_pct=ex) - cost).mean())
        out["alt_sigma"] = alt_sigma
    # cost-adjusted payoff limits and breakevens for single-expiry structures
    if idea.get("breakevens") is not None and len({l["expiry"] for l in legs}) == 1:
        lo = min(l["strike"] for l in legs) * 0.85
        hi = max(l["strike"] for l in legs) * 1.15
        grid = np.linspace(lo, hi, 4001)
        pay = np.zeros_like(grid)
        for l in legs:
            intr = np.maximum(grid - l["strike"], 0) if l["type"] == "CE" else np.maximum(l["strike"] - grid, 0)
            pay += l["lots"] * (intr - l["price"]) * LOT
        pay -= cost
        sign = np.sign(pay)
        out["breakevens_after"] = [float(grid[i]) for i in range(1, len(grid)) if sign[i] != sign[i - 1]]
        out["max_profit_after"] = (float("inf") if math.isinf(idea.get("max_profit") or 0)
                                   else float(pay.max()))
        out["max_loss_after"] = (float("-inf") if math.isinf(idea.get("max_loss") or 0)
                                 else float(pay.min()))
    elif idea.get("max_loss") is not None and not math.isinf(idea["max_loss"]):
        out["max_loss_after"] = idea["max_loss"] - cost
    return out


def rank(ideas, S, sigma, c, alt_sigma=None):
    for i in ideas:
        i["eval"] = evaluate(i, S, sigma, c, alt_sigma)
    ideas.sort(key=lambda i: i["eval"]["ev_on_risk"], reverse=True)
    best = ideas[0] if ideas and ideas[0]["eval"]["ev"] > 0 else None
    for i in ideas:
        i["best"] = i is best
    return best


def drawdown(legs, S, sigma, exit_date, today, cost=0.0, stop_loss=None, n=4000, seed=11):
    """Expected maximum drawdown of a position marked to market daily until `exit_date`.

    Spot follows a driftless lognormal walk at `sigma` (one step per trading day); each leg
    is revalued with Black-76 at its current IV. P&L starts at minus the round-trip costs.
    Returns mean and 95th-percentile peak-to-trough drawdown (₹, positive numbers), the
    worst P&L on the way, and the chance the path touches `stop_loss` (₹ P&L, negative).
    """
    days = max(int(np.busday_count(today.isoformat(), exit_date)), 1)
    T1 = min(l["T"] for l in legs)
    dT = min(T1, days * 365 / 252 / 365) / days          # calendar time per trading day (years)
    rng = np.random.default_rng(seed)
    z = rng.standard_normal((n, days))
    step = sigma * math.sqrt(1 / 252)
    logS = np.cumsum(-0.5 * step * step + step * z, axis=1)
    pnl = np.full((n, days + 1), -cost)
    for k in range(1, days + 1):
        Sk = S * np.exp(logS[:, k - 1])
        tot = np.zeros(n)
        for l in legs:
            Fk = l["F"] * Sk / S
            v = _b76(Fk, l["strike"], max(l["T"] - k * dT, 0.0), l["iv"], l["type"] == "CE")
            tot += l["lots"] * (v - l["price"]) * LOT
        pnl[:, k] = tot - cost
    peak = np.maximum.accumulate(np.concatenate([np.zeros((n, 1)), pnl], axis=1), axis=1)[:, 1:]
    mdd = (peak - pnl).max(axis=1)
    out = dict(days=days, mean=float(mdd.mean()), p95=float(np.percentile(mdd, 95)),
               worst_pnl_median=float(np.median(pnl.min(axis=1))), sigma=sigma)
    if stop_loss is not None:
        out["p_stop"] = float((pnl.min(axis=1) <= stop_loss).mean())
    return out
