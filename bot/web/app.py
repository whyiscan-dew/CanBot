"""Panel sunucusu (FastAPI): masaüstü penceresi, tarayıcı ve iPhone aynı arayüzü kullanır."""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..binance_client import friendly_error
from ..brokers import connect_live_broker
from ..diagnostics import diagnose
from ..engine import Engine
from ..settings import BOUNDS, MODES, SecretStore, SettingsStore, data_dir
from ..storage import Storage

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent / "static"
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}


def lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # paket gönderilmez, yalnızca yerel IP öğrenilir
            ip = s.getsockname()[0]
            if not ip.startswith("127."):
                return ip
    except OSError:
        pass
    return "127.0.0.1"


def qr_svg(text: str) -> str:
    try:
        import qrcode
        import qrcode.image.svg

        img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
        return img.to_string(encoding="unicode")
    except Exception:  # noqa: BLE001
        return ""


def host_allowed(host_header: str) -> bool:
    """DNS rebinding saldırılarına karşı: yalnızca IP, localhost ve yerel ağ adlarına izin verilir."""
    host = (host_header or "").strip().lower()
    if host.startswith("["):  # [::1]:8765
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    if not host:
        return False
    if host in ("localhost", "testserver"):
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if "." not in host or host.endswith((".local", ".lan", ".home", ".ts.net")):
        return True
    extra = {h.strip().lower() for h in os.environ.get("CANBOT_ALLOWED_HOSTS", "").split(",") if h.strip()}
    return host in extra


def is_local(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in LOCAL_HOSTS


class LoginLimiter:
    def __init__(self, attempts: int = 10, window: float = 300):
        self.attempts = attempts
        self.window = window
        self._hits: dict[str, list[float]] = {}

    def check(self, key: str) -> None:
        now = time.time()
        hits = [t for t in self._hits.get(key, []) if now - t < self.window]
        if len(hits) >= self.attempts:
            raise HTTPException(429, "Çok fazla hatalı deneme. 5 dakika sonra tekrar deneyin.")
        hits.append(now)
        self._hits[key] = hits

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)


def default_engine_builder() -> Engine:
    root = data_dir()
    secrets = SecretStore(root / "secrets.json")
    return Engine(SettingsStore(root / "settings.json"), secrets, Storage(root / "canbot.db"))


