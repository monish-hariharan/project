#!/usr/bin/env python3
"""
NIFTY 50 implied-volatility surface from the Dhan HQ v2 Option Chain API.

Usage
-----
    export DHAN_CLIENT_ID=1000000001
    export DHAN_ACCESS_TOKEN=eyJ...
    python nifty_vol_surface.py                 # live data, first 6 expiries
    python nifty_vol_surface.py --expiries 10   # more expiries
    python nifty_vol_surface.py --demo          # offline, synthetic chain

Outputs (in --out, default ./output):
    nifty_vol_surface_<date>.html   interactive 3D surface + smiles + term structure
    nifty_iv_points_<date>.csv      every quote used (strike, expiry, IV, ...)
    nifty_iv_grid_<date>.csv        the interpolated surface grid

Method
------
* For each expiry, the forward F is implied from put-call parity
  (median of C - P + K over the strikes nearest ATM).
* IV is solved from the bid/ask mid with Black-76 on F (falls back to LTP,
  then to Dhan's own `implied_volatility` field if no price is usable).
* Only OTM options are used: puts for K < F, calls for K >= F.
* The surface is interpolated in total-variance (sigma^2 * T) space,
  linearly in T for each log-moneyness, so it stays calendar-consistent.
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests
from scipy.optimize import brentq
from scipy.stats import norm

API = "https://api.dhan.co/v2"
NIFTY_SCRIP = 13          # NIFTY 50 security id on Dhan
NIFTY_SEG = "IDX_I"
IST = timezone(timedelta(hours=5, minutes=30))
YEAR_SECONDS = 365.0 * 24 * 3600
RATE_LIMIT_S = 3.1        # Dhan allows one option-chain request per 3 s


# --------------------------------------------------------------------------- API

class Dhan:
    def __init__(self, client_id: str, token: str):
        self.s = requests.Session()
        self.s.headers.update({
            "access-token": token,
            "client-id": client_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        self._last = 0.0

    def _post(self, path: str, body: dict) -> dict:
        wait = RATE_LIMIT_S - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        r = self.s.post(f"{API}{path}", json=body, timeout=20)
        self._last = time.monotonic()
        if r.status_code != 200:
            raise RuntimeError(f"Dhan {path} -> HTTP {r.status_code}: {r.text[:300]}")
        j = r.json()
        if j.get("status") not in (None, "success"):
            raise RuntimeError(f"Dhan {path} -> {j}")
        return j

    def expiries(self) -> list[str]:
        j = self._post("/optionchain/expirylist",
                       {"UnderlyingScrip": NIFTY_SCRIP, "UnderlyingSeg": NIFTY_SEG})
        return sorted(j["data"])

    def chain(self, expiry: str) -> dict:
        j = self._post("/optionchain", {"UnderlyingScrip": NIFTY_SCRIP,
                                        "UnderlyingSeg": NIFTY_SEG, "Expiry": expiry})
        return j["data"]


# ----------------------------------------------------------------------- pricing

def black76(F, K, T, sigma, df, is_call):
    if sigma <= 0 or T <= 0:
        return df * max((F - K) if is_call else (K - F), 0.0)
    sd = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sd * sd) / sd
    d2 = d1 - sd
    if is_call:
        return df * (F * norm.cdf(d1) - K * norm.cdf(d2))
    return df * (K * norm.cdf(-d2) - F * norm.cdf(-d1))


def implied_vol(price, F, K, T, df, is_call):
    intrinsic = df * max((F - K) if is_call else (K - F), 0.0)
    upper = df * (F if is_call else K)
    if not (intrinsic + 1e-6 < price < upper):
        return None
    try:
        return brentq(lambda s: black76(F, K, T, s, df, is_call) - price, 1e-4, 5.0,
                      xtol=1e-7, maxiter=200)
    except ValueError:
        return None


def mid_price(leg: dict) -> float | None:
    bid, ask = leg.get("top_bid_price") or 0, leg.get("top_ask_price") or 0
    if bid > 0 and ask > 0 and ask >= bid and (ask - bid) <= 0.5 * ask:
        return 0.5 * (bid + ask)
    ltp = leg.get("last_price") or 0
    # without a two-sided quote, trust the LTP only if the contract has open interest
    return ltp if ltp > 0 and (leg.get("oi") or 0) > 0 else None


def time_to_expiry(expiry: str, now: datetime) -> float:
    exp = datetime.strptime(expiry, "%Y-%m-%d").replace(hour=15, minute=30, tzinfo=IST)
    return max((exp - now).total_seconds(), 60.0) / YEAR_SECONDS


@dataclass
class Point:
    expiry: str
    T: float
    strike: float
    forward: float
    k: float           # log-moneyness ln(K/F)
    iv: float
    side: str
    source: str        # mid | ltp | dhan
    oi: int


def implied_forward(oc: dict, spot: float, T: float, df: float) -> float:
    rows = []
    for ks, legs in oc.items():
        K = float(ks)
        c, p = legs.get("ce") or {}, legs.get("pe") or {}
        cm, pm = mid_price(c), mid_price(p)
        if cm and pm and (c.get("oi") or 0) > 0 and (p.get("oi") or 0) > 0:
            rows.append((abs(K - spot), K + (cm - pm) / df))
    if not rows:
        return spot / df
    rows.sort()
    return float(np.median([f for _, f in rows[:6]]))


def chain_points(expiry: str, data: dict, now: datetime, r: float,
                 max_abs_k: float) -> list[Point]:
    spot = float(data["last_price"])
    oc = data["oc"]
    T = time_to_expiry(expiry, now)
    df = math.exp(-r * T)
    F = implied_forward(oc, spot, T, df)
    pts = []
    for ks, legs in oc.items():
        K = float(ks)
        k = math.log(K / F)
        if abs(k) > max_abs_k:
            continue
        side = "pe" if K < F else "ce"
        leg = legs.get(side) or {}
        if not leg:
            continue
        iv, src = None, None
        m = mid_price(leg)
        if m:
            iv = implied_vol(m, F, K, T, df, side == "ce")
            src = "mid" if (leg.get("top_bid_price") or 0) > 0 else "ltp"
        if iv is None and (leg.get("implied_volatility") or 0) > 0:
            iv, src = leg["implied_volatility"] / 100.0, "dhan"
        if iv is None or not (0.01 < iv < 2.0):
            continue
        pts.append(Point(expiry, T, K, F, k, iv, side.upper(), src, int(leg.get("oi") or 0)))
    pts.sort(key=lambda p: p.strike)
    return pts


# ------------------------------------------------------------------- the surface

def build_grid(points: list[Point], n_k: int = 61):
    """Interpolate to a (T, k) grid in total-variance space."""
    expiries = sorted({p.expiry for p in points}, key=lambda e: e)
    Ts, smiles = [], []
    for e in expiries:
        ps = [p for p in points if p.expiry == e]
        if len(ps) < 5:
            continue
        Ts.append(ps[0].T)
        smiles.append((np.array([p.k for p in ps]), np.array([p.iv for p in ps])))
    if len(Ts) < 2:
        raise RuntimeError("Need at least two expiries with usable quotes to build a surface.")

    # common moneyness range covered by every expiry (avoids extrapolating wings)
    lo = max(s[0].min() for s in smiles)
    hi = min(s[0].max() for s in smiles)
    k_grid = np.linspace(lo, hi, n_k)

    # per-expiry smooth smile: light quadratic-in-k smoothing via local poly fit
    w = np.empty((len(Ts), n_k))
    for i, (T, (ks, ivs)) in enumerate(zip(Ts, smiles)):
        order = np.argsort(ks)
        iv_k = np.interp(k_grid, ks[order], ivs[order])
        w[i] = iv_k ** 2 * T

    # enforce non-decreasing total variance in T (no calendar arbitrage)
    w = np.maximum.accumulate(w, axis=0)

    T_grid = np.linspace(Ts[0], Ts[-1], 40)
    W = np.empty((len(T_grid), n_k))
    for j in range(n_k):
        W[:, j] = np.interp(T_grid, Ts, w[:, j])
    IV = np.sqrt(W / T_grid[:, None])
    return k_grid, T_grid, IV, np.array(Ts), smiles


# ------------------------------------------------------------------------ output

def write_csvs(out: Path, tag: str, points, k_grid, T_grid, IV, spot):
    with open(out / f"nifty_iv_points_{tag}.csv", "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["expiry", "days", "strike", "forward", "log_moneyness", "iv_pct",
                     "side", "source", "oi"])
        for p in points:
            wr.writerow([p.expiry, round(p.T * 365, 3), p.strike, round(p.forward, 2),
                         round(p.k, 5), round(p.iv * 100, 3), p.side, p.source, p.oi])
    with open(out / f"nifty_iv_grid_{tag}.csv", "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["days \\ moneyness K/F"] + [f"{math.exp(k):.4f}" for k in k_grid])
        for T, row in zip(T_grid, IV):
            wr.writerow([round(T * 365, 2)] + [round(v * 100, 3) for v in row])


def write_html(out: Path, tag: str, points, k_grid, T_grid, IV, spot, asof):
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    days = T_grid * 365
    mny = np.exp(k_grid) * 100
    fig = make_subplots(
        rows=2, cols=2, specs=[[{"type": "scene", "colspan": 2}, None],
                               [{"type": "xy"}, {"type": "xy"}]],
        row_heights=[0.62, 0.38], vertical_spacing=0.07,
        subplot_titles=("Implied volatility surface", "Smile by expiry",
                        "ATM term structure"))

    fig.add_trace(go.Surface(
        x=mny, y=days, z=IV * 100, colorscale="Viridis",
        colorbar=dict(title="IV %", len=0.55, y=0.72),
        hovertemplate="K/F %{x:.1f}%<br>%{y:.1f} days<br>IV %{z:.2f}%<extra></extra>"),
        row=1, col=1)

    expiries = sorted({p.expiry for p in points})
    atm_days, atm_iv = [], []
    for e in expiries:
        ps = [p for p in points if p.expiry == e]
        x = [p.strike for p in ps]
        y = [p.iv * 100 for p in ps]
        fig.add_trace(go.Scatter(
            x=x, y=y, mode="lines+markers", name=f"{e} ({ps[0].T*365:.1f}d)",
            marker=dict(size=4),
            hovertemplate="K %{x:.0f}<br>IV %{y:.2f}%<extra>" + e + "</extra>"),
            row=2, col=1)
        ks = np.array([p.k for p in ps])
        order = np.argsort(ks)
        atm_days.append(ps[0].T * 365)
        atm_iv.append(float(np.interp(0.0, ks[order], np.array(y)[order])))
    fig.add_shape(type="line", x0=spot, x1=spot, y0=0, y1=1, xref="x", yref="y domain",
                  line=dict(dash="dot", color="gray"))
    fig.add_trace(go.Scatter(x=atm_days, y=atm_iv, mode="lines+markers",
                             name="ATM IV", showlegend=False,
                             hovertemplate="%{x:.1f} days<br>ATM IV %{y:.2f}%<extra></extra>"),
                  row=2, col=2)

    fig.update_layout(
        title=f"NIFTY 50 implied volatility surface — {asof:%d %b %Y %H:%M} IST "
              f"(spot {spot:,.2f})",
        height=1100, template="plotly_white",
        scene=dict(xaxis_title="Moneyness K/F (%)", yaxis_title="Days to expiry",
                   zaxis_title="IV (%)", camera=dict(eye=dict(x=1.6, y=-1.6, z=0.8))),
        legend=dict(orientation="h", y=-0.08))
    fig.update_xaxes(title_text="Strike", row=2, col=1)
    fig.update_yaxes(title_text="IV (%)", row=2, col=1)
    fig.update_xaxes(title_text="Days to expiry", row=2, col=2)
    fig.update_yaxes(title_text="ATM IV (%)", row=2, col=2)
    path = out / f"nifty_vol_surface_{tag}.html"
    fig.write_html(path, include_plotlyjs="cdn")
    return path


# -------------------------------------------------------------------------- demo

def demo_chains(now: datetime, n: int):
    """Synthetic chains in Dhan's response format (for offline testing)."""
    rng = np.random.default_rng(7)
    spot, r = 25000.0, 0.065
    d = now.date()
    exps = []
    while len(exps) < n:                       # weekly Tuesday expiries
        d += timedelta(days=1)
        if d.weekday() == 1:
            exps.append(d.isoformat())
    chains = {}
    for e in exps:
        T = time_to_expiry(e, now)
        F = spot * math.exp(r * T)
        df = math.exp(-r * T)
        oc = {}
        for K in np.arange(round(spot / 50) * 50 - 2500, round(spot / 50) * 50 + 2550, 50):
            k = math.log(K / F)
            vol = 0.115 + 0.01 * math.sqrt(T) - 0.35 * k * (0.08 / math.sqrt(T + 0.02)) \
                + 1.2 * k * k / math.sqrt(T + 0.02) * 0.1
            legs = {}
            for side in ("ce", "pe"):
                px = black76(F, K, T, vol, df, side == "ce")
                if px < 0.1:
                    continue
                spr = max(0.05, px * 0.01)
                px *= 1 + rng.normal(0, 0.003)
                legs[side] = {"last_price": round(px, 2),
                              "top_bid_price": round(px - spr / 2, 2),
                              "top_ask_price": round(px + spr / 2, 2),
                              "implied_volatility": vol * 100, "oi": int(rng.integers(1e3, 1e6))}
            oc[f"{K:.6f}"] = legs
        chains[e] = {"last_price": spot, "oc": oc}
    return chains


