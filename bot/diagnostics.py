"""Bağlantı teşhisi: Binance'e neden ulaşılamadığını adım adım bulur ve Türkçe öneri verir."""

from __future__ import annotations

import asyncio
import socket
import time
import urllib.request
from typing import Any
from urllib.parse import urlsplit

import httpx

from .net import describe_network_error, ssl_context

STEP_INTERNET = "İnternet bağlantısı"
STEP_DNS = "Binance adresi (DNS)"
STEP_REDIRECT = "Adres yönlendirme kontrolü"
STEP_TCP = "Binance'e bağlantı (port 443)"
STEP_HTTPS = "Binance vadeli sunucusu (HTTPS)"
STEP_PUBLIC_IP = "Bu bilgisayarın internet IP adresi"
STEP_PROXY = "Vekil sunucu (proxy)"


async def _fetch(url: str, timeout: float = 10.0, json: bool = False, redirects: bool = False) -> tuple[int, Any, str]:
    """(HTTP durum kodu, içerik, hata metni). Bağlantı kurulamazsa durum 0 olur."""
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=ssl_context(), follow_redirects=redirects) as client:
            resp = await client.get(url, headers={"User-Agent": "CanBot/1.0", "Accept": "application/json, */*"})
            body: Any = resp.text
            if json:
                try:
                    body = resp.json()
                except ValueError:
                    body = None
            return resp.status_code, body, ""
    except Exception as exc:  # noqa: BLE001
        return 0, None, describe_network_error(exc)


async def _get(url: str, timeout: float = 10.0) -> tuple[int, str]:
    status, _, err = await _fetch(url, timeout)
    return status, err


async def _doh_lookup(host: str) -> tuple[set[str], list[str]] | None:
    """Google'ın DNS hizmetine (HTTPS üzerinden) sorar: (gerçek IP'ler, CNAME zinciri). Yalnızca karşılaştırma için."""
    status, data, _ = await _fetch(f"https://dns.google/resolve?name={host}&type=A", timeout=8, json=True)
    if status != 200 or not isinstance(data, dict):
        return None
    ips: set[str] = set()
    cnames: list[str] = []
    for ans in data.get("Answer", []) or []:
        if ans.get("type") == 1:
            ips.add(str(ans.get("data", "")))
        elif ans.get("type") == 5:
            cnames.append(str(ans.get("data", "")).rstrip(".").lower())
    return (ips, cnames) if ips else None


async def _ip_owner(ip: str) -> str:
    """IP adresinin hangi kuruluşa ait olduğunu (RDAP) bulmaya çalışır."""
    status, data, _ = await _fetch(f"https://rdap.org/ip/{ip}", timeout=8, json=True, redirects=True)
    if status != 200 or not isinstance(data, dict):
        return ""
    fallback = ""
    for ent in data.get("entities", []) or []:
        handle = str(ent.get("handle", "")).upper()
        vcard = (ent.get("vcardArray") or [None, []])[1]
        fn = next((str(item[3]) for item in vcard if item and item[0] == "fn"), "")
        if not fn or "RIPE" in fn.upper():
            continue
        if handle.startswith("ORG-"):  # kuruluş kaydı (ör. internet sağlayıcısının şirket adı)
            return fn
        if not fallback and "registrant" in (ent.get("roles") or []) and not handle.startswith("MNT"):
            fallback = fn
    return fallback or str(data.get("name") or "")


async def _public_ip() -> str:
    """Bu bilgisayarın internete çıktığı IPv4 adresi (Binance IP listesi IPv4 ister)."""
    status, body, _ = await _fetch("https://1.1.1.1/cdn-cgi/trace", timeout=8)
    if status == 200 and isinstance(body, str):
        for line in body.splitlines():
            if line.startswith("ip="):
                return line[3:].strip()
    status, body, _ = await _fetch("https://api.ipify.org", timeout=8)
    if status == 200 and isinstance(body, str) and body.strip().count(".") == 3:
        return body.strip()
    return ""


