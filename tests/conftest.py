from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    """Her test kendi geçici veri klasörünü kullanır; ortam anahtarları temizlenir."""
    monkeypatch.setenv("CANBOT_DATA_DIR", str(tmp_path / "data"))
    for key in (
        "BINANCE_API_KEY",
        "BINANCE_API_SECRET",
        "BINANCE_TESTNET_API_KEY",
        "BINANCE_TESTNET_API_SECRET",
        "DASHBOARD_PASSWORD",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "CANBOT_DATA_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    yield tmp_path / "data"
