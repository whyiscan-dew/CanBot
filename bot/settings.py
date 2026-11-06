"""Ayarlar ve gizli bilgiler.

- İşlem ayarları ``data/settings.json`` dosyasında tutulur ve panelden değiştirilebilir.
- Gizli bilgiler (API anahtarları, panel şifresi, Telegram) ``data/secrets.json``
  dosyasında tutulur. Ortam değişkenleri (.env) bu dosyadaki değerlerin önüne geçer.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets as _secrets
import sys
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """Veri klasörü: CANBOT_DATA_DIR > (exe ise) %LOCALAPPDATA%\\CanBot > proje/data"""
    env = os.environ.get("CANBOT_DATA_DIR")
    if env:
        path = Path(env)
    elif getattr(sys, "frozen", False):
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".canbot")
        path = Path(base) / "CanBot"
    else:
        path = ROOT_DIR / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


MODES = ("paper", "testnet", "live")
MODE_LABELS = {"paper": "Sanal (kağıt)", "testnet": "Binance Testnet", "live": "GERÇEK PARA"}
TIMEFRAMES = ("5m", "15m", "30m", "1h", "2h", "4h", "1d")
ENTRY_MODES = ("breakout", "pullback")
TESTNET_URLS = ("auto", "https://demo-fapi.binance.com", "https://testnet.binancefuture.com")
LIVE_CONFIRM_TEXT = "GERÇEK PARA"

DEFAULT_BLACKLIST = [
    "USDCUSDT",
    "FDUSDUSDT",
    "TUSDUSDT",
    "USDPUSDT",
    "BUSDUSDT",
    "USDEUSDT",
    "EURUSDT",
    "XAUUSDT",
    "XAGUSDT",
]


@dataclass
class TradingSettings:
    mode: str = "paper"
    auto_trade: bool = False
    leverage: int = 5
    margin_type: str = "ISOLATED"
    risk_per_trade_pct: float = 1.0
    max_open_positions: int = 3
    max_position_margin_pct: float = 30.0
    daily_loss_limit_pct: float = 5.0
    max_drawdown_pct: float = 25.0
    allow_long: bool = True
    allow_short: bool = True
    min_quote_volume_usdt: float = 50_000_000.0
    universe_size: int = 20
    trend_timeframe: str = "1d"
    entry_timeframe: str = "4h"
    entry_mode: str = "breakout"
    stop_atr_mult: float = 2.5
    take_profit_r: float = 0.0
    trailing_atr_mult: float = 3.0
    breakeven_at_r: float = 0.0
    max_hold_hours: float = 0.0
    cooldown_minutes: int = 60
    max_funding_rate_pct: float = 0.05
    min_score: float = 50.0
    symbol_blacklist: list[str] = field(default_factory=lambda: list(DEFAULT_BLACKLIST))
    symbol_whitelist: list[str] = field(default_factory=list)
    paper_start_balance: float = 1000.0
    testnet_url: str = "auto"
    wizard_done: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# (alt sınır, üst sınır, Türkçe ad)
BOUNDS: dict[str, tuple[float, float, str]] = {
    "leverage": (1, 20, "Kaldıraç"),
    "risk_per_trade_pct": (0.1, 5.0, "İşlem başı risk (%)"),
    "max_open_positions": (1, 10, "En fazla açık pozisyon"),
    "max_position_margin_pct": (5, 100, "Pozisyon başı en fazla teminat (%)"),
    "daily_loss_limit_pct": (1, 50, "Günlük zarar limiti (%)"),
    "max_drawdown_pct": (5, 90, "En büyük düşüş limiti (%)"),
    "min_quote_volume_usdt": (1_000_000, 100_000_000_000, "En az 24s hacim (USDT)"),
    "universe_size": (5, 100, "Taranacak coin sayısı"),
    "stop_atr_mult": (0.5, 6.0, "Stop mesafesi (ATR katı)"),
    "take_profit_r": (0.0, 20.0, "Kâr hedefi (R, 0 = yok)"),
    "trailing_atr_mult": (0.0, 10.0, "İz süren stop (ATR katı, 0 = kapalı)"),
    "breakeven_at_r": (0.0, 5.0, "Başabaş stop (R)"),
    "max_hold_hours": (0.0, 720.0, "En uzun tutma süresi (saat)"),
    "cooldown_minutes": (0, 1440, "Zarar sonrası bekleme (dk)"),
    "max_funding_rate_pct": (0.01, 1.0, "En fazla fonlama oranı (%)"),
    "min_score": (0.0, 100.0, "En düşük skor"),
    "paper_start_balance": (10, 10_000_000, "Sanal başlangıç bakiyesi"),
}


def _clean_symbols(value: Any) -> list[str]:
    if isinstance(value, str):
        value = value.replace("\n", ",").split(",")
    if not isinstance(value, (list, tuple)):
        raise ValueError("Coin listesi geçersiz")
    out: list[str] = []
    for item in value:
        sym = str(item).strip().upper()
        if not sym:
            continue
        if not sym.isalnum() or len(sym) > 30:
            raise ValueError(f"Geçersiz coin sembolü: {sym}")
        if not sym.endswith("USDT"):
            sym += "USDT"
        if sym not in out:
            out.append(sym)
    return out


def validate_settings(raw: dict[str, Any], base: TradingSettings | None = None) -> TradingSettings:
    """Panelden gelen ayarları doğrular. Hatalı değerde Türkçe ``ValueError`` fırlatır."""
    current = (base or TradingSettings()).to_dict()
    known = {f.name: f for f in fields(TradingSettings)}
    for key, value in raw.items():
        if key not in known:
            continue
        default = current[key]
        if key == "mode":
            if value not in MODES:
                raise ValueError("Geçersiz mod")
        elif key == "margin_type":
            value = str(value).upper()
            if value not in ("ISOLATED", "CROSSED"):
                raise ValueError("Marjin tipi ISOLATED veya CROSSED olmalı")
        elif key in ("trend_timeframe", "entry_timeframe"):
            if value not in TIMEFRAMES:
                raise ValueError(f"Geçersiz zaman dilimi: {value}")
        elif key == "entry_mode":
            if value not in ENTRY_MODES:
                raise ValueError("Geçersiz giriş tipi")
        elif key == "testnet_url":
            if value not in TESTNET_URLS:
                raise ValueError("Geçersiz testnet adresi")
        elif key in ("symbol_blacklist", "symbol_whitelist"):
            value = _clean_symbols(value)
        elif isinstance(default, bool):
            if not isinstance(value, bool):
                raise ValueError(f"{key} açık/kapalı olmalı")
        elif isinstance(default, (int, float)):
            try:
                num = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"{BOUNDS.get(key, (0, 0, key))[2]} sayı olmalı") from None
            if num != num:  # NaN
                raise ValueError(f"{key} geçersiz")
            if key in BOUNDS:
                lo, hi, label = BOUNDS[key]
                if num < lo or num > hi:
                    raise ValueError(f"{label} {lo:g} ile {hi:g} arasında olmalı")
            value = int(round(num)) if isinstance(default, int) else num
        current[key] = value

    settings = TradingSettings(**current)
    if settings.trend_timeframe == settings.entry_timeframe:
        raise ValueError("Trend ve giriş zaman dilimleri farklı olmalı")
    if _tf_minutes(settings.trend_timeframe) < _tf_minutes(settings.entry_timeframe):
        raise ValueError("Trend zaman dilimi giriş zaman diliminden büyük olmalı")
    if settings.take_profit_r == 0 and settings.trailing_atr_mult == 0 and settings.max_hold_hours == 0:
        raise ValueError("Kâr hedefi, iz süren stop ve süre sınırından en az biri açık olmalı")
    if 0 < settings.take_profit_r < 0.5:
        raise ValueError("Kâr hedefi 0 (kapalı) ya da en az 0,5 R olmalı")
    if not settings.allow_long and not settings.allow_short:
        raise ValueError("Long ve short'tan en az biri açık olmalı")
    return settings


def _tf_minutes(tf: str) -> int:
    unit = tf[-1]
    num = int(tf[:-1])
    return num * {"m": 1, "h": 60, "d": 1440}[unit]


def timeframe_ms(tf: str) -> int:
    return _tf_minutes(tf) * 60_000


def _atomic_write_json(path: Path, data: Any, private: bool = False) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    if private:
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    os.replace(tmp, path)


class SettingsStore:
    def __init__(self, path: Path | None = None):
        self.path = path or data_dir() / "settings.json"
        self._lock = threading.Lock()
        self._settings = self._load()

    def _load(self) -> TradingSettings:
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                return validate_settings(raw)
            except (ValueError, OSError, json.JSONDecodeError):
                pass
        settings = TradingSettings()
        _atomic_write_json(self.path, settings.to_dict())
        return settings

    @property
    def settings(self) -> TradingSettings:
        return self._settings

    def update(self, raw: dict[str, Any]) -> TradingSettings:
        with self._lock:
            new = validate_settings(raw, self._settings)
            _atomic_write_json(self.path, new.to_dict())
            self._settings = new
            return new


ENV_MAP = {
    "live_api_key": "BINANCE_API_KEY",
    "live_api_secret": "BINANCE_API_SECRET",
    "testnet_api_key": "BINANCE_TESTNET_API_KEY",
    "testnet_api_secret": "BINANCE_TESTNET_API_SECRET",
    "telegram_token": "TELEGRAM_BOT_TOKEN",
    "telegram_chat_id": "TELEGRAM_CHAT_ID",
}


def mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 10:
        return "•" * len(value)
    return f"{value[:4]}…{value[-4:]}"


class SecretStore:
    """API anahtarları, panel şifresi ve oturum anahtarı."""

    TOKEN_TTL = 30 * 24 * 3600

    def __init__(self, path: Path | None = None):
        self.path = path or data_dir() / "secrets.json"
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self._data = {}
        if not self._data.get("token_key"):
            self._data["token_key"] = _secrets.token_hex(32)
            self._save()

    def _save(self) -> None:
        _atomic_write_json(self.path, self._data, private=True)

    # -- genel --
    def get(self, name: str) -> str:
        env_name = ENV_MAP.get(name)
        if env_name and os.environ.get(env_name):
            return os.environ[env_name].strip()
        return str(self._data.get(name, "") or "")

    def set_many(self, **values: str) -> None:
        with self._lock:
            for key, value in values.items():
                if key not in ENV_MAP:
                    raise KeyError(key)
                value = (value or "").strip()
                if value:
                    self._data[key] = value
                else:
                    self._data.pop(key, None)
            self._save()

    def binance_keys(self, mode: str) -> tuple[str, str]:
        prefix = "live" if mode == "live" else "testnet"
        return self.get(f"{prefix}_api_key"), self.get(f"{prefix}_api_secret")

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for prefix in ("testnet", "live"):
            key, secret = self.binance_keys(prefix)
            out[prefix] = {"set": bool(key and secret), "api_key": mask(key)}
        out["telegram"] = {"set": bool(self.get("telegram_token") and self.get("telegram_chat_id"))}
        return out

    # -- panel şifresi --
    def has_password(self) -> bool:
        return bool(os.environ.get("DASHBOARD_PASSWORD") or self._data.get("password_hash"))

    def set_password(self, password: str) -> None:
        if len(password) < 6:
            raise ValueError("Şifre en az 6 karakter olmalı")
        salt = _secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
        with self._lock:
            self._data["password_salt"] = salt.hex()
            self._data["password_hash"] = digest.hex()
            # Şifre değişince eski oturumlar geçersiz olur.
            self._data["token_key"] = _secrets.token_hex(32)
            self._save()

    def check_password(self, password: str) -> bool:
        env_pw = os.environ.get("DASHBOARD_PASSWORD")
        if env_pw:
            return hmac.compare_digest(password.encode(), env_pw.encode())
        salt_hex = self._data.get("password_salt")
        hash_hex = self._data.get("password_hash")
        if not salt_hex or not hash_hex:
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 200_000)
        return hmac.compare_digest(digest.hex(), hash_hex)

    # -- oturum jetonu (imzalı, durumsuz) --
    def issue_token(self, ttl: int | None = None) -> str:
        payload = json.dumps({"exp": int(time.time()) + (ttl or self.TOKEN_TTL)}).encode()
        body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        sig = hmac.new(self._data["token_key"].encode(), body.encode(), hashlib.sha256).hexdigest()
        return f"{body}.{sig}"

    def verify_token(self, token: str) -> bool:
        try:
            body, sig = token.split(".", 1)
        except ValueError:
            return False
        expected = hmac.new(self._data["token_key"].encode(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return False
        try:
            payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
            return int(payload.get("exp", 0)) > time.time()
        except (ValueError, json.JSONDecodeError):
            return False
