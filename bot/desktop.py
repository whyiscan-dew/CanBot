"""Windows masaüstü uygulaması.

- Konsol (cmd) penceresi açılmaz; panel kendi penceresinde açılır (Edge WebView2).
- Pencere kapatılınca bot saatin yanındaki simgede (sistem tepsisi) çalışmaya devam eder.
- İsteğe bağlı: Windows açılınca başlama, bilgisayarı uyanık tutma, masaüstü bildirimleri.
- Aynı anda yalnızca bir CanBot çalışır (çift işlem riskine karşı).

Başlatma:  CanBot.exe  |  pythonw CanBot.pyw  |  python -m bot.desktop
Seçenekler: --minimized (simge durumunda başla), --port N, --selftest (paketleme testi)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

APP_NAME = "CanBot"
DEFAULT_PORT = 8765
IS_WINDOWS = sys.platform == "win32"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
ROOT_DIR = Path(__file__).resolve().parent.parent

log = logging.getLogger("canbot.desktop")


def _ensure_streams() -> None:
    """Pencereli (konsolsuz) çalışmada sys.stdout/stderr None olur; yazma hatalarını önler."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115


# ------------------------------------------------------------------ Windows
def launch_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    runner = pyw if pyw.exists() else exe
    return f'"{runner}" "{ROOT_DIR / "CanBot.pyw"}" --minimized'


def autostart_enabled() -> bool:
    if not IS_WINDOWS:
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(enable: bool) -> None:
    if not IS_WINDOWS:
        raise ValueError("Bu seçenek yalnızca Windows'ta kullanılabilir")
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enable:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except OSError:
                pass


def set_keep_awake(active: bool) -> None:
    """Windows'un uykuya geçmesini engeller (ekranın kapanmasına izin verir)."""
    if not IS_WINDOWS:
        return
    import ctypes

    es_continuous, es_system_required = 0x80000000, 0x00000001
    flags = es_continuous | (es_system_required if active else 0)
    ctypes.windll.kernel32.SetThreadExecutionState(flags)


def find_app_browser() -> str | None:
    if not IS_WINDOWS:
        return None
    candidates = [
        r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
        r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
    ]
    for c in candidates:
        path = os.path.expandvars(c)
        if os.path.exists(path):
            return path
    return None


def open_app_window(url: str) -> None:
    """pywebview yoksa: Edge/Chrome 'uygulama penceresi' (adres çubuksuz), o da yoksa tarayıcı."""
    browser = find_app_browser()
    if browser:
        try:
            flags = 0x08000000 if IS_WINDOWS else 0  # CREATE_NO_WINDOW
            subprocess.Popen([browser, f"--app={url}", "--window-size=1320,860"], creationflags=flags)
            return
        except OSError:
            pass
    webbrowser.open(url)


# ------------------------------------------------------------------ yardımcılar
def port_free(port: int) -> bool:
    # Not: SO_REUSEADDR kullanılmaz; Windows'ta dolu portu boş gösterebilir.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def running_instance(port: int) -> bool:
    import httpx

    try:
        r = httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=1.5)
        return r.status_code == 200 and r.json().get("app") == "canbot"
    except Exception:  # noqa: BLE001
        return False


class ServerThread(threading.Thread):
    """FastAPI sunucusunu (ve bot motorunu) arka plan iş parçacığında çalıştırır."""

    def __init__(self, app: Any, host: str, port: int):
        super().__init__(name="canbot-server", daemon=True)
        import uvicorn

        self.app = app
        self.config = uvicorn.Config(app, host=host, port=port, log_config=None, log_level="warning")
        self.server = uvicorn.Server(self.config)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.error: BaseException | None = None

    def run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self.server.serve())
        except BaseException as exc:  # noqa: BLE001
            self.error = exc
            log.exception("Sunucu durdu")

    def wait_ready(self, timeout: float = 30.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if self.server.started:
                return True
            if self.error is not None or not self.is_alive():
                return False
            time.sleep(0.05)
        return False

    def stop(self) -> None:
        self.server.should_exit = True

    def call(self, coro: Any, timeout: float = 30.0) -> Any:
        assert self.loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)


