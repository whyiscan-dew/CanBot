import asyncio
from decimal import Decimal

import pytest

from bot.binance_client import BinanceAPIError
from bot.brokers import LiveBroker, PaperBroker, connect_live_broker
from bot.market import SymbolInfo
from bot.storage import Storage
from fakes import FakeFuturesClient, make_candles, symbol_info_raw

H = 3600 * 1000


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def storage(tmp_path):
    s = Storage(tmp_path / "t.db")
    yield s
    s.close()


@pytest.fixture
def info():
    return SymbolInfo.from_exchange_info(symbol_info_raw("ABCUSDT", tick="0.01", step="0.001"))


def open_trade(storage, broker_fill, side="LONG", stop=95.0, tp=None, mode="paper", meta=None):
    return storage.open_trade(
        mode=mode,
        symbol="ABCUSDT",
        side=side,
        qty=str(broker_fill.qty),
        leverage=5,
        entry_price=broker_fill.price,
        stop_price=stop,
        take_profit=tp,
        fees=broker_fill.fee,
        score=70,
        meta={**broker_fill.meta, **(meta or {})},
    )


def test_paper_open_close_and_fees(storage, info):
    b = PaperBroker(storage, 1000, slippage=0.0)
    fill = run(b.open_position(info, "LONG", Decimal("1"), 5, "ISOLATED", 95.0, None, 100.0))
    assert fill.price == 100.0 and fill.fee == pytest.approx(0.05)
    assert b.balance == pytest.approx(999.95)
    t = open_trade(storage, fill)
    acc = run(b.account([t], {"ABCUSDT": 110.0}))
    assert acc.unrealized == pytest.approx(10.0)
    assert acc.equity == pytest.approx(1009.95)
    close = run(b.close_position(info, t, 110.0))
    assert close.realized_pnl == pytest.approx(10.0)
    assert b.balance == pytest.approx(999.95 + 10 - 110 * 0.0005)
    # bakiye kalıcı
    assert PaperBroker(storage, 5).balance == pytest.approx(b.balance)


def test_paper_detects_stop_and_target(storage, info):
    b = PaperBroker(storage, 1000, slippage=0.0)
    f1 = run(b.open_position(info, "LONG", Decimal("1"), 5, "ISOLATED", 95.0, 120.0, 100.0))
    t1 = open_trade(storage, f1, stop=95.0, tp=120.0)
    assert run(b.detect_closed([t1], {"ABCUSDT": 100.0})) == []
    closed = run(b.detect_closed([t1], {"ABCUSDT": 94.0}))
    assert closed[0].reason == "Stop-loss" and closed[0].realized_pnl == pytest.approx(-6.0)
    t2 = open_trade(storage, f1, side="SHORT", stop=105.0, tp=90.0)
    closed = run(b.detect_closed([t2], {"ABCUSDT": 89.0}))
    assert closed[0].reason == "Kâr hedefi"
    t3 = open_trade(storage, f1, stop=101.0, meta={"trailed": True})
    closed = run(b.detect_closed([t3], {"ABCUSDT": 100.5}))
    assert closed[0].reason == "İz süren stop"


def test_paper_rejects_when_balance_too_small(storage, info):
    b = PaperBroker(storage, 10)
    with pytest.raises(RuntimeError):
        run(b.open_position(info, "LONG", Decimal("10"), 1, "ISOLATED", 95.0, None, 100.0))


def live(fake=None):
    fake = fake or FakeFuturesClient()
    fake.add_symbol("ABCUSDT", make_candles(10, H, 10 * H), make_candles(10, H, 10 * H, 100.0))
    fake.marks["ABCUSDT"] = 100.0
    return LiveBroker(fake, "testnet"), fake


def test_live_open_places_exchange_stop(storage, info):
    b, fake = live()
    fill = run(b.open_position(info, "LONG", Decimal("2"), 5, "ISOLATED", 95.004, 120.0, 100.0))
    assert fill.price == 100.0 and fill.qty == Decimal("2")
    algos = [p for kind, p in fake.calls if kind == "algo"]
    assert len(algos) == 2
    sl, tp = algos
    assert sl["type"] == "STOP_MARKET" and sl["side"] == "SELL" and sl["closePosition"] is True
    assert sl["workingType"] == "MARK_PRICE"
    assert sl["triggerPrice"] == Decimal("95.01")  # long stop yukarı yuvarlanır
    assert tp["type"] == "TAKE_PROFIT_MARKET" and tp["triggerPrice"] == Decimal("120.00")
    assert fill.meta["sl_kind"] == "algo" and fill.meta["tp_kind"] == "algo"
    assert ("marginType", {"symbol": "ABCUSDT", "marginType": "ISOLATED"}) in fake.calls


def test_live_open_closes_position_if_stop_fails(storage, info):
    b, fake = live()
    fake.fail_algo = True
    with pytest.raises(RuntimeError, match="stop-loss konulamadı"):
        run(b.open_position(info, "LONG", Decimal("2"), 5, "ISOLATED", 95.0, None, 100.0))
    assert fake.positions["ABCUSDT"]["amt"] == 0  # güvenlik için kapatıldı


def test_live_falls_back_to_classic_orders(storage, info):
    b, fake = live()
    fake.algo_missing = True
    fill = run(b.open_position(info, "SHORT", Decimal("1"), 5, "ISOLATED", 105.0, None, 100.0))
    classic = [p for kind, p in fake.calls if kind == "order" and p.get("type") == "STOP_MARKET"]
    assert classic and classic[0]["stopPrice"] == Decimal("105.00") and classic[0]["side"] == "BUY"
    assert fill.meta["sl_kind"] == "order"


