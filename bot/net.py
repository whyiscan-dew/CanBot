"""Ağ yardımcıları: işletim sisteminin sertifika deposu ve anlaşılır bağlantı hata mesajları."""

from __future__ import annotations

import ssl
from typing import Any


def ssl_context() -> Any:
    """İşletim sisteminin güvendiği sertifikaları kullanan SSL ayarı.

    Windows'ta antivirüs ya da şirket ağı HTTPS bağlantılarını kendi sertifikasıyla
    inceliyorsa tarayıcı çalışır ama Python'un kendi sertifika listesi bunu tanımaz.
    ``truststore`` ile Windows'un sertifika deposu kullanılır (tarayıcıyla aynı davranış).
    """
    try:
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # noqa: BLE001 - kütüphane yoksa varsayılan doğrulama
        return True


def _full_text(exc: BaseException) -> str:
    parts = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        parts.append(f"{cur.__class__.__name__}: {cur}")
        cur = cur.__cause__ or cur.__context__
    return " | ".join(parts)


def describe_network_error(exc: BaseException) -> str:
    """Bağlantı hatasını sade Türkçe açıklamaya çevirir (teknik ayrıntı parantez içinde)."""
    text = _full_text(exc)
    low = text.lower()
    detail = text[:180]
    if "certificate" in low or "ssl" in low or "tls" in low:
        return (
            "Güvenli bağlantı (SSL) doğrulanamadı. Antivirüs programı ya da şirket ağı bağlantıyı "
            f"inceliyor veya engelliyor olabilir. ({detail})"
        )
    if any(k in low for k in ("getaddrinfo", "name or service", "nodename", "11001", "11002", "11004", "name resolution")):
        return (
            "Binance sunucusunun adresi bulunamadı (DNS). İnternet bağlantınızı kontrol edin; "
            f"bulunduğunuz ağ Binance'i engelliyor olabilir. ({detail})"
        )
    if "proxy" in low:
        return f"Ağdaki vekil sunucu (proxy) bağlantıya izin vermedi. ({detail})"
    if any(k in low for k in ("timed out", "timeout", "10060")):
        return (
            "Binance'e bağlanırken zaman aşımı oldu. Bulunduğunuz ağ ya da güvenlik duvarı "
            f"engelliyor olabilir. ({detail})"
        )
    if any(k in low for k in ("refused", "10061")):
        return f"Bağlantı reddedildi. Güvenlik duvarı ya da ağ engelliyor olabilir. ({detail})"
    if any(k in low for k in ("reset", "10054", "forcibly closed", "connection aborted", "10053")):
        return (
            "Bağlantı ağ tarafından kesildi. Bulunduğunuz ağ (ör. şirket ağı) Binance'i engelliyor "
            f"olabilir. ({detail})"
        )
    return f"Bağlantı hatası ({detail})"
