"""Strateji: trend takibi.

1. Büyük zaman diliminde trend yönü belirlenir:
   - LONG: fiyat EMA200 üstünde, EMA21 > EMA55 ve EMA55 yükseliyor
   - SHORT: tam tersi
2. Trendin gücü ADX, verimlilik oranı ve DI farkıyla 0-100 arası skorlanır.
3. Küçük zaman diliminde giriş sinyali aranır (iki tip):
   - "breakout" (kırılım): kapanış, son N mumun en yükseğini (short için en düşüğünü) aşar.
   - "pullback" (geri çekilme): fiyat EMA21'e çekilip trend yönünde onay mumu verir.
4. Stop-loss ATR ile belirlenir. Çıkış: sabit kâr hedefi (R katı) ve/veya ATR ile
   iz süren stop, trend dönüşü ya da süre sınırı.

Bu kurallar kâr GARANTİ ETMEZ; amaç, disiplinli ve riski sınırlı işlem yapmaktır.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any

from .indicators import adx, atr, efficiency_ratio, ema, rsi
from .market import Candle

HTF_HISTORY = 320  # canlı modda çekilecek büyük zaman dilimi mum sayısı
LTF_HISTORY = 200  # canlı modda çekilecek küçük zaman dilimi mum sayısı


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def tr_num(x: float, digits: int = 2) -> str:
    """Türkçe ondalık gösterim: 1.5 -> '1,5'."""
    return f"{x:.{digits}f}".replace(".", ",")


def tr_price(x: float) -> str:
    return f"{x:.6g}".replace(".", ",")


@dataclass
class StrategyParams:
    ema_fast: int = 21
    ema_slow: int = 55
    ema_trend: int = 200
    adx_period: int = 14
    adx_min: float = 20.0
    er_period: int = 24
    ltf_ema: int = 21
    rsi_period: int = 14
    rsi_long_min: float = 45.0
    rsi_long_max: float = 70.0
    rsi_short_min: float = 30.0
    rsi_short_max: float = 55.0
    atr_period: int = 14
    pullback_bars: int = 3
    entry_mode: str = "breakout"
    breakout_bars: int = 20
    stop_atr_mult: float = 2.0
    take_profit_r: float = 2.0
    trailing_atr_mult: float = 0.0
    min_stop_pct: float = 0.6
    max_stop_pct: float = 8.0

    @classmethod
    def from_settings(cls, settings: Any) -> "StrategyParams":
        return cls(
            entry_mode=getattr(settings, "entry_mode", "breakout"),
            stop_atr_mult=settings.stop_atr_mult,
            take_profit_r=settings.take_profit_r,
            trailing_atr_mult=getattr(settings, "trailing_atr_mult", 0.0),
        )


@dataclass
class Analysis:
    symbol: str
    time: int
    price: float
    direction: str | None = None
    score: float = 0.0
    signal: bool = False
    stop: float | None = None
    take_profit: float | None = None
    adx: float | None = None
    rsi: float | None = None
    atr: float | None = None
    atr_pct: float | None = None
    efficiency: float | None = None
    trend_exit_long: bool = False
    trend_exit_short: bool = False
    notes: list[str] = field(default_factory=list)
    change_24h: float = 0.0
    quote_volume: float = 0.0
    funding_rate: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for key in ("price", "stop", "take_profit", "atr"):
            if d[key] is not None:
                d[key] = float(f"{d[key]:.8g}")
        for key in ("adx", "rsi", "atr_pct", "efficiency"):
            if d[key] is not None:
                d[key] = round(d[key], 3)
        return d


class HtfSeries:
    """Büyük zaman dilimi göstergeleri (bir kere hesaplanır, indeksle okunur)."""

    def __init__(self, candles: list[Candle], p: StrategyParams):
        self.close = [c.close for c in candles]
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        self.close_time = [c.close_time for c in candles]
        self.ema_fast = ema(self.close, p.ema_fast)
        self.ema_slow = ema(self.close, p.ema_slow)
        self.ema_trend = ema(self.close, p.ema_trend)
        self.adx, self.pdi, self.mdi = adx(highs, lows, self.close, p.adx_period)
        self.atr = atr(highs, lows, self.close, p.atr_period)

    def index_at(self, t_ms: int) -> int:
        """``t_ms`` anında kapanmış olan son mumun indeksi (yoksa -1)."""
        return bisect_right(self.close_time, t_ms) - 1

    def trend(self, j: int) -> str | None:
        if j < 3:
            return None
        ef, es, et, es_prev = self.ema_fast[j], self.ema_slow[j], self.ema_trend[j], self.ema_slow[j - 3]
        if ef is None or es is None or et is None or es_prev is None:
            return None
        c = self.close[j]
        if c > et and ef > es and es > es_prev:
            return "LONG"
        if c < et and ef < es and es < es_prev:
            return "SHORT"
        return None


def _rolling_prev_extreme(values: list[float], n: int, highest: bool) -> list[float | None]:
    """out[i] = önceki n değerin (i hariç) en yükseği / en düşüğü."""
    out: list[float | None] = [None] * len(values)
    window: deque[int] = deque()
    for i in range(len(values)):
        if i >= n:
            out[i] = values[window[0]]
        while window and window[0] <= i - n:
            window.popleft()
        while window and (values[window[-1]] <= values[i] if highest else values[window[-1]] >= values[i]):
            window.pop()
        window.append(i)
    return out


class LtfSeries:
    """Küçük zaman dilimi göstergeleri."""

    def __init__(self, candles: list[Candle], p: StrategyParams):
        self.open = [c.open for c in candles]
        self.high = [c.high for c in candles]
        self.low = [c.low for c in candles]
        self.close = [c.close for c in candles]
        self.close_time = [c.close_time for c in candles]
        self.ema = ema(self.close, p.ltf_ema)
        self.rsi = rsi(self.close, p.rsi_period)
        self.atr = atr(self.high, self.low, self.close, p.atr_period)
        self.prev_high = _rolling_prev_extreme(self.high, p.breakout_bars, True)
        self.prev_low = _rolling_prev_extreme(self.low, p.breakout_bars, False)


def evaluate(symbol: str, htf: HtfSeries, j: int, ltf: LtfSeries, i: int, p: StrategyParams) -> Analysis:
    """``i`` indeksli küçük mumun kapanışında, ``j`` indeksli büyük muma göre analiz."""
    price = ltf.close[i]
    a = Analysis(symbol=symbol, time=ltf.close_time[i], price=price)
    if j < 0 or i < p.pullback_bars:
        a.notes.append("Yetersiz veri")
        return a

    adx_v = htf.adx[j]
    a.adx = adx_v
    er = efficiency_ratio(htf.close, p.er_period, j)
    a.efficiency = er
    atr_ltf = ltf.atr[i]
    rsi_v = ltf.rsi[i]
    ema_v = ltf.ema[i]
    a.rsi = rsi_v
    a.atr = atr_ltf
    a.atr_pct = (atr_ltf / price * 100) if (atr_ltf and price) else None

    ef, es = htf.ema_fast[j], htf.ema_slow[j]
    if ef is not None and es is not None:
        a.trend_exit_long = ef < es
        a.trend_exit_short = ef > es

    direction = htf.trend(j)
    if direction is None:
        a.notes.append("Belirgin trend yok")
        return a
    a.direction = direction

    # --- Trend gücü skoru (0-100) ---
    score = 0.0
    if adx_v is not None:
        score += _clamp((adx_v - 15.0) / 25.0) * 40.0
    if er is not None:
        score += _clamp(er / 0.5) * 30.0
    pdi, mdi = htf.pdi[j], htf.mdi[j]
    if pdi is not None and mdi is not None:
        dominance = (pdi - mdi) if direction == "LONG" else (mdi - pdi)
        score += _clamp(dominance / 25.0) * 15.0
    atr_h = htf.atr[j]
    if atr_h and ef is not None and es is not None:
        score += _clamp(abs(ef - es) / atr_h / 2.0) * 15.0
    a.score = round(score, 1)

    # --- Giriş sinyali ---
    if adx_v is None or adx_v < p.adx_min:
        a.notes.append(f"Trend zayıf (ADX {adx_v or 0:.0f} < {p.adx_min:.0f})")
        return a
    if ema_v is None or rsi_v is None or not atr_ltf:
        a.notes.append("Yetersiz veri")
        return a

    c, o, prev_c = ltf.close[i], ltf.open[i], ltf.close[i - 1]
    if p.entry_mode == "breakout":
        level = ltf.prev_high[i] if direction == "LONG" else ltf.prev_low[i]
        if level is None:
            a.notes.append("Yetersiz veri")
            return a
        broke = c > level if direction == "LONG" else c < level
        if not broke:
            gap = abs(level - c) / c * 100
            a.notes.append(f"Kırılım bekleniyor (seviye {tr_price(level)}, %{tr_num(gap)} uzakta)")
            return a
    else:
        window = range(i - p.pullback_bars + 1, i + 1)
        if direction == "LONG":
            touched = any(ltf.ema[m] is not None and ltf.low[m] <= ltf.ema[m] for m in window)
            side_ok = c > ema_v and c > o and c > prev_c
            rsi_ok = p.rsi_long_min <= rsi_v <= p.rsi_long_max
        else:
            touched = any(ltf.ema[m] is not None and ltf.high[m] >= ltf.ema[m] for m in window)
            side_ok = c < ema_v and c < o and c < prev_c
            rsi_ok = p.rsi_short_min <= rsi_v <= p.rsi_short_max
        if not touched:
            a.notes.append("Geri çekilme bekleniyor")
            return a
        if not side_ok:
            a.notes.append("Onay mumu bekleniyor")
            return a
        if not rsi_ok:
            a.notes.append(f"RSI uygun değil ({rsi_v:.0f})")
            return a

    dist = p.stop_atr_mult * atr_ltf
    dist = max(dist, c * p.min_stop_pct / 100.0)
    if dist > c * p.max_stop_pct / 100.0:
        a.notes.append("Volatilite çok yüksek")
        return a

    sign = 1 if direction == "LONG" else -1
    a.signal = True
    a.stop = c - sign * dist
    a.take_profit = (c + sign * p.take_profit_r * dist) if p.take_profit_r > 0 else None
    a.notes.append("GİRİŞ SİNYALİ")
    return a


def trailing_stop(side: str, extreme: float, atr_value: float, mult: float) -> float:
    """İz süren stop: pozisyon açıldığından beri görülen en iyi fiyattan ``mult`` × ATR geride."""
    return extreme - mult * atr_value if side == "LONG" else extreme + mult * atr_value


def analyze_latest(symbol: str, htf_candles: list[Candle], ltf_candles: list[Candle], p: StrategyParams) -> Analysis:
    """Canlı mod: yalnızca kapanmış mumlarla, son mum üzerinde analiz."""
    if not ltf_candles or not htf_candles:
        a = Analysis(symbol=symbol, time=0, price=0.0)
        a.notes.append("Veri yok")
        return a
    htf = HtfSeries(htf_candles, p)
    ltf = LtfSeries(ltf_candles, p)
    i = len(ltf_candles) - 1
    j = htf.index_at(ltf.close_time[i])
    return evaluate(symbol, htf, j, ltf, i, p)
