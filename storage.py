from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any


class Storage:
    def __init__(self, path: str = "bot_data.sqlite3"):
        self.path = Path(path)
        self.lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self.lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    chat_id INTEGER PRIMARY KEY,
                    timeframe TEXT NOT NULL DEFAULT '1h',
                    visualization TEXT NOT NULL DEFAULT 'combined',
                    stakan_enabled INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS watchlist (
                    chat_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    PRIMARY KEY(chat_id, symbol)
                );
                CREATE TABLE IF NOT EXISTS orderbook_snapshots (
                    chat_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(chat_id, symbol)
                );
                """
            )

    def ensure_user(self, chat_id: int) -> None:
        with self.lock, self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO users(chat_id) VALUES(?)", (chat_id,))

    def get_user(self, chat_id: int) -> dict[str, Any]:
        self.ensure_user(chat_id)
        with self.lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()
            return dict(row)

    def set_timeframe(self, chat_id: int, timeframe: str) -> None:
        self.ensure_user(chat_id)
        with self.lock, self._connect() as conn:
            conn.execute("UPDATE users SET timeframe=? WHERE chat_id=?", (timeframe, chat_id))

    def set_visualization(self, chat_id: int, mode: str) -> None:
        self.ensure_user(chat_id)
        with self.lock, self._connect() as conn:
            conn.execute("UPDATE users SET visualization=? WHERE chat_id=?", (mode, chat_id))

    def set_stakan(self, chat_id: int, enabled: bool) -> None:
        self.ensure_user(chat_id)
        with self.lock, self._connect() as conn:
            conn.execute("UPDATE users SET stakan_enabled=? WHERE chat_id=?", (1 if enabled else 0, chat_id))

    def add_symbol(self, chat_id: int, symbol: str) -> None:
        self.ensure_user(chat_id)
        with self.lock, self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO watchlist(chat_id, symbol) VALUES(?,?)", (chat_id, symbol))

    def del_symbol(self, chat_id: int, symbol: str) -> None:
        with self.lock, self._connect() as conn:
            conn.execute("DELETE FROM watchlist WHERE chat_id=? AND symbol=?", (chat_id, symbol))
            conn.execute("DELETE FROM orderbook_snapshots WHERE chat_id=? AND symbol=?", (chat_id, symbol))

    def list_symbols(self, chat_id: int) -> list[str]:
        with self.lock, self._connect() as conn:
            rows = conn.execute("SELECT symbol FROM watchlist WHERE chat_id=? ORDER BY symbol", (chat_id,)).fetchall()
            return [r["symbol"] for r in rows]

    def enabled_watchlists(self) -> list[tuple[int, list[str]]]:
        with self.lock, self._connect() as conn:
            users = conn.execute("SELECT chat_id FROM users WHERE stakan_enabled=1").fetchall()
            result: list[tuple[int, list[str]]] = []
            for user in users:
                result.append((user["chat_id"], self.list_symbols(user["chat_id"])))
            return result

    def get_snapshot(self, chat_id: int, symbol: str) -> dict[str, Any] | None:
        with self.lock, self._connect() as conn:
            row = conn.execute(
                "SELECT snapshot_json FROM orderbook_snapshots WHERE chat_id=? AND symbol=?",
                (chat_id, symbol),
            ).fetchone()
            return json.loads(row["snapshot_json"]) if row else None

    def set_snapshot(self, chat_id: int, symbol: str, snapshot: dict[str, Any], updated_at: int) -> None:
        with self.lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO orderbook_snapshots(chat_id, symbol, snapshot_json, updated_at)
                VALUES(?,?,?,?)
                ON CONFLICT(chat_id, symbol) DO UPDATE SET
                    snapshot_json=excluded.snapshot_json,
                    updated_at=excluded.updated_at
                """,
                (chat_id, symbol, json.dumps(snapshot), updated_at),
            )
