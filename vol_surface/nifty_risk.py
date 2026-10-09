"""
Exit plans for strategy ideas and daily alerts for open positions.

Thresholds live in rules.json. Dates are counted in weekdays (NSE holidays are not
skipped), so check an exit date that lands on a holiday and move it a day earlier.
"""
from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path

import numpy as np

LOT = 65

DEFAULT_RULES = {
    "credit_take_profit_pct": 50,
    "credit_stop_loss_multiple": 2.0,
    "credit_exit_days_before_expiry": 3,
    "debit_take_profit_pct": 50,
    "debit_stop_loss_pct": 40,
    "debit_exit_days_before_expiry": 1,
    "short_strike_delta_alert": 0.35,
    "short_strike_buffer_pct": 0.5,
    "short_premium_warn_multiple": 1.5,
    "short_premium_stop_multiple": 2.0,
    "long_premium_stop_pct": 50,
    "book_loss_alert_rs": 10000,
    "hedge_delta_rs_per_pct": 5000,
    "roll_days_before_expiry": 3,
}


def load_rules(path=None):
    r = dict(DEFAULT_RULES)
    if path and Path(path).exists():
        r.update(json.loads(Path(path).read_text()))
    return r


def _exit_date(expiry, days_before):
    return str(np.busday_offset(np.datetime64(expiry), -int(days_before), roll="backward"))


def _tdays(today, expiry):
    return int(np.busday_count(today.isoformat(), expiry))


# ------------------------------------------------------------------ exit plans

def exit_plan(idea, rules, today: date):
    """Profit target, stop loss and time exit for a strategy idea (₹ per lot)."""
    legs = idea["legs"]
    first_exp = min(l["expiry"] for l in legs)
    net = idea["net"]                                   # + credit, - debit
    shorts = sorted({(l["strike"], l["type"]) for l in legs if l["lots"] < 0})
    cost = idea.get("eval", {}).get("cost", 0.0)
    if net > 0:                                         # credit structure
        tp = rules["credit_take_profit_pct"] / 100
        m = rules["credit_stop_loss_multiple"]
        ex = _exit_date(first_exp, rules["credit_exit_days_before_expiry"])
        plan = dict(
            kind="credit",
            take_profit=f"Buy back for ≤ ₹{net*(1-tp):,.0f}/lot (keeps {tp*100:.0f}% of the "
                        f"₹{net:,.0f} credit, ≈ ₹{net*tp - cost:,.0f} after costs)",
            stop_loss=f"Close if buy-back cost reaches ₹{net*(1+m):,.0f}/lot (loss ≈ {m:g}× credit, "
                      f"₹{net*m + cost:,.0f} with costs)"
                      + ("" if not shorts else ", or as soon as spot trades through a short strike ("
                         + ", ".join(f"{k:.0f} {t}" for k, t in shorts) + ")"),
            time_exit=f"Close by {ex} ({rules['credit_exit_days_before_expiry']} trading days before "
                      f"{first_exp}) to avoid expiry-week gamma",
            exit_date=ex)
    else:
        debit = -net
        tp = rules["debit_take_profit_pct"] / 100
        sl = rules["debit_stop_loss_pct"] / 100
        ex = _exit_date(first_exp, rules["debit_exit_days_before_expiry"])
        plan = dict(
            kind="debit",
            take_profit=f"Sell when the position is worth ≥ ₹{debit*(1+tp):,.0f}/lot "
                        f"(+{tp*100:.0f}% on the ₹{debit:,.0f} debit)",
            stop_loss=f"Close if it falls to ₹{debit*(1-sl):,.0f}/lot (−{sl*100:.0f}%, "
                      f"≈ ₹{debit*sl + cost:,.0f} loss with costs)",
            time_exit=f"Close by {ex} ({rules['debit_exit_days_before_expiry']} trading day(s) before "
                      f"{first_exp}); time decay is fastest in the final days",
            exit_date=ex)
    plan["days_held"] = max(_tdays(today, plan["exit_date"]), 0)
    return plan


# ------------------------------------------------------------------- alerts

def _next_month(summary, expiry):
    cur = next((s for s in summary if s["expiry"] == expiry), None)
    if cur is None:
        return None
    return next((s for s in summary if s["days"] >= cur["days"] + 14), None)


def _row(table, expiry, K):
    rows = [r for r in table if r["expiry"] == expiry]
    return min(rows, key=lambda r: abs(r["strike"] - K)) if rows else None


