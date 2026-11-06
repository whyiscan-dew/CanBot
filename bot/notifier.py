"""İsteğe bağlı Telegram bildirimleri (pozisyon açıldı/kapandı, uyarılar)."""

from __future__ import annotations

import logging

import httpx

from .net import ssl_context
from .settings import SecretStore

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, secrets: SecretStore, transport: httpx.AsyncBaseTransport | None = None):
        self.secrets = secrets
        self._transport = transport

    @property
    def enabled(self) -> bool:
        return bool(self.secrets.get("telegram_token") and self.secrets.get("telegram_chat_id"))

    async def send(self, text: str, *, raise_errors: bool = False) -> bool:
        token = self.secrets.get("telegram_token")
        chat_id = self.secrets.get("telegram_chat_id")
        if not (token and chat_id):
            return False
        try:
            async with httpx.AsyncClient(timeout=10, transport=self._transport, verify=ssl_context()) as client:
                resp = await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
                )
                if resp.status_code != 200:
                    raise RuntimeError(f"Telegram hata kodu {resp.status_code}: {resp.text[:120]}")
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Telegram bildirimi gönderilemedi: %s", exc)
            if raise_errors:
                raise
            return False
