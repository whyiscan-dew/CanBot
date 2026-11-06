"""CanBot'u sunucu olarak başlatır (macOS / Linux / sanal sunucu ya da geliştirme).

Windows'ta pencereli masaüstü uygulaması için CanBot.exe ya da CanBot.pyw kullanın.

Kullanım:
    python run.py                 # panel http://localhost:8765 adresinde açılır
    python run.py --no-browser    # tarayıcı açmadan
    python run.py --port 9000
"""

from __future__ import annotations

import argparse
import os
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_dotenv(path: Path) -> None:
    """Basit .env okuyucu (KEY=VALUE satırları). Var olan ortam değişkenlerini ezmez."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def main() -> None:
    load_dotenv(ROOT / ".env")
    from bot.web.app import create_app, env_port, lan_ip

    parser = argparse.ArgumentParser(description="CanBot sunucusu")
    parser.add_argument("--host", default=os.environ.get("CANBOT_HOST", "0.0.0.0"), help="Dinlenecek adres")
    parser.add_argument("--port", type=int, default=env_port(), help="Port (varsayılan 8765)")
    parser.add_argument("--no-browser", action="store_true", help="Tarayıcıyı otomatik açma")
    args = parser.parse_args()

    import uvicorn

    from bot.logsetup import setup_logging

    log_path = setup_logging()
    app = create_app(port=args.port)
    local_url = f"http://localhost:{args.port}"
    lan_url = f"http://{lan_ip()}:{args.port}"
    print("\n  CanBot çalışıyor")
    print(f"  Bu bilgisayardan : {local_url}")
    if args.host in ("0.0.0.0", "::"):
        print(f"  Telefondan      : {lan_url}  (aynı Wi-Fi)")
        try:
            import qrcode

            qr = qrcode.QRCode(border=1)
            qr.add_data(lan_url)
            qr.make(fit=True)
            qr.print_ascii(invert=True)
        except Exception:  # noqa: BLE001
            pass
    print(f"  Günlük dosyası  : {log_path}")
    print("  Durdurmak için Ctrl+C\n")
    if not args.no_browser:
        threading.Timer(1.5, webbrowser.open, [local_url]).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", log_config=None)


if __name__ == "__main__":
    main()
