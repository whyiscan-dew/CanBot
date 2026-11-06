"""Binance USDⓈ-M Vadeli İşlemler REST istemcisi (asenkron).

Yalnızca botun ihtiyaç duyduğu uç noktaları içerir. Koşullu emirler (stop-loss /
take-profit) Aralık 2025'ten beri ``/fapi/v1/algoOrder`` üzerinden gönderilir.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx

from .net import describe_network_error, ssl_context

log = logging.getLogger(__name__)

PROD_URL = "https://fapi.binance.com"
TESTNET_URL_CANDIDATES = ("https://demo-fapi.binance.com", "https://testnet.binancefuture.com")


def error_kind(exc: BaseException) -> str:
    """Hata türü: 'network' (Binance'e ulaşılamadı), 'auth' (anahtar reddedildi) ya da 'other'."""
    if isinstance(exc, BinanceAPIError):
        if exc.status == 0:
            return "network"
        if exc.code in (-2014, -2015, -1022) or exc.status == 401:
            return "auth"
    return "other"


class BinanceAPIError(Exception):
    def __init__(self, status: int, code: int | None, msg: str):
        super().__init__(f"Binance hatası {code}: {msg} (HTTP {status})")
        self.status = status
        self.code = code
        self.msg = msg

    @property
    def endpoint_missing(self) -> bool:
        return self.status == 404 or self.code == -5000

    @property
    def is_network(self) -> bool:
        return self.status == 0


FRIENDLY = {
    -1021: "Bilgisayarınızın saati Binance ile uyumsuz. Saati otomatik ayara alın.",
    -1022: "İmza geçersiz: API Secret yanlış girilmiş olabilir.",
    -2014: "API anahtarı biçimi geçersiz.",
    -2015: (
        "Binance API anahtarını reddetti: anahtar Binance'te silinmiş olabilir (yenisini girin), "
        "anahtarın IP listesinde bu bilgisayarın internet IP'si olmayabilir ya da Vadeli İşlemler izni kapalı olabilir."
    ),
    -2019: "Yetersiz teminat (bakiye).",
    -2021: "Emir anında tetiklenirdi (fiyat stop seviyesini geçmiş).",
    -4164: "Emir tutarı Binance'in en düşük tutarının altında.",
    -4120: "Bu emir tipi için Algo Order uç noktası kullanılmalı.",
    -4131: "Piyasa fiyatı koruma sınırının dışında, emir reddedildi.",
}


def friendly_error(exc: Exception) -> str:
    if isinstance(exc, BinanceAPIError):
        if exc.status == 0:
            return exc.msg
        if exc.status == 451:
            return "Binance bulunduğunuz konumdan erişime izin vermiyor (HTTP 451)."
        if exc.status in (418, 429):
            return "Binance istek limiti aşıldı, kısa süre bekleniyor."
        if exc.code in FRIENDLY:
            return f"{FRIENDLY[exc.code]} (kod {exc.code})"
        return f"Binance: {exc.msg} (kod {exc.code})"
    return str(exc) or exc.__class__.__name__


def fmt_param(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        text = format(value, "f")
    elif isinstance(value, float):
        text = format(Decimal(repr(value)), "f")
    else:
        return str(value)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def sign(secret: str, query: str) -> str:
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


class BinanceFuturesClient:
    def __init__(
        self,
        base_url: str = PROD_URL,
        api_key: str = "",
        api_secret: str = "",
        *,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        recv_window: int = 5000,
        time_path: str = "/fapi/v1/time",
    ):
        self.base_url = base_url.rstrip("/")
        self.time_path = time_path
        self.api_key = api_key
        self.api_secret = api_secret
        self.recv_window = recv_window
        self.used_weight = 0
        self._time_offset_ms = 0
        self._time_synced_at = 0.0
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
            verify=ssl_context(),
            headers={"User-Agent": "CanBot/1.0"},
        )

    @property
    def has_keys(self) -> bool:
        return bool(self.api_key and self.api_secret)

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ altyapı
    def now_ms(self) -> int:
        return int(time.time() * 1000) + self._time_offset_ms

    async def sync_time(self) -> None:
        t0 = time.time()
        data = await self._request("GET", self.time_path)
        t1 = time.time()
        self._time_offset_ms = int(data["serverTime"]) - int((t0 + t1) / 2 * 1000)
        self._time_synced_at = t1

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        signed: bool = False,
        max_retries: int = 3,
    ) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if signed:
            if not self.has_keys:
                raise BinanceAPIError(0, None, "API anahtarı tanımlı değil")
            if time.time() - self._time_synced_at > 3600:
                try:
                    await self.sync_time()
                except BinanceAPIError:
                    pass
        attempt = 0
        resynced = False
        # Emir gönderen istekler ağ hatasında tekrar edilmez (çift emir riskini önlemek için).
        retry_allowed = method == "GET"
        while True:
            attempt += 1
            items = [(k, fmt_param(v)) for k, v in params.items()]
            headers: dict[str, str] = {}
            if signed:
                items.append(("recvWindow", str(self.recv_window)))
                items.append(("timestamp", str(self.now_ms())))
                query = urlencode(items)
                query += "&signature=" + sign(self.api_secret, query)
                headers["X-MBX-APIKEY"] = self.api_key
            else:
                query = urlencode(items)
            url = f"{path}?{query}" if query else path
            try:
                resp = await self._http.request(method, url, headers=headers)
            except httpx.HTTPError as exc:
                if retry_allowed and attempt <= max_retries:
                    await asyncio.sleep(min(2**attempt, 10))
                    continue
                raise BinanceAPIError(0, None, describe_network_error(exc)) from exc

            weight = resp.headers.get("x-mbx-used-weight-1m")
            if weight and weight.isdigit():
                self.used_weight = int(weight)

            if resp.status_code in (418, 429):
                retry_after = resp.headers.get("Retry-After", "")
                wait = int(retry_after) if retry_after.isdigit() else 5
                if resp.status_code == 429 and retry_allowed and attempt <= max_retries and wait <= 60:
                    await asyncio.sleep(wait)
                    continue
                raise BinanceAPIError(resp.status_code, None, "İstek limiti aşıldı")
            if resp.status_code >= 500 and retry_allowed and attempt <= max_retries:
                await asyncio.sleep(min(2**attempt, 10))
                continue

            try:
                data = resp.json()
            except ValueError:
                data = None
            error_code = data.get("code") if isinstance(data, dict) else None
            is_error_code = isinstance(error_code, int) and error_code < 0
            if resp.status_code >= 400 or is_error_code:
                msg = data.get("msg") if isinstance(data, dict) else None
                if error_code == -1021 and not resynced:
                    # Zaman damgası hatası: emir hiç işlenmedi, saati eşitleyip tekrar dene.
                    await self.sync_time()
                    resynced = True
                    continue
                raise BinanceAPIError(resp.status_code, error_code, msg or resp.text[:200] or "bilinmeyen hata")
            return data

    # ------------------------------------------------------------ genel veriler
    async def ping(self) -> Any:
        return await self._request("GET", "/fapi/v1/ping")

    async def exchange_info(self) -> dict[str, Any]:
        return await self._request("GET", "/fapi/v1/exchangeInfo")

    async def ticker_24h(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/fapi/v1/ticker/24hr")

    async def premium_index(self, symbol: str | None = None) -> Any:
        return await self._request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})

    async def klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 500,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[list[Any]]:
        return await self._request(
            "GET",
            "/fapi/v1/klines",
            {"symbol": symbol, "interval": interval, "limit": limit, "startTime": start_time, "endTime": end_time},
        )

    # ------------------------------------------------------------- hesap (imzalı)
    async def _versioned(self, paths: tuple[str, ...], params: dict[str, Any] | None = None) -> Any:
        last: BinanceAPIError | None = None
        for path in paths:
            try:
                return await self._request("GET", path, params, signed=True)
            except BinanceAPIError as exc:
                if not exc.endpoint_missing:
                    raise
                last = exc
        assert last is not None
        raise last

    async def account(self) -> dict[str, Any]:
        return await self._versioned(("/fapi/v3/account", "/fapi/v2/account"))

    async def position_risk(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return await self._versioned(("/fapi/v3/positionRisk", "/fapi/v2/positionRisk"), {"symbol": symbol})

    async def is_hedge_mode(self) -> bool:
        data = await self._request("GET", "/fapi/v1/positionSide/dual", signed=True)
        return bool(data.get("dualSidePosition"))

    async def set_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        return await self._request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}, signed=True)

    async def set_margin_type(self, symbol: str, margin_type: str) -> None:
        try:
            await self._request(
                "POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": margin_type}, signed=True
            )
        except BinanceAPIError as exc:
            if exc.code == -4046:  # "No need to change margin type."
                return
            raise

    async def user_trades(
        self, symbol: str, start_time: int | None = None, limit: int = 200, order_id: int | None = None
    ) -> list[dict[str, Any]]:
        params = {"symbol": symbol, "orderId": order_id, "startTime": None if order_id else start_time, "limit": limit}
        return await self._request("GET", "/fapi/v1/userTrades", params, signed=True)

    async def income(
        self, symbol: str, income_type: str, start_time: int | None = None, limit: int = 1000
    ) -> list[dict[str, Any]]:
        params = {"symbol": symbol, "incomeType": income_type, "startTime": start_time, "limit": limit}
        return await self._request("GET", "/fapi/v1/income", params, signed=True)

    async def api_restrictions(self) -> dict[str, Any]:
        """Yalnızca api.binance.com (spot) adresinde çalışır: anahtarın izinlerini döndürür."""
        return await self._request("GET", "/sapi/v1/account/apiRestrictions", signed=True)

    # ------------------------------------------------------------ emirler (imzalı)
    async def new_order(self, **params: Any) -> dict[str, Any]:
        return await self._request("POST", "/fapi/v1/order", params, signed=True)

    async def cancel_order(self, symbol: str, order_id: int) -> dict[str, Any]:
        return await self._request("DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id}, signed=True)

    async def open_orders(self, symbol: str) -> list[dict[str, Any]]:
        return await self._request("GET", "/fapi/v1/openOrders", {"symbol": symbol}, signed=True)

    async def open_algo_orders(self, symbol: str) -> list[dict[str, Any]]:
        data = await self._request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol}, signed=True)
        if isinstance(data, dict):  # yanıt biçimi farklıysa da çalışsın
            data = data.get("orders") or data.get("rows") or data.get("data") or []
        return data if isinstance(data, list) else []

    async def cancel_all_open_orders(self, symbol: str) -> Any:
        return await self._request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol}, signed=True)

    async def new_algo_order(self, **params: Any) -> dict[str, Any]:
        params = {"algoType": "CONDITIONAL", **params}
        return await self._request("POST", "/fapi/v1/algoOrder", params, signed=True)

    async def cancel_algo_order(self, algo_id: int) -> Any:
        return await self._request("DELETE", "/fapi/v1/algoOrder", {"algoId": algo_id}, signed=True)

    async def cancel_all_algo_orders(self, symbol: str) -> Any:
        return await self._request("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol}, signed=True)
