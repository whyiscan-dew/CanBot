"""Piyasa veri yapıları: mumlar ve sembol kuralları (adım, tick, minimum tutar)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any


@dataclass(frozen=True)
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int
    quote_volume: float = 0.0


def parse_klines(raw: list[list[Any]]) -> list[Candle]:
    return [
        Candle(
            open_time=int(k[0]),
            open=float(k[1]),
            high=float(k[2]),
            low=float(k[3]),
            close=float(k[4]),
            volume=float(k[5]),
            close_time=int(k[6]),
            quote_volume=float(k[7]) if len(k) > 7 else 0.0,
        )
        for k in raw
    ]


def closed_only(candles: list[Candle], now_ms: int) -> list[Candle]:
    """Henüz kapanmamış (oluşmakta olan) son mumu çıkarır."""
    out = list(candles)
    while out and out[-1].close_time >= now_ms:
        out.pop()
    return out


def _dec(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


@dataclass
class SymbolInfo:
    symbol: str
    base_asset: str
    quote_asset: str
    status: str
    contract_type: str
    onboard_date: int
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    market_step_size: Decimal
    market_min_qty: Decimal
    market_max_qty: Decimal
    min_notional: Decimal

    @classmethod
    def from_exchange_info(cls, data: dict[str, Any]) -> "SymbolInfo":
        filters = {f.get("filterType"): f for f in data.get("filters", [])}
        price = filters.get("PRICE_FILTER", {})
        lot = filters.get("LOT_SIZE", {})
        mlot = filters.get("MARKET_LOT_SIZE", lot)
        notional = filters.get("MIN_NOTIONAL", {})
        step = _dec(lot.get("stepSize", "0.001"))
        mstep = _dec(mlot.get("stepSize", lot.get("stepSize", "0.001")))
        return cls(
            symbol=data["symbol"],
            base_asset=data.get("baseAsset", ""),
            quote_asset=data.get("quoteAsset", ""),
            status=data.get("status", ""),
            contract_type=data.get("contractType", ""),
            onboard_date=int(data.get("onboardDate", 0) or 0),
            tick_size=_dec(price.get("tickSize", "0.01")),
            step_size=step,
            min_qty=_dec(lot.get("minQty", "0")),
            max_qty=_dec(lot.get("maxQty", "1e12")),
            market_step_size=mstep if mstep > 0 else step,
            market_min_qty=_dec(mlot.get("minQty", lot.get("minQty", "0"))),
            market_max_qty=_dec(mlot.get("maxQty", lot.get("maxQty", "1e12"))),
            min_notional=_dec(notional.get("notional", notional.get("minNotional", "5"))),
        )

    @property
    def tradable(self) -> bool:
        return self.status == "TRADING" and self.contract_type == "PERPETUAL" and self.quote_asset == "USDT"

    def floor_qty(self, qty: float | Decimal, market: bool = True) -> Decimal:
        step = self.market_step_size if market else self.step_size
        if step <= 0:
            return _dec(qty)
        q = (_dec(qty) / step).to_integral_value(rounding=ROUND_DOWN) * step
        max_qty = self.market_max_qty if market else self.max_qty
        if q > max_qty:
            q = (max_qty / step).to_integral_value(rounding=ROUND_DOWN) * step
        return q.quantize(step) if q > 0 else Decimal(0)

    def round_price(self, price: float | Decimal, mode: str = "nearest") -> Decimal:
        tick = self.tick_size
        if tick <= 0:
            return _dec(price)
        rounding = {"nearest": ROUND_HALF_UP, "down": ROUND_FLOOR, "up": ROUND_CEILING}[mode]
        p = (_dec(price) / tick).to_integral_value(rounding=rounding) * tick
        return p.quantize(tick)

    def min_qty_for_notional(self, price: float) -> Decimal:
        """Minimum tutarı karşılayan en küçük miktar (adıma yuvarlanmış)."""
        if price <= 0:
            return Decimal(0)
        step = self.market_step_size
        raw = self.min_notional / _dec(price)
        q = (raw / step).to_integral_value(rounding=ROUND_CEILING) * step
        return max(q, self.market_min_qty).quantize(step)


def parse_exchange_info(data: dict[str, Any]) -> dict[str, SymbolInfo]:
    out: dict[str, SymbolInfo] = {}
    for item in data.get("symbols", []):
        try:
            info = SymbolInfo.from_exchange_info(item)
        except (KeyError, TypeError):
            continue
        out[info.symbol] = info
    return out
