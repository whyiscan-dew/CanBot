"""İşlem katmanı.

- ``PaperBroker``: gerçek fiyatlarla sanal işlem (para riske girmez).
- ``LiveBroker``: Binance Testnet ya da gerçek hesap üzerinde emir gönderir.
  Her pozisyon açıldığında borsa tarafında stop-loss emri konur; stop konamazsa
  pozisyon güvenlik için hemen kapatılır.
"""

from __future__ import annotations

import abc
import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .binance_client import (
    PROD_URL,
    TESTNET_URL_CANDIDATES,
    BinanceAPIError,
    BinanceFuturesClient,
    friendly_error,
)
from .market import SymbolInfo
from .risk import TAKER_FEE
from .storage import Storage, Trade

log = logging.getLogger(__name__)


@dataclass
class AccountSnapshot:
    equity: float
    available: float
    wallet: float
    unrealized: float


@dataclass
class PositionInfo:
    symbol: str
    side: str  # LONG / SHORT
    amount: float  # mutlak miktar
    entry_price: float
    mark_price: float
    unrealized: float
    liquidation_price: float | None = None


@dataclass
class Fill:
    price: float
    qty: Decimal
    fee: float
    realized_pnl: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)


BOT_TAG = "canbot-"  # botun emirlerini tanımak için istemci emir kimliği öneki


class StopMoveError(RuntimeError):
    """Stop taşınamadı; ``meta`` borsadaki güncel stop emrini gösterir (kaydedilmeli)."""

    def __init__(self, message: str, meta: dict[str, Any]):
        super().__init__(message)
        self.meta = meta


@dataclass
class ClosedInfo:
    trade_id: int
    exit_price: float
    fee: float
    realized_pnl: float | None
    reason: str


def classify_exit(trade: Trade, exit_price: float) -> str:
    """Borsa tarafında kapanan pozisyonun nedenini tahmin eder."""
    if trade.take_profit and abs(exit_price - trade.take_profit) <= abs(trade.take_profit) * 0.004:
        return "Kâr hedefi"
    if trade.stop_price and abs(exit_price - trade.stop_price) <= abs(trade.stop_price) * 0.004:
        return stop_reason(trade)
    return "Borsada kapandı"


def stop_reason(trade: Trade) -> str:
    if trade.meta.get("trailed"):
        return "İz süren stop"
    if trade.meta.get("breakeven"):
        return "Başabaş stop"
    return "Stop-loss"


class Broker(abc.ABC):
    mode: str = "paper"

    async def connect(self) -> None:  # pragma: no cover - varsayılan boş
        return None

    async def aclose(self) -> None:  # pragma: no cover - varsayılan boş
        return None

    @abc.abstractmethod
    async def account(self, trades: list[Trade], marks: dict[str, float]) -> AccountSnapshot: ...

    @abc.abstractmethod
    async def positions(self, marks: dict[str, float]) -> dict[tuple[str, str], PositionInfo]: ...

    @abc.abstractmethod
    async def open_position(
        self,
        info: SymbolInfo,
        side: str,
        qty: Decimal,
        leverage: int,
        margin_type: str,
        stop: float,
        take_profit: float | None,
        ref_price: float,
    ) -> Fill: ...

    @abc.abstractmethod
    async def close_position(self, info: SymbolInfo, trade: Trade, ref_price: float) -> Fill: ...

    @abc.abstractmethod
    async def move_stop(self, info: SymbolInfo, trade: Trade, new_stop: float) -> dict[str, Any]: ...

    @abc.abstractmethod
    async def detect_closed(self, trades: list[Trade], marks: dict[str, float]) -> list[ClosedInfo]: ...