def position_alerts(book, table, summary, S, rules, today: date):
    """Return alerts (level: action / warn / info) for analysed positions."""
    alerts = []
    rows = book["rows"]
    if not rows:
        return alerts

    # --- whole book: loss and delta hedge
    pnl = sum(p["pnl"] or 0 for p in rows)
    if pnl <= -rules["book_loss_alert_rs"]:
        alerts.append(dict(level="action", kind="Loss",
                           msg=f"Book is down ₹{-pnl:,.0f} (limit ₹{rules['book_loss_alert_rs']:,.0f}). "
                               "Review the stop-loss and hedge alerts below."))
    delta = sum(p["delta"] for p in rows)               # ₹ per index point
    per_pct = delta * S / 100
    if abs(per_pct) >= rules["hedge_delta_rs_per_pct"]:
        fut = -delta / LOT
        monthly = next((s for s in summary if s["days"] >= 12), summary[-1])
        atm = _row(table, monthly["expiry"], monthly["forward"])
        side = "pe" if delta > 0 else "ce"
        d_opt = abs(atm[f"{side}_delta"]) if atm else 0.5
        n_opt = abs(delta) / (d_opt * LOT) if d_opt else 0
        direction = "long" if delta > 0 else "short"
        alerts.append(dict(level="action", kind="Hedge",
                           msg=f"Net delta {delta:+.0f} (₹{per_pct:+,.0f} per 1% move) is {direction} beyond "
                               f"the ₹{rules['hedge_delta_rs_per_pct']:,.0f} limit. Neutralise with "
                               f"{'sell' if fut < 0 else 'buy'} {abs(fut):.1f} lots NIFTY futures (≈{max(round(abs(fut)), 1)} "
                               f"in whole lots), or buy "
                               f"{n_opt:.1f} lots (≈{max(round(n_opt), 1)}) {monthly['expiry']} {atm['strike']:.0f} "
                               f"{side.upper()} (Δ {d_opt:.2f}) for a hedge that also caps the tail."))

    # --- per leg
    for p in rows:
        name = f"{p['expiry']} {p['strike']:.0f} {p['type']} ({p['lots']:+g} lots)"
        unit_delta = p["delta"] / (p["lots"] * LOT)
        tdays = _tdays(today, p["expiry"])
        if p["lots"] < 0 and p["ltp"] is not None:
            ratio = p["ltp"] / p["entry"] if p["entry"] else 0
            if ratio >= rules["short_premium_stop_multiple"]:
                alerts.append(dict(level="action", kind="Stop loss",
                                   msg=f"{name}: premium {p['ltp']:.2f} is {ratio:.1f}× the {p['entry']:.2f} "
                                       f"entry — stop level hit. Buy back or hedge."))
            elif ratio >= rules["short_premium_warn_multiple"]:
                alerts.append(dict(level="warn", kind="Loss",
                                   msg=f"{name}: premium up {ratio:.1f}× from entry "
                                       f"(stop at {rules['short_premium_stop_multiple']:g}×)."))
            elif ratio <= 1 - rules["credit_take_profit_pct"] / 100:
                alerts.append(dict(level="info", kind="Take profit",
                                   msg=f"{name}: {(1-ratio)*100:.0f}% of the premium is captured "
                                       f"({p['entry']:.2f} → {p['ltp']:.2f}). Consider closing."))
            # tested short strike -> recentre
            dist = (S - p["strike"]) / S * 100 * (1 if p["type"] == "CE" else -1)
            tested = abs(unit_delta) >= rules["short_strike_delta_alert"] or dist > -rules["short_strike_buffer_pct"]
            if tested:
                exp = next((s for s in summary if s["expiry"] == p["expiry"]), None)
                if exp:
                    K_new = exp["forward"] * (1 + exp["implied_move"] if p["type"] == "CE" else 1 - exp["implied_move"])
                    new = _row(table, p["expiry"], K_new)
                    side = "ce" if p["type"] == "CE" else "pe"
                    roll = (new[f"{side}_ltp"] - p["ltp"]) * abs(p["lots"]) * LOT
                    alerts.append(dict(level="action", kind="Recentre",
                                       msg=f"{name}: short strike under pressure (Δ {abs(unit_delta):.2f}, spot "
                                           f"{abs(dist):.1f}% {'past' if dist > 0 else 'from'} it). Roll to "
                                           f"{new['strike']:.0f} {p['type']} (at the ±{exp['implied_move']*100:.1f}% "
                                           f"implied move): buy back at {p['ltp']:.2f}, sell at {new[f'{side}_ltp']:.2f}, "
                                           f"net {'credit' if roll > 0 else 'debit'} ₹{abs(roll):,.0f} before costs."))
        if p["lots"] > 0 and p["ltp"] is not None and p["entry"]:
            drop = 1 - p["ltp"] / p["entry"]
            if drop * 100 >= rules["long_premium_stop_pct"]:
                alerts.append(dict(level="warn", kind="Loss",
                                   msg=f"{name}: worth {p['ltp']:.2f} vs {p['entry']:.2f} paid "
                                       f"(−{drop*100:.0f}%). Decide whether the view still holds."))
        # near expiry -> roll to next month (short legs) or exit (long legs)
        if tdays <= rules["roll_days_before_expiry"]:
            nxt = _next_month(summary, p["expiry"])
            if p["lots"] < 0 and nxt:
                cur = next(s for s in summary if s["expiry"] == p["expiry"])
                K_new = nxt["forward"] * p["strike"] / cur["forward"]
                new = _row(table, nxt["expiry"], K_new)
                side = "ce" if p["type"] == "CE" else "pe"
                credit = (new[f"{side}_ltp"] - (p["ltp"] or 0)) * abs(p["lots"]) * LOT
                alerts.append(dict(level="warn", kind="Roll",
                                   msg=f"{name}: {tdays} trading day(s) to expiry. Roll to {nxt['expiry']} "
                                       f"{new['strike']:.0f} {p['type']} (same moneyness): buy back "
                                       f"{p['ltp'] or 0:.2f}, sell {new[f'{side}_ltp']:.2f}, net "
                                       f"{'credit' if credit > 0 else 'debit'} ₹{abs(credit):,.0f} before costs."))
            elif p["lots"] > 0:
                alerts.append(dict(level="warn", kind="Time exit",
                                   msg=f"{name}: {tdays} trading day(s) to expiry; long premium decays "
                                       "fastest now. Exit or roll unless you expect the move imminently."))
    order = {"action": 0, "warn": 1, "info": 2}
    return sorted(alerts, key=lambda a: order[a["level"]])
