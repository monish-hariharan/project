# Early-exit test (NSE bhavcopy, 2019-2026)

Same 905 entries in all three runs (nearest expiry >= 24 calendar days, median 28 DTE; 16Δ condor
median credit 42.6 pts); only the time exit differs: 3, 7 or 10 trading days before expiry.
TP 50% of credit and the stops unchanged. ₹ per 65-unit lot after charges and slippage.

16Δ condor (n=208)
| exit | win | mean | median | worst | 5th pct | stop rate | PF |
|---|---|---|---|---|---|---|---|
| 3 days | 67.8% | −676 | +916 | −9,403 | −5,990 | 30.8% | 0.55 |
| 7 days | 61.5% | −754 | +657 | −8,491 | −5,718 | 27.4% | 0.47 |
| 10 days | 55.8% | −763 | +295 | −8,491 | −5,364 | 18.3% | 0.42 |

Paired difference exit10 − exit3 per trade: condor −115 (95% bootstrap CI −344 to +111),
VIX-range condor −14 (−310 to +295), bull +51, bear −31; none distinguishable from zero.
The 86 condors that the 10-day rule closed on its exit date averaged −832 at that point; under the
3-day rule 56 of them went on to hit take profit.

Conclusion: exiting 10 days early cuts stop-outs and the loss tail a little but gives up as much in
take-profits; the average is unchanged (all negative). Dashboard keeps the 3-day rule.