# ---------------------------------------------------------------------- sanal
class PaperBroker(Broker):
    """Sanal hesap. Pozisyonlar veritabanındaki açık işlemlerdir; burada yalnızca bakiye tutulur."""

    mode = "paper"
    STATE_KEY = "paper_balance"

    def __init__(self, storage: Storage, start_balance: float, fee_rate: float = TAKER_FEE, slippage: float = 0.0002):
        self.storage = storage
        self.fee_rate = fee_rate
        self.slippage = slippage
        balance = storage.kv_get(self.STATE_KEY)
        if balance is None:
            balance = float(start_balance)
            storage.kv_set(self.STATE_KEY, balance)
        self.balance = float(balance)

    def _save(self) -> None:
        self.storage.kv_set(self.STATE_KEY, self.balance)

    def reset(self, start_balance: float) -> None:
        self.balance = float(start_balance)
        self._save()

    def adjust(self, amount: float) -> None:
        self.balance += amount
        self._save()

    @staticmethod
    def _upnl(trade: Trade, mark: float) -> float:
        return (mark - trade.entry_price) * trade.qty_f * trade.direction

    async def account(self, trades: list[Trade], marks: dict[str, float]) -> AccountSnapshot:
        upnl = sum(self._upnl(t, marks.get(t.symbol, t.entry_price)) for t in trades)
        used = sum(t.entry_price * t.qty_f / max(t.leverage, 1) for t in trades)
        equity = self.balance + upnl
        return AccountSnapshot(equity=equity, available=max(0.0, equity - used), wallet=self.balance, unrealized=upnl)

    async def positions(self, marks: dict[str, float]) -> dict[tuple[str, str], PositionInfo]:
        return {}

    async def open_position(self, info, side, qty, leverage, margin_type, stop, take_profit, ref_price) -> Fill:
        sign = 1 if side == "LONG" else -1
        price = ref_price * (1 + sign * self.slippage)
        notional = float(qty) * price
        fee = notional * self.fee_rate
        if notional / leverage + fee > self.balance:
            raise RuntimeError("Sanal bakiye yetersiz")
        self.balance -= fee
        self._save()
        return Fill(price=price, qty=qty, fee=fee)

    def _close(self, trade: Trade, mark: float) -> tuple[float, float, float]:
        price = mark * (1 - trade.direction * self.slippage)
        gross = (price - trade.entry_price) * trade.qty_f * trade.direction
        margin = trade.entry_price * trade.qty_f / max(trade.leverage, 1)
        gross = max(gross, -margin)  # izole marjinde kayıp teminatla sınırlı (tasfiye)
        fee = price * trade.qty_f * self.fee_rate
        self.balance += gross - fee
        self._save()
        return price, gross, fee

    async def close_position(self, info: SymbolInfo, trade: Trade, ref_price: float) -> Fill:
        price, gross, fee = self._close(trade, ref_price)
        return Fill(price=price, qty=Decimal(trade.qty), fee=fee, realized_pnl=gross)

    async def move_stop(self, info: SymbolInfo, trade: Trade, new_stop: float) -> dict[str, Any]:
        return dict(trade.meta)

    async def detect_closed(self, trades: list[Trade], marks: dict[str, float]) -> list[ClosedInfo]:
        out: list[ClosedInfo] = []
        for t in trades:
            mark = marks.get(t.symbol)
            if mark is None:
                continue
            hit_stop = t.stop_price is not None and (mark <= t.stop_price if t.side == "LONG" else mark >= t.stop_price)
            hit_tp = t.take_profit is not None and (
                mark >= t.take_profit if t.side == "LONG" else mark <= t.take_profit
            )
            if not (hit_stop or hit_tp):
                continue
            price, gross, fee = self._close(t, mark)
            reason = stop_reason(t) if hit_stop else "Kâr hedefi"
            out.append(ClosedInfo(t.id, price, fee, gross, reason))
        return out


