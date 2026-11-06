"""Risk yönetimi: pozisyon büyüklüğü ve koruma kuralları."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .market import SymbolInfo

TAKER_FEE = 0.0005  # Binance USDⓈ-M taker komisyonu (%0,05)


@dataclass
class SizeResult:
    ok: bool
    qty: Decimal = Decimal(0)
    notional: float = 0.0
    margin: float = 0.0
    risk_usdt: float = 0.0
    leverage: int = 1
    reason: str = ""


def safe_leverage(stop_pct: float, wanted: int) -> int:
    """Stop mesafesi, tasfiye mesafesinin en fazla yarısı olacak şekilde kaldıracı düşürür.

    İzole marjinde tasfiye yaklaşık %100/kaldıraç hareketle olur. Stop bunun yarısından
    uzaksa kaldıraç azaltılır; böylece stop her zaman tasfiyeden önce çalışır.
    """
    lev = max(1, int(wanted))
    while lev > 1 and stop_pct > 0.5 / lev:
        lev -= 1
    return lev


def size_position(
    *,
    equity: float,
    available: float,
    entry: float,
    stop: float,
    risk_pct: float,
    leverage: int,
    max_margin_pct: float,
    info: SymbolInfo,
    fee_rate: float = TAKER_FEE,
) -> SizeResult:
    if equity <= 0 or entry <= 0:
        return SizeResult(False, reason="Bakiye yok")
    dist = abs(entry - stop)
    if dist <= 0:
        return SizeResult(False, reason="Geçersiz stop")
    stop_pct = dist / entry
    lev = safe_leverage(stop_pct, leverage)
    if stop_pct > 0.5 / lev:
        return SizeResult(False, reason="Stop çok uzak")

    risk_usdt = equity * risk_pct / 100.0
    # Giriş + çıkış komisyonu da risk bütçesine dahil edilir.
    qty = risk_usdt / (dist + 2 * fee_rate * entry)
    max_margin = min(equity * max_margin_pct / 100.0, available * 0.95)
    if max_margin <= 0:
        return SizeResult(False, reason="Kullanılabilir bakiye yok")
    qty = min(qty, max_margin * lev / entry)
    q = info.floor_qty(qty)
    if q <= 0 or q < info.market_min_qty:
        return SizeResult(False, reason="Miktar Binance'in en küçük miktarının altında")
    notional = float(q) * entry
    if Decimal(str(notional)) < info.min_notional:
        return SizeResult(
            False,
            reason=f"Tutar ({notional:.2f}) en düşük emir tutarının ({info.min_notional} USDT) altında",
        )
    margin = notional / lev
    return SizeResult(
        True,
        qty=q,
        notional=notional,
        margin=margin,
        risk_usdt=float(q) * (dist + 2 * fee_rate * entry),
        leverage=lev,
    )


@dataclass
class GuardDecision:
    can_open: bool
    halt: bool = False
    reason: str = ""


def check_guards(
    *,
    equity: float,
    day_start_equity: float,
    peak_equity: float,
    daily_loss_limit_pct: float,
    max_drawdown_pct: float,
) -> GuardDecision:
    if peak_equity > 0 and equity <= peak_equity * (1 - max_drawdown_pct / 100.0):
        return GuardDecision(
            False,
            halt=True,
            reason=f"En büyük düşüş limiti aşıldı (zirveden %{max_drawdown_pct:g}). Bot durduruldu.",
        )
    if day_start_equity > 0 and equity <= day_start_equity * (1 - daily_loss_limit_pct / 100.0):
        return GuardDecision(
            False,
            reason=f"Günlük zarar limiti (%{daily_loss_limit_pct:g}) doldu. Yarın (UTC) yeniden işlem açılır.",
        )
    return GuardDecision(True)