# ------------------------------------------------------------------ kontrol
class DesktopController:
    """Panel ↔ masaüstü köprüsü: pencere, tepsi simgesi ve Windows seçenekleri."""

    def __init__(self, options_path: Path):
        self.options_path = options_path
        self.options = {"keep_awake": True, "notifications": True}
        try:
            self.options.update(json.loads(options_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        self.window: Any = None
        self.tray: Any = None
        self.server: ServerThread | None = None
        self.secrets: Any = None
        self.port = DEFAULT_PORT
        self.quitting = False
        self._told_tray = False
        self._awake_stop = threading.Event()

    # -- panelin kullandığı arayüz --
    def status(self) -> dict[str, Any]:
        return {
            "autostart": autostart_enabled(),
            "autostart_supported": IS_WINDOWS,
            "keep_awake": bool(self.options.get("keep_awake")),
            "notifications": bool(self.options.get("notifications")),
            "window": self.window is not None,
        }

    def apply_options(self, body: dict[str, Any]) -> None:
        if "autostart" in body and IS_WINDOWS and bool(body["autostart"]) != autostart_enabled():
            set_autostart(bool(body["autostart"]))
        for key in ("keep_awake", "notifications"):
            if key in body:
                self.options[key] = bool(body[key])
        try:
            self.options_path.write_text(json.dumps(self.options), encoding="utf-8")
        except OSError:
            pass

    def panel_url(self) -> str:
        # Masaüstü penceresi bu bilgisayarda çalışır; oturumu uzun süreli tutulur.
        token = self.secrets.issue_token(ttl=10 * 365 * 86400) if self.secrets is not None else ""
        return f"http://127.0.0.1:{self.port}/#desktop={token}"

    def show_window(self) -> None:
        if self.window is not None:
            try:
                self.window.show()
                self.window.restore()
                return
            except Exception:  # noqa: BLE001
                log.debug("pencere gösterilemedi", exc_info=True)
        open_app_window(self.panel_url())

    def notify(self, text: str) -> None:
        if not self.options.get("notifications") or self.tray is None:
            return
        try:
            self.tray.notify(text[:250], APP_NAME)
        except Exception:  # noqa: BLE001
            log.debug("bildirim gösterilemedi", exc_info=True)

    # -- motor --
    def _engine(self) -> Any:
        if self.server is None:
            return None
        return getattr(self.server.app.state, "engine", None)

    def auto_trade_on(self) -> bool:
        engine = self._engine()
        return bool(engine and engine.settings.auto_trade)

    def toggle_auto(self) -> None:
        engine = self._engine()
        if engine is None or self.server is None:
            return
        want = not engine.settings.auto_trade
        if want and engine.settings.mode == "live":
            # Gerçek parayla başlatma, onay için paneldan yapılır.
            self.show_window()
            self.notify("Gerçek modda otomatik işlemi panelden onaylayarak açın.")
            return
        try:
            self.server.call(engine.set_auto_trade(want))
            self.notify("Otomatik işlem açıldı" if want else "Otomatik işlem kapatıldı")
        except Exception as exc:  # noqa: BLE001
            self.notify(f"İşlem yapılamadı: {exc}")

    def keep_awake_loop(self) -> None:
        """Otomatik işlem açıkken bilgisayarın uykuya geçmesini engeller."""
        last = None
        while not self._awake_stop.wait(30):
            want = bool(self.options.get("keep_awake")) and self.auto_trade_on()
            if want != last:
                set_keep_awake(want)
                last = want
        set_keep_awake(False)

    def on_window_closing(self) -> bool:
        """Pencere kapatılınca programı kapatmak yerine tepsiye gizler.

        pywebview bu olayı beklerken arayüz iş parçacığını kilitleyebilir; bu yüzden
        gizleme kısa bir gecikmeyle başka bir iş parçacığından yapılır.
        """
        if self.quitting or self.tray is None:
            return True

        def hide() -> None:
            try:
                self.window.hide()
            except Exception:  # noqa: BLE001
                log.debug("pencere gizlenemedi", exc_info=True)

        threading.Timer(0.1, hide).start()
        if not self._told_tray:
            self._told_tray = True
            self.notify("CanBot arka planda çalışmaya devam ediyor. Açmak için saatin yanındaki simgeye tıklayın.")
        return False

    def quit(self) -> None:
        self.quitting = True
        self._awake_stop.set()
        if self.server is not None:
            self.server.stop()
        if self.window is not None:
            try:
                self.window.destroy()
            except Exception:  # noqa: BLE001
                pass
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:  # noqa: BLE001
                pass


def build_tray(ctrl: DesktopController, icon_path: Path) -> Any:
    import pystray
    from PIL import Image

    image = Image.open(icon_path)
    menu = pystray.Menu(
        pystray.MenuItem("Paneli aç", lambda icon, item: ctrl.show_window(), default=True),
        pystray.MenuItem("Otomatik işlem", lambda icon, item: ctrl.toggle_auto(), checked=lambda item: ctrl.auto_trade_on()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Çıkış", lambda icon, item: ctrl.quit()),
    )
    return pystray.Icon(APP_NAME, image, f"{APP_NAME} — otomatik işlem botu", menu)


def static_icon(name: str) -> Path:
    return Path(__file__).resolve().parent / "web" / "static" / "icons" / name


# ------------------------------------------------------------------ çalıştırma
def selftest(out: str | None, strict: bool) -> int:
    """Paketlenmiş uygulamanın parçalarını dener (CI için). Pencere açmaz."""
    import tempfile

    import httpx

    result: dict[str, Any] = {"ok": False}
    tmp = tempfile.mkdtemp(prefix="canbot-selftest-")
    os.environ["CANBOT_DATA_DIR"] = tmp
    try:
        from .web.app import create_app

        port = next(p for p in range(18765, 18800) if port_free(p))
        app = create_app(port=port, start_engine=False)
        server = ServerThread(app, "127.0.0.1", port)
        server.start()
        result["server_started"] = server.wait_ready(30)
        base = f"http://127.0.0.1:{port}"
        result["health"] = httpx.get(f"{base}/api/health", timeout=5).json()
        result["index_ok"] = httpx.get(f"{base}/", timeout=5).status_code == 200
        result["app_js_ok"] = httpx.get(f"{base}/static/app.js", timeout=5).status_code == 200
        result["icon_exists"] = static_icon("canbot.ico").exists()
        server.stop()
        server.join(10)
        for mod in ("webview", "pystray", "PIL.Image", "qrcode", "truststore"):
            try:
                __import__(mod)
                result[f"import_{mod}"] = True
            except Exception as exc:  # noqa: BLE001
                result[f"import_{mod}"] = f"HATA: {exc}"
        if IS_WINDOWS:
            try:
                from webview.platforms import winforms  # noqa: F401

                result["webview_winforms"] = True
            except Exception as exc:  # noqa: BLE001
                result["webview_winforms"] = f"HATA: {exc}"
        checks = [result.get("server_started"), result.get("index_ok"), result.get("app_js_ok"), result.get("icon_exists")]
        if strict:
            checks += [v is True for k, v in result.items() if k.startswith("import_") or k == "webview_winforms"]
        result["ok"] = all(bool(c) for c in checks)
    except Exception as exc:  # noqa: BLE001
        result["error"] = repr(exc)
    text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if out:
        Path(out).write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0 if result.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    _ensure_streams()
    parser = argparse.ArgumentParser(description="CanBot masaüstü uygulaması")
    parser.add_argument("--minimized", action="store_true", help="Simge durumunda başla")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CANBOT_PORT", DEFAULT_PORT)))
    parser.add_argument("--local-only", action="store_true", help="Yalnızca bu bilgisayardan erişim (telefon kapalı)")
    parser.add_argument("--selftest", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--selftest-out", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--selftest-strict", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest(args.selftest_out, args.selftest_strict)

    from .engine import Engine
    from .logsetup import setup_logging
    from .settings import SecretStore, SettingsStore, data_dir
    from .storage import Storage
    from .web.app import create_app

    setup_logging(console=False)

    # Tek kopya: CanBot zaten çalışıyorsa onun penceresini öne getir.
    for port in range(args.port, args.port + 10):
        if running_instance(port):
            try:
                import httpx

                httpx.post(f"http://127.0.0.1:{port}/api/desktop/show", timeout=3)
            except Exception:  # noqa: BLE001
                pass
            return 0
    port = next((p for p in range(args.port, args.port + 10) if port_free(p)), None)
    if port is None:
        log.error("Boş port bulunamadı")
        return 1

    root = data_dir()
    secrets = SecretStore(root / "secrets.json")
    ctrl = DesktopController(root / "desktop.json")
    ctrl.secrets = secrets
    ctrl.port = port

    def build_engine() -> Engine:
        engine = Engine(SettingsStore(root / "settings.json"), secrets, Storage(root / "canbot.db"))
        engine.listeners.append(ctrl.notify)
        return engine

    app = create_app(build_engine, port=port, desktop=ctrl)
    host = "127.0.0.1" if args.local_only else "0.0.0.0"
    server = ServerThread(app, host, port)
    ctrl.server = server
    server.start()
    if not server.wait_ready(45):
        log.error("Sunucu başlatılamadı: %s", server.error)
        return 1
    log.info("CanBot masaüstü başladı (port %s)", port)

    threading.Thread(target=ctrl.keep_awake_loop, name="canbot-awake", daemon=True).start()

    try:
        ctrl.tray = build_tray(ctrl, static_icon("icon-64.png"))
    except Exception:  # noqa: BLE001
        log.warning("Sistem tepsisi simgesi oluşturulamadı", exc_info=True)
        ctrl.tray = None

    try:
        import webview

        ctrl.window = webview.create_window(
            APP_NAME,
            ctrl.panel_url(),
            width=1320,
            height=860,
            min_size=(960, 640),
            background_color="#0b1020",
            hidden=args.minimized,
            text_select=True,
        )
        ctrl.window.events.closing += ctrl.on_window_closing
        if ctrl.tray is not None:
            try:
                ctrl.tray.run_detached()
            except Exception:  # noqa: BLE001
                threading.Thread(target=ctrl.tray.run, name="canbot-tray", daemon=True).start()
        webview.start(private_mode=False, storage_path=str(root / "webview"))
    except Exception:  # noqa: BLE001
        log.warning("Yerleşik pencere açılamadı, tarayıcı penceresi kullanılacak", exc_info=True)
        ctrl.window = None
        if not args.minimized:
            open_app_window(ctrl.panel_url())
        if ctrl.tray is not None and not ctrl.quitting:
            try:
                ctrl.tray.run()  # Çıkış seçilene kadar bekler
            except Exception:  # noqa: BLE001
                log.warning("Tepsi simgesi çalışmadı", exc_info=True)
                server.join()
        elif not ctrl.quitting:
            server.join()

    ctrl.quit()
    server.join(15)
    log.info("CanBot kapandı")
    return 0


if __name__ == "__main__":
    sys.exit(main())
