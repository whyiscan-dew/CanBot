import asyncio
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from bot.binance_client import BinanceAPIError, BinanceFuturesClient, fmt_param, friendly_error, sign


def run(coro):
    return asyncio.run(coro)


def test_signature_matches_binance_docs_example():
    secret = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
    query = "symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1&recvWindow=5000&timestamp=1499827319559"
    assert sign(secret, query) == "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"


def test_fmt_param():
    assert fmt_param(True) == "true"
    assert fmt_param(False) == "false"
    assert fmt_param(Decimal("0.0010")) == "0.001"
    assert fmt_param(Decimal("100")) == "100"
    assert fmt_param(1e-7) == "0.0000001"
    assert fmt_param(2.50) == "2.5"
    assert fmt_param(7) == "7"


def client_with(handler):
    return BinanceFuturesClient("https://example.test", "KEY", "SECRET", transport=httpx.MockTransport(handler))


def test_signed_request_has_signature_and_key():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": 1_700_000_000_000})
        seen["headers"] = dict(request.headers)
        seen["query"] = parse_qs(urlsplit(str(request.url)).query)
        raw = urlsplit(str(request.url)).query
        body, sig = raw.rsplit("&signature=", 1)
        seen["valid"] = sign("SECRET", body) == sig
        return httpx.Response(200, json={"dualSidePosition": False})

    c = client_with(handler)
    assert run(c.is_hedge_mode()) is False
    assert seen["headers"]["x-mbx-apikey"] == "KEY"
    assert "timestamp" in seen["query"] and "recvWindow" in seen["query"]
    assert seen["valid"]


def test_error_json_raises_with_code_and_friendly_text():
    def handler(request):
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": 1})
        return httpx.Response(400, json={"code": -2019, "msg": "Margin is insufficient."})

    c = client_with(handler)
    with pytest.raises(BinanceAPIError) as exc:
        run(c.new_order(symbol="BTCUSDT", side="BUY", type="MARKET", quantity=1))
    assert exc.value.code == -2019
    assert "Yetersiz teminat" in friendly_error(exc.value)


def test_timestamp_error_resyncs_and_retries():
    calls = {"order": 0, "time": 0}

    def handler(request):
        if request.url.path == "/fapi/v1/time":
            calls["time"] += 1
            return httpx.Response(200, json={"serverTime": 1_700_000_000_000})
        calls["order"] += 1
        if calls["order"] == 1:
            return httpx.Response(400, json={"code": -1021, "msg": "Timestamp outside recvWindow"})
        return httpx.Response(200, json={"orderId": 5})

    c = client_with(handler)
    assert run(c.new_order(symbol="X", side="BUY", type="MARKET", quantity=1))["orderId"] == 5
    assert calls["order"] == 2 and calls["time"] >= 2


def test_get_retries_on_server_error_but_post_does_not(monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr("bot.binance_client.asyncio.sleep", no_sleep)
    hits = {"GET": 0, "POST": 0}

    def handler(request):
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": 1})
        hits[request.method] += 1
        if request.method == "GET" and hits["GET"] < 3:
            return httpx.Response(503, text="busy")
        if request.method == "POST":
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json=[])

    c = client_with(handler)
    assert run(c.ticker_24h()) == []
    assert hits["GET"] == 3
    with pytest.raises(BinanceAPIError):
        run(c.new_order(symbol="X", side="BUY", type="MARKET", quantity=1))
    assert hits["POST"] == 1  # emir isteği tekrar gönderilmez (çift emir riski)


def test_restricted_location_message():
    e = BinanceAPIError(451, None, "Service unavailable from a restricted location")
    assert "konumdan" in friendly_error(e)
    assert BinanceAPIError(404, -5000, "x").endpoint_missing


def test_missing_keys_error():
    c = BinanceFuturesClient("https://example.test")
    with pytest.raises(BinanceAPIError, match="API anahtarı"):
        run(c.account())
