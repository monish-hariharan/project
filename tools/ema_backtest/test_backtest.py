import unittest

from backtest import atr, backtest, ema, round_trip_cost, COSTS

FREE = {**COSTS, "brokerage_per_lot": 0.0, "stt_sell": 0.0, "exchange_txn": 0.0,
        "sebi": 0.0, "stamp_buy": 0.0}


def bars_from(closes, spread=1.0):
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append({"date": f"d{i // 24}", "seq": i % 24, "open": o,
                    "high": max(o, c) + spread, "low": min(o, c) - spread, "close": c})
        prev = c
    return out


class EmaAtr(unittest.TestCase):
    def test_ema_seed_and_step(self):
        e = ema([1, 2, 3, 4], 3)
        self.assertEqual(e[:2], [None, None])
        self.assertAlmostEqual(e[2], 2.0)
        self.assertAlmostEqual(e[3], 4 * 0.5 + 2.0 * 0.5)

    def test_atr_wilder(self):
        b = bars_from([10, 10, 10, 10], spread=1)
        a = atr(b, 2)
        self.assertAlmostEqual(a[1], 2.0)
        self.assertAlmostEqual(a[3], 2.0)


class Mechanics(unittest.TestCase):
    def setUp(self):
        # Flat, then up-trend (bullish cross), then down-trend (bearish cross).
        self.closes = [100.0] * 60 + [100 + i for i in range(1, 41)] + [140 - i for i in range(1, 61)]
        self.bars = bars_from(self.closes, spread=0.5)

    def test_fills_at_next_open(self):
        trades, _ = backtest(self.bars, 3, 6, 2, 3, None, "long", costs=FREE, warmup=10)
        self.assertTrue(trades)
        t = trades[0]
        ef = ema(self.closes, 3)
        es = ema(self.closes, 6)
        sig = t["entry_i"] - 1
        self.assertTrue(ef[sig - 1] <= es[sig - 1] and ef[sig] > es[sig])
        self.assertEqual(t["entry"], self.bars[t["entry_i"]]["open"])
        self.assertEqual(t["reason"], "ema_exit")
        self.assertEqual(t["exit"], self.bars[t["exit_i"]]["open"])

    def test_short_only_and_reverse(self):
        shorts, _ = backtest(self.bars, 3, 6, 2, 3, None, "short", costs=FREE, warmup=10)
        self.assertTrue(all(t["side"] == "short" for t in shorts))
        self.assertTrue(any(t["points"] > 0 for t in shorts))

    def test_stop_fills_at_stop_or_gap_open(self):
        closes = [100.0] * 60 + [100 + i for i in range(1, 15)] + [60.0, 59.0, 58.0]
        bars = bars_from(closes, spread=0.5)
        trades, _ = backtest(bars, 3, 6, 2, 3, 1.5, "long", costs=FREE, warmup=10)
        stopped = [t for t in trades if t["reason"] == "stop"]
        self.assertTrue(stopped)
        t = stopped[0]
        b = bars[t["exit_i"]]
        self.assertEqual(t["exit"], min(b["open"], t["stop"]))

    def test_costs_positive_and_stt_on_sell_only(self):
        c = round_trip_cost(23000, 23100, "long", COSTS)
        self.assertGreater(c, 80)
        no_stt = round_trip_cost(23000, 23100, "long", {**COSTS, "stt_sell": 0})
        self.assertAlmostEqual(c - no_stt, 23100 * 65 * 0.0005)


if __name__ == "__main__":
    unittest.main()
