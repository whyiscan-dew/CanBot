import pytest
from fastapi.testclient import TestClient

from bot.engine import Engine
from bot.settings import SecretStore, SettingsStore
from bot.storage import Storage
from bot.web.app import create_app
from fakes import FakeFuturesClient, make_candles

H = 3600 * 1000


@pytest.fixture
def app(tmp_path):
    fake = FakeFuturesClient()
    fake.add_symbol("BTCUSDT", make_candles(320, 24 * H, 400 * 24 * H), make_candles(200, 4 * H, 400 * 24 * H))

    def builder():
        return Engine(
            SettingsStore(tmp_path / "settings.json"),
            SecretStore(tmp_path / "secrets.json"),
            Storage(tmp_path / "db.sqlite"),
            client_factory=lambda url, k, s: fake,
        )

    return create_app(builder, start_engine=False)


@pytest.fixture
def local(app):
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        yield c


@pytest.fixture
def remote(app):
    with TestClient(app, client=("192.168.1.50", 50000)) as c:
        yield c


def setup_password(client, pw="gizli123"):
    r = client.post("/api/auth/setup", json={"password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_static_pages(local):
    r = local.get("/")
    assert r.status_code == 200 and "CanBot" in r.text
    assert local.get("/manifest.webmanifest").headers["content-type"].startswith("application/manifest+json")
    assert local.get("/sw.js").status_code == 200
    assert local.get("/static/app.js").status_code == 200
    assert local.get("/favicon.ico").status_code == 200


def test_auth_flow(local):
    st = local.get("/api/auth/status").json()
    assert st == {"has_password": False, "can_setup": True}
    assert local.get("/api/state").status_code == 401
    auth = setup_password(local)
    assert local.post("/api/auth/setup", json={"password": "baskasi1"}).status_code == 409
    assert local.post("/api/auth/login", json={"password": "yanlis"}).status_code == 401
    assert local.post("/api/auth/login", json={"password": "gizli123"}).status_code == 200
    r = local.get("/api/state", headers=auth)
    assert r.status_code == 200
    body = r.json()
    for key in ("bot", "settings", "bounds", "keys", "positions", "scanner", "has_password"):
        assert key in body
    assert body["keys"]["live"]["set"] is False
    assert local.get("/api/state", headers={"Authorization": "Bearer bozuk.token"}).status_code == 401


def test_remote_cannot_create_first_password(remote):
    assert remote.get("/api/auth/status").json()["can_setup"] is False
    assert remote.post("/api/auth/setup", json={"password": "gizli123"}).status_code == 403


def test_login_rate_limit(local):
    setup_password(local)
    codes = [local.post("/api/auth/login", json={"password": "x"}).status_code for _ in range(11)]
    assert codes[-1] == 429


def test_password_change_invalidates_old_tokens(local):
    auth = setup_password(local)
    r = local.post("/api/auth/password", headers=auth, json={"old_password": "gizli123", "new_password": "yenisifre1"})
    assert r.status_code == 200
    assert local.get("/api/state", headers=auth).status_code == 401
    new_auth = {"Authorization": f"Bearer {r.json()['token']}"}
    assert local.get("/api/state", headers=new_auth).status_code == 200


def test_settings_validation_and_live_confirmation(local):
    auth = setup_password(local)
    r = local.put("/api/settings", headers=auth, json={"leverage": 50})
    assert r.status_code == 400 and "Kaldıraç" in r.json()["detail"]
    r = local.put("/api/settings", headers=auth, json={"risk_per_trade_pct": 0.5, "symbol_whitelist": "btc, eth"})
    assert r.status_code == 200
    assert r.json()["settings"]["symbol_whitelist"] == ["BTCUSDT", "ETHUSDT"]
    r = local.put("/api/settings", headers=auth, json={"mode": "live"})
    assert r.status_code == 400 and "GERÇEK PARA" in r.json()["detail"]


def test_keys_are_stored_masked(local):
    auth = setup_password(local)
    assert local.put("/api/keys", headers=auth, json={"mode": "live", "api_key": "kisa", "api_secret": "x"}).status_code == 400
    key = "A" * 30 + "WXYZ"
    r = local.put("/api/keys", headers=auth, json={"mode": "live", "api_key": key, "api_secret": "S" * 40})
    assert r.status_code == 200
    status = r.json()["keys"]["live"]
    assert status["set"] and status["api_key"] == "AAAA…WXYZ"
    state = local.get("/api/state", headers=auth).json()
    assert "S" * 40 not in str(state)  # gizli anahtar asla geri gönderilmez
    r = local.post("/api/keys/test", headers=auth, json={"mode": "live"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert any("PARA ÇEKME" in w for w in r.json()["warnings"])


def test_connect_info_and_analysis(local):
    auth = setup_password(local)
    c = local.get("/api/connect", headers=auth).json()
    assert c["url"].startswith("http://") and "<svg" in c["qr_svg"]
    assert local.get("/api/analysis/bad-symbol!", headers=auth).status_code in (400, 404)


def test_desktop_endpoints_without_desktop(local, remote):
    auth = setup_password(local)
    assert local.put("/api/desktop", headers=auth, json={"keep_awake": True}).status_code == 400
    assert local.post("/api/desktop/show").json()["ok"] is False
    assert remote.post("/api/desktop/show").status_code == 403


def test_host_and_origin_protection(local):
    from bot.web.app import host_allowed

    assert host_allowed("127.0.0.1:8765") and host_allowed("localhost:8765") and host_allowed("[::1]:8765")
    assert host_allowed("192.168.1.20:8765") and host_allowed("masaustu:8765") and host_allowed("pc.tailnet.ts.net")
    assert not host_allowed("evil.example.com:8765") and not host_allowed("")
    # DNS rebinding: yabancı alan adıyla gelen istek reddedilir
    assert local.get("/api/health", headers={"Host": "evil.example.com:8765"}).status_code == 400
    # Başka siteden gelen değiştirici istek reddedilir
    r = local.post("/api/auth/setup", json={"password": "gizli123"}, headers={"Origin": "http://evil.example.com"})
    assert r.status_code == 403
    r = local.post("/api/auth/setup", json={"password": "gizli123"}, headers={"Origin": "http://testserver"})
    assert r.status_code == 200
