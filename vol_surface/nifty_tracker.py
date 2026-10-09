#!/usr/bin/env python3
"""
Suggestion log and active monitoring of suggested trades that were actually taken.

Files (in logs/):
  suggestions.jsonl   every idea the dashboard produced, one JSON object per line
  tracked.json        suggestions the user took, with entry prices and check history
  monitor_log.csv     one row per check of a tracked trade (time, spot, value, P&L, alerts)

A suggestion becomes a tracked trade when
  * the user's positions contain every leg of it (same expiry, strike, type and side), or
  * the user takes it by ID:  python nifty_tracker.py take S20261009-1 --lots 2 --prices 81,46.5
Only tracked trades are monitored; other positions are ignored.

Hourly check during market hours:
  python nifty_tracker.py monitor --chain data/nifty_chain_<date>_<HHMM>.csv --spot 22510 \
      --asof "2026-10-12 11:15"
The chain file only needs the expiries the tracked trades use (plus the next month for
roll suggestions). Alerts already sent for a trade are not repeated.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import datetime
from pathlib import Path

LOT = 65
HERE = Path(__file__).parent
LOG_DIR = HERE / "logs"


# ------------------------------------------------------------------ storage

def _paths(log_dir=LOG_DIR):
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "suggestions.jsonl", log_dir / "tracked.json", log_dir / "monitor_log.csv"


def read_suggestions(log_dir=LOG_DIR):
    path, _, _ = _paths(log_dir)
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def read_tracked(log_dir=LOG_DIR):
    _, path, _ = _paths(log_dir)
    return json.loads(path.read_text()) if path.exists() else {"trades": []}


def write_tracked(data, log_dir=LOG_DIR):
    _, path, _ = _paths(log_dir)
    path.write_text(json.dumps(data, indent=2, default=str))


def log_suggestions(ideas, now, S, log_dir=LOG_DIR):
    """Append today's ideas (re-running the same day replaces that day's entries)."""
    path, _, _ = _paths(log_dir)
    day = now.strftime("%Y%m%d")
    keep = [s for s in read_suggestions(log_dir) if not s["id"].startswith(f"S{day}-")]
    new = []
    for n, i in enumerate(ideas, 1):
        e, x = i["eval"], i["exit"]
        new.append(dict(
            id=f"S{day}-{n}", date=now.strftime("%Y-%m-%d"), time=now.strftime("%H:%M"),
            spot=S, name=i["name"], expiry=i["expiry"], fit=i["fit"], best=i["best"],
            legs=[dict(expiry=l["expiry"], strike=l["strike"], type=l["type"], lots=l["lots"],
                       price=l["price"]) for l in i["legs"]],
            family=i.get("family"), net=round(i["net"], 2), cost=round(e["cost"], 2),
            payoff_ratio=e.get("payoff_ratio"), record=(e.get("hist") or {}).get("source"),
            exit={k: v for k, v in x.items() if k != "days_held"}))
        i["id"] = new[-1]["id"]
    path.write_text("".join(json.dumps(s, default=str) + "\n" for s in keep + new))
    return new


# ------------------------------------------------------------- take / match

def _new_trade(sug, lots, prices, source, now):
    legs = []
    for l, px in zip(sug["legs"], prices):
        legs.append(dict(l, lots=l["lots"] * lots, entry=px))
    entry_net = -sum(l["lots"] * l["entry"] for l in legs) * LOT
    # re-base the numeric exit levels on the actual fill (per suggested size)
    exit_ = dict(sug["exit"])
    scale = (entry_net / lots) / sug["net"] if sug["net"] else 1.0
    if scale > 0:
        for k in ("tp_close_cost", "sl_close_cost", "warn_close_cost", "tp_value", "sl_value", "warn_value"):
            if k in exit_:
                exit_[k] = exit_[k] * scale
    exit_["rebased_on_fill"] = round(scale, 4)
    return dict(id=sug["id"], name=sug["name"], taken=now.strftime("%Y-%m-%d %H:%M"),
                source=source, multiplier=lots, legs=legs, entry_net=entry_net,
                exit=exit_, status="open", alerts_sent=[], checks=[])


def take(sug_id, lots, prices, now, log_dir=LOG_DIR):
    sug = next((s for s in read_suggestions(log_dir) if s["id"] == sug_id), None)
    if sug is None:
        raise SystemExit(f"No suggestion {sug_id} in the log")
    prices = prices or [l["price"] for l in sug["legs"]]
    if len(prices) != len(sug["legs"]):
        raise SystemExit(f"{sug_id} has {len(sug['legs'])} legs; give that many prices")
    data = read_tracked(log_dir)
    if any(t["id"] == sug_id and t["status"] == "open" for t in data["trades"]):
        raise SystemExit(f"{sug_id} is already tracked")
    data["trades"].append(_new_trade(sug, lots, prices, "manual", now))
    write_tracked(data, log_dir)
    return data["trades"][-1]


def add_custom(name, legs, now, rules, log_dir=LOG_DIR, opened=None):
    """Track a position the user built themselves (not from the suggestion log)."""
    from nifty_risk import exit_plan
    idea = dict(legs=[dict(l, price=l["entry"]) for l in legs],
                net=-sum(l["lots"] * l["entry"] for l in legs) * LOT, eval={"cost": 0.0})
    plan = exit_plan(idea, rules, now.date())
    data = read_tracked(log_dir)
    n = 1 + sum(t["id"].startswith(f"P{now:%Y%m%d}-") for t in data["trades"])
    trade = dict(id=f"P{now:%Y%m%d}-{n}", name=name, taken=opened or now.strftime("%Y-%m-%d %H:%M"),
                 source="user position", multiplier=1, legs=legs, entry_net=idea["net"],
                 exit=plan, status="open", alerts_sent=[], checks=[])
    data["trades"].append(trade)
    write_tracked(data, log_dir)
    return trade


def close(sug_id, reason, now, log_dir=LOG_DIR):
    data = read_tracked(log_dir)
    for t in data["trades"]:
        if t["id"] == sug_id and t["status"] == "open":
            t["status"], t["closed"], t["close_reason"] = "closed", now.strftime("%Y-%m-%d %H:%M"), reason
    write_tracked(data, log_dir)


def match_positions(positions, now, log_dir=LOG_DIR, lookback_days=30):
    """Start tracking suggestions whose every leg is in the user's positions."""
    data = read_tracked(log_dir)
    open_ids = {t["id"] for t in data["trades"] if t["status"] == "open"}
    today = now.date().isoformat()
    pool = [dict(p) for p in positions]
    # positions already explained by open tracked trades are not reused
    for t in data["trades"]:
        if t["status"] != "open":
            continue
        for l in t["legs"]:
            for p in pool:
                if (p["expiry"], p["strike"], p["type"]) == (l["expiry"], l["strike"], l["type"]):
                    p["lots"] -= l["lots"]
    started = []
    sugs = [s for s in read_suggestions(log_dir) if s["id"] not in open_ids
            and min(l["expiry"] for l in s["legs"]) >= today
            and (now - datetime.fromisoformat(s["date"]).replace(tzinfo=now.tzinfo)).days <= lookback_days]
    for s in sorted(sugs, key=lambda s: s["id"], reverse=True):     # newest suggestion first
        ratios, found = [], []
        for l in s["legs"]:
            p = next((p for p in pool if (p["expiry"], p["strike"], p["type"]) ==
                      (l["expiry"], l["strike"], l["type"]) and p["lots"] * l["lots"] > 0), None)
            if p is None:
                break
            ratios.append(abs(p["lots"]) / abs(l["lots"]))
            found.append(p)
        if len(found) != len(s["legs"]):
            continue
        mult = math.floor(min(ratios))
        if mult < 1:
            continue
        prices = [p.get("entry") or l["price"] for p, l in zip(found, s["legs"])]
        trade = _new_trade(s, mult, prices, "positions", now)
        data["trades"].append(trade)
        started.append(trade)
        for p, l in zip(found, s["legs"]):
            p["lots"] -= l["lots"] * mult
    write_tracked(data, log_dir)
    return started


