"""Testler için sahte Binance istemcisi ve sentetik mum üretici."""

from __future__ import annotations

import random
from typing import Any

from bot.binance_client import BinanceAPIError
from bot.market import Candle

H4 = 4 * 3600 * 1000
D1 = 24 * 3600 * 1000


def make_candles(
    n: int,
    interval_ms: int,
    end_close_time: int,
    start_price: float = 100.0,
    drift: float = 0.0,
    noise: float = 0.01,
    seed: int = 1,
) -> list[Candle]:
    """Kapanış zamanı ``end_close_time`` olan son mumla biten, ``n`` adet sentetik mum."""
    rnd = random.Random(seed)
    first_open = end_close_time + 1 - n * interval_ms
    out: list[Candle] = []
    price = start_price
    for i in range(n):
        o = price
        c = o * (1 + drift + rnd.uniform(-noise, noise))
        hi = max(o, c) * (1 + rnd.uniform(0, noise / 2))
        lo = min(o, c) * (1 - rnd.uniform(0, noise / 2))
        ot = first_open + i * interval_ms
        out.append(Candle(ot, o, hi, lo, c, 1000.0, ot + interval_ms - 1, 1000.0 * c))
        price = c
    return out


def breakout_ltf(n: int, end_close_time: int, base_price: float, direction: int = 1, seed: int = 2) -> list[Candle]:
    """Yatay seyreden ve son mumda yukarı (ya da aşağı) kırılım yapan 4s mumları."""
    candles = make_candles(n - 1, H4, end_close_time - H4, base_price, 0.0, 0.004, seed)
    last = candles[-1]
    highs = [c.high for c in candles[-20:]]
    lows = [c.low for c in candles[-20:]]
    o = last.close
    if direction > 0:
        c = max(highs) * 1.012
        hi, lo = c * 1.002, o * 0.998
    else:
        c = min(lows) * 0.988
        hi, lo = o * 1.002, c * 0.998
    ot = last.open_time + H4
    candles.append(Candle(ot, o, hi, lo, c, 1000.0, ot + H4 - 1, 1000.0 * c))
    return candles


def to_raw(candles: list[Candle]) -> list[list[Any]]:
    return [
        [c.open_time, str(c.open), str(c.high), str(c.low), str(c.close), str(c.volume), c.close_time, str(c.quote_volume)]
        for c in candles
    ]


def symbol_info_raw(symbol: str, tick: str = "0.01", step: str = "0.001", min_notional: str = "5") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "pair": symbol,
        "contractType": "PERPETUAL",
        "status": "TRADING",
        "baseAsset": symbol.replace("USDT", ""),
        "quoteAsset": "USDT",
        "marginAsset": "USDT",
        "onboardDate": 1_500_000_000_000,
        "filters": [
            {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "1000000", "tickSize": tick},
            {"filterType": "LOT_SIZE", "minQty": step, "maxQty": "100000", "stepSize": step},
            {"filterType": "MARKET_LOT_SIZE", "minQty": step, "maxQty": "10000", "stepSize": step},
            {"filterType": "MIN_NOTIONAL", "notional": min_notional},
        ],
    }


