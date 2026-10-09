# NIFTY 50 monthly seasonality

Is each calendar month bullish or bearish for the NIFTY 50, and is it statistically significant?
Pure price analysis: no news or event filtering.

- `data/nifty_monthly_dhan.csv` – monthly OHLC of NIFTY 50 spot (Dhan API v2, `IDX_I`, securityId 13), Jan 2006 – Oct 2026 (Oct 2026 is month-to-date). Dhan's history starts in 2006. The 5,148 trading days sum to exactly what Dhan returns for the full range.
- `analyze.py` – computes close-to-close monthly returns and, per calendar month, a one-sample t-test, Wilcoxon signed-rank, binomial sign test, Welch t-test vs the other months, Benjamini-Hochberg adjusted p-values, and a Kruskal-Wallis test across all 12 months. Run `python3 analyze.py [YYYY-MM-DD]`; the in-progress month is excluded from stats.
- `data/seasonality.json`, `data/dashboard/*` – outputs feeding the dashboard.
- `dashboard/index.html` – the dashboard page (published as a claude.ai Dashboard artifact).

Yahoo Finance (`^NSEI`) was not reachable from the build environment, so Dhan is the only source.