# ---------------------------------------------------------------------- canlı
class LiveBroker(Broker):
    def __init__(self, client: BinanceFuturesClient, mode: str):
        self.client = client
        self.mode = mode
        self.hedge = False

    async def connect(self) -> None:
        await self.client.sync_time()
        await self.client.account()  # anahtarları doğrular
        self.hedge = await self.client.is_hedge_mode()

    async def aclose(self) -> None:
        await self.client.aclose()

    async def account(self, trades: list[Trade], marks: dict[str, float]) -> AccountSnapshot:
        acc = await self.client.account()
        wallet = float(acc.get("totalWalletBalance", 0) or 0)
        upnl = float(acc.get("totalUnrealizedProfit", 0) or 0)
        equity = float(acc.get("totalMarginBalance", wallet + upnl) or 0)
        available = float(acc.get("availableBalance", 0) or 0)
        return AccountSnapshot(equity=equity, available=available, wallet=wallet, unrealized=upnl)

    async def positions(self, marks: dict[str, float]) -> dict[tuple[str, str], PositionInfo]:
        rows = await self.client.position_risk()
        out: dict[tuple[str, str], PositionInfo] = {}
        for r in rows:
            amt = float(r.get("positionAmt", 0) or 0)
            if amt == 0:
                continue
            ps = r.get("positionSide", "BOTH")
            side = ps if ps in ("LONG", "SHORT") else ("LONG" if amt > 0 else "SHORT")
            liq = float(r.get("liquidationPrice", 0) or 0)
            out[(r["symbol"], side)] = PositionInfo(
                symbol=r["symbol"],
                side=side,
                amount=abs(amt),
                entry_price=float(r.get("entryPrice", 0) or 0),
                mark_price=float(r.get("markPrice", 0) or 0),
                unrealized=float(r.get("unRealizedProfit", 0) or 0),
                liquidation_price=liq or None,
            )
        return out

    # -- yardımcılar --
    def _close_params(self, symbol: str, side: str) -> dict[str, Any]:
        params: dict[str, Any] = {"symbol": symbol, "side": "SELL" if side == "LONG" else "BUY"}
        if self.hedge:
            params["positionSide"] = side
        return params

    async def _place_conditional(self, info: SymbolInfo, side: str, otype: str, trigger: float) -> tuple[str, int]:
        if otype == "STOP_MARKET":
            rounding = "up" if side == "LONG" else "down"
        else:
            rounding = "down" if side == "LONG" else "up"
        trig = info.round_price(trigger, rounding)
        params = self._close_params(info.symbol, side)
        params.update(type=otype, closePosition=True, workingType="MARK_PRICE")
        tag = f"{BOT_TAG}{'sl' if otype == 'STOP_MARKET' else 'tp'}-{int(time.time() * 1000)}"
        try:
            res = await self.client.new_algo_order(triggerPrice=trig, clientAlgoId=tag, **params)
            return "algo", int(res["algoId"])
        except BinanceAPIError as exc:
            if not exc.endpoint_missing:
                raise
        # Algo uç noktası olmayan eski sistemler için klasik emir.
        res = await self.client.new_order(stopPrice=trig, newClientOrderId=tag, **params)
        return "order", int(res["orderId"])

    async def _cancel(self, symbol: str, kind: str | None, order_id: Any) -> bool:
        """Emri iptal eder. Emir zaten yoksa da başarılı sayılır; diğer hatalarda False döner."""
        if not order_id:
            return True
        try:
            if kind == "order":
                await self.client.cancel_order(symbol, int(order_id))
            else:
                await self.client.cancel_algo_order(int(order_id))
            return True
        except BinanceAPIError as exc:
            gone = exc.code in (-2011, -2013) or "unknown" in (exc.msg or "").lower()
            log.info("%s emir iptali: %s", symbol, friendly_error(exc))
            return gone

    async def _sweep_bot_orders(self, symbol: str) -> None:
        """Bu coinde botun bıraktığı (artık pozisyonu olmayan) stop / kâr al emirlerini temizler."""
        try:
            for o in await self.client.open_algo_orders(symbol):
                if str(o.get("clientAlgoId", "")).startswith(BOT_TAG) and o.get("algoId"):
                    await self._cancel(symbol, "algo", o["algoId"])
        except BinanceAPIError as exc:
            log.debug("%s algo emir listesi alınamadı: %s", symbol, friendly_error(exc))
        try:
            for o in await self.client.open_orders(symbol):
                if str(o.get("clientOrderId", "")).startswith(BOT_TAG) and o.get("orderId"):
                    await self._cancel(symbol, "order", o["orderId"])
        except BinanceAPIError as exc:
            log.debug("%s emir listesi alınamadı: %s", symbol, friendly_error(exc))

    async def _cancel_trade_orders(self, trade: Trade) -> None:
        await self._cancel(trade.symbol, trade.meta.get("sl_kind"), trade.meta.get("sl_id"))
        await self._cancel(trade.symbol, trade.meta.get("tp_kind"), trade.meta.get("tp_id"))

    async def _market_close(self, info: SymbolInfo, side: str, qty: Decimal) -> dict[str, Any]:
        params = self._close_params(info.symbol, side)
        params.update(type="MARKET", quantity=info.floor_qty(qty), newOrderRespType="RESULT")
        if not self.hedge:
            params["reduceOnly"] = True
        return await self.client.new_order(**params)

    async def _funding_since(self, symbol: str, start_time: int) -> float:
        """Pozisyon açıkken ödenen/alınan fonlama ücretlerinin toplamı (+ alınan, - ödenen)."""
        try:
            rows = await self.client.income(symbol, "FUNDING_FEE", start_time=start_time)
        except BinanceAPIError:
            return 0.0
        return sum(float(r.get("income", 0) or 0) for r in rows if r.get("asset", "USDT") == "USDT")

    async def _fills(self, symbol: str, order_id: Any) -> tuple[float, float, float, float]:
        """(miktar, ortalama fiyat, gerçekleşen K/Z, USDT komisyon) - emir dolumlarından."""
        if not order_id:
            return 0.0, 0.0, 0.0, 0.0
        try:
            rows = await self.client.user_trades(symbol, limit=100, order_id=int(order_id))
        except BinanceAPIError:
            return 0.0, 0.0, 0.0, 0.0
        rows = [r for r in rows if str(r.get("orderId")) == str(order_id)]
        qty = sum(float(r["qty"]) for r in rows)
        if qty <= 0:
            return 0.0, 0.0, 0.0, 0.0
        avg = sum(float(r["price"]) * float(r["qty"]) for r in rows) / qty
        pnl = sum(float(r.get("realizedPnl", 0) or 0) for r in rows)
        fee = sum(float(r.get("commission", 0) or 0) for r in rows if r.get("commissionAsset") == "USDT")
        return qty, avg, pnl, fee

    # -- arayüz --
    async def open_position(self, info, side, qty, leverage, margin_type, stop, take_profit, ref_price) -> Fill:
        sym = info.symbol
        await self._sweep_bot_orders(sym)  # önceki işlemlerden kalmış olabilecek stopları temizle
        try:
            await self.client.set_margin_type(sym, margin_type)
        except BinanceAPIError as exc:
            log.warning("%s marjin tipi ayarlanamadı: %s", sym, friendly_error(exc))
        await self.client.set_leverage(sym, leverage)

        params: dict[str, Any] = {
            "symbol": sym,
            "side": "BUY" if side == "LONG" else "SELL",
            "type": "MARKET",
            "quantity": qty,
            "newOrderRespType": "RESULT",
        }
        if self.hedge:
            params["positionSide"] = side
        try:
            order = await self.client.new_order(**params)
        except BinanceAPIError as exc:
            if not (exc.is_network or exc.status >= 500):
                raise
            # Yanıt gelmedi: emir gerçekleşmiş olabilir. Pozisyon varsa sahiplenip stop koyarız.
            await asyncio.sleep(1.5)
            pos = (await self.positions({})).get((sym, side))
            if pos is None:
                raise
            log.warning("%s emir yanıtı alınamadı ama pozisyon açılmış; stop konuyor", sym)
            order = {"orderId": None, "executedQty": str(pos.amount), "avgPrice": str(pos.entry_price)}
        executed = Decimal(str(order.get("executedQty") or "0"))
        avg = float(order.get("avgPrice") or 0)
        if executed <= 0 or avg <= 0:
            await asyncio.sleep(1.0)
            pos = (await self.positions({})).get((sym, side))
            if pos is None:
                raise RuntimeError("Piyasa emri gerçekleşmedi")
            executed = info.floor_qty(pos.amount)
            avg = pos.entry_price
        _, _, _, real_fee = await self._fills(sym, order.get("orderId"))
        fee = real_fee or float(executed) * avg * TAKER_FEE
        meta: dict[str, Any] = {"entry_order_id": order.get("orderId")}

        try:
            kind, oid = await self._place_conditional(info, side, "STOP_MARKET", stop)
            meta.update(sl_kind=kind, sl_id=oid)
        except Exception as exc:  # noqa: BLE001 - stop yoksa pozisyon tutulmaz
            log.error("%s stop-loss konulamadı, pozisyon kapatılıyor: %s", sym, friendly_error(exc))
            try:
                await self._market_close(info, side, executed)
            except Exception as close_exc:  # noqa: BLE001
                raise RuntimeError(
                    f"{sym}: stop-loss konulamadı VE pozisyon kapatılamadı! Binance'ten elle kontrol edin. "
                    f"({friendly_error(close_exc)})"
                ) from close_exc
            raise RuntimeError(f"{sym}: stop-loss konulamadı, pozisyon güvenlik için kapatıldı ({friendly_error(exc)})")

        if take_profit:
            try:
                kind, oid = await self._place_conditional(info, side, "TAKE_PROFIT_MARKET", take_profit)
                meta.update(tp_kind=kind, tp_id=oid)
            except Exception as exc:  # noqa: BLE001 - stop var, kâr al opsiyonel
                log.warning("%s kâr al emri konulamadı: %s", sym, friendly_error(exc))
        return Fill(price=avg, qty=executed, fee=fee, meta=meta)

    async def close_position(self, info: SymbolInfo, trade: Trade, ref_price: float) -> Fill:
        pos = (await self.positions({})).get((trade.symbol, trade.side))
        if pos is None:
            await self._cancel_trade_orders(trade)
            return Fill(price=ref_price, qty=Decimal(trade.qty), fee=0.0, realized_pnl=None)
        order = await self._market_close(info, trade.side, Decimal(str(pos.amount)))
        # Önce pozisyon kapatılır, sonra kalan stop/kâr al emirleri iptal edilir.
        await self._cancel_trade_orders(trade)
        await self._sweep_bot_orders(trade.symbol)
        qty, avg, pnl, fee = await self._fills(trade.symbol, order.get("orderId"))
        realized: float | None = pnl
        if qty <= 0:
            avg = float(order.get("avgPrice") or 0) or ref_price
            realized = (avg - trade.entry_price) * trade.qty_f * trade.direction
            fee = float(trade.qty) * avg * TAKER_FEE
        realized += await self._funding_since(trade.symbol, trade.entry_time)
        return Fill(price=avg, qty=Decimal(trade.qty), fee=fee, realized_pnl=realized)

    async def move_stop(self, info: SymbolInfo, trade: Trade, new_stop: float) -> dict[str, Any]:
        meta = dict(trade.meta)
        if not await self._cancel(trade.symbol, meta.get("sl_kind"), meta.get("sl_id")):
            # Eski stop iptal edilemedi (ör. bağlantı sorunu): eski stop borsada duruyor, dokunma.
            raise StopMoveError(f"{trade.symbol}: eski stop iptal edilemedi, mevcut stop korunuyor", meta)
        try:
            kind, oid = await self._place_conditional(info, trade.side, "STOP_MARKET", new_stop)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s yeni stop konulamadı (%s), eski stop geri konuyor", trade.symbol, friendly_error(exc))
            try:
                kind, oid = await self._place_conditional(info, trade.side, "STOP_MARKET", float(trade.stop_price or 0))
            except Exception:  # noqa: BLE001
                await self._market_close(info, trade.side, Decimal(trade.qty))
                meta.update(sl_kind=None, sl_id=None)
                raise StopMoveError(f"{trade.symbol}: stop yenilenemedi, pozisyon güvenlik için kapatıldı", meta) from exc
            meta.update(sl_kind=kind, sl_id=oid)
            raise StopMoveError(f"{trade.symbol}: stop taşınamadı, eski stop korunuyor", meta) from exc
        meta.update(sl_kind=kind, sl_id=oid)
        return meta

    async def detect_closed(self, trades: list[Trade], marks: dict[str, float]) -> list[ClosedInfo]:
        if not trades:
            return []
        positions = await self.positions(marks)
        now = int(time.time() * 1000)
        out: list[ClosedInfo] = []
        for t in trades:
            if (t.symbol, t.side) in positions or now - t.entry_time < 5000:
                continue
            await self._cancel_trade_orders(t)
            await self._sweep_bot_orders(t.symbol)
            close_side = "SELL" if t.side == "LONG" else "BUY"
            try:
                rows = await self.client.user_trades(t.symbol, start_time=t.entry_time)
            except BinanceAPIError:
                rows = []
            rows = [
                r
                for r in rows
                if r.get("side") == close_side
                and int(r.get("time", 0)) >= t.entry_time
                and (not self.hedge or r.get("positionSide") == t.side)
            ]
            qty = sum(float(r["qty"]) for r in rows)
            if qty > 0:
                exit_price = sum(float(r["price"]) * float(r["qty"]) for r in rows) / qty
                pnl: float | None = sum(float(r.get("realizedPnl", 0) or 0) for r in rows)
                fee = sum(float(r.get("commission", 0) or 0) for r in rows if r.get("commissionAsset") == "USDT")
            else:
                exit_price = marks.get(t.symbol, t.entry_price)
                pnl = (exit_price - t.entry_price) * t.qty_f * t.direction
                fee = t.qty_f * exit_price * TAKER_FEE
            pnl += await self._funding_since(t.symbol, t.entry_time)
            out.append(ClosedInfo(t.id, exit_price, fee, pnl, classify_exit(t, exit_price)))
        return out


