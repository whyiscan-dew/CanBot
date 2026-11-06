"""Geçmiş testi (backtest): stratejiyi geçmiş Binance verisinde, komisyon ve kaymayla birlikte
çalıştırır. Canlı botla aynı strateji ve risk kodunu kullanır.

Komut satırından:  python -m bot.backtest --days 60 --symbols 15

Uyarılar: Fonlama ücretleri hesaba katılmaz. Coin listesi bugünkü hacme göre seçildiği için
sonuçlar biraz iyimser olabilir (hayatta kalma yanılgısı). Geçmiş sonuçlar geleceği garanti etmez.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from .market import Candle, SymbolInfo, parse_klines
from .risk import TAKER_FEE, check_guards, size_position
from .settings import TradingSettings, timeframe_ms
from .strategy import HtfSeries, LtfSeries, StrategyParams, evaluate, trailing_stop

LTF_WARMUP = 120  # küçük zaman dilimi ısınma mumu
BREAKEVEN_OFFSET = 0.0012


def generic_info(symbol: str) -> SymbolInfo:
    d = Decimal
    return SymbolInfo(
        symbol=symbol,
        base_asset=symbol.removesuffix("USDT"),
        quote_asset="USDT",
        status="TRADING",
        contract_type="PERPETUAL",
        onboard_date=0,
        tick_size=d("0.00000001"),
        step_size=d("0.00000001"),
        min_qty=d("0"),
        max_qty=d("1e15"),
        market_step_size=d("0.00000001"),
        market_min_qty=d("0"),
        market_max_qty=d("1e15"),
        min_notional=d("5"),
    )


@dataclass
class BtTrade:
    symbol: str
    side: str
    entry_time: int
    entry_price: float
    qty: float
    stop: float
    initial_stop: float
    take_profit: float | None
    leverage: int
    score: float
    entry_fee: float
    risk_usdt: float
    extreme: float = 0.0
    breakeven: bool = False
    trailed: bool = False
    exit_time: int = 0
    exit_price: float = 0.0
    pnl: float = 0.0
    fees: float = 0.0
    reason: str = ""

    @property
    def d(self) -> int:
        return 1 if self.side == "LONG" else -1


@dataclass
class BacktestResult:
    days: float
    symbols: list[str]
    start_balance: float
    end_balance: float
    return_pct: float
    max_drawdown_pct: float
    trades: int
    wins: int
    win_rate: float
    profit_factor: float | None
    avg_r: float
    total_fees: float
    long_trades: int
    short_trades: int
    max_losing_streak: int
    halted: bool
    equity_curve: list[list[float]] = field(default_factory=list)
    trade_list: list[dict[str, Any]] = field(default_factory=list)
    per_symbol: dict[str, float] = field(default_factory=dict)
    exit_reasons: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_backtest(
    data: dict[str, tuple[list[Candle], list[Candle]]],
    settings: TradingSettings,
    *,
    start_ms: int | None = None,
    infos: dict[str, SymbolInfo] | None = None,
    start_balance: float = 1000.0,
    fee_rate: float = TAKER_FEE,
    slippage: float = 0.0002,
) -> BacktestResult:
    s = settings
    p = StrategyParams.from_settings(s)
    series: dict[str, tuple[HtfSeries, LtfSeries, dict[int, int]]] = {}
    for sym, (htf_c, ltf_c) in data.items():
        if len(htf_c) < p.ema_trend + 10 or len(ltf_c) < LTF_WARMUP:
            continue
        ltf = LtfSeries(ltf_c, p)
        series[sym] = (HtfSeries(htf_c, p), ltf, {ct: i for i, ct in enumerate(ltf.close_time)})
    timeline = sorted({ct for _, ltf, _ in series.values() for ct in ltf.close_time if start_ms is None or ct >= start_ms})

    balance = start_balance
    open_pos: dict[str, BtTrade] = {}
    last_close: dict[str, float] = {}
    closed: list[BtTrade] = []
    curve: list[list[float]] = []
    peak = start_balance
    max_dd = 0.0
    day_key = None
    day_start_eq = start_balance
    halted = False
    hold_ms = s.max_hold_hours * 3_600_000
    cooldown_ms = s.cooldown_minutes * 60_000
    cooldown_until: dict[str, int] = {}

    def close(pos: BtTrade, t: int, price: float, reason: str) -> None:
        nonlocal balance
        price *= 1 - pos.d * slippage
        gross = (price - pos.entry_price) * pos.qty * pos.d
        margin = pos.entry_price * pos.qty / pos.leverage
        gross = max(gross, -margin)
        exit_fee = price * pos.qty * fee_rate
        balance += gross - exit_fee
        pos.exit_time, pos.exit_price, pos.reason = t, price, reason
        pos.fees = pos.entry_fee + exit_fee
        pos.pnl = gross - pos.fees
        closed.append(pos)
        if pos.pnl < 0 and cooldown_ms:
            cooldown_until[pos.symbol] = t + cooldown_ms

    for t in timeline:
        # 1) Açık pozisyonların çıkışları (bu mumun yüksek/düşük değerleriyle)
        for sym, pos in list(open_pos.items()):
            htf, ltf, idx = series[sym]
            i = idx.get(t)
            if i is None:
                continue
            hi, lo, op, cl = ltf.high[i], ltf.low[i], ltf.open[i], ltf.close[i]
            last_close[sym] = cl
            d = pos.d
            if (d == 1 and lo <= pos.stop) or (d == -1 and hi >= pos.stop):
                # Aynı mumda hem stop hem hedef olabiliyorsa temkinli davranıp stop varsayılır.
                price = min(op, pos.stop) if d == 1 else max(op, pos.stop)
                reason = "İz süren stop" if pos.trailed else ("Başabaş stop" if pos.breakeven else "Stop-loss")
                close(pos, t, price, reason)
                del open_pos[sym]
                continue
            if pos.take_profit is not None and (
                (d == 1 and hi >= pos.take_profit) or (d == -1 and lo <= pos.take_profit)
            ):
                price = max(op, pos.take_profit) if d == 1 else min(op, pos.take_profit)
                close(pos, t, price, "Kâr hedefi")
                del open_pos[sym]
                continue
            if hold_ms > 0 and t - pos.entry_time >= hold_ms:
                close(pos, t, cl, "Süre doldu")
                del open_pos[sym]
                continue
            j = htf.index_at(t)
            ef, es = htf.ema_fast[j], htf.ema_slow[j]
            if ef is not None and es is not None and ((d == 1 and ef < es) or (d == -1 and ef > es)):
                close(pos, t, cl, "Trend döndü")
                del open_pos[sym]
                continue
            if s.breakeven_at_r > 0 and not pos.breakeven:
                r = abs(pos.entry_price - pos.initial_stop)
                favorable = (hi - pos.entry_price) if d == 1 else (pos.entry_price - lo)
                if favorable >= s.breakeven_at_r * r:
                    new_stop = pos.entry_price * (1 + d * BREAKEVEN_OFFSET)
                    if (new_stop - pos.stop) * d > 0 and (cl - new_stop) * d > 0:
                        pos.stop = new_stop
                        pos.breakeven = True
            if s.trailing_atr_mult > 0:
                pos.extreme = max(pos.extreme, hi) if d == 1 else min(pos.extreme, lo)
                atr_v = ltf.atr[i]
                if atr_v:
                    new_stop = trailing_stop(pos.side, pos.extreme, atr_v, s.trailing_atr_mult)
                    if (new_stop - pos.stop) * d > 0 and (cl - new_stop) * d > 0:
                        pos.stop = new_stop
                        pos.trailed = True

        # 2) Bakiye (açık pozisyonlar kapanış fiyatıyla değerlenir)
        for sym in series:
            i = series[sym][2].get(t)
            if i is not None:
                last_close[sym] = series[sym][1].close[i]
        upnl = sum((last_close.get(x.symbol, x.entry_price) - x.entry_price) * x.qty * x.d for x in open_pos.values())
        equity = balance + upnl
        curve.append([t, equity])
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak * 100)
        day = t // 86_400_000
        if day != day_key:
            day_key, day_start_eq = day, equity
        guard = check_guards(
            equity=equity,
            day_start_equity=day_start_eq,
            peak_equity=peak,
            daily_loss_limit_pct=s.daily_loss_limit_pct,
            max_drawdown_pct=s.max_drawdown_pct,
        )
        if guard.halt:
            halted = True
        if halted or not guard.can_open:
            continue

        # 3) Yeni girişler
        slots = s.max_open_positions - len(open_pos)
        if slots <= 0:
            continue
        candidates = []
        for sym, (htf, ltf, idx) in series.items():
            if sym in open_pos or cooldown_until.get(sym, 0) > t:
                continue
            i = idx.get(t)
            if i is None or i < LTF_WARMUP // 2:
                continue
            j = htf.index_at(t)
            if j < p.ema_trend:
                continue
            a = evaluate(sym, htf, j, ltf, i, p)
            if not (a.signal and a.direction and a.stop and a.score >= s.min_score):
                continue
            if (a.direction == "LONG" and not s.allow_long) or (a.direction == "SHORT" and not s.allow_short):
                continue
            candidates.append(a)
        candidates.sort(key=lambda a: a.score, reverse=True)
        used_margin = sum(x.entry_price * x.qty / x.leverage for x in open_pos.values())
        for a in candidates[:slots]:
            d = 1 if a.direction == "LONG" else -1
            entry = a.price * (1 + d * slippage)
            dist = abs(a.price - (a.stop or a.price))
            stop = entry - d * dist
            tp = (entry + d * s.take_profit_r * dist) if s.take_profit_r > 0 else None
            info = (infos or {}).get(a.symbol) or generic_info(a.symbol)
            size = size_position(
                equity=equity,
                available=equity - used_margin,
                entry=entry,
                stop=stop,
                risk_pct=s.risk_per_trade_pct,
                leverage=s.leverage,
                max_margin_pct=s.max_position_margin_pct,
                info=info,
                fee_rate=fee_rate,
            )
            if not size.ok:
                continue
            qty = float(size.qty)
            fee = entry * qty * fee_rate
            balance -= fee
            used_margin += size.margin
            open_pos[a.symbol] = BtTrade(
                symbol=a.symbol,
                side=a.direction or "LONG",
                entry_time=t,
                entry_price=entry,
                qty=qty,
                stop=stop,
                initial_stop=stop,
                take_profit=tp,
                leverage=size.leverage,
                score=a.score,
                entry_fee=fee,
                risk_usdt=size.risk_usdt,
                extreme=entry,
            )

    # Test sonunda açık kalanlar son fiyattan kapatılır.
    end_t = timeline[-1] if timeline else 0
    for sym, pos in list(open_pos.items()):
        close(pos, end_t, last_close.get(sym, pos.entry_price), "Test bitti")
    open_pos.clear()

    wins = [x for x in closed if x.pnl > 0]
    losses = [x for x in closed if x.pnl <= 0]
    gross_loss = -sum(x.pnl for x in losses)
    streak = worst = 0
    for x in sorted(closed, key=lambda x: x.exit_time):
        streak = streak + 1 if x.pnl <= 0 else 0
        worst = max(worst, streak)
    per_symbol: dict[str, float] = {}
    reasons: dict[str, int] = {}
    for x in closed:
        per_symbol[x.symbol] = per_symbol.get(x.symbol, 0.0) + x.pnl
        reasons[x.reason] = reasons.get(x.reason, 0) + 1
    if len(curve) > 300:
        step = len(curve) / 300
        sampled = [curve[int(k * step)] for k in range(300)]
        sampled[-1] = curve[-1]
        curve = sampled
    if curve:
        curve[-1] = [curve[-1][0], balance]
    days = (timeline[-1] - timeline[0]) / 86_400_000 if len(timeline) > 1 else 0.0
    return BacktestResult(
        days=round(days, 1),
        symbols=sorted(series),
        start_balance=start_balance,
        end_balance=balance,
        return_pct=(balance - start_balance) / start_balance * 100,
        max_drawdown_pct=max_dd,
        trades=len(closed),
        wins=len(wins),
        win_rate=(len(wins) / len(closed) * 100) if closed else 0.0,
        profit_factor=(sum(x.pnl for x in wins) / gross_loss) if gross_loss > 0 else None,
        avg_r=(sum(x.pnl / x.risk_usdt for x in closed if x.risk_usdt) / len(closed)) if closed else 0.0,
        total_fees=sum(x.fees for x in closed),
        long_trades=sum(1 for x in closed if x.side == "LONG"),
        short_trades=sum(1 for x in closed if x.side == "SHORT"),
        max_losing_streak=worst,
        halted=halted,
        equity_curve=curve,
        trade_list=[
            {
                "symbol": x.symbol,
                "side": x.side,
                "entry_time": x.entry_time,
                "exit_time": x.exit_time,
                "entry_price": x.entry_price,
                "exit_price": x.exit_price,
                "pnl": x.pnl,
                "reason": x.reason,
            }
            for x in sorted(closed, key=lambda x: x.exit_time, reverse=True)[:200]
        ],
        per_symbol=dict(sorted(per_symbol.items(), key=lambda kv: kv[1], reverse=True)),
        exit_reasons=reasons,
    )


# ------------------------------------------------------------------ veri indirme
async def fetch_history(client: Any, symbol: str, interval: str, start_ms: int, end_ms: int) -> list[Candle]:
    out: dict[int, Candle] = {}
    cursor = start_ms
    limit = 1000
    while cursor < end_ms:
        raw = await client.klines(symbol, interval, limit=limit, start_time=cursor, end_time=end_ms)
        if not raw:
            break
        for c in parse_klines(raw):
            if c.close_time < end_ms:
                out[c.open_time] = c
        if len(raw) < limit:
            break
        cursor = int(raw[-1][0]) + 1
        if getattr(client, "used_weight", 0) > 1500:
            await asyncio.sleep(15)
    return [out[k] for k in sorted(out)]


async def load_backtest_data(
    client: Any,
    symbols: list[str],
    settings: TradingSettings,
    days: int,
    progress: Callable[[int, int, str], None] | None = None,
) -> tuple[dict[str, tuple[list[Candle], list[Candle]]], int]:
    import time

    p = StrategyParams.from_settings(settings)
    ltf_ms = timeframe_ms(settings.entry_timeframe)
    htf_ms = timeframe_ms(settings.trend_timeframe)
    now = int(time.time() * 1000)
    end = now // ltf_ms * ltf_ms
    start = end - days * 86_400_000
    data: dict[str, tuple[list[Candle], list[Candle]]] = {}
    for n, sym in enumerate(symbols, 1):
        if progress:
            progress(n - 1, len(symbols), sym)
        htf = await fetch_history(client, sym, settings.trend_timeframe, start - (p.ema_trend + 60) * htf_ms, end)
        ltf = await fetch_history(client, sym, settings.entry_timeframe, start - LTF_WARMUP * ltf_ms, end)
        data[sym] = (htf, ltf)
    return data, start


def _print_result(r: BacktestResult) -> None:
    pf = f"{r.profit_factor:.2f}" if r.profit_factor is not None else "-"
    print("\n=== GEÇMİŞ TESTİ SONUCU ===")
    print(f"Süre: {r.days} gün, coin: {len(r.symbols)}")
    print(f"Başlangıç: {r.start_balance:.2f} USDT → Bitiş: {r.end_balance:.2f} USDT ({r.return_pct:+.2f}%)")
    print(f"En büyük düşüş: %{r.max_drawdown_pct:.2f}")
    print(f"İşlem: {r.trades} (long {r.long_trades}, short {r.short_trades}), kazanma oranı: %{r.win_rate:.1f}")
    print(f"Kâr faktörü: {pf}, ortalama R: {r.avg_r:+.2f}, toplam komisyon: {r.total_fees:.2f} USDT")
    print(f"Üst üste en fazla zarar: {r.max_losing_streak}")
    print(f"Çıkış nedenleri: {r.exit_reasons}")
    if r.halted:
        print("UYARI: Test sırasında en büyük düşüş limiti aşıldı ve işlemler durdu.")
    print("Not: Geçmiş sonuçlar geleceği garanti etmez. Fonlama ücretleri dahil değildir.")


def main() -> None:
    from .binance_client import PROD_URL, BinanceFuturesClient
    from .engine import select_universe
    from .market import parse_exchange_info
    from .settings import SettingsStore

    parser = argparse.ArgumentParser(description="CanBot geçmiş testi")
    parser.add_argument("--days", type=int, default=60, help="Kaç günlük veri (varsayılan 60)")
    parser.add_argument("--symbols", type=int, default=15, help="Kaç coin (hacme göre ilk N, varsayılan 15)")
    parser.add_argument("--balance", type=float, default=1000.0, help="Başlangıç bakiyesi (USDT)")
    parser.add_argument("--url", default=PROD_URL, help="Veri adresi")
    args = parser.parse_args()

    async def go() -> None:
        client = BinanceFuturesClient(args.url)
        try:
            settings = SettingsStore().settings
            infos = parse_exchange_info(await client.exchange_info())
            tickers = await client.ticker_24h()
            universe = select_universe(tickers, infos, settings, int(__import__("time").time() * 1000))
            universe = universe[: args.symbols]
            print(f"Coinler: {', '.join(universe)}")

            def progress(done: int, total: int, sym: str) -> None:
                print(f"  [{done + 1}/{total}] {sym} indiriliyor…")

            data, start = await load_backtest_data(client, universe, settings, args.days, progress)
            result = run_backtest(data, settings, start_ms=start, infos=infos, start_balance=args.balance)
            _print_result(result)
        finally:
            await client.aclose()

    asyncio.run(go())


if __name__ == "__main__":
    main()
