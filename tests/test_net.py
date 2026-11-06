import asyncio

import httpx

from bot import diagnostics
from bot.binance_client import BinanceAPIError, BinanceFuturesClient, friendly_error
from bot.diagnostics import STEP_DNS, STEP_HTTPS, STEP_INTERNET, STEP_TCP, advise
from bot.net import describe_network_error, ssl_context


def err(text, cause=None):
    e = httpx.ConnectError(text)
    if cause is not None:
        e.__cause__ = cause
    return e


def test_describe_network_error_maps_common_causes():
    assert "SSL" in describe_network_error(err("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"))
    assert "DNS" in describe_network_error(err("[Errno 11001] getaddrinfo failed"))
    assert "zaman aşımı" in describe_network_error(httpx.ConnectTimeout("timed out"))
    assert "kesildi" in describe_network_error(err("", OSError(10054, "An existing connection was forcibly closed")))
    assert "reddedildi" in describe_network_error(err("[WinError 10061] No connection could be made because the target machine actively refused it"))
    assert "Bağlantı hatası" in describe_network_error(err("weird"))


def test_network_error_reaches_user_message():
    def handler(request):
        raise httpx.ConnectError("[Errno 11001] getaddrinfo failed")

    c = BinanceFuturesClient("https://example.test", transport=httpx.MockTransport(handler))
    try:
        asyncio.run(c._request("POST", "/fapi/v1/order"))
    except BinanceAPIError as e:
        msg = friendly_error(e)
        assert "DNS" in msg and "kod None" not in msg
    else:
        raise AssertionError("hata bekleniyordu")


def test_ssl_context_uses_os_store_when_available():
    ctx = ssl_context()
    assert ctx is True or hasattr(ctx, "wrap_socket")


def step(name, ok, detail=""):
    return {"name": name, "ok": ok, "detail": detail}


def test_advise_messages():
    ok = [step(STEP_INTERNET, True), step(STEP_DNS, True), step(STEP_TCP, True), step(STEP_HTTPS, True)]
    assert "sorunsuz" in advise(ok, 200)
    dns = [step(STEP_INTERNET, True), step(STEP_DNS, False), step(STEP_HTTPS, False, "DNS")]
    assert "adresini çözmüyor" in advise(dns, 0)
    ssl = [step(STEP_INTERNET, True), step(STEP_DNS, True), step(STEP_TCP, True), step(STEP_HTTPS, False, "Güvenli bağlantı (SSL) doğrulanamadı")]
    assert "Antivirüs" in advise(ssl, 0)
    fw = [step(STEP_INTERNET, True), step(STEP_DNS, True), step(STEP_TCP, False), step(STEP_HTTPS, False, "zaman aşımı")]
    assert "güvenlik duvarı" in advise(fw, 0)
    geo = [step(STEP_INTERNET, True), step(STEP_DNS, True), step(STEP_TCP, True), step(STEP_HTTPS, False, "HTTP 451")]
    assert "451" in advise(geo, 451)
    offline = [step(STEP_INTERNET, False), step(STEP_DNS, False), step(STEP_HTTPS, False)]
    assert "internet bağlantısı yok" in advise(offline, 0)


def test_diagnose_reports_dns_block(monkeypatch):
    async def fake_get(url, timeout=10.0):
        if "google" in url:
            return 204, ""
        return 0, describe_network_error(err("[Errno 11001] getaddrinfo failed"))

    class Loop:
        async def getaddrinfo(self, *a, **k):
            raise OSError(11001, "getaddrinfo failed")

    async def no_ip():
        return ""

    monkeypatch.setattr(diagnostics, "_get", fake_get)
    monkeypatch.setattr(diagnostics, "_public_ip", no_ip)
    monkeypatch.setattr(diagnostics.asyncio, "get_running_loop", lambda: Loop())
    r = asyncio.run(diagnostics.diagnose("https://fapi.binance.com"))
    names = [s["name"] for s in r["steps"]]
    assert names[:2] == [STEP_INTERNET, STEP_DNS] and STEP_TCP not in names
    assert r["ok"] is False and "adresini çözmüyor" in r["summary"]