# ----------------------------------------------------------------- monitor

def check_trade(t, table, info, S, now, rules):
    """Evaluate one tracked trade against its exit plan; returns (snapshot, alerts)."""
    from nifty_risk import position_alerts

    x = t["exit"]
    rows_by = {(r["expiry"], r["strike"]): r for r in table}
    value, legs_now, missing = 0.0, [], []
    delta = 0.0
    for l in t["legs"]:
        r = rows_by.get((l["expiry"], l["strike"]))
        side = "ce" if l["type"] == "CE" else "pe"
        if r is None:
            missing.append(l)
            continue
        ltp = r[f"{side}_ltp"]
        value += l["lots"] * ltp * LOT
        delta += l["lots"] * r[f"{side}_delta"] * LOT
        legs_now.append(dict(l, ltp=ltp, iv=r["iv"], F=r["forward"], T=r["days"] / 365,
                             pnl=(ltp - l["entry"]) * l["lots"] * LOT,
                             delta=r[f"{side}_delta"] * l["lots"] * LOT,
                             gamma=r[f"{side}_gamma"] * l["lots"] * LOT,
                             theta=r[f"{side}_theta"] * l["lots"] * LOT,
                             vega=r[f"{side}_vega"] * l["lots"] * LOT))
    pnl = t["entry_net"] + value
    snap = dict(time=now.strftime("%Y-%m-%d %H:%M"), spot=S, value=round(value, 2),
                pnl=round(pnl, 2), delta=round(delta, 2))
    snap["ltps"] = {f"{l['expiry']}|{l['strike']:.0f}|{l['type']}": l["ltp"] for l in legs_now}
    alerts = []
    if missing:
        alerts.append(dict(level="warn", kind="Data",
                           msg=f"{t['id']}: no price for " + ", ".join(
                               f"{l['expiry']} {l['strike']:.0f} {l['type']}" for l in missing)
                           + "; add that expiry/strike to the chain file."))
        return snap, alerts
    m = t["multiplier"]
    if x["kind"] == "credit":
        buyback = -value
        if buyback <= x["tp_close_cost"] * m:
            alerts.append(dict(level="action", kind="Take profit",
                               msg=f"{t['id']} {t['name']}: buy-back cost ₹{buyback:,.0f} ≤ target "
                                   f"₹{x['tp_close_cost']*m:,.0f}. Close it; P&L ₹{pnl:,.0f} before exit costs."))
        elif buyback >= x["sl_close_cost"] * m:
            alerts.append(dict(level="action", kind="Stop loss",
                               msg=f"{t['id']} {t['name']}: buy-back cost ₹{buyback:,.0f} ≥ stop "
                                   f"₹{x['sl_close_cost']*m:,.0f}. Close or hedge now; P&L ₹{pnl:,.0f}."))
        elif buyback >= x["warn_close_cost"] * m:
            alerts.append(dict(level="warn", kind="Loss",
                               msg=f"{t['id']} {t['name']}: buy-back ₹{buyback:,.0f} is past halfway to the "
                                   f"₹{x['sl_close_cost']*m:,.0f} stop; P&L ₹{pnl:,.0f}."))
    else:
        if value >= x["tp_value"] * m:
            alerts.append(dict(level="action", kind="Take profit",
                               msg=f"{t['id']} {t['name']}: worth ₹{value:,.0f} ≥ target ₹{x['tp_value']*m:,.0f}. "
                                   f"Sell it; P&L ₹{pnl:,.0f}."))
        elif value <= x["sl_value"] * m:
            alerts.append(dict(level="action", kind="Stop loss",
                               msg=f"{t['id']} {t['name']}: worth ₹{value:,.0f} ≤ stop ₹{x['sl_value']*m:,.0f}. "
                                   f"Close it; P&L ₹{pnl:,.0f}."))
        elif value <= x["warn_value"] * m:
            alerts.append(dict(level="warn", kind="Loss",
                               msg=f"{t['id']} {t['name']}: worth ₹{value:,.0f}, past halfway to the "
                                   f"₹{x['sl_value']*m:,.0f} stop; P&L ₹{pnl:,.0f}."))
    for K, typ in x.get("short_strikes", []):
        through = (S >= K) if typ == "CE" else (S <= K)
        near = abs(S - K) / S * 100 <= rules["short_strike_buffer_pct"]
        if through:
            alerts.append(dict(level="action", kind="Strike breached",
                               msg=f"{t['id']} {t['name']}: spot {S:,.0f} has traded through the short "
                                   f"{K:.0f} {typ}. Exit rule says close or recentre."))
        elif near:
            alerts.append(dict(level="warn", kind="Strike tested",
                               msg=f"{t['id']} {t['name']}: spot {S:,.0f} is within "
                                   f"{rules['short_strike_buffer_pct']}% of the short {K:.0f} {typ}."))
    today = now.date().isoformat()
    if today >= x["exit_date"]:
        alerts.append(dict(level="action", kind="Exit date",
                           msg=f"{t['id']} {t['name']}: exit-by date {x['exit_date']} reached. Close it"
                               + (", or roll to next month (see roll alert)." if x["kind"] == "credit" else ".")))
    # hedge sizing from the shared rules (trade-level delta)
    book = dict(rows=legs_now)
    for a in position_alerts(book, table, info, S, dict(rules, book_loss_alert_rs=float("inf")), now.date()):
        if a["kind"] == "Hedge":
            alerts.append(dict(a, msg=f"{t['id']} {t['name']}: " + a["msg"]))

    # recentre: move the whole structure so the tested short strike sits at the implied move
    shorts = [l for l in legs_now if l["lots"] < 0]
    tested = [l for l in shorts
              if abs(l["delta"] / (l["lots"] * LOT)) >= rules["short_strike_delta_alert"]
              or ((S - l["strike"]) if l["type"] == "CE" else (l["strike"] - S)) / S * 100
              > -rules["short_strike_buffer_pct"]]
    if tested:
        l0 = max(tested, key=lambda l: abs(l["delta"] / l["lots"]))
        e0 = next((e for e in info if e["expiry"] == l0["expiry"]), None)
        if e0:
            target = e0["forward"] * (1 + e0["implied_move"] if l0["type"] == "CE" else 1 - e0["implied_move"])
            shift = round((target - l0["strike"]) / 50) * 50
            roll = _roll_quote(t["legs"], value, table, lambda l: (l["expiry"], l["strike"] + shift))
            if roll:
                alerts.append(dict(level="action", kind="Recentre",
                                   msg=f"{t['id']} {t['name']}: short {l0['strike']:.0f} {l0['type']} under pressure "
                                       f"(Δ {abs(l0['delta'] / (l0['lots'] * LOT)):.2f}). Move the whole position "
                                       f"{shift:+.0f} points to {roll['desc']}: close for ₹{-value:,.0f}, reopen for "
                                       f"{roll['open_txt']}, net {roll['net_txt']} before costs."))

    # roll to next month on/after the exit date (credit structures)
    if today >= x["exit_date"] and x["kind"] == "credit":
        first = min(l["expiry"] for l in t["legs"])
        cur = next((e for e in info if e["expiry"] == first), None)
        nxt = next((e for e in info if cur and e["days"] >= cur["days"] + 14), None)
        if cur and nxt:
            ratio = nxt["forward"] / cur["forward"]
            roll = _roll_quote(t["legs"], value, table,
                               lambda l: (nxt["expiry"] if l["expiry"] == first else l["expiry"], l["strike"] * ratio))
            if roll:
                alerts.append(dict(level="warn", kind="Roll",
                                   msg=f"{t['id']} {t['name']}: exit date reached. Roll to {nxt['expiry']} at the same "
                                       f"moneyness ({roll['desc']}): close for ₹{-value:,.0f}, reopen for "
                                       f"{roll['open_txt']}, net {roll['net_txt']} before costs."))
    return snap, alerts


