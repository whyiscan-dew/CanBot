import math
import random

from bot.indicators import adx, atr, efficiency_ratio, ema, rsi
from bot.strategy import _rolling_prev_extreme


def test_ema_known_values():
    assert ema([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]


def test_ema_short_input():
    assert ema([1, 2], 3) == [None, None]


def test_rsi_extremes():
    up = [float(i) for i in range(1, 40)]
    assert rsi(up, 14)[-1] == 100.0
    flat = [5.0] * 40
    assert rsi(flat, 14)[-1] == 50.0
    down = [float(40 - i) for i in range(40)]
    assert rsi(down, 14)[-1] < 1.0


def test_atr_constant_range():
    highs = [11.0] * 30
    lows = [9.0] * 30
    closes = [10.0] * 30
    values = atr(highs, lows, closes, 14)
    assert values[12] is None
    assert math.isclose(values[-1], 2.0)


def test_adx_detects_trend_direction():
    rnd = random.Random(3)
    price = 100.0
    highs, lows, closes = [], [], []
    for _ in range(120):
        price *= 1 + 0.01 + rnd.uniform(-0.004, 0.004)
        closes.append(price)
        highs.append(price * 1.004)
        lows.append(price * 0.996)
    a, pdi, mdi = adx(highs, lows, closes, 14)
    assert a[-1] > 40
    assert pdi[-1] > mdi[-1]
    a2, pdi2, mdi2 = adx(lows[::-1], highs[::-1], closes[::-1], 14)
    assert mdi2[-1] > pdi2[-1]


def test_efficiency_ratio():
    straight = [float(i) for i in range(30)]
    assert efficiency_ratio(straight, 10) == 1.0
    zigzag = [10.0 + (i % 2) for i in range(30)]
    assert efficiency_ratio(zigzag, 10) < 0.2
    assert efficiency_ratio(straight, 50) is None


def test_rolling_prev_extreme_matches_brute_force():
    rnd = random.Random(7)
    values = [rnd.uniform(0, 100) for _ in range(200)]
    n = 20
    hi = _rolling_prev_extreme(values, n, True)
    lo = _rolling_prev_extreme(values, n, False)
    for i in range(len(values)):
        if i < n:
            assert hi[i] is None and lo[i] is None
        else:
            assert hi[i] == max(values[i - n : i])
            assert lo[i] == min(values[i - n : i])