def test_diagnose_detects_isp_redirect(monkeypatch):
    """Sağlayıcı DNS'i Binance adresini kendi sunucusuna yönlendirip bağlantıyı kesiyorsa açıkça söylenir."""

    async def fake_get(url, timeout=10.0):
        if "google" in url:
            return 204, ""
        return 0, describe_network_error(err("", OSError(64, "Belirtilen ağ adı artık geçersiz")))

    class Loop:
        async def getaddrinfo(self, host, port, **kw):
            return [(2, 1, 6, "fapi.binance.com", ("81.8.66.123", 443))]

    class Writer:
        def close(self):
            pass

    async def fake_open(ip, port):
        return None, Writer()

    async def fake_doh(host):
        return {"108.138.85.11", "108.138.85.26"}, ["d2ukl3c6tymv7q.cloudfront.net"]

    async def fake_owner(ip):
        return "Vodafone Net Iletisim Hizmetler AS"

    async def fake_ip():
        return "88.1.2.3"

    monkeypatch.setattr(diagnostics, "_get", fake_get)
    monkeypatch.setattr(diagnostics, "_doh_lookup", fake_doh)
    monkeypatch.setattr(diagnostics, "_ip_owner", fake_owner)
    monkeypatch.setattr(diagnostics, "_public_ip", fake_ip)
    monkeypatch.setattr(diagnostics.asyncio, "get_running_loop", lambda: Loop())
    monkeypatch.setattr(diagnostics.asyncio, "open_connection", fake_open)
    r = asyncio.run(diagnostics.diagnose("https://fapi.binance.com"))
    redirect = next(s for s in r["steps"] if s["name"] == diagnostics.STEP_REDIRECT)
    assert redirect["ok"] is False and "Vodafone" in redirect["detail"] and "81.8.66.123" in redirect["detail"]
    assert "yönlendiriyor" in r["summary"]
    assert r["public_ip"] == "88.1.2.3"
    assert any(s["name"] == diagnostics.STEP_PUBLIC_IP and "88.1.2.3" in s["detail"] for s in r["steps"])


def test_diagnose_no_false_redirect_for_cdn_answer(monkeypatch):
    async def fake_get(url, timeout=10.0):
        return (204, "") if "google" in url else (200, "")

    class Loop:
        async def getaddrinfo(self, host, port, **kw):
            return [(2, 1, 6, "d2ukl3c6tymv7q.cloudfront.net", ("13.1.1.1", 443))]

    class Writer:
        def close(self):
            pass

    async def fake_open(ip, port):
        return None, Writer()

    async def fake_doh(host):
        return {"108.138.85.11"}, ["d2ukl3c6tymv7q.cloudfront.net"]

    async def fake_ip():
        return ""

    monkeypatch.setattr(diagnostics, "_get", fake_get)
    monkeypatch.setattr(diagnostics, "_doh_lookup", fake_doh)
    monkeypatch.setattr(diagnostics, "_public_ip", fake_ip)
    monkeypatch.setattr(diagnostics.asyncio, "get_running_loop", lambda: Loop())
    monkeypatch.setattr(diagnostics.asyncio, "open_connection", fake_open)
    r = asyncio.run(diagnostics.diagnose("https://fapi.binance.com"))
    redirect = next(s for s in r["steps"] if s["name"] == diagnostics.STEP_REDIRECT)
    assert redirect["ok"] is True and r["ok"] is True


def test_error_kind():
    from bot.binance_client import error_kind

    assert error_kind(BinanceAPIError(0, None, "x")) == "network"
    assert error_kind(BinanceAPIError(401, -2015, "Invalid API-key")) == "auth"
    assert error_kind(BinanceAPIError(400, -2019, "margin")) == "other"
