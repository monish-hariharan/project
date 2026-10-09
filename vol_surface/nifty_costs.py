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
value of exercised long options (stt_exercise_pct) applies to ITM legs held to expiry.

Slippage: with only last-traded prices available, each order is assumed to fill
max(min_ticks × ₹0.05, pct × premium) worse than LTP, multiplied by a liquidity factor
from the strike's open interest (deep book ×1, thin ×2, very thin ×4).

Ranking uses real data only: the strategy family's historical expectancy per unit of risk
from your backtest (backtest_stats.json) or from the forward record of logged suggestions
marked to market at real prices. Without a record, ideas are ranked by their payoff ratio
from today's actual prices after costs. Nothing is simulated.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

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


def evaluate(idea, c, history=None):
    """Real costs and the payoff after costs from today's prices; history stats if available.

    No simulation: expected value, win rate, drawdown and stop rate come only from real
    outcomes (your backtest file and the forward record of logged suggestions). Until a
    strategy family has a record, those fields are None and ranking falls back to the
    payoff ratio (max profit after costs / max loss after costs).
    """
    legs = idea["legs"]
    charges, slip = trade_costs(legs, c)
    cost = charges["total"] + slip
    out = dict(charges=charges, slippage=slip, cost=cost, net_after=idea["net"] - cost)
    mp, ml = idea.get("max_profit"), idea.get("max_loss")
    out["max_profit_after"] = mp if mp is None or math.isinf(mp) else mp - cost
    out["max_loss_after"] = ml if ml is None or math.isinf(ml) else ml - cost
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
    risk = -out["max_loss_after"] if out["max_loss_after"] not in (None,) and not math.isinf(out["max_loss_after"]) else None
    out["risk"] = risk
    reward = out["max_profit_after"]
    out["payoff_ratio"] = (reward / risk if risk and reward is not None and not math.isinf(reward) else None)
    h = (history or {}).get(idea.get("family"))
    out["hist"] = h
    out["ev"] = h["expectancy"] if h else None
    out["pop"] = h["win_rate"] if h else None
    if h and risk:
        out["score"], out["score_basis"] = h["expectancy"] / risk, f"{h['source']} expectancy / risk"
    else:
        out["score"], out["score_basis"] = (out["payoff_ratio"] or 0.0), "payoff ratio (no record yet)"
    out["ev_on_risk"] = out["score"]
    return out


def rank(ideas, c, history=None):
    for i in ideas:
        i["eval"] = evaluate(i, c, history)
    ideas.sort(key=lambda i: i["eval"]["score"], reverse=True)
