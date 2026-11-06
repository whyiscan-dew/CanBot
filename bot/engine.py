"""Bot motoru: piyasayı tarar, en güçlü trendleri sıralar, sinyal gelince işlem açar ve
açık pozisyonları (stop taşıma, süre sınırı, trend dönüşü) yönetir."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from . import __version__
from .backtest import BacktestResult, load_backtest_data, run_backtest
from .binance_client import PROD_URL, BinanceFuturesClient, error_kind, friendly_error
from .brokers import (
    AccountSnapshot,
    Broker,
    ClientFactory,
    LiveBroker,
    PaperBroker,
    StopMoveError,
    connect_live_broker,
    default_client_factory,
)
from .market import Candle, SymbolInfo, closed_only, parse_exchange_info, parse_klines
from .notifier import Notifier
from .risk import check_guards, size_position
from .settings import LIVE_CONFIRM_TEXT, MODE_LABELS, SecretStore, SettingsStore, TradingSettings, timeframe_ms
from .storage import Storage, Trade, now_ms
from .strategy import (
    HTF_HISTORY,
    LTF_HISTORY,
    Analysis,
    HtfSeries,
    LtfSeries,
    StrategyParams,
    analyze_latest,
    tr_num,
    tr_price,
    trailing_stop,
)

log = logging.getLogger(__name__)

MIN_LISTING_AGE_MS = 14 * 86_400_000
ENTRY_FRESH_MS = 3 * 60_000  # sinyal mumu kapandıktan sonra en fazla 3 dk içinde girilir
BREAKEVEN_OFFSET = 0.0012  # başabaş stop, komisyonları karşılamak için girişin biraz ötesine konur


def utc_day_start_ms(ts_ms: int | None = None) -> int:
    ts = (ts_ms or now_ms()) // 1000
    d = datetime.fromtimestamp(ts, tz=timezone.utc)
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def select_universe(
    tickers: list[dict[str, Any]], symbols: dict[str, SymbolInfo], s: TradingSettings, now: int
) -> list[str]:
    """Hacme göre en likit, yeterince eski, işlem gören USDT vadeli kontratları seçer."""
    rows: list[tuple[float, str]] = []
    for t in tickers:
        sym = t.get("symbol", "")
        info = symbols.get(sym)
        if info is None or not info.tradable or sym in s.symbol_blacklist:
            continue
        if s.symbol_whitelist and sym not in s.symbol_whitelist:
            continue
        if info.onboard_date and now - info.onboard_date < MIN_LISTING_AGE_MS:
            continue
        qv = float(t.get("quoteVolume") or 0)
        if not s.symbol_whitelist and qv < s.min_quote_volume_usdt:
            continue
        rows.append((qv, sym))
    rows.sort(reverse=True)
    return [sym for _, sym in rows[: s.universe_size]]


class Engine:
    LOOP_SECONDS = 10
    EQUITY_EVERY = 60
    SYMBOLS_REFRESH = 6 * 3600
    RECONNECT_EVERY = 60

    def __init__(
        self,
        store: SettingsStore,
        secrets: SecretStore,
        storage: Storage,
        notifier: Notifier | None = None,
        client_factory: ClientFactory = default_client_factory,
        data_url: str | None = None,
    ):
        self.store = store
        self.secrets = secrets
        self.storage = storage
        self.notifier = notifier or Notifier(secrets)
        self.client_factory = client_factory
        self.data_url = data_url or os.environ.get("CANBOT_DATA_URL") or PROD_URL
        self.broker: Broker | None = None
        self.data: BinanceFuturesClient | None = None
        self._public: BinanceFuturesClient | None = None
        self.symbols: dict[str, SymbolInfo] = {}
        self._symbols_at = 0.0
        self.marks: dict[str, float] = {}
        self.funding: dict[str, float] = {}
        self.next_funding: dict[str, int] = {}
        self.listeners: list[Callable[[str], None]] = []
        self.scan: list[Analysis] = []
        self.scan_time = 0
        self.universe_count = 0
        self.last_scanned_candle = 0
        self.account: AccountSnapshot | None = None
        self.external: list[dict[str, Any]] = []
        self.status = "starting"
        self.error: str | None = None
        self.error_kind: str | None = None
        self.guard_reason: str | None = None
        self.halt_reason: str | None = None
        self.cooldowns: dict[str, float] = {}
        self.logs: deque[dict[str, Any]] = deque(maxlen=200)
        self.lock = asyncio.Lock()
        self.busy = False
        self.backtest: dict[str, Any] = {"status": "idle"}
        self._backtest_task: asyncio.Task | None = None
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._scan_now = False
        self._last_equity_at = 0.0
        self._last_connect_try = 0.0
        self._last_prune = 0.0
        self._last_problem: tuple[str, float] = ("", 0.0)
        self._curve_cache: tuple[str, float, list[list[float]]] = ("", 0.0, [])
        for ev in reversed(storage.events(50)):
            self.logs.appendleft(ev)

    # ----------------------------------------------------------------- yardımcı
    @property
    def settings(self) -> TradingSettings:
        return self.store.settings

    @property
    def mode(self) -> str:
        return self.settings.mode

    def log(self, message: str, level: str = "info", persist: bool = False) -> None:
        if level in ("warning", "error"):
            # Aynı hata art arda tekrarlanırsa (ör. internet yok) günlüğü doldurmasın: 10 dk'da bir yaz.
            last_msg, last_ts = self._last_problem
            if message == last_msg and time.time() - last_ts < 600:
                return
            self._last_problem = (message, time.time())
        self.logs.appendleft({"ts": now_ms(), "level": level, "message": message})
        getattr(log, level if level in ("info", "warning", "error") else "info")(message)
        if persist or level in ("warning", "error"):
            self.storage.add_event(level, message)

    def notify(self, text: str) -> None:
        """Önemli olaylar: Telegram (ayarlıysa) ve masaüstü bildirimleri."""
        if self.notifier.enabled:
            asyncio.ensure_future(self.notifier.send(text))
        for listener in list(self.listeners):
            try:
                listener(text)
            except Exception:  # noqa: BLE001
                log.debug("bildirim dinleyicisi hatası", exc_info=True)

    def public_client(self) -> BinanceFuturesClient:
        if self._public is None:
            self._public = self.client_factory(self.data_url, "", "")
        return self._public

    # ---------------------------------------------------------------- yaşam döngüsü
    async def start(self) -> None:
        await self._setup_broker()
        self._task = asyncio.create_task(self._run(), name="canbot-engine")

    async def shutdown(self) -> None:
        for task in (self._task, self._backtest_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        await self._close_broker()
        if self._public is not None:
            await self._public.aclose()
            self._public = None

    async def _close_broker(self) -> None:
        broker, self.broker = self.broker, None
        if isinstance(broker, LiveBroker):
            await broker.aclose()
        self.data = None

    async def _setup_broker(self) -> None:
        await self._close_broker()
        self._last_connect_try = time.time()
        mode = self.mode
        self.symbols = {}
        self.marks = {}
        self.account = None
        self.external = []
        try:
            if mode == "paper":
                self.broker = PaperBroker(self.storage, self.settings.paper_start_balance)
                self.data = self.public_client()
            else:
                key, secret = self.secrets.binance_keys(mode)
                broker = await connect_live_broker(
                    mode, key, secret, self.settings.testnet_url, self.client_factory
                )
                self.broker = broker
                self.data = broker.client
                if broker.hedge:
                    self.log("Hesap 'Hedge' modunda; emirler buna göre gönderilecek.")
            self.status = "ready"
            self.error = None
            self.error_kind = None
            self.log(f"Mod: {MODE_LABELS[mode]} — bağlantı hazır", persist=True)
        except Exception as exc:  # noqa: BLE001
            self.broker = None
            self.data = None
            self.status = "error"
            self.error = friendly_error(exc)
            self.error_kind = error_kind(exc.__cause__ or exc)
            self.log(f"Bağlantı kurulamadı: {self.error}", "error")
        self.last_scanned_candle = 0

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.error = friendly_error(exc)
                self.error_kind = error_kind(exc)
                self.log(f"Döngü hatası: {self.error}", "error")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.LOOP_SECONDS)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    def wake(self) -> None:
        self._wake.set()

    # ------------------------------------------------------------------- döngü
    async def tick(self) -> None:
        if self.broker is None:
            if time.time() - self._last_connect_try >= self.RECONNECT_EVERY:
                await self._setup_broker()
            if self.broker is None:
                return
        assert self.data is not None
        now = time.time()
        if not self.symbols or now - self._symbols_at > self.SYMBOLS_REFRESH:
            self.symbols = parse_exchange_info(await self.data.exchange_info())
            self._symbols_at = now
        await self._refresh_marks()
        async with self.lock:
            await self._sync_closed()
            await self._update_account()
            self._apply_guards()
            await self._manage_positions()
        if self._scan_due():
            await self.run_scan()
        self.error = None
        self.error_kind = None
        self.status = "running"
        if self.account and now - self._last_equity_at >= self.EQUITY_EVERY:
            self._last_equity_at = now
            self.storage.add_equity(self.mode, self.account.equity)
        if now - self._last_prune > 86_400:
            self._last_prune = now
            self.storage.prune_equity()
            self.storage.prune_events()

    async def _refresh_marks(self) -> None:
        assert self.data is not None
        rows = await self.data.premium_index()
        if isinstance(rows, dict):
            rows = [rows]
        paper = isinstance(self.broker, PaperBroker)
        for r in rows:
            try:
                sym = r["symbol"]
                self.marks[sym] = float(r["markPrice"])
                rate = float(r.get("lastFundingRate") or 0)
                nxt = int(r.get("nextFundingTime") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            prev_next = self.next_funding.get(sym)
            if paper and prev_next and nxt > prev_next:
                self._paper_funding(sym, self.funding.get(sym, rate), prev_next)
            self.funding[sym] = rate
            if nxt:
                self.next_funding[sym] = nxt

    def _paper_funding(self, symbol: str, rate: float, settle_ms: int) -> None:
        """Sanal modda fonlama ücreti: pozitif oranda long öder, short alır."""
        broker = self.broker
        if not isinstance(broker, PaperBroker) or rate == 0:
            return
        for t in self.storage.open_trades("paper"):
            if t.symbol != symbol or t.entry_time >= settle_ms:
                continue
            amount = -t.direction * rate * t.qty_f * self.marks.get(symbol, t.entry_price)
            broker.adjust(amount)
            meta = dict(t.meta)
            meta["funding"] = float(meta.get("funding", 0.0)) + amount
            self.storage.update_trade(t.id, meta=meta)

    async def _sync_closed(self) -> None:
        assert self.broker is not None
        trades = self.storage.open_trades(self.mode)
        if not trades:
            return
        by_id = {t.id: t for t in trades}
        for c in await self.broker.detect_closed(trades, self.marks):
            trade = by_id.get(c.trade_id)
            if trade:
                self._finalize_close(trade, c.exit_price, c.realized_pnl, c.fee, c.reason)

    def _finalize_close(
        self, trade: Trade, exit_price: float, realized: float | None, exit_fee: float, reason: str
    ) -> Trade:
        gross = realized if realized is not None else (exit_price - trade.entry_price) * trade.qty_f * trade.direction
        if trade.mode == "paper":
            gross += float(trade.meta.get("funding", 0.0))
        fees = (trade.fees or 0.0) + exit_fee
        net = gross - fees
        closed = self.storage.close_trade(trade.id, exit_price=exit_price, pnl=net, fees=fees, reason=reason)
        if net < 0 and self.settings.cooldown_minutes > 0:
            self.cooldowns[trade.symbol] = time.time() + self.settings.cooldown_minutes * 60
        icon = "✅" if net > 0 else "❌"
        msg = f"{icon} {trade.symbol} {trade.side} kapandı ({reason}) — K/Z: {net:+.2f} USDT"
        self.log(msg, persist=True)
        self.notify(f"CanBot [{MODE_LABELS[self.mode]}]\n{msg}")
        return closed

    async def _update_account(self) -> None:
        assert self.broker is not None
        trades = self.storage.open_trades(self.mode)
        self.account = await self.broker.account(trades, self.marks)
        if isinstance(self.broker, LiveBroker):
            managed = {(t.symbol, t.side) for t in trades}
            positions = await self.broker.positions(self.marks)
            self.external = [
                {
                    "symbol": p.symbol,
                    "side": p.side,
                    "amount": p.amount,
                    "entry_price": p.entry_price,
                    "mark_price": p.mark_price,
                    "pnl": p.unrealized,
                }
                for key, p in positions.items()
                if key not in managed
            ]

    def _apply_guards(self) -> None:
        if self.account is None:
            return
        mode = self.mode
        eq = self.account.equity
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        day_state = self.storage.kv_get(f"day_start:{mode}")
        if not day_state or day_state.get("day") != day:
            day_state = {"day": day, "equity": eq}
            self.storage.kv_set(f"day_start:{mode}", day_state)
        peak = float(self.storage.kv_get(f"peak:{mode}", eq) or eq)
        if eq > peak:
            peak = eq
            self.storage.kv_set(f"peak:{mode}", peak)
        s = self.settings
        decision = check_guards(
            equity=eq,
            day_start_equity=float(day_state["equity"]),
            peak_equity=peak,
            daily_loss_limit_pct=s.daily_loss_limit_pct,
            max_drawdown_pct=s.max_drawdown_pct,
        )
        if decision.halt and s.auto_trade:
            self.store.update({"auto_trade": False})
            self.halt_reason = decision.reason
            self.log(decision.reason, "error")
            self.notify(f"⛔ CanBot: {decision.reason}")
        self.guard_reason = None if decision.can_open else decision.reason

    async def _manage_positions(self) -> None:
        assert self.broker is not None
        s = self.settings
        now = now_ms()
        for t in self.storage.open_trades(self.mode):
            mark = self.marks.get(t.symbol)
            info = self.symbols.get(t.symbol)
            if mark is None or info is None:
                continue
            try:
                if s.max_hold_hours > 0 and now - t.entry_time > s.max_hold_hours * 3_600_000:
                    await self._close_trade(t, "Süre doldu")
                    continue
                r = t.risk_per_unit
                if s.breakeven_at_r > 0 and r > 0 and not t.meta.get("breakeven") and t.stop_price is not None:
                    if (mark - t.entry_price) * t.direction >= s.breakeven_at_r * r:
                        new_stop = t.entry_price * (1 + t.direction * BREAKEVEN_OFFSET)
                        if (new_stop - t.stop_price) * t.direction > 0 and (mark - new_stop) * t.direction > 0:
                            try:
                                meta = await self.broker.move_stop(info, t, new_stop)
                            except StopMoveError as exc:
                                self.storage.update_trade(t.id, meta=exc.meta)
                                raise
                            meta["breakeven"] = True
                            self.storage.update_trade(t.id, stop_price=new_stop, meta=meta)
                            self.log(f"🛡️ {t.symbol}: stop başabaş seviyesine taşındı ({new_stop:.6g})", persist=True)
            except Exception as exc:  # noqa: BLE001
                self.log(f"{t.symbol} pozisyon yönetimi hatası: {friendly_error(exc)}", "error")

    async def _close_trade(self, trade: Trade, reason: str) -> Trade:
        assert self.broker is not None
        info = self.symbols.get(trade.symbol)
        if info is None:
            raise RuntimeError(f"{trade.symbol} sembol bilgisi yok")
        mark = self.marks.get(trade.symbol, trade.entry_price)
        fill = await self.broker.close_position(info, trade, mark)
        return self._finalize_close(trade, fill.price, fill.realized_pnl, fill.fee, reason)

    # -------------------------------------------------------------------- tarama
    def _scan_due(self) -> bool:
        if self._scan_now:
            return True
        tf = timeframe_ms(self.settings.entry_timeframe)
        now = now_ms()
        candle_open = now // tf * tf
        if now - candle_open < 8_000:
            return False
        return candle_open > self.last_scanned_candle

    def request_scan(self) -> None:
        self._scan_now = True
        self.wake()

    async def run_scan(self) -> None:
        if self.busy or self.data is None:
            return
        self.busy = True
        self._scan_now = False
        try:
            s = self.settings
            now = now_ms()
            tf = timeframe_ms(s.entry_timeframe)
            candle_open = now // tf * tf
            self.last_scanned_candle = candle_open
            tickers = await self.data.ticker_24h()
            tmap = {t["symbol"]: t for t in tickers}
            universe = select_universe(tickers, self.symbols, s, now)
            self.universe_count = len(universe)
            for t in self.storage.open_trades(self.mode):
                if t.symbol not in universe:
                    universe.append(t.symbol)
            params = StrategyParams.from_settings(s)
            sem = asyncio.Semaphore(6)
            data = self.data
            ltf_map: dict[str, list[Candle]] = {}

            async def work(sym: str) -> Analysis:
                async with sem:
                    htf_raw = await data.klines(sym, s.trend_timeframe, limit=HTF_HISTORY)
                    ltf_raw = await data.klines(sym, s.entry_timeframe, limit=LTF_HISTORY)
                ltf = closed_only(parse_klines(ltf_raw), now)
                ltf_map[sym] = ltf
                a = analyze_latest(sym, closed_only(parse_klines(htf_raw), now), ltf, params)
                tk = tmap.get(sym, {})
                a.change_24h = float(tk.get("priceChangePercent", 0) or 0)
                a.quote_volume = float(tk.get("quoteVolume", 0) or 0)
                a.funding_rate = self.funding.get(sym, 0.0) * 100
                return a

            results = await asyncio.gather(*(work(x) for x in universe), return_exceptions=True)
            analyses = [r for r in results if isinstance(r, Analysis)]
            errors = [r for r in results if isinstance(r, Exception)]
            if errors:
                self.log(f"{len(errors)} coin taranamadı: {friendly_error(errors[0])}", "warning")
            analyses.sort(key=lambda a: (a.signal, a.score), reverse=True)
            self.scan = analyses
            self.scan_time = now
            signals = [a for a in analyses if a.signal]
            self.log(
                f"Tarama bitti: {len(analyses)} coin, {len(signals)} sinyal"
                + (f" ({', '.join(a.symbol for a in signals[:5])})" if signals else "")
            )
            fresh = now - candle_open <= ENTRY_FRESH_MS
            by_symbol = {a.symbol: a for a in analyses}
            async with self.lock:
                await self._trend_exits(by_symbol)
                await self._trail_stops(by_symbol, ltf_map)
                if fresh:
                    await self._entries(analyses)
        finally:
            self.busy = False

    async def _trend_exits(self, by_symbol: dict[str, Analysis]) -> None:
        if self.broker is None:
            return
        for t in self.storage.open_trades(self.mode):
            a = by_symbol.get(t.symbol)
            if a is None:
                continue
            if (t.side == "LONG" and a.trend_exit_long) or (t.side == "SHORT" and a.trend_exit_short):
                try:
                    await self._close_trade(t, "Trend döndü")
                except Exception as exc:  # noqa: BLE001
                    self.log(f"{t.symbol} kapatılamadı: {friendly_error(exc)}", "error")

    async def _trail_stops(self, by_symbol: dict[str, Analysis], ltf_map: dict[str, list[Candle]]) -> None:
        """İz süren stop: her mum kapanışında stop, en iyi fiyattan k × ATR geriye çekilir (yalnızca lehte)."""
        s = self.settings
        if s.trailing_atr_mult <= 0 or self.broker is None:
            return
        for t in self.storage.open_trades(self.mode):
            a = by_symbol.get(t.symbol)
            candles = ltf_map.get(t.symbol)
            info = self.symbols.get(t.symbol)
            if a is None or not candles or info is None or not a.atr or t.stop_price is None:
                continue
            since = [c for c in candles if c.close_time >= t.entry_time]
            prev = float(t.meta.get("extreme", t.entry_price))
            if t.side == "LONG":
                extreme = max([prev] + [c.high for c in since])
            else:
                extreme = min([prev] + [c.low for c in since])
            new_stop = trailing_stop(t.side, extreme, a.atr, s.trailing_atr_mult)
            mark = self.marks.get(t.symbol, a.price)
            meta = dict(t.meta)
            meta["extreme"] = extreme
            improves = (new_stop - t.stop_price) * t.direction >= 0.1 * a.atr
            if improves and (mark - new_stop) * t.direction > 0:
                try:
                    meta = await self.broker.move_stop(info, t, new_stop)
                except StopMoveError as exc:
                    self.storage.update_trade(t.id, meta={**exc.meta, "extreme": extreme})
                    self.log(f"{t.symbol} iz süren stop taşınamadı: {exc}", "error")
                    continue
                except Exception as exc:  # noqa: BLE001
                    self.log(f"{t.symbol} iz süren stop taşınamadı: {friendly_error(exc)}", "error")
                    continue
                meta.update(extreme=extreme, trailed=True)
                self.storage.update_trade(t.id, stop_price=new_stop, meta=meta)
                self.log(f"🔒 {t.symbol}: iz süren stop {t.stop_price:.6g} → {new_stop:.6g}", persist=True)
            else:
                self.storage.update_trade(t.id, meta=meta)

    async def _entries(self, analyses: list[Analysis]) -> None:
        s = self.settings
        if not s.auto_trade or self.guard_reason or self.halt_reason or self.broker is None or self.account is None:
            return
        open_trades = self.storage.open_trades(self.mode)
        slots = s.max_open_positions - len(open_trades)
        if slots <= 0:
            return
        busy = {t.symbol for t in open_trades} | {p["symbol"] for p in self.external}
        candidates = sorted(
            (a for a in analyses if a.signal and a.direction and a.stop and a.score >= s.min_score),
            key=lambda a: a.score,
            reverse=True,
        )
        for a in candidates:
            if slots <= 0:
                break
            if a.symbol in busy or self.cooldowns.get(a.symbol, 0) > time.time():
                continue
            if (a.direction == "LONG" and not s.allow_long) or (a.direction == "SHORT" and not s.allow_short):
                continue
            if a.direction == "LONG" and a.funding_rate > s.max_funding_rate_pct:
                self.log(f"{a.symbol} atlandı: fonlama oranı yüksek (%{a.funding_rate:.3f})")
                continue
            if a.direction == "SHORT" and a.funding_rate < -s.max_funding_rate_pct:
                self.log(f"{a.symbol} atlandı: fonlama oranı düşük (%{a.funding_rate:.3f})")
                continue
            if await self._open_trade(a):
                slots -= 1
                busy.add(a.symbol)

    async def _open_trade(self, a: Analysis) -> bool:
        assert self.broker is not None and self.account is not None
        s = self.settings
        info = self.symbols.get(a.symbol)
        if info is None or a.stop is None or a.direction is None:
            return False
        sign = 1 if a.direction == "LONG" else -1
        entry_ref = self.marks.get(a.symbol, a.price)
        dist = abs(a.price - a.stop)
        stop = entry_ref - sign * dist
        take_profit = (entry_ref + sign * s.take_profit_r * dist) if s.take_profit_r > 0 else None
        size = size_position(
            equity=self.account.equity,
            available=self.account.available,
            entry=entry_ref,
            stop=stop,
            risk_pct=s.risk_per_trade_pct,
            leverage=s.leverage,
            max_margin_pct=s.max_position_margin_pct,
            info=info,
        )
        if not size.ok:
            self.log(f"{a.symbol} atlandı: {size.reason}")
            return False
        try:
            fill = await self.broker.open_position(
                info, a.direction, size.qty, size.leverage, s.margin_type, stop, take_profit, entry_ref
            )
        except Exception as exc:  # noqa: BLE001
            msg = f"{a.symbol} pozisyon açılamadı: {friendly_error(exc)}"
            self.log(msg, "error")
            self.notify(f"⚠️ CanBot: {msg}")
            return False
        self.storage.open_trade(
            mode=self.mode,
            symbol=a.symbol,
            side=a.direction,
            qty=str(fill.qty),
            leverage=size.leverage,
            entry_price=fill.price,
            stop_price=stop,
            take_profit=take_profit,
            fees=fill.fee,
            score=a.score,
            meta={**fill.meta, "extreme": fill.price},
        )
        arrow = "📈" if a.direction == "LONG" else "📉"
        msg = (
            f"{arrow} {a.symbol} {a.direction} açıldı: {fill.qty} adet @ {fill.price:.6g}, {size.leverage}x, "
            f"stop {stop:.6g}, "
            + (f"hedef {take_profit:.6g}, " if take_profit else "iz süren stop, ")
            + f"risk ≈ {size.risk_usdt:.2f} USDT (skor {a.score:.0f})"
        )
        self.log(msg, persist=True)
        self.notify(f"CanBot [{MODE_LABELS[self.mode]}]\n{msg}")
        self.account = await self.broker.account(self.storage.open_trades(self.mode), self.marks)
        return True

    # ----------------------------------------------------------- panel komutları
    async def set_auto_trade(self, on: bool) -> None:
        async with self.lock:
            if on:
                if self.broker is None:
                    raise ValueError(f"Önce bağlantı sorununu çözün: {self.error or 'bağlantı yok'}")
                if self.account is not None:
                    self.storage.kv_set(f"peak:{self.mode}", self.account.equity)
                self.halt_reason = None
            self.store.update({"auto_trade": on})
            self.log("▶️ Otomatik işlem AÇILDI" if on else "⏸️ Otomatik işlem KAPATILDI", persist=True)
        if on:
            self.request_scan()

    async def update_settings(self, raw: dict[str, Any]) -> TradingSettings:
        async with self.lock:
            old = self.settings
            raw = dict(raw)
            confirm = raw.pop("confirm", None)
            new_mode = raw.get("mode", old.mode)
            if new_mode != old.mode:
                if self.storage.open_trades(old.mode):
                    raise ValueError("Mod değiştirmeden önce açık pozisyonları kapatın")
                if new_mode == "live" and confirm != LIVE_CONFIRM_TEXT:
                    raise ValueError(f"Gerçek para moduna geçmek için onay kutusuna '{LIVE_CONFIRM_TEXT}' yazın")
                raw["auto_trade"] = False
            new = self.store.update(raw)
            reconnect = new.mode != old.mode or (new.mode == "testnet" and new.testnet_url != old.testnet_url)
            if reconnect:
                await self._setup_broker()
            if (new.entry_timeframe, new.trend_timeframe) != (old.entry_timeframe, old.trend_timeframe) or reconnect:
                self.scan = []
                self.last_scanned_candle = 0
            if (
                new.mode == "paper"
                and new.paper_start_balance != old.paper_start_balance
                and not self.storage.open_trades("paper")
                and isinstance(self.broker, PaperBroker)
            ):
                self.broker.reset(new.paper_start_balance)
                self.storage.kv_set("peak:paper", new.paper_start_balance)
            self.log("⚙️ Ayarlar kaydedildi", persist=True)
        self.wake()
        return new

    async def reconnect(self) -> None:
        async with self.lock:
            await self._setup_broker()
        self.wake()

    async def close_trade_by_id(self, trade_id: int) -> Trade:
        async with self.lock:
            trade = self.storage.get_trade(trade_id)
            if trade is None or trade.status != "OPEN" or trade.mode != self.mode:
                raise ValueError("Açık pozisyon bulunamadı")
            if self.broker is None:
                raise ValueError("Bağlantı yok")
            return await self._close_trade(trade, "Elle kapatıldı")

    async def close_all(self, reason: str = "Tümü kapatıldı") -> int:
        count = 0
        async with self.lock:
            if self.broker is None:
                raise ValueError("Bağlantı yok")
            for t in self.storage.open_trades(self.mode):
                try:
                    await self._close_trade(t, reason)
                    count += 1
                except Exception as exc:  # noqa: BLE001
                    self.log(f"{t.symbol} kapatılamadı: {friendly_error(exc)}", "error")
        return count

    async def reset_paper(self) -> None:
        async with self.lock:
            if self.mode != "paper" or not isinstance(self.broker, PaperBroker):
                raise ValueError("Sıfırlama yalnızca sanal modda yapılabilir")
            for t in self.storage.open_trades("paper"):
                await self._close_trade(t, "Sanal hesap sıfırlandı")
            self.broker.reset(self.settings.paper_start_balance)
            self.storage.clear_mode("paper")
            self._curve_cache = ("", 0.0, [])
            self.storage.kv_set("peak:paper", self.settings.paper_start_balance)
            self.storage.kv_set("day_start:paper", None)
            self.log(f"Sanal hesap {self.settings.paper_start_balance:g} USDT ile sıfırlandı", persist=True)
        self.wake()

    # ------------------------------------------------------------- geçmiş testi
    def start_backtest(self, days: int, symbols: int) -> None:
        if self._backtest_task and not self._backtest_task.done():
            raise ValueError("Bir test zaten çalışıyor")
        days = max(7, min(int(days), 180))
        symbols = max(1, min(int(symbols), 30))
        self.backtest = {"status": "running", "progress": 0.0, "message": "Veriler indiriliyor…", "days": days}
        self._backtest_task = asyncio.create_task(self._backtest_job(days, symbols))

    async def _backtest_job(self, days: int, count: int) -> None:
        try:
            client = self.public_client()
            s = self.settings
            infos = parse_exchange_info(await client.exchange_info())
            tickers = await client.ticker_24h()
            universe = select_universe(tickers, infos, s, now_ms())[:count]
            if not universe:
                raise RuntimeError("Teste uygun coin bulunamadı (hacim/filtre ayarlarını kontrol edin)")

            def progress(done: int, total: int, sym: str) -> None:
                self.backtest.update(progress=done / total * 0.9, message=f"{sym} verisi indiriliyor ({done}/{total})")

            data, start_ms = await load_backtest_data(client, universe, s, days, progress)
            self.backtest.update(progress=0.92, message="Simülasyon çalışıyor…")
            result: BacktestResult = await asyncio.to_thread(
                run_backtest, data, s, start_ms=start_ms, infos=infos, start_balance=1000.0
            )
            self.backtest = {"status": "done", "progress": 1.0, "message": "Tamamlandı", "result": result.to_dict()}
            self.log(
                f"Geçmiş testi ({days} gün, {len(universe)} coin): getiri %{result.return_pct:+.1f}, "
                f"{result.trades} işlem, kazanma %{result.win_rate:.0f}",
                persist=True,
            )
        except Exception as exc:  # noqa: BLE001
            self.backtest = {"status": "error", "message": friendly_error(exc)}
            self.log(f"Geçmiş testi başarısız: {friendly_error(exc)}", "warning")

    # ------------------------------------------------------------ piyasa analizi
    def market_overview(self) -> dict[str, Any] | None:
        if not self.scan:
            return None
        up = sum(1 for a in self.scan if a.direction == "LONG")
        down = sum(1 for a in self.scan if a.direction == "SHORT")
        total = len(self.scan)
        if up >= max(1, down) * 1.5 and up >= total * 0.3:
            mood, tone = "Yükseliş ağırlıklı", "up"
        elif down >= max(1, up) * 1.5 and down >= total * 0.3:
            mood, tone = "Düşüş ağırlıklı", "down"
        else:
            mood, tone = "Kararsız / yatay", "flat"
        btc = next((a for a in self.scan if a.symbol == "BTCUSDT"), None)
        changes = [a.change_24h for a in self.scan]
        return {
            "up": up,
            "down": down,
            "flat": total - up - down,
            "total": total,
            "mood": mood,
            "tone": tone,
            "signals": sum(1 for a in self.scan if a.signal),
            "avg_change": sum(changes) / len(changes) if changes else 0.0,
            "btc": btc.to_dict() if btc else None,
        }

    async def coin_detail(self, symbol: str) -> dict[str, Any]:
        """Tek coin için grafik verisi ve Türkçe analiz açıklaması."""
        if self.data is None:
            raise ValueError(f"Bağlantı yok: {self.error or 'bekleniyor'}")
        if self.symbols and symbol not in self.symbols:
            raise ValueError("Bu coin Binance vadeli piyasasında bulunamadı")
        s = self.settings
        p = StrategyParams.from_settings(s)
        now = now_ms()
        htf_raw, ltf_raw = await asyncio.gather(
            self.data.klines(symbol, s.trend_timeframe, limit=HTF_HISTORY),
            self.data.klines(symbol, s.entry_timeframe, limit=LTF_HISTORY),
        )
        htf = closed_only(parse_klines(htf_raw), now)
        ltf_all = parse_klines(ltf_raw)
        ltf = closed_only(ltf_all, now)
        if len(ltf) < 30 or len(htf) < 30:
            raise ValueError("Bu coin için yeterli geçmiş veri yok")
        a = analyze_latest(symbol, htf, ltf, p)
        tk = next((x for x in self.scan if x.symbol == symbol), None)
        if tk:
            a.change_24h, a.quote_volume = tk.change_24h, tk.quote_volume
        a.funding_rate = self.funding.get(symbol, 0.0) * 100
        hs = HtfSeries(htf, p)
        ls = LtfSeries(ltf, p)
        j = len(hs.close) - 1
        show = 120
        start = max(0, len(ltf) - show)
        recent = ltf[-p.breakout_bars :]
        levels = {
            "breakout_high": max(c.high for c in recent),
            "breakout_low": min(c.low for c in recent),
        }
        htf_info = {
            "close": hs.close[j],
            "ema_fast": hs.ema_fast[j],
            "ema_slow": hs.ema_slow[j],
            "ema_trend": hs.ema_trend[j],
            "adx": hs.adx[j],
            "pdi": hs.pdi[j],
            "mdi": hs.mdi[j],
        }
        forming = [c for c in ltf_all if c.close_time >= now][-1:]
        candles = [[c.open_time, c.open, c.high, c.low, c.close] for c in ltf[start:] + forming]
        ema_line = ls.ema[start:] + ([None] if forming else [])
        trade = next((t for t in self.storage.open_trades(self.mode) if t.symbol == symbol), None)
        rank = next((n for n, x in enumerate(sorted(self.scan, key=lambda z: -z.score), 1) if x.symbol == symbol), None)
        return {
            "symbol": symbol,
            "timeframe": s.entry_timeframe,
            "trend_timeframe": s.trend_timeframe,
            "candles": candles,
            "ema": ema_line,
            "levels": levels,
            "analysis": a.to_dict(),
            "htf": htf_info,
            "explanation": self.explain(a, htf_info, levels, rank),
            "trade": trade.to_dict() if trade else None,
            "mark": self.marks.get(symbol),
        }

    def explain(
        self, a: Analysis, htf: dict[str, Any], levels: dict[str, float], rank: int | None
    ) -> list[dict[str, str]]:
        """Analizi sade Türkçe cümlelere çevirir. tone: up / down / flat / warn / info"""
        s = self.settings
        p = StrategyParams.from_settings(s)
        tf_names = {"5m": "5 dakikalık", "15m": "15 dakikalık", "30m": "30 dakikalık", "1h": "1 saatlik",
                    "2h": "2 saatlik", "4h": "4 saatlik", "1d": "günlük"}
        big = tf_names.get(s.trend_timeframe, s.trend_timeframe)
        small = tf_names.get(s.entry_timeframe, s.entry_timeframe)
        out: list[dict[str, str]] = []
        close, et = htf.get("close"), htf.get("ema_trend")
        if a.direction == "LONG":
            dist = (close / et - 1) * 100 if close and et else 0
            out.append({"tone": "up", "text": f"{big.capitalize()} grafikte YÜKSELİŞ trendi var: fiyat uzun vadeli ortalamanın (EMA200) %{tr_num(dist, 1)} üzerinde ve kısa ortalama uzun ortalamanın üstünde."})
        elif a.direction == "SHORT":
            dist = (1 - close / et) * 100 if close and et else 0
            out.append({"tone": "down", "text": f"{big.capitalize()} grafikte DÜŞÜŞ trendi var: fiyat uzun vadeli ortalamanın (EMA200) %{tr_num(dist, 1)} altında ve kısa ortalama uzun ortalamanın altında."})
        elif et is None:
            out.append({"tone": "flat", "text": f"Bu coinin {big} grafikte trendi ölçmeye yetecek kadar geçmişi yok (en az 200 mum gerekir). Bot bu coinde işlem açmaz."})
        else:
            out.append({"tone": "flat", "text": f"{big.capitalize()} grafikte belirgin bir trend yok (ortalamalar karışık). Bot bu coinde işlem açmaz; trend netleşince tekrar değerlendirir."})
        adx_v = a.adx
        if adx_v is not None:
            if adx_v < 20:
                label = "zayıf"
            elif adx_v < 25:
                label = "orta"
            elif adx_v < 40:
                label = "güçlü"
            else:
                label = "çok güçlü"
            tone = "warn" if adx_v < p.adx_min else "info"
            out.append({"tone": tone, "text": f"Trend gücü (ADX) {adx_v:.0f}: {label}. Bot en az {p.adx_min:.0f} ister."})
        if a.efficiency is not None:
            out.append({"tone": "info", "text": f"Fiyat hareketinin düzenliliği %{a.efficiency * 100:.0f} (yüksek olması, fiyatın zikzak yapmadan ilerlediğini gösterir)."})
        if a.atr_pct is not None:
            out.append({"tone": "info", "text": f"{small.capitalize()} ortalama oynaklık (ATR) %{tr_num(a.atr_pct)}. Stop, giriş fiyatından yaklaşık %{tr_num(a.atr_pct * s.stop_atr_mult, 1)} uzağa konur."})
        if a.signal:
            side = "AL (long)" if a.direction == "LONG" else "SAT (short)"
            if p.entry_mode == "breakout":
                why = f"Fiyat son {p.breakout_bars} mumun {'en yükseğini' if a.direction == 'LONG' else 'en düşüğünü'} kırdı."
            else:
                why = "Fiyat ortalamaya geri çekilip trend yönünde onay mumu verdi."
            out.append({"tone": "up" if a.direction == "LONG" else "down", "text": f"GİRİŞ SİNYALİ: {side}. {why}"})
        elif a.direction and p.entry_mode == "breakout":
            if a.direction == "LONG":
                gap = (levels["breakout_high"] / a.price - 1) * 100
                lvl = levels["breakout_high"]
                word = "üzerine çıkması"
            else:
                gap = (1 - levels["breakout_low"] / a.price) * 100
                lvl = levels["breakout_low"]
                word = "altına inmesi"
            out.append({"tone": "info", "text": f"Giriş için {small} kapanışın {tr_price(lvl)} {word} bekleniyor (şu an %{tr_num(gap)} uzakta)."})
        elif a.direction:
            out.append({"tone": "info", "text": f"Giriş için {small} grafikte fiyatın ortalamaya (EMA21) geri çekilip trend yönünde onay mumu vermesi bekleniyor."})
        fr = a.funding_rate
        if (a.direction == "LONG" and fr > s.max_funding_rate_pct) or (a.direction == "SHORT" and fr < -s.max_funding_rate_pct):
            out.append({"tone": "warn", "text": f"Fonlama oranı %{tr_num(fr, 3)} — bu yön için yüksek. Pozisyon taşımak masraflı olur; bot bu durumda girişi atlar."})
        else:
            out.append({"tone": "info", "text": f"Fonlama oranı %{tr_num(fr, 3)} (normal)."})
        if rank:
            out.append({"tone": "info", "text": f"Trend skoru {a.score:.0f}/100 — taranan {len(self.scan)} coin arasında {rank}. sırada. Bot en az {s.min_score:.0f} skor ister."})
        return out

    async def key_permission_warnings(self, mode: str, key: str, secret: str) -> list[str]:
        """Gerçek hesapta API anahtarı izinlerini kontrol eder (para çekme izni açıksa uyarır)."""
        if mode != "live":
            return []
        client = self.client_factory("https://api.binance.com", key, secret)
        client.time_path = "/api/v3/time"
        try:
            r = await client.api_restrictions()
        except Exception:  # noqa: BLE001
            return []
        finally:
            await client.aclose()
        warnings = []
        if r.get("enableWithdrawals"):
            warnings.append("Bu API anahtarında PARA ÇEKME izni açık! Güvenliğiniz için Binance'te bu izni hemen kapatın.")
        if r.get("enableFutures") is False:
            warnings.append("Bu anahtarda 'Vadeli İşlemler (Futures)' izni kapalı; bot işlem açamaz.")
        if r.get("ipRestrict") is False:
            warnings.append(
                "Anahtarda IP kısıtlaması yok. Binance bu durumda anahtara yalnızca okuma izni verir; bot işlem "
                "yapamaz. API Yönetimi'nde 'Yalnızca güvenilir IP'lere erişimi kısıtla'yı seçip IP adresinizi ekleyin."
            )
        return warnings

    # ------------------------------------------------------------------ durum
    def status_text(self) -> str:
        s = self.settings
        if self.broker is None:
            return f"Bağlantı yok: {self.error or 'bağlanılıyor…'}"
        if self.error:
            if self.error_kind == "auth":
                return f"Binance API anahtarını reddediyor: {self.error}"
            return f"Binance'e ulaşılamıyor, tekrar deneniyor: {self.error}"
        if self.halt_reason:
            return self.halt_reason
        if not s.auto_trade:
            return "Otomatik işlem kapalı — piyasa izleniyor, yeni işlem açılmaz."
        if self.guard_reason:
            return self.guard_reason
        n = len(self.storage.open_trades(self.mode))
        return f"Çalışıyor — {self.universe_count} coin izleniyor, {n}/{s.max_open_positions} pozisyon açık."

    def _equity_curve(self, mode: str) -> list[list[float]]:
        """Bakiye grafiği dakikada bir değişir; her panel isteğinde veritabanını taramamak için önbellek."""
        cached_mode, cached_at, data = self._curve_cache
        if cached_mode == mode and time.time() - cached_at < 30:
            return data
        data = self.storage.equity_curve(mode, since_ms=now_ms() - 30 * 86_400_000)
        self._curve_cache = (mode, time.time(), data)
        return data

    def snapshot(self) -> dict[str, Any]:
        s = self.settings
        mode = s.mode
        now = now_ms()
        positions = []
        for t in self.storage.open_trades(mode):
            mark = self.marks.get(t.symbol)
            margin = t.entry_price * t.qty_f / max(t.leverage, 1)
            pnl = (mark - t.entry_price) * t.qty_f * t.direction if mark else None
            risk = t.risk_per_unit * t.qty_f
            d = t.to_dict()
            d.update(
                mark_price=mark,
                pnl=pnl,
                pnl_pct=(pnl / margin * 100) if (pnl is not None and margin) else None,
                r_multiple=(pnl / risk) if (pnl is not None and risk) else None,
                margin=margin,
                breakeven=bool(t.meta.get("breakeven")),
            )
            positions.append(d)
        tf = timeframe_ms(s.entry_timeframe)
        return {
            "version": __version__,
            "server_time": now,
            "bot": {
                "mode": mode,
                "mode_label": MODE_LABELS[mode],
                "auto_trade": s.auto_trade,
                "status": self.status,
                "status_text": self.status_text(),
                "error": self.error,
                "error_kind": self.error_kind,
                "guard": self.guard_reason,
                "halt": self.halt_reason,
                "busy": self.busy,
                "connected": self.broker is not None,
                "last_scan": self.scan_time,
                "next_scan": (now // tf + 1) * tf + 8_000,
                "universe": self.universe_count,
            },
            "account": asdict(self.account) if self.account else None,
            "stats": {"all": self.storage.stats(mode), "today": self.storage.stats(mode, utc_day_start_ms())},
            "positions": positions,
            "external_positions": self.external,
            "scanner": [a.to_dict() for a in self.scan[:40]],
            "market": self.market_overview(),
            "trades": [t.to_dict() for t in self.storage.closed_trades(mode, 50)],
            "equity_curve": self._equity_curve(mode),
            "logs": list(self.logs)[:80],
            "backtest": self.backtest,
        }
