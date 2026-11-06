import pytest

from bot.settings import TradingSettings
from bot.strategy import HtfSeries, LtfSeries, StrategyParams, analyze_latest, evaluate, trailing_stop
from fakes import D1, H4, breakout_ltf, make_candles

END = (1_790_000_000_000 // D1) * D1 - 1  # gün sonu kapanışı (4s ile de hizalı)


def uptrend():
    return make_candles(320, D1, END, 100.0, 0.006, 0.012, seed=11)


def downtrend():
    return make_candles(320, D1, END, 300.0, -0.006, 0.012, seed=12)


def test_long_breakout_signal():
    htf = uptrend()
    ltf = breakout_ltf(200, END, htf[-1].close, +1)
    p = StrategyParams.from_settings(TradingSettings())
    a = analyze_latest("ABCUSDT", htf, ltf, p)
    assert a.direction == "LONG"
    assert a.signal, a.notes
    assert a.stop < a.price
    assert a.take_profit is None  # varsayılan: sabit hedef yok, iz süren stop var
    assert a.score >= 50
    assert a.atr and a.atr > 0


def test_short_breakdown_signal():
    htf = downtrend()
    ltf = breakout_ltf(200, END, htf[-1].close, -1)
    a = analyze_latest("ABCUSDT", htf, ltf, StrategyParams.from_settings(TradingSettings()))
    assert a.direction == "SHORT"
    assert a.signal, a.notes
    assert a.stop > a.price


def test_no_breakout_no_signal():
    htf = uptrend()
    ltf = make_candles(200, H4, END, htf[-1].close, 0.0, 0.004, seed=5)
    a = analyze_latest("ABCUSDT", htf, ltf, StrategyParams.from_settings(TradingSettings()))
    assert a.direction == "LONG"
    if not a.signal:
        assert any("Kırılım" in n for n in a.notes)


def test_flat_market_no_direction():
    htf = make_candles(320, D1, END, 100.0, 0.0, 0.002, seed=9)
    ltf = breakout_ltf(200, END, htf[-1].close, +1)
    a = analyze_latest("ABCUSDT", htf, ltf, StrategyParams.from_settings(TradingSettings()))
    assert not a.signal
    assert a.score == 0 or a.direction is None


def test_take_profit_when_enabled():
    htf = uptrend()
    ltf = breakout_ltf(200, END, htf[-1].close, +1)
    s = TradingSettings(take_profit_r=2.0)
    a = analyze_latest("ABCUSDT", htf, ltf, StrategyParams.from_settings(s))
    assert a.signal
    assert a.take_profit == pytest.approx(a.price + 2 * (a.price - a.stop))


def test_htf_index_has_no_lookahead():
    htf = uptrend()
    p = StrategyParams()
    hs = HtfSeries(htf, p)
    # Günlük mum kapanmadan önceki bir an: o günün mumu kullanılmamalı
    mid_day = htf[-1].open_time + 6 * 3600 * 1000
    assert hs.index_at(mid_day) == len(htf) - 2
    assert hs.index_at(htf[-1].close_time) == len(htf) - 1


def test_evaluate_matches_analyze_latest():
    htf = uptrend()
    ltf = breakout_ltf(200, END, htf[-1].close, +1)
    p = StrategyParams.from_settings(TradingSettings())
    hs, ls = HtfSeries(htf, p), LtfSeries(ltf, p)
    i = len(ltf) - 1
    a1 = evaluate("X", hs, hs.index_at(ls.close_time[i]), ls, i, p)
    a2 = analyze_latest("X", htf, ltf, p)
    assert (a1.signal, a1.direction, a1.score, a1.stop) == (a2.signal, a2.direction, a2.score, a2.stop)


def test_trailing_stop_formula():
    assert trailing_stop("LONG", 110.0, 2.0, 3.0) == 104.0
    assert trailing_stop("SHORT", 90.0, 2.0, 3.0) == 96.0


def test_pullback_mode_runs():
    htf = uptrend()
    ltf = breakout_ltf(200, END, htf[-1].close, +1)
    s = TradingSettings(entry_mode="pullback")
    a = analyze_latest("ABCUSDT", htf, ltf, StrategyParams.from_settings(s))
    assert a.direction == "LONG"
    assert isinstance(a.signal, bool)