def create_app(
    engine_builder: Callable[[], Engine] = default_engine_builder,
    *,
    start_engine: bool = True,
    port: int = 8765,
    desktop: Any = None,
) -> FastAPI:
    """``desktop``: masaüstü uygulamasının kontrol nesnesi (pencere göster, başlangıçta aç vb.)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = engine_builder()
        app.state.engine = engine
        app.state.secrets = engine.secrets
        app.state.limiter = LoginLimiter()
        if start_engine:
            await engine.start()
        try:
            yield
        finally:
            await engine.shutdown()
            engine.storage.close()

    app = FastAPI(title="CanBot", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.port = port
    app.state.desktop = desktop

    @app.exception_handler(ValueError)
    async def value_error_handler(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        if not host_allowed(request.headers.get("host", "")):
            return JSONResponse(status_code=400, content={"detail": "Geçersiz adres (host)"})
        origin = request.headers.get("origin")
        if origin and request.method not in ("GET", "HEAD", "OPTIONS"):
            origin_host = origin.split("://", 1)[-1].rstrip("/").lower()
            if origin_host != request.headers.get("host", "").lower():
                return JSONResponse(status_code=403, content={"detail": "İzin verilmeyen kaynak"})
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def engine_of(request: Request) -> Engine:
        return request.app.state.engine

    def require_auth(request: Request) -> None:
        secrets: SecretStore = request.app.state.secrets
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else ""
        if not token or not secrets.verify_token(token):
            raise HTTPException(401, "Oturum açmanız gerekiyor")

    auth = [Depends(require_auth)]

    # ------------------------------------------------------------- statik
    def _file(name: str, media_type: str | None = None, cache: str = "no-cache") -> FileResponse:
        resp = FileResponse(STATIC_DIR / name, media_type=media_type)
        resp.headers["Cache-Control"] = cache
        return resp

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return _file("index.html", "text/html; charset=utf-8")

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def manifest() -> FileResponse:
        return _file("manifest.webmanifest", "application/manifest+json")

    @app.get("/sw.js", include_in_schema=False)
    async def service_worker() -> FileResponse:
        resp = _file("sw.js", "text/javascript")
        resp.headers["Service-Worker-Allowed"] = "/"
        return resp

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return _file("icons/favicon.ico", "image/x-icon", cache="max-age=86400")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # --------------------------------------------------------------- giriş
    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"app": "canbot", "version": __version__}

    @app.get("/api/auth/status")
    async def auth_status(request: Request) -> dict[str, Any]:
        secrets: SecretStore = request.app.state.secrets
        has_pw = secrets.has_password()
        return {"has_password": has_pw, "can_setup": (not has_pw) and is_local(request)}

    @app.post("/api/auth/setup")
    async def auth_setup(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        secrets: SecretStore = request.app.state.secrets
        if secrets.has_password():
            raise HTTPException(409, "Şifre zaten belirlenmiş")
        if not is_local(request):
            raise HTTPException(403, "İlk şifre yalnızca botun çalıştığı bilgisayardan belirlenebilir")
        secrets.set_password(str(body.get("password", "")))
        return {"token": secrets.issue_token()}

    @app.post("/api/auth/login")
    async def auth_login(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        secrets: SecretStore = request.app.state.secrets
        limiter: LoginLimiter = request.app.state.limiter
        ip = request.client.host if request.client else "?"
        limiter.check(ip)
        if not secrets.has_password():
            raise HTTPException(400, "Henüz şifre belirlenmemiş. Bilgisayardaki CanBot'tan şifre belirleyin.")
        if not secrets.check_password(str(body.get("password", ""))):
            raise HTTPException(401, "Şifre yanlış")
        limiter.reset(ip)
        return {"token": secrets.issue_token()}

    @app.post("/api/auth/password", dependencies=auth)
    async def change_password(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        secrets: SecretStore = request.app.state.secrets
        if secrets.has_password() and not secrets.check_password(str(body.get("old_password", ""))):
            raise HTTPException(400, "Mevcut şifre yanlış")
        secrets.set_password(str(body.get("new_password", "")))
        return {"token": secrets.issue_token()}

    # ---------------------------------------------------------------- durum
    @app.get("/api/state", dependencies=auth)
    async def state(request: Request) -> dict[str, Any]:
        engine = engine_of(request)
        secrets: SecretStore = request.app.state.secrets
        snap = engine.snapshot()
        snap["settings"] = engine.settings.to_dict()
        snap["bounds"] = {k: [lo, hi] for k, (lo, hi, _) in BOUNDS.items()}
        snap["keys"] = secrets.status()
        snap["has_password"] = secrets.has_password()
        desktop = request.app.state.desktop
        snap["desktop"] = desktop.status() if desktop is not None else None
        return snap

    @app.get("/api/connect", dependencies=auth)
    async def connect_info(request: Request) -> dict[str, Any]:
        url = f"http://{lan_ip()}:{request.app.state.port}"
        return {"url": url, "qr_svg": qr_svg(url)}

    @app.get("/api/analysis/{symbol}", dependencies=auth)
    async def analysis(request: Request, symbol: str) -> dict[str, Any]:
        symbol = symbol.upper()
        if not symbol.isalnum() or len(symbol) > 30:
            raise HTTPException(400, "Geçersiz sembol")
        try:
            return await engine_of(request).coin_detail(symbol)
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, friendly_error(exc)) from exc

    # ------------------------------------------------------------- komutlar
    @app.post("/api/bot/auto", dependencies=auth)
    async def bot_auto(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        await engine_of(request).set_auto_trade(bool(body.get("on")))
        return {"ok": True}

    @app.post("/api/bot/scan", dependencies=auth)
    async def bot_scan(request: Request) -> dict[str, Any]:
        engine_of(request).request_scan()
        return {"ok": True}

    @app.post("/api/bot/reconnect", dependencies=auth)
    async def bot_reconnect(request: Request) -> dict[str, Any]:
        await engine_of(request).reconnect()
        return {"ok": True, "error": engine_of(request).error}

    @app.post("/api/positions/close-all", dependencies=auth)
    async def close_all(request: Request, body: dict[str, Any] | None = None) -> dict[str, Any]:
        engine = engine_of(request)
        if (body or {}).get("stop", True) and engine.settings.auto_trade:
            await engine.set_auto_trade(False)
        count = await engine.close_all("Tümü elle kapatıldı")
        return {"ok": True, "closed": count}

    @app.post("/api/positions/{trade_id}/close", dependencies=auth)
    async def close_position(request: Request, trade_id: int) -> dict[str, Any]:
        trade = await engine_of(request).close_trade_by_id(trade_id)
        return {"ok": True, "pnl": trade.pnl}

    @app.put("/api/settings", dependencies=auth)
    async def update_settings(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        new = await engine_of(request).update_settings(body)
        return {"ok": True, "settings": new.to_dict()}

    @app.post("/api/paper/reset", dependencies=auth)
    async def paper_reset(request: Request) -> dict[str, Any]:
        await engine_of(request).reset_paper()
        return {"ok": True}

    @app.post("/api/backtest", dependencies=auth)
    async def backtest(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        engine_of(request).start_backtest(int(body.get("days", 90)), int(body.get("symbols", 20)))
        return {"ok": True}

    # ------------------------------------------------------------ anahtarlar
    @app.put("/api/keys", dependencies=auth)
    async def save_keys(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        mode = body.get("mode")
        if mode not in ("testnet", "live"):
            raise HTTPException(400, "Geçersiz mod")
        key = str(body.get("api_key", "")).strip()
        secret = str(body.get("api_secret", "")).strip()
        if (key or secret) and (len(key) < 16 or len(secret) < 16):
            raise HTTPException(400, "API Key ve Secret eksik ya da hatalı görünüyor")
        secrets: SecretStore = request.app.state.secrets
        secrets.set_many(**{f"{mode}_api_key": key, f"{mode}_api_secret": secret})
        engine = engine_of(request)
        if engine.mode == mode:
            await engine.reconnect()
        return {"ok": True, "keys": secrets.status()}

    @app.post("/api/keys/test", dependencies=auth)
    async def test_keys(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        mode = body.get("mode")
        if mode not in ("testnet", "live"):
            raise HTTPException(400, "Geçersiz mod")
        engine = engine_of(request)
        key, secret = engine.secrets.binance_keys(mode)
        try:
            broker = await connect_live_broker(
                mode, key, secret, engine.settings.testnet_url, engine.client_factory
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": friendly_error(exc)}
        try:
            acc = await broker.account([], {})
            warnings = await engine.key_permission_warnings(mode, key, secret)
            return {
                "ok": True,
                "message": f"Bağlantı başarılı. Vadeli hesap bakiyesi: {acc.equity:.2f} USDT",
                "warnings": warnings,
                "url": broker.client.base_url,
            }
        finally:
            await broker.aclose()

    @app.post("/api/diagnose", dependencies=auth)
    async def diagnose_connection(request: Request) -> dict[str, Any]:
        """Binance'e neden bağlanılamadığını adım adım test eder."""
        return await diagnose(engine_of(request).data_url)

    @app.put("/api/telegram", dependencies=auth)
    async def save_telegram(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        secrets: SecretStore = request.app.state.secrets
        secrets.set_many(
            telegram_token=str(body.get("token", "")).strip(),
            telegram_chat_id=str(body.get("chat_id", "")).strip(),
        )
        return {"ok": True, "keys": secrets.status()}

    @app.post("/api/telegram/test", dependencies=auth)
    async def test_telegram(request: Request) -> dict[str, Any]:
        engine = engine_of(request)
        try:
            ok = await engine.notifier.send("✅ CanBot bildirim testi başarılı.", raise_errors=True)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": str(exc)}
        return {"ok": ok, "message": "Mesaj gönderildi" if ok else "Telegram bilgileri eksik"}

    # --------------------------------------------------------------- masaüstü
    @app.post("/api/desktop/show")
    async def desktop_show(request: Request) -> dict[str, Any]:
        """Program ikinci kez açılırsa mevcut pencereyi öne getirir (yalnızca bu bilgisayardan)."""
        if not is_local(request):
            raise HTTPException(403, "Yalnızca yerel")
        desktop = request.app.state.desktop
        if desktop is not None:
            desktop.show_window()
        return {"ok": desktop is not None}

    @app.put("/api/desktop", dependencies=auth)
    async def desktop_options(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        desktop = request.app.state.desktop
        if desktop is None:
            raise HTTPException(400, "Bu seçenekler yalnızca Windows masaüstü uygulamasında var")
        desktop.apply_options(body)
        return {"ok": True, "desktop": desktop.status()}

    app.state.modes = MODES
    return app


def env_port(default: int = 8765) -> int:
    try:
        return int(os.environ.get("CANBOT_PORT", default))
    except ValueError:
        return default