def _roll_quote(legs, value_now, table, where):
    """Price closing the current legs and reopening them at new (expiry, strike) locations."""
    new_val, desc = 0.0, []
    for l in legs:
        exp, K = where(l)
        rows = [r for r in table if r["expiry"] == exp]
        if not rows:
            return None
        r = min(rows, key=lambda r: abs(r["strike"] - K))
        px = r["ce_ltp" if l["type"] == "CE" else "pe_ltp"]
        new_val += l["lots"] * px * LOT
        desc.append(f"{'buy' if l['lots'] > 0 else 'sell'} {exp} {r['strike']:.0f} {l['type']} @ {px:.2f}")
    open_net = -new_val                    # + credit received when reopening
    net = value_now + open_net             # closing receives value_now (negative = pay to close)
    return dict(desc="; ".join(desc),
                open_txt=f"{'a credit of' if open_net > 0 else 'a debit of'} ₹{abs(open_net):,.0f}",
                net_txt=f"{'credit' if net > 0 else 'debit'} ₹{abs(net):,.0f}")


def monitor(table, info, S, now, rules, log_dir=LOG_DIR, only_new=True):
    """Check every open tracked trade; returns list of (trade, snapshot, alerts, new_alerts)."""
    data = read_tracked(log_dir)
    _, _, mlog = _paths(log_dir)
    new_file = not mlog.exists()
    results = []
    with open(mlog, "a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["time", "id", "name", "spot", "value", "pnl", "delta", "alerts"])
        for t in data["trades"]:
            if t["status"] != "open":
                continue
            if max(l["expiry"] for l in t["legs"]) < now.date().isoformat():
                t["status"] = "expired"
                continue
            snap, alerts = check_trade(t, table, info, S, now, rules)
            day = now.strftime("%Y-%m-%d")
            keys = [f"{day}|{a['kind']}|{a['msg'].split(':')[0]}" for a in alerts]
            new = [a for a, k in zip(alerts, keys) if k not in t["alerts_sent"]]
            t["alerts_sent"] = sorted(set(t["alerts_sent"]) | set(keys))[-200:]
            t["checks"] = (t["checks"] + [dict(snap, alerts=[a["kind"] for a in alerts])])[-500:]
            t["last"] = dict(snap, alerts=alerts)
            w.writerow([snap["time"], t["id"], t["name"], S, snap["value"], snap["pnl"],
                        snap["delta"], "; ".join(a["kind"] for a in alerts)])
            results.append((t, snap, alerts, new if only_new else alerts))
    write_tracked(data, log_dir)
    return results


# --------------------------------------------------------------------- CLI

def main():
    from nifty_vol_surface import IST
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log-dir", default=str(LOG_DIR))
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("take", help="start tracking a suggestion you entered")
    t.add_argument("id")
    t.add_argument("--lots", type=int, default=1, help="multiple of the suggested size")
    t.add_argument("--prices", help="your fill price per leg, comma separated, in leg order")
    ad = sub.add_parser("add", help="track your own position (not from the suggestion log)")
    ad.add_argument("--name", required=True)
    ad.add_argument("--leg", action="append", required=True,
                    help="expiry,strike,CE|PE,lots(+long/-short),entry_price  (repeat per leg)")
    ad.add_argument("--opened", help="when the position was opened, 'YYYY-MM-DD HH:MM'")
    ad.add_argument("--rules", default=str(HERE / "rules.json"))
    c = sub.add_parser("close", help="stop tracking a trade you exited")
    c.add_argument("id")
    c.add_argument("--reason", default="closed by user")
    m = sub.add_parser("monitor", help="check tracked trades against a fresh chain")
    m.add_argument("--chain", required=True)
    m.add_argument("--spot", type=float, required=True)
    m.add_argument("--asof", required=True)
    m.add_argument("--rate", type=float, default=0.055)
    m.add_argument("--rules", default=str(HERE / "rules.json"))
    m.add_argument("--all", action="store_true", help="print all alerts, not only new ones")
    sub.add_parser("list", help="show tracked trades")
    a = ap.parse_args()
    now = (datetime.strptime(a.asof, "%Y-%m-%d %H:%M").replace(tzinfo=IST)
           if getattr(a, "asof", None) else datetime.now(IST))

    if a.cmd == "take":
        prices = [float(p) for p in a.prices.split(",")] if a.prices else None
        tr = take(a.id, a.lots, prices, now, a.log_dir)
        print(f"Tracking {tr['id']} {tr['name']} ×{tr['multiplier']}, entry net ₹{tr['entry_net']:,.0f}")
    elif a.cmd == "add":
        from nifty_risk import load_rules
        legs = []
        for spec in a.leg:
            e, k, ty, lots, px = [x.strip() for x in spec.split(",")]
            legs.append(dict(expiry=e, strike=float(k), type=ty.upper(), lots=float(lots), entry=float(px)))
        tr = add_custom(a.name, legs, now, load_rules(a.rules), a.log_dir, a.opened)
        print(f"Tracking {tr['id']} {tr['name']}: entry {'credit' if tr['entry_net'] > 0 else 'debit'} "
              f"₹{abs(tr['entry_net']):,.0f}, exit by {tr['exit']['exit_date']}")
    elif a.cmd == "close":
        close(a.id, a.reason, now, a.log_dir)
        print(f"Closed {a.id}")
    elif a.cmd == "list":
        for tr in read_tracked(a.log_dir)["trades"]:
            last = tr.get("last", {})
            print(f"{tr['id']} {tr['name']} ×{tr['multiplier']} [{tr['status']}] "
                  f"P&L ₹{last.get('pnl', 0):,.0f} at {last.get('time', '-')}")
    else:
        from nifty_dashboard import expiry_info, make_table
        from nifty_risk import load_rules
        from nifty_vol_surface import chain_points, load_csv
        chains = load_csv(a.chain, a.spot)
        points = []
        for e, d in chains.items():
            points += chain_points(e, d, now, a.rate, 0.15)
        table = make_table(chains, points, a.spot, a.rate)
        info = expiry_info(table, a.spot, now)
        res = monitor(table, info, a.spot, now, load_rules(a.rules), a.log_dir, only_new=not a.all)
        if not res:
            print("No open tracked trades.")
        for tr, snap, alerts, new in res:
            print(f"{tr['id']} {tr['name']} ×{tr['multiplier']}: P&L ₹{snap['pnl']:,.0f}, "
                  f"delta {snap['delta']:+.0f}, {len(alerts)} alert(s), {len(new)} new")
            for al in new:
                print(f"  NEW [{al['level']}] {al['kind']}: {al['msg']}")


if __name__ == "__main__":
    main()