class FakeFuturesClient:
    """BinanceFuturesClient yerine geçen bellek içi sahte borsa."""

    def __init__(self, base_url: str = "https://fake", api_key: str = "", api_secret: str = ""):
        self.base_url = base_url
        self.api_key = api_key
        self.api_secret = api_secret
        self.used_weight = 0
        self.time_path = "/fapi/v1/time"
        self.candles: dict[tuple[str, str], list[Candle]] = {}
        self.symbols: list[str] = []
        self.marks: dict[str, float] = {}
        self.funding_rate = 0.0001
        self.next_funding = 1_900_000_000_000
        self.hedge = False
        self.balance = 1000.0
        self.positions: dict[str, dict[str, float]] = {}  # symbol -> {amt, entry}
        self.algo_orders: dict[int, dict[str, Any]] = {}
        self.orders: list[dict[str, Any]] = []
        self.trades: list[dict[str, Any]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_algo = False
        self.fail_connect: BinanceAPIError | None = None
        self.algo_missing = False
        self._next_id = 1000
        self.closed = False

    # -- kurulum yardımcıları --
    def add_symbol(self, symbol: str, htf: list[Candle], ltf: list[Candle], htf_tf: str = "1d", ltf_tf: str = "4h") -> None:
        self.symbols.append(symbol)
        self.candles[(symbol, htf_tf)] = htf
        self.candles[(symbol, ltf_tf)] = ltf
        self.marks[symbol] = ltf[-1].close

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def aclose(self) -> None:
        self.closed = True

    async def sync_time(self) -> None:
        if self.fail_connect:
            raise self.fail_connect

    # -- genel --
    async def exchange_info(self) -> dict[str, Any]:
        return {"symbols": [symbol_info_raw(s) for s in self.symbols]}

    async def ticker_24h(self) -> list[dict[str, Any]]:
        return [
            {"symbol": s, "quoteVolume": str(1e9 - i * 1e6), "priceChangePercent": "1.5", "lastPrice": str(self.marks[s])}
            for i, s in enumerate(self.symbols)
        ]

    async def premium_index(self, symbol: str | None = None) -> Any:
        rows = [
            {"symbol": s, "markPrice": str(self.marks[s]), "lastFundingRate": str(self.funding_rate), "nextFundingTime": self.next_funding}
            for s in self.symbols
        ]
        return rows

    async def klines(self, symbol: str, interval: str, limit: int = 500, start_time: int | None = None, end_time: int | None = None):
        data = self.candles.get((symbol, interval), [])
        if start_time is not None:
            data = [c for c in data if c.open_time >= start_time]
        if end_time is not None:
            data = [c for c in data if c.open_time <= end_time]
        if start_time is not None:
            data = data[:limit]
        else:
            data = data[-limit:]
        return to_raw(data)

    # -- hesap --
    async def account(self) -> dict[str, Any]:
        if self.fail_connect:
            raise self.fail_connect
        upnl = sum((self.marks[s] - p["entry"]) * p["amt"] for s, p in self.positions.items())
        return {
            "totalWalletBalance": str(self.balance),
            "totalUnrealizedProfit": str(upnl),
            "totalMarginBalance": str(self.balance + upnl),
            "availableBalance": str(self.balance + upnl),
        }

    async def position_risk(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return [
            {
                "symbol": s,
                "positionAmt": str(p["amt"]),
                "entryPrice": str(p["entry"]),
                "markPrice": str(self.marks[s]),
                "unRealizedProfit": str((self.marks[s] - p["entry"]) * p["amt"]),
                "liquidationPrice": "0",
                "positionSide": "BOTH",
            }
            for s, p in self.positions.items()
            if p["amt"] != 0
        ]

    async def is_hedge_mode(self) -> bool:
        return self.hedge

    async def set_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        self.calls.append(("leverage", {"symbol": symbol, "leverage": leverage}))
        return {"leverage": leverage}

    async def set_margin_type(self, symbol: str, margin_type: str) -> None:
        self.calls.append(("marginType", {"symbol": symbol, "marginType": margin_type}))

    async def user_trades(self, symbol: str, start_time: int | None = None, limit: int = 200, order_id: int | None = None):
        rows = [t for t in self.trades if t["symbol"] == symbol]
        if order_id is not None:
            rows = [t for t in rows if t["orderId"] == order_id]
        if start_time is not None:
            rows = [t for t in rows if t["time"] >= start_time]
        return rows

    async def income(self, symbol: str, income_type: str, start_time: int | None = None, limit: int = 1000):
        return []

    async def api_restrictions(self) -> dict[str, Any]:
        return {"enableWithdrawals": True, "enableFutures": True, "ipRestrict": False}

    # -- emirler --
    def _fill(self, symbol: str, side: str, qty: float, reduce: bool) -> dict[str, Any]:
        price = self.marks[symbol]
        sign = 1 if side == "BUY" else -1
        pos = self.positions.setdefault(symbol, {"amt": 0.0, "entry": 0.0})
        realized = 0.0
        if reduce or (pos["amt"] != 0 and (pos["amt"] > 0) != (sign > 0)):
            close_qty = min(qty, abs(pos["amt"]))
            realized = (price - pos["entry"]) * close_qty * (1 if pos["amt"] > 0 else -1)
            pos["amt"] += sign * close_qty
            self.balance += realized
        else:
            new_amt = pos["amt"] + sign * qty
            pos["entry"] = price if pos["amt"] == 0 else (pos["entry"] * abs(pos["amt"]) + price * qty) / abs(new_amt)
            pos["amt"] = new_amt
        oid = self._id()
        commission = price * qty * 0.0005
        self.balance -= commission
        import time

        self.trades.append(
            {
                "symbol": symbol,
                "orderId": oid,
                "side": side,
                "price": str(price),
                "qty": str(qty),
                "realizedPnl": str(realized),
                "commission": str(commission),
                "commissionAsset": "USDT",
                "positionSide": "BOTH",
                "time": int(time.time() * 1000),
            }
        )
        return {"orderId": oid, "executedQty": str(qty), "avgPrice": str(price), "status": "FILLED"}

    async def new_order(self, **params: Any) -> dict[str, Any]:
        self.calls.append(("order", params))
        if params.get("type") in ("STOP_MARKET", "TAKE_PROFIT_MARKET"):
            oid = self._id()
            self.algo_orders[oid] = {"kind": "order", **params}
            return {"orderId": oid}
        return self._fill(params["symbol"], params["side"], float(params["quantity"]), bool(params.get("reduceOnly")))

    async def new_algo_order(self, **params: Any) -> dict[str, Any]:
        self.calls.append(("algo", params))
        if self.algo_missing:
            raise BinanceAPIError(404, -5000, "Path /fapi/v1/algoOrder, Method POST is invalid")
        if self.fail_algo:
            raise BinanceAPIError(400, -2021, "Order would immediately trigger.")
        aid = self._id()
        self.algo_orders[aid] = {"kind": "algo", **params}
        return {"algoId": aid, "algoStatus": "NEW"}

    async def cancel_algo_order(self, algo_id: int) -> Any:
        self.calls.append(("cancel_algo", {"algoId": algo_id}))
        if algo_id not in self.algo_orders:
            raise BinanceAPIError(400, -2011, "Unknown order sent.")
        del self.algo_orders[algo_id]
        return {"code": "200"}

    async def open_algo_orders(self, symbol: str) -> list[dict[str, Any]]:
        return [
            {"algoId": k, "clientAlgoId": v.get("clientAlgoId", ""), "symbol": v.get("symbol")}
            for k, v in self.algo_orders.items()
            if v.get("kind") == "algo" and v.get("symbol") == symbol
        ]

    async def open_orders(self, symbol: str) -> list[dict[str, Any]]:
        return [
            {"orderId": k, "clientOrderId": v.get("newClientOrderId", ""), "symbol": v.get("symbol")}
            for k, v in self.algo_orders.items()
            if v.get("kind") == "order" and v.get("symbol") == symbol
        ]

    async def cancel_order(self, symbol: str, order_id: int) -> Any:
        self.calls.append(("cancel_order", {"orderId": order_id}))
        self.algo_orders.pop(order_id, None)
        return {}

    # -- test yardımcıları --
    def trigger_stop(self, symbol: str, price: float) -> None:
        """Borsa tarafında stop tetiklenmiş gibi pozisyonu kapatır."""
        self.marks[symbol] = price
        pos = self.positions[symbol]
        side = "SELL" if pos["amt"] > 0 else "BUY"
        self._fill(symbol, side, abs(pos["amt"]), True)
