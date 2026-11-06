import asyncio

import pytest

import bot.engine as engine_mod
from bot.engine import Engine, select_universe
from bot.market import Candle, parse_exchange_info
from bot.settings import SecretStore, SettingsStore, TradingSettings
from bot.storage import Storage
from fakes import D1, FakeFuturesClient, breakout_ltf, make_candles

BOUNDARY = (1_790_000_000_000 // D1) * D1  # gece yarısı (4s ve 1g mumları burada kapanır)
NOW = BOUNDARY + 10_000  # mum kapanışından 10 sn sonra


def run(coro):
    return asyncio.run(coro)


def make_fake() -> FakeFuturesClient:
    fake = FakeFuturesClient()
    up = make_candles(320, D1, BOUNDARY - 1, 100.0, 0.006, 0.012, seed=11)
    fake.add_symbol("UPUSDT", up, breakout_ltf(200, BOUNDARY - 1, up[-1].close, +1))
    flat = make_candles(320, D1, BOUNDARY - 1, 50.0, 0.0, 0.002, seed=3)
    fake.add_symbol("FLATUSDT", flat, make_candles(200, 4 * 3600 * 1000, BOUNDARY - 1, flat[-1].close, 0, 0.003, 4))
    down = make_candles(320, D1, BOUNDARY - 1, 300.0, -0.006, 0.012, seed=12)
    fake.add_symbol("DOWNUSDT", down, breakout_ltf(200, BOUNDARY - 1, down[-1].close, -1))
    return fake


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_mod, "now_ms", lambda: NOW)
    fake = make_fake()
    store = SettingsStore(tmp_path / "settings.json")
    secrets = SecretStore(tmp_path / "secrets.json")
    storage = Storage(tmp_path / "db.sqlite")
    eng = Engine(store, secrets, storage, client_factory=lambda url, k, s: fake)
    yield eng, fake
    storage.close()


def test_scan_opens_trades_on_signals(env):
    eng, fake = env
    run(eng._setup_broker())
    eng.store.update({"auto_trade": True})
    run(eng.tick())
    assert {a.symbol for a in eng.scan} == {"UPUSDT", "FLATUSDT", "DOWNUSDT"}
    trades = {t.symbol: t for t in eng.storage.open_trades("paper")}
    assert set(trades) == {"UPUSDT", "DOWNUSDT"}
    up, down = trades["UPUSDT"], trades["DOWNUSDT"]
    assert up.side == "LONG" and up.stop_price < up.entry_price and up.take_profit is None
    assert down.side == "SHORT" and down.stop_price > down.entry_price
    # risk ≈ %1 bakiye
    risk = (up.entry_price - up.stop_price) * up.qty_f
    assert 7 <= risk <= 10.5
    m = eng.market_overview()
    assert m["up"] >= 1 and m["down"] >= 1 and m["signals"] == 2 and m["total"] == 3


def test_no_trades_when_auto_trade_off(env):
    eng, _ = env
    run(eng._setup_broker())
    run(eng.tick())
    assert eng.storage.open_trades("paper") == []
    assert "kapalı" in eng.status_text()


def test_stale_signal_not_traded(env, monkeypatch):
    eng, _ = env
    monkeypatch.setattr(engine_mod, "now_ms", lambda: BOUNDARY + 20 * 60_000)  # 20 dk sonra
    run(eng._setup_broker())
    eng.store.update({"auto_trade": True})
    run(eng.tick())
    assert eng.scan and eng.storage.open_trades("paper") == []


def test_stop_hit_closes_with_loss_and_cooldown(env):
    eng, fake = env
    run(eng._setup_broker())
    eng.store.update({"auto_trade": True})
    run(eng.tick())
    t = next(t for t in eng.storage.open_trades("paper") if t.symbol == "UPUSDT")
    fake.marks["UPUSDT"] = t.stop_price * 0.995
    run(eng.tick())
    closed = eng.storage.closed_trades("paper")
    assert closed and closed[0].symbol == "UPUSDT" and closed[0].pnl < 0
    assert closed[0].exit_reason == "Stop-loss"
    assert eng.cooldowns.get("UPUSDT", 0) > 0
    stats = eng.storage.stats("paper")
    assert stats["trades"] == 1 and stats["losses"] == 1


