from decimal import Decimal

import pytest

from bot.market import Candle, SymbolInfo, closed_only, parse_exchange_info, parse_klines
from bot.risk import check_guards, safe_leverage, size_position
from fakes import symbol_info_raw


def info(tick="0.01", step="0.001", min_notional="5") -> SymbolInfo:
    return SymbolInfo.from_exchange_info(symbol_info_raw("ABCUSDT", tick, step, min_notional))


def test_symbol_info_parsing_and_rounding():
    i = info(tick="0.10", step="0.001")
    assert i.tradable
    assert i.floor_qty(1.23456) == Decimal("1.234")
    assert i.floor_qty(0.0004) == Decimal(0)
    assert i.round_price(100.07, "nearest") == Decimal("100.10")
    assert i.round_price(100.07, "down") == Decimal("100.00")
    assert i.round_price(100.01, "up") == Decimal("100.10")
    assert i.min_qty_for_notional(100) == Decimal("0.050")


def test_parse_exchange_info_and_klines():
    data = {"symbols": [symbol_info_raw("BTCUSDT"), {"bad": True}]}
    infos = parse_exchange_info(data)
    assert list(infos) == ["BTCUSDT"]
    raw = [[0, "1", "2", "0.5", "1.5", "10", 59_999, "15"]]
    c = parse_klines(raw)[0]
    assert (c.open, c.high, c.low, c.close, c.close_time) == (1.0, 2.0, 0.5, 1.5, 59_999)


def test_closed_only_drops_forming_candle():
    cs = [Candle(0, 1, 1, 1, 1, 1, 999), Candle(1000, 1, 1, 1, 1, 1, 1999)]
    assert len(closed_only(cs, 1500)) == 1
    assert len(closed_only(cs, 2000)) == 2


def test_size_position_risk_based():
    r = size_position(
        equity=1000, available=1000, entry=100, stop=95, risk_pct=1, leverage=5, max_margin_pct=30, info=info()
    )
    assert r.ok
    # 10 USDT risk / (5 stop + 0.1 komisyon) ≈ 1.96
    assert r.qty == Decimal("1.960")
    assert r.risk_usdt == pytest.approx(10.0, rel=0.01)
    assert r.leverage == 5
    assert r.margin == pytest.approx(1.96 * 100 / 5)


def test_size_position_caps_margin():
    r = size_position(
        equity=1000, available=1000, entry=100, stop=99.9, risk_pct=5, leverage=2, max_margin_pct=10, info=info()
    )
    assert r.ok
    assert r.margin <= 100.0 + 1e-6  # %10 teminat sınırı


def test_size_position_min_notional_fails():
    r = size_position(
        equity=20, available=20, entry=100, stop=90, risk_pct=0.5, leverage=5, max_margin_pct=30, info=info(min_notional="100")
    )
    assert not r.ok
    assert "en düşük" in r.reason or "en küçük" in r.reason


def test_safe_leverage_keeps_stop_inside_liquidation():
    assert safe_leverage(0.02, 20) == 20
    assert safe_leverage(0.05, 20) == 10
    assert safe_leverage(0.2, 5) == 2
    r = size_position(
        equity=1000, available=1000, entry=100, stop=60, risk_pct=1, leverage=5, max_margin_pct=30, info=info()
    )
    assert r.ok and r.leverage == 1


def test_guards():
    ok = check_guards(equity=990, day_start_equity=1000, peak_equity=1000, daily_loss_limit_pct=5, max_drawdown_pct=25)
    assert ok.can_open and not ok.halt
    daily = check_guards(equity=940, day_start_equity=1000, peak_equity=1000, daily_loss_limit_pct=5, max_drawdown_pct=25)
    assert not daily.can_open and not daily.halt
    dd = check_guards(equity=700, day_start_equity=720, peak_equity=1000, daily_loss_limit_pct=5, max_drawdown_pct=25)
    assert dd.halt