ClientFactory = Callable[[str, str, str], BinanceFuturesClient]


def default_client_factory(base_url: str, api_key: str = "", api_secret: str = "") -> BinanceFuturesClient:
    return BinanceFuturesClient(base_url, api_key, api_secret)


async def connect_live_broker(
    mode: str,
    api_key: str,
    api_secret: str,
    testnet_url: str = "auto",
    client_factory: ClientFactory = default_client_factory,
) -> LiveBroker:
    """Testnet için 'auto' seçiliyse bilinen adresler sırayla denenir."""
    if not (api_key and api_secret):
        label = "gerçek hesap" if mode == "live" else "testnet"
        raise RuntimeError(f"{label} için API anahtarı girilmemiş (Ayarlar → API Anahtarları)")
    if mode == "live":
        urls = [PROD_URL]
    else:
        urls = list(TESTNET_URL_CANDIDATES) if testnet_url == "auto" else [testnet_url]
    errors: list[str] = []
    last_exc: Exception | None = None
    for url in urls:
        client = client_factory(url, api_key, api_secret)
        broker = LiveBroker(client, mode)
        try:
            await broker.connect()
            return broker
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{url.replace('https://', '')}: {friendly_error(exc)}")
            last_exc = exc
            await client.aclose()
    raise RuntimeError("Binance'e bağlanılamadı → " + " | ".join(errors)) from last_exc
