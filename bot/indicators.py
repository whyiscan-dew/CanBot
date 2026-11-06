"""Teknik göstergeler (saf Python). Hesaplanamayan başlangıç değerleri ``None`` olur."""

from __future__ import annotations

from collections.abc import Sequence

Series = list[float | None]


def ema(values: Sequence[float], period: int) -> Series:
    out: Series = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    k = 2.0 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def _wilder(values: Sequence[float], period: int, start: int) -> Series:
    """Wilder ortalaması. ``start`` indeksinden itibaren ``period`` değerin ortalamasıyla başlar."""
    out: Series = [None] * len(values)
    first = start + period - 1
    if first >= len(values):
        return out
    prev = sum(values[start : first + 1]) / period
    out[first] = prev
    for i in range(first + 1, len(values)):
        prev = (prev * (period - 1) + values[i]) / period
        out[i] = prev
    return out


def rsi(closes: Sequence[float], period: int = 14) -> Series:
    n = len(closes)
    out: Series = [None] * n
    if n <= period:
        return out
    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        diff = closes[i] - closes[i - 1]
        gains[i] = max(diff, 0.0)
        losses[i] = max(-diff, 0.0)
    avg_gain = _wilder(gains, period, 1)
    avg_loss = _wilder(losses, period, 1)
    for i in range(n):
        g, loss = avg_gain[i], avg_loss[i]
        if g is None or loss is None:
            continue
        if loss == 0:
            out[i] = 100.0 if g > 0 else 50.0
        else:
            out[i] = 100.0 - 100.0 / (1.0 + g / loss)
    return out


def true_range(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> list[float]:
    tr = []
    for i in range(len(closes)):
        if i == 0:
            tr.append(highs[0] - lows[0])
        else:
            prev = closes[i - 1]
            tr.append(max(highs[i] - lows[i], abs(highs[i] - prev), abs(lows[i] - prev)))
    return tr


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> Series:
    return _wilder(true_range(highs, lows, closes), period, 0)


def adx(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14
) -> tuple[Series, Series, Series]:
    """(ADX, +DI, -DI) döndürür."""
    n = len(closes)
    adx_out: Series = [None] * n
    plus_di: Series = [None] * n
    minus_di: Series = [None] * n
    if n <= 2 * period:
        return adx_out, plus_di, minus_di
    tr = true_range(highs, lows, closes)
    pdm = [0.0] * n
    mdm = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        pdm[i] = up if (up > down and up > 0) else 0.0
        mdm[i] = down if (down > up and down > 0) else 0.0
    # Wilder toplamları: ilk değer 1..period toplamı
    s_tr = sum(tr[1 : period + 1])
    s_p = sum(pdm[1 : period + 1])
    s_m = sum(mdm[1 : period + 1])
    dx: Series = [None] * n
    for i in range(period, n):
        if i > period:
            s_tr = s_tr - s_tr / period + tr[i]
            s_p = s_p - s_p / period + pdm[i]
            s_m = s_m - s_m / period + mdm[i]
        if s_tr <= 0:
            pdi = mdi = 0.0
        else:
            pdi = 100.0 * s_p / s_tr
            mdi = 100.0 * s_m / s_tr
        plus_di[i] = pdi
        minus_di[i] = mdi
        total = pdi + mdi
        dx[i] = 0.0 if total == 0 else 100.0 * abs(pdi - mdi) / total
    first = 2 * period - 1
    prev = sum(v for v in dx[period : first + 1] if v is not None) / period
    adx_out[first] = prev
    for i in range(first + 1, n):
        prev = (prev * (period - 1) + (dx[i] or 0.0)) / period
        adx_out[i] = prev
    return adx_out, plus_di, minus_di


def efficiency_ratio(closes: Sequence[float], period: int, end: int | None = None) -> float | None:
    """Kaufman verimlilik oranı (0..1): fiyatın ne kadar 'düz bir çizgide' gittiğini ölçer."""
    if end is None:
        end = len(closes) - 1
    start = end - period
    if start < 0:
        return None
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(start + 1, end + 1))
    if path == 0:
        return 0.0
    return abs(closes[end] - closes[start]) / path
