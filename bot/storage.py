"""SQLite kayıtları: işlemler, bakiye geçmişi, olaylar ve küçük anahtar-değer verileri."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    status TEXT NOT NULL,
    qty TEXT NOT NULL,
    leverage INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    entry_time INTEGER NOT NULL,
    stop_price REAL,
    initial_stop REAL,
    take_profit REAL,
    exit_price REAL,
    exit_time INTEGER,
    pnl REAL,
    fees REAL DEFAULT 0,
    score REAL,
    exit_reason TEXT,
    meta TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_trades_mode_status ON trades(mode, status);
CREATE TABLE IF NOT EXISTS equity (ts INTEGER NOT NULL, mode TEXT NOT NULL, equity REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_equity_mode_ts ON equity(mode, ts);
CREATE TABLE IF NOT EXISTS events (ts INTEGER NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class Trade:
    id: int
    mode: str
    symbol: str
    side: str
    status: str
    qty: str
    leverage: int
    entry_price: float
    entry_time: int
    stop_price: float | None
    initial_stop: float | None
    take_profit: float | None
    exit_price: float | None = None
    exit_time: int | None = None
    pnl: float | None = None
    fees: float = 0.0
    score: float | None = None
    exit_reason: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def direction(self) -> int:
        return 1 if self.side == "LONG" else -1

    @property
    def qty_f(self) -> float:
        return float(self.qty)

    @property
    def risk_per_unit(self) -> float:
        ref = self.initial_stop if self.initial_stop is not None else self.stop_price
        return abs(self.entry_price - ref) if ref is not None else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "mode": self.mode,
            "symbol": self.symbol,
            "side": self.side,
            "status": self.status,
            "qty": self.qty,
            "leverage": self.leverage,
            "entry_price": self.entry_price,
            "entry_time": self.entry_time,
            "stop_price": self.stop_price,
            "initial_stop": self.initial_stop,
            "take_profit": self.take_profit,
            "exit_price": self.exit_price,
            "exit_time": self.exit_time,
            "pnl": self.pnl,
            "fees": self.fees,
            "score": self.score,
            "exit_reason": self.exit_reason,
        }


class Storage:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, params)
            self._db.commit()
            return cur

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    # ------------------------------------------------------------- işlemler
    @staticmethod
    def _row_to_trade(row: sqlite3.Row) -> Trade:
        d = dict(row)
        d["meta"] = json.loads(d.get("meta") or "{}")
        return Trade(**d)

    def open_trade(
        self,
        *,
        mode: str,
        symbol: str,
        side: str,
        qty: str,
        leverage: int,
        entry_price: float,
        stop_price: float,
        take_profit: float | None,
        fees: float,
        score: float | None,
        meta: dict[str, Any] | None = None,
        entry_time: int | None = None,
    ) -> Trade:
        cur = self._exec(
            """INSERT INTO trades (mode, symbol, side, status, qty, leverage, entry_price, entry_time,
               stop_price, initial_stop, take_profit, fees, score, meta)
               VALUES (?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                mode,
                symbol,
                side,
                qty,
                leverage,
                entry_price,
                entry_time or now_ms(),
                stop_price,
                stop_price,
                take_profit,
                fees,
                score,
                json.dumps(meta or {}),
            ),
        )
        trade = self.get_trade(int(cur.lastrowid))
        assert trade is not None
        return trade

    def get_trade(self, trade_id: int) -> Trade | None:
        rows = self._query("SELECT * FROM trades WHERE id = ?", (trade_id,))
        return self._row_to_trade(rows[0]) if rows else None

    def open_trades(self, mode: str) -> list[Trade]:
        rows = self._query("SELECT * FROM trades WHERE mode = ? AND status = 'OPEN' ORDER BY id", (mode,))
        return [self._row_to_trade(r) for r in rows]

    def closed_trades(self, mode: str, limit: int = 50) -> list[Trade]:
        rows = self._query(
            "SELECT * FROM trades WHERE mode = ? AND status = 'CLOSED' ORDER BY exit_time DESC, id DESC LIMIT ?",
            (mode, limit),
        )
        return [self._row_to_trade(r) for r in rows]

    def update_trade(self, trade_id: int, **fields_: Any) -> None:
        if not fields_:
            return
        if "meta" in fields_:
            fields_["meta"] = json.dumps(fields_["meta"])
        cols = ", ".join(f"{k} = ?" for k in fields_)
        self._exec(f"UPDATE trades SET {cols} WHERE id = ?", (*fields_.values(), trade_id))

    def close_trade(
        self, trade_id: int, *, exit_price: float, pnl: float, fees: float, reason: str, exit_time: int | None = None
    ) -> Trade:
        self._exec(
            """UPDATE trades SET status = 'CLOSED', exit_price = ?, exit_time = ?, pnl = ?, fees = ?, exit_reason = ?
               WHERE id = ?""",
            (exit_price, exit_time or now_ms(), pnl, fees, reason, trade_id),
        )
        trade = self.get_trade(trade_id)
        assert trade is not None
        return trade

    def stats(self, mode: str, since_ms: int = 0) -> dict[str, Any]:
        rows = self._query(
            "SELECT pnl FROM trades WHERE mode = ? AND status = 'CLOSED' AND exit_time >= ?", (mode, since_ms)
        )
        pnls = [float(r["pnl"] or 0.0) for r in rows]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        gross_win = sum(wins)
        gross_loss = -sum(losses)
        return {
            "trades": len(pnls),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": (len(wins) / len(pnls) * 100.0) if pnls else 0.0,
            "pnl": sum(pnls),
            "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
            "avg_win": (gross_win / len(wins)) if wins else 0.0,
            "avg_loss": (-gross_loss / len(losses)) if losses else 0.0,
        }

    # ----------------------------------------------------------- bakiye geçmişi
    def add_equity(self, mode: str, equity: float, ts: int | None = None) -> None:
        self._exec("INSERT INTO equity (ts, mode, equity) VALUES (?, ?, ?)", (ts or now_ms(), mode, equity))

    def equity_curve(self, mode: str, since_ms: int = 0, points: int = 240) -> list[list[float]]:
        rows = self._query("SELECT ts, equity FROM equity WHERE mode = ? AND ts >= ? ORDER BY ts", (mode, since_ms))
        data = [[int(r["ts"]), float(r["equity"])] for r in rows]
        if len(data) <= points:
            return data
        step = len(data) / points
        sampled = [data[int(i * step)] for i in range(points)]
        sampled[-1] = data[-1]
        return sampled

    def prune_equity(self, keep_days: int = 120) -> None:
        self._exec("DELETE FROM equity WHERE ts < ?", (now_ms() - keep_days * 86_400_000,))

    def prune_events(self, keep_days: int = 90) -> None:
        self._exec("DELETE FROM events WHERE ts < ?", (now_ms() - keep_days * 86_400_000,))

    def clear_mode(self, mode: str) -> None:
        """Bir modun işlem ve bakiye geçmişini siler (sanal hesap sıfırlama)."""
        self._exec("DELETE FROM trades WHERE mode = ?", (mode,))
        self._exec("DELETE FROM equity WHERE mode = ?", (mode,))

    # ------------------------------------------------------------------ olaylar
    def add_event(self, level: str, message: str) -> None:
        self._exec("INSERT INTO events (ts, level, message) VALUES (?, ?, ?)", (now_ms(), level, message))

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._query("SELECT ts, level, message FROM events ORDER BY ts DESC, rowid DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # -------------------------------------------------------------- anahtar-değer
    def kv_get(self, key: str, default: Any = None) -> Any:
        rows = self._query("SELECT value FROM kv WHERE key = ?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def kv_set(self, key: str, value: Any) -> None:
        self._exec(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