def load_csv(path: str, spot: float) -> dict:
    """Read a saved snapshot into Dhan's option-chain response shape."""
    chains: dict = {}
    with open(path) as f:
        rows = csv.reader(line for line in f if not line.startswith("#"))
        for e, k, cl, co, pl, po in rows:
            oc = chains.setdefault(e, {"last_price": spot, "oc": {}})["oc"]
            oc[f"{float(k):.6f}"] = {
                "ce": {"last_price": float(cl), "oi": int(co)},
                "pe": {"last_price": float(pl), "oi": int(po)},
            }
    return chains


# -------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--expiries", type=int, default=6, help="number of expiries (default 6)")
    ap.add_argument("--rate", type=float, default=0.055, help="risk-free rate (default 5.5%%)")
    ap.add_argument("--max-moneyness", type=float, default=0.10,
                    help="max |ln(K/F)| kept (default 0.10 ≈ ±10%%)")
    ap.add_argument("--out", default="output")
    ap.add_argument("--demo", action="store_true", help="use synthetic data, no API call")
    ap.add_argument("--csv", help="load a saved chain snapshot instead of calling the API "
                                  "(columns: expiry,strike,ce_ltp,ce_oi,pe_ltp,pe_oi)")
    ap.add_argument("--spot", type=float, help="underlying spot (required with --csv)")
    ap.add_argument("--asof", help="snapshot time, 'YYYY-MM-DD HH:MM' IST (with --csv)")
    a = ap.parse_args()

    now = datetime.now(IST)
    if a.asof:
        now = datetime.strptime(a.asof, "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    if a.csv:
        if not a.spot:
            sys.exit("--spot is required with --csv")
        chains = load_csv(a.csv, a.spot)
    elif a.demo:
        chains = demo_chains(now, a.expiries)
    else:
        cid, tok = os.environ.get("DHAN_CLIENT_ID"), os.environ.get("DHAN_ACCESS_TOKEN")
        if not cid or not tok:
            sys.exit("Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN (or use --demo).")
        api = Dhan(cid, tok)
        exps = [e for e in api.expiries() if e >= now.date().isoformat()][: a.expiries]
        print(f"Expiries: {', '.join(exps)}")
        chains = {}
        for e in exps:
            print(f"  fetching {e} ...", flush=True)
            chains[e] = api.chain(e)

    points, spot = [], None
    for e, data in chains.items():
        spot = spot or float(data["last_price"])
        ps = chain_points(e, data, now, a.rate, a.max_moneyness)
        print(f"  {e}: {len(ps):3d} OTM quotes, F = {ps[0].forward if ps else float('nan'):,.2f}")
        points += ps

    k_grid, T_grid, IV, _, _ = build_grid(points)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    tag = now.strftime("%Y-%m-%d") + ("_demo" if a.demo and not a.csv else "")
    write_csvs(out, tag, points, k_grid, T_grid, IV, spot)
    html = write_html(out, tag, points, k_grid, T_grid, IV, spot, now)
    atm = IV[0, np.argmin(np.abs(k_grid))] * 100
    print(f"\nSpot {spot:,.2f} | front ATM IV {atm:.2f}% | {len(points)} quotes")
    print(f"Surface: {html.resolve()}")


if __name__ == "__main__":
    main()
