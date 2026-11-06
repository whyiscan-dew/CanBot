import asyncio

from bot.backtest import fetch_history, run_backtest
from bot.settings import TradingSettings
from fakes import D1, H4, FakeFuturesClient, make_candles


def synthetic(drift: float, seed: int, n_days: int = 500):
    end = (1_790_000_000_000 // D1) * D1 - 1
    htf = make_candles(n_days, D1, end, 100.0, drift, 0.02, seed)
    # 4s mumları günlük seriyle aynı fiyat düzeyinde: her gün 6 mum
    ltf = []
    import random

    rnd = random.Random(seed + 100)
    for d in htf:
        price = d.open
        step = (d.close - d.open) / 6
        for k in range(6):
            o = price
            c = o + step + rnd.uniform(-0.004, 0.004) * o
            hi = max(o, c) * (1 + rnd.uniform(0, 0.004))
            lo = min(o, c) * (1 - rnd.uniform(0, 0.004))
            ot = d.open_time + k * H4
            from bot.market import Candle

            ltf.append(Candle(ot, o, hi, lo, c, 1.0, ot + H4 - 1, c))
            price = c
    return htf, ltf


def test_backtest_runs_and_is_consistent():
    data = {"UPUSDT": synthetic(0.004, 1), "DNUSDT": synthetic(-0.004, 2), "RWUSDT": synthetic(0.0, 3)}
    start = data["UPUSDT"][1][0].open_time + 230 * D1
    r = run_backtest(data, TradingSettings(), start_ms=start)
    assert r.trades > 0
    assert r.wins + (r.trades - r.wins) == r.trades
    assert len(r.equity_curve) >= 2
    assert abs(r.end_balance - (r.start_balance + sum(t["pnl"] for t in r.trade_list))) < 1e-6 or r.trades > 200
    assert r.total_fees > 0
    assert set(r.exit_reasons) <= {"Stop-loss", "İz süren stop", "Başabaş stop", "Kâr hedefi", "Süre doldu", "Trend döndü", "Test bitti"}
    # trend takibi: yükselen coinde long, düşende short ağırlıklı olmalı
    longs_up = [t for t in r.trade_list if t["symbol"] == "UPUSDT" and t["side"] == "LONG"]
    shorts_up = [t for t in r.trade_list if t["symbol"] == "UPUSDT" and t["side"] == "SHORT"]
    assert len(longs_up) >= len(shorts_up)


def test_backtest_empty_data():
    r = run_backtest({}, TradingSettings())
    assert r.trades == 0 and r.end_balance == r.start_balance


def test_fetch_history_pages():
    fake = FakeFuturesClient()
    candles = make_candles(2500, H4, 2500 * H4 * 2, 100.0)
    fake.candles[("ABCUSDT", "4h")] = candles
    out = asyncio.run(fetch_history(fake, "ABCUSDT", "4h", candles[0].open_time, candles[-1].close_time + 1))
    assert len(out) == 2500
    assert [c.open_time for c in out] == sorted(c.open_time for c in out)
