# Monthly-expiry test (NSE bhavcopy, 2019-2026)

Entry every trading day on the nearest monthly expiry 12-45 calendar days out (94 monthly expiries,
all in the last week of their month); exit 3 trading days before expiry, TP 50% of credit, stop capped
at half max loss; 200-pt wings. ₹ per 65-unit lot after charges and slippage. 95% CI from a bootstrap
that resamples whole expiries (daily entries on the same expiry are not independent).

| family | entries | win | mean | 95% CI | median | worst | years positive |
|---|---|---|---|---|---|---|---|
| condor 16Δ | 1,830 | 67.6% | −577 | −911 to −258 | +921 | −9,072 | 1/8 (2019) |
| condor VIX range | 1,213 | 67.4% | −749 | −1,157 to −373 | +655 | −11,534 | 0/6 |
| condor spot±500 | 1,855 | 40.6% | −1,050 | −1,363 to −735 | −1,176 | −10,256 | 0/8 |
| bull call | 1,877 | 40.3% | −1,310 | −1,783 to −835 | −3,378 | −9,881 | 0/8 |
| bear put | 1,877 | 36.3% | −915 | −1,238 to −597 | −2,466 | −6,673 | 0/8 |

16Δ condor by days to expiry at entry: 12-17 −426, 18-24 −353, 25-31 −699, 32-38 −793, 39-45 −698.
By vol edge: Rich −606, Fair −562, Cheap −504. 47% of expiries had a positive average.
The dashboard now uses this record whenever the suggested expiry is a monthly one.
