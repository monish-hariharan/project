# 500-point condor test (NSE bhavcopy, 2019-2026)

Same entries as the main bhavcopy run (nearest expiry >= 12 days, exit 3 trading days before expiry,
TP 50% of credit, stop capped at half max loss). ₹ per 65-unit lot after charges and slippage.

| shorts / wings | n | win | mean | median | worst | 5th pct | credit (pts) | max loss/lot | mean as % of max loss |
|---|---|---|---|---|---|---|---|---|---|
| 16Δ / 200 (current) | 333 | 61.3% | −762 | +662 | −8,948 | −5,304 | 39.6 | 10,477 | −7.3% |
| spot±500 / 200 | 324 | 50.9% | −857 | +40 | −9,123 | −5,255 | 46.9 | 10,023 | −8.6% |
| 16Δ / 500 | 342 | 67.3% | −635 | +1,499 | −16,378 | −8,909 | 66.7 | 28,221 | −2.3% |
| spot±500 / 500 | 323 | 60.4% | −705 | +656 | −16,724 | −8,748 | 81.6 | 27,550 | −2.6% |
| VIX range / 500 | 244 | 68.4% | −616 | +1,362 | −15,433 | −10,515 | 60.5 | 28,675 | −2.1% |

Paired (same entry day) 500-pt minus 200-pt wings: 16Δ +120 (95% CI −114 to +352),
spot±500 +159 (−116 to +437), VIX range +73 (−206 to +347) — not distinguishable from zero.
Shorts at spot±500 vs 16Δ (200 wings): −120 (−291 to +43).
Every variant is negative in 7 or 8 of 8 years. Dashboard condor unchanged.