def test_live_close_and_detect(storage, info):
    b, fake = live()
    fill = run(b.open_position(info, "LONG", Decimal("1"), 5, "ISOLATED", 95.0, None, 100.0))
    t = open_trade(storage, fill, mode="testnet")
    fake.marks["ABCUSDT"] = 110.0
    out = run(b.close_position(info, t, 110.0))
    assert out.price == 110.0 and out.realized_pnl == pytest.approx(10.0)
    assert fake.positions["ABCUSDT"]["amt"] == 0
    assert not fake.algo_orders  # kalan stop iptal edildi

    for row in fake.trades:  # önceki işlemin dolumları 1 dk önceymiş gibi
        row["time"] -= 60_000
    fill2 = run(b.open_position(info, "LONG", Decimal("1"), 5, "ISOLATED", 95.0, None, 100.0))
    t2 = open_trade(storage, fill2, mode="testnet")
    assert run(b.detect_closed([t2], fake.marks)) == []  # yeni açılan pozisyon 5 sn kontrol edilmez
    storage.update_trade(t2.id, entry_time=t2.entry_time - 10_000)
    t2 = storage.get_trade(t2.id)
    assert run(b.detect_closed([t2], fake.marks)) == []  # pozisyon hâlâ açık
    fake.trigger_stop("ABCUSDT", 95.0)
    closed = run(b.detect_closed([t2], fake.marks))
    assert len(closed) == 1 and closed[0].reason == "Stop-loss"
    assert closed[0].realized_pnl == pytest.approx(-15.0)  # 110'dan açıldı, 95'te kapandı


def test_live_move_stop(storage, info):
    b, fake = live()
    fill = run(b.open_position(info, "LONG", Decimal("1"), 5, "ISOLATED", 95.0, None, 100.0))
    t = open_trade(storage, fill, mode="testnet")
    old = t.meta["sl_id"]
    meta = run(b.move_stop(info, t, 99.0))
    assert meta["sl_id"] != old and old not in fake.algo_orders
    assert fake.algo_orders[meta["sl_id"]]["triggerPrice"] == Decimal("99.00")


def test_connect_live_tries_testnet_urls():
    tried = []

    def factory(url, key, secret):
        tried.append(url)
        f = FakeFuturesClient(url, key, secret)
        if "demo-fapi" in url:
            f.fail_connect = BinanceAPIError(451, None, "restricted")
        return f

    broker = run(connect_live_broker("testnet", "k" * 20, "s" * 20, "auto", factory))
    assert tried == ["https://demo-fapi.binance.com", "https://testnet.binancefuture.com"]
    assert broker.client.base_url == "https://testnet.binancefuture.com"


def test_connect_live_requires_keys():
    with pytest.raises(RuntimeError, match="API anahtarı"):
        run(connect_live_broker("live", "", "", "auto", lambda u, k, s: FakeFuturesClient(u)))


def test_orphan_bot_stops_are_swept(storage, info):
    b, fake = live()
    fake.algo_orders[1] = {"kind": "algo", "symbol": "ABCUSDT", "clientAlgoId": "canbot-sl-1", "type": "STOP_MARKET"}
    fake.algo_orders[2] = {"kind": "algo", "symbol": "ABCUSDT", "clientAlgoId": "elle-kullanici", "type": "STOP_MARKET"}
    run(b.open_position(info, "LONG", Decimal("1"), 5, "ISOLATED", 95.0, None, 100.0))
    assert 1 not in fake.algo_orders  # botun eski emri temizlendi
    assert 2 in fake.algo_orders  # kullanıcının emrine dokunulmadı
    new_sl = [p for kind, p in fake.calls if kind == "algo"][-1]
    assert new_sl["clientAlgoId"].startswith("canbot-sl-")


def test_move_stop_keeps_old_stop_when_cancel_fails(storage, info):
    from bot.brokers import StopMoveError

    b, fake = live()
    fill = run(b.open_position(info, "LONG", Decimal("1"), 5, "ISOLATED", 95.0, None, 100.0))
    t = open_trade(storage, fill, mode="testnet")

    async def broken_cancel(algo_id):
        raise BinanceAPIError(0, None, "Bağlantı hatası")

    fake.cancel_algo_order = broken_cancel
    with pytest.raises(StopMoveError) as exc:
        run(b.move_stop(info, t, 99.0))
    assert exc.value.meta["sl_id"] == t.meta["sl_id"]
    assert len([o for o in fake.algo_orders.values() if o.get("type") == "STOP_MARKET"]) == 1


def test_entry_timeout_adopts_open_position(storage, info):
    b, fake = live()
    real_new_order = fake.new_order

    async def flaky(**params):
        if params.get("type") == "MARKET":
            await real_new_order(**params)  # emir borsada gerçekleşti...
            raise BinanceAPIError(0, None, "Bağlantı hatası")  # ...ama yanıt gelmedi
        return await real_new_order(**params)

    fake.new_order = flaky
    fill = run(b.open_position(info, "LONG", Decimal("1"), 5, "ISOLATED", 95.0, None, 100.0))
    assert fill.qty == Decimal("1.000") and fill.meta.get("sl_id")  # stop konuldu