def test_trailing_stop_moves_up(env):
    eng, fake = env
    run(eng._setup_broker())
    eng.store.update({"auto_trade": True})
    run(eng.tick())
    t = next(t for t in eng.storage.open_trades("paper") if t.symbol == "UPUSDT")
    a = next(a for a in eng.scan if a.symbol == "UPUSDT")
    # Fiyat yükselmiş gibi: aşırı değer yüksel, iz süren stop yukarı taşınmalı
    meta = dict(t.meta)
    meta["extreme"] = t.entry_price * 1.2
    eng.storage.update_trade(t.id, meta=meta)
    fake.marks["UPUSDT"] = t.entry_price * 1.19
    eng.marks["UPUSDT"] = t.entry_price * 1.19
    old_candle = Candle(0, 1.0, 1.0, 1.0, 1.0, 1.0, 0)  # girişten önce kapanmış: hesaba katılmaz
    run(eng._trail_stops({"UPUSDT": a}, {"UPUSDT": [old_candle]}))
    t2 = eng.storage.get_trade(t.id)
    assert t2.stop_price > t.stop_price
    assert t2.meta.get("trailed") is True


def test_daily_loss_guard_blocks_entries(env):
    eng, _ = env
    run(eng._setup_broker())
    eng.store.update({"auto_trade": True})
    eng.storage.kv_set("day_start:paper", {"day": engine_mod.datetime.now(engine_mod.timezone.utc).strftime("%Y-%m-%d"), "equity": 2000})
    run(eng.tick())
    assert eng.guard_reason and "Günlük" in eng.guard_reason
    assert eng.storage.open_trades("paper") == []


def test_drawdown_halts_bot(env):
    eng, _ = env
    run(eng._setup_broker())
    eng.store.update({"auto_trade": True})
    eng.storage.kv_set("peak:paper", 5000)
    run(eng.tick())
    assert eng.halt_reason
    assert eng.settings.auto_trade is False


def test_mode_change_rules(env):
    eng, _ = env
    run(eng._setup_broker())
    with pytest.raises(ValueError, match="GERÇEK PARA"):
        run(eng.update_settings({"mode": "live"}))
    eng.store.update({"auto_trade": True})
    run(eng.tick())
    assert eng.storage.open_trades("paper")
    with pytest.raises(ValueError, match="açık pozisyon"):
        run(eng.update_settings({"mode": "testnet"}))
    run(eng.close_all())
    assert eng.storage.open_trades("paper") == []
    run(eng.update_settings({"mode": "testnet"}))
    assert eng.settings.mode == "testnet" and eng.settings.auto_trade is False
    assert eng.broker is None and "API anahtarı" in (eng.error or "")


def test_paper_funding_is_charged(env):
    eng, fake = env
    run(eng._setup_broker())
    eng.store.update({"auto_trade": True})
    run(eng.tick())
    t = next(t for t in eng.storage.open_trades("paper") if t.symbol == "UPUSDT")
    eng.storage.update_trade(t.id, entry_time=fake.next_funding - 1000)
    before = eng.broker.balance
    fake.next_funding += 8 * 3600 * 1000  # fonlama zamanı geçti
    run(eng._refresh_marks())
    t2 = eng.storage.get_trade(t.id)
    assert t2.meta["funding"] < 0  # pozitif oranda long öder
    short = next(x for x in eng.storage.open_trades("paper") if x.symbol == "DOWNUSDT")
    assert short.meta["funding"] > 0  # short alır
    assert eng.broker.balance == pytest.approx(before + t2.meta["funding"] + short.meta["funding"])


def test_coin_detail_and_explanation(env):
    eng, _ = env
    run(eng._setup_broker())
    run(eng.tick())
    d = run(eng.coin_detail("UPUSDT"))
    assert d["candles"] and len(d["candles"]) == len(d["ema"])
    assert any("YÜKSELİŞ" in x["text"] for x in d["explanation"])
    assert any("GİRİŞ SİNYALİ" in x["text"] for x in d["explanation"])
    with pytest.raises(ValueError):
        run(eng.coin_detail("NOPEUSDT"))


def test_select_universe_filters():
    fake = make_fake()
    infos = parse_exchange_info(run(fake.exchange_info()))
    tickers = run(fake.ticker_24h())
    s = TradingSettings(symbol_blacklist=["FLATUSDT"])
    assert select_universe(tickers, infos, s, NOW) == ["UPUSDT", "DOWNUSDT"]
    s2 = TradingSettings(symbol_whitelist=["DOWNUSDT"])
    assert select_universe(tickers, infos, s2, NOW) == ["DOWNUSDT"]
    s3 = TradingSettings(min_quote_volume_usdt=1e11)
    assert select_universe(tickers, infos, s3, NOW) == []


def test_snapshot_shape(env):
    eng, _ = env
    run(eng._setup_broker())
    eng.store.update({"auto_trade": True})
    run(eng.tick())
    snap = eng.snapshot()
    for key in ("bot", "account", "stats", "positions", "scanner", "trades", "equity_curve", "logs", "market", "backtest"):
        assert key in snap
    assert snap["bot"]["mode"] == "paper" and snap["positions"]
    assert snap["positions"][0]["mark_price"] is not None