def advise(steps: list[dict[str, Any]], https_status: int) -> str:
    by_name = {s["name"]: s for s in steps}

    def failed(name: str) -> bool:
        s = by_name.get(name)
        return s is not None and not s["ok"]

    https = by_name.get(STEP_HTTPS, {})
    detail = str(https.get("detail", "")).lower()
    if https.get("ok"):
        return "Binance'e bağlantı sorunsuz. Hata sürerse API anahtarlarını ve izinlerini kontrol edin."
    if failed(STEP_INTERNET) and failed(STEP_DNS):
        return "Bu bilgisayarın internet bağlantısı yok gibi görünüyor. Bağlantıyı kontrol edip tekrar deneyin."
    if https_status == 451:
        return "Binance, bağlandığınız konumdan vadeli işlem erişimine izin vermiyor (HTTP 451)."
    if failed(STEP_REDIRECT):
        return (
            "İnternet sağlayıcınız Binance'in vadeli işlem sunucusunun adresini kendi sunucusuna yönlendiriyor ve "
            "bağlantıyı kesiyor. Bu, genellikle bir erişim engeli ya da 'Güvenli İnternet' filtresi olduğunda görülür; "
            "bu bağlantıdan Binance'e ulaşılamaz. Farklı bir internet bağlantısında (ör. ev internetiniz) deneyin; "
            "şirket hattıysa BT birimine danışın."
        )
    if failed(STEP_DNS):
        return (
            "Bu ağ Binance'in adresini çözmüyor; ağınız ya da internet sağlayıcınız Binance'i engelliyor olabilir. "
            "Farklı bir bağlantıyla (ör. ev Wi-Fi'si ya da telefonun kişisel erişim noktası) deneyin."
        )
    if "ssl" in detail or "sertifika" in detail:
        return (
            "Güvenli bağlantı doğrulanamadı. Antivirüs programınız ya da şirket ağı HTTPS bağlantılarını "
            "inceliyor olabilir: antivirüste 'HTTPS / SSL tarama' özelliğini kapatmayı ya da CanBot'u "
            "istisnalara eklemeyi deneyin. Şirket ağındaysanız farklı bir bağlantı deneyin."
        )
    if failed(STEP_TCP) or "zaman aşımı" in detail or "kesildi" in detail or "reddedildi" in detail:
        return (
            "Bu ağın güvenlik duvarı Binance'e bağlantıyı engelliyor gibi görünüyor. Şirket ya da okul "
            "ağındaysanız bu tür sitelere izin verilmeyebilir; farklı bir bağlantıyla (ör. ev Wi-Fi'si ya da "
            "telefonun kişisel erişim noktası) deneyin."
        )
    if https_status in (403, 407):
        return "Ağ ya da vekil sunucu Binance'e erişimi reddetti. Farklı bir bağlantı deneyin."
    return f"Binance'e ulaşılamadı: {https.get('detail', 'bilinmeyen hata')}"


async def diagnose(base_url: str = "https://fapi.binance.com") -> dict[str, Any]:
    host = urlsplit(base_url).hostname or "fapi.binance.com"
    steps: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        steps.append({"name": name, "ok": ok, "detail": detail})

    status, err = await _get("https://www.google.com/generate_204", timeout=8)
    add(STEP_INTERNET, status in (200, 204), "Var" if status in (200, 204) else (err or f"HTTP {status}"))

    loop = asyncio.get_running_loop()
    ips: list[str] = []
    canon = ""
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM, flags=socket.AI_CANONNAME), 8
        )
        ips = sorted({str(i[4][0]) for i in infos})
        canon = next((str(i[3]).rstrip(".").lower() for i in infos if i[3]), "")
        add(STEP_DNS, True, f"{host} → {', '.join(ips[:3])}")
    except Exception as exc:  # noqa: BLE001
        add(STEP_DNS, False, describe_network_error(exc))

    # Sağlayıcının DNS'i adresi başka bir sunucuya yönlendiriyor mu? (yalnızca karşılaştırma yapılır)
    doh = await _doh_lookup(host) if ips else None
    if doh:
        real_ips, cnames = doh
        cdn = next((c for c in cnames if c != host), "")
        redirected = not (set(ips) & real_ips) and bool(cdn) and (canon in ("", host))
        if redirected:
            owner = await _ip_owner(ips[0])
            who = f" ({owner} ağına ait)" if owner else ""
            add(
                STEP_REDIRECT,
                False,
                f"Bu ağın DNS'i {host} adresini {ips[0]}{who} sunucusuna yönlendiriyor. "
                f"Binance'in gerçek adresi: {', '.join(sorted(real_ips)[:2])} ({cdn}).",
            )
        else:
            add(STEP_REDIRECT, True, "Adres Binance'in gerçek sunucusunu gösteriyor")

    proxies = {k: v for k, v in urllib.request.getproxies().items() if k in ("http", "https", "all")}
    if ips:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(ips[0], 443), 8)
            writer.close()
            add(STEP_TCP, True, f"{ips[0]}:443 açık")
        except Exception as exc:  # noqa: BLE001
            note = " Ağda vekil sunucu ayarlı; doğrudan bağlantı engelli olabilir." if proxies else ""
            add(STEP_TCP, False, describe_network_error(exc) + note)

    status, err = await _get(f"{base_url.rstrip('/')}/fapi/v1/ping")
    if status == 200:
        add(STEP_HTTPS, True, "Binance yanıt verdi")
    elif status:
        add(STEP_HTTPS, False, f"Binance HTTP {status} döndürdü")
    else:
        add(STEP_HTTPS, False, err)

    public_ip = await _public_ip()
    if public_ip:
        add(
            STEP_PUBLIC_IP,
            True,
            f"{public_ip} — Binance API anahtarının 'güvenilir IP' listesinde bu adres olmalı "
            "(bot başka bir yerde çalışacaksa oranın IP'si).",
        )

    if proxies:
        add(STEP_PROXY, True, ", ".join(f"{k}: {v}" for k, v in proxies.items()))

    https_ok = next(s["ok"] for s in steps if s["name"] == STEP_HTTPS)
    return {
        "ok": https_ok,
        "host": host,
        "public_ip": public_ip,
        "time": int(time.time() * 1000),
        "steps": steps,
        "summary": advise(steps, status),
    }
