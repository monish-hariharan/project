"""NIFTY 50 month-of-year seasonality: is each calendar month bullish or bearish?

Pure price analysis -- no news/event filtering or adjustment of any kind.

Input : data/nifty_monthly_dhan.csv  (monthly OHLC built from Dhan daily candles, NIFTY spot, securityId 13)
Output: data/seasonality.json        (consumed by dashboard.html)

Monthly return = close(M) / close(M-1) - 1. For the first month (Jan 2006) the month's
open is used in place of the prior close. A month still in progress is reported as
month-to-date and excluded from every statistic.

Tests per calendar month (H0 in brackets):
  * one-sample t-test           [mean monthly return = 0]           -> primary p-value
  * Wilcoxon signed-rank        [median monthly return = 0]          -> robust to fat tails
  * binomial sign test          [P(up month) = 0.5]                  -> win-rate significance
  * Welch t-test vs other months [month mean = mean of other 11]     -> seasonality beyond drift
Plus Benjamini-Hochberg adjusted p-values across the 12 months, and Kruskal-Wallis
across all 12 months together [no month-of-year effect at all].
"""
import json
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

HERE = Path(__file__).parent
ALPHA = 0.05
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def bh_adjust(p):
    p = np.asarray(p, dtype=float)
    n = len(p)
    order = np.argsort(p)
    ranked = p[order] * n / np.arange(1, n + 1)
    ranked = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty(n)
    out[order] = np.minimum(ranked, 1.0)
    return out


def verdict(mean, p):
    if p < ALPHA:
        return "Bullish" if mean > 0 else "Bearish"
    return "Leans bullish" if mean > 0 else "Leans bearish"


def main(today=None):
    today = today or date.today()
    df = pd.read_csv(HERE / "data" / "nifty_monthly_dhan.csv")
    df["prev_close"] = df["close"].shift(1).fillna(df["open"])
    df["ret"] = df["close"] / df["prev_close"] - 1
    df["year"] = df["month"].str[:4].astype(int)
    df["m"] = df["month"].str[5:7].astype(int)

    current = f"{today.year:04d}-{today.month:02d}"
    partial = df[df["month"] == current]
    full = df[df["month"] != current].copy()

    rows = []
    for m in range(1, 13):
        r = full.loc[full["m"] == m, "ret"].to_numpy()
        rest = full.loc[full["m"] != m, "ret"].to_numpy()
        ups = int((r > 0).sum())
        rows.append({
            "month": MONTHS[m - 1],
            "n": int(len(r)),
            "mean": float(r.mean()),
            "median": float(np.median(r)),
            "std": float(r.std(ddof=1)),
            "up": ups,
            "down": int(len(r) - ups),
            "win_rate": ups / len(r),
            "best": float(r.max()),
            "worst": float(r.min()),
            "t_stat": float(stats.ttest_1samp(r, 0).statistic),
            "p_t": float(stats.ttest_1samp(r, 0).pvalue),
            "p_wilcoxon": float(stats.wilcoxon(r).pvalue),
            "p_sign": float(stats.binomtest(ups, len(r), 0.5).pvalue),
            "p_vs_rest": float(stats.ttest_ind(r, rest, equal_var=False).pvalue),
        })

    for key in ("p_t", "p_wilcoxon", "p_sign", "p_vs_rest"):
        adj = bh_adjust([row[key] for row in rows])
        for row, a in zip(rows, adj):
            row[key + "_bh"] = float(a)
    for row in rows:
        row["verdict"] = verdict(row["mean"], row["p_t"])

    all_r = full["ret"].to_numpy()
    groups = [full.loc[full["m"] == m, "ret"].to_numpy() for m in range(1, 13)]
    out = {
        "source": "Dhan API v2 historical daily candles, NIFTY 50 spot index (IDX_I, securityId 13), aggregated to calendar months",
        "generated": today.isoformat(),
        "first_month": full["month"].iloc[0],
        "last_full_month": full["month"].iloc[-1],
        "n_months": int(len(full)),
        "n_trading_days": int(full["trading_days"].sum()),
        "alpha": ALPHA,
        "overall": {
            "mean": float(all_r.mean()),
            "median": float(np.median(all_r)),
            "win_rate": float((all_r > 0).mean()),
            "p_t": float(stats.ttest_1samp(all_r, 0).pvalue),
            "p_sign": float(stats.binomtest(int((all_r > 0).sum()), len(all_r), 0.5).pvalue),
            "kruskal_h": float(stats.kruskal(*groups).statistic),
            "kruskal_p": float(stats.kruskal(*groups).pvalue),
            "anova_p": float(stats.f_oneway(*groups).pvalue),
        },
        "months": rows,
        "series": [
            {"month": r.month, "close": r.close, "ret": float(r.ret)} for r in full.itertuples()
        ],
        "partial": None if partial.empty else {
            "month": partial["month"].iloc[0],
            "close": float(partial["close"].iloc[0]),
            "ret": float(partial["ret"].iloc[0]),
            "trading_days": int(partial["trading_days"].iloc[0]),
        },
    }
    (HERE / "data" / "seasonality.json").write_text(json.dumps(out, indent=1))

    print(f"{out['first_month']} .. {out['last_full_month']}: {out['n_months']} months, "
          f"{out['n_trading_days']} trading days")
    print(f"{'Mon':4}{'n':>4}{'mean%':>8}{'med%':>8}{'win%':>7}{'p(t)':>8}{'p_BH':>7}"
          f"{'p(wil)':>8}{'p(sign)':>8}{'p(vs rest)':>11}  verdict")
    for r in rows:
        print(f"{r['month']:4}{r['n']:>4}{r['mean']*100:>8.2f}{r['median']*100:>8.2f}"
              f"{r['win_rate']*100:>7.1f}{r['p_t']:>8.3f}{r['p_t_bh']:>7.3f}{r['p_wilcoxon']:>8.3f}"
              f"{r['p_sign']:>8.3f}{r['p_vs_rest']:>11.3f}  {r['verdict']}")
    o = out["overall"]
    print(f"All months: mean {o['mean']*100:.2f}%  win {o['win_rate']*100:.1f}%  p(t) {o['p_t']:.4f}")
    print(f"Kruskal-Wallis across months: H={o['kruskal_h']:.2f} p={o['kruskal_p']:.3f}; ANOVA p={o['anova_p']:.3f}")




def export_dashboard_files():
    """Flatten seasonality.json into the three row files the dashboard loads."""
    s = json.loads((HERE / "data" / "seasonality.json").read_text())
    out = HERE / "data" / "dashboard"
    out.mkdir(exist_ok=True)
    (out / "month_stats.json").write_text(json.dumps(s["months"], indent=1))
    pd.DataFrame(
        [{"month": r["month"], "year": int(r["month"][:4]), "mon": MONTHS[int(r["month"][5:]) - 1],
          "close": r["close"], "ret": round(r["ret"], 6)} for r in s["series"]]
    ).to_csv(out / "monthly_returns.csv", index=False)
    summary = {k: s[k] for k in ("first_month", "last_full_month", "n_months", "n_trading_days", "alpha")}
    summary.update({f"overall_{k}": v for k, v in s["overall"].items()})
    if s["partial"]:
        summary.update({f"mtd_{k}": v for k, v in s["partial"].items()})
    (out / "summary.json").write_text(json.dumps([summary], indent=1))


if __name__ == "__main__":
    main(date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else None)
    export_dashboard_files()
