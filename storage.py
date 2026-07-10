from __future__ import annotations

import json
from contextlib import contextmanager
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
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _connection(self):
        """SQLite context that commits/rolls back and always closes the connection."""
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self.lock, self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    chat_id INTEGER PRIMARY KEY,
                    timeframe TEXT NOT NULL DEFAULT '1h',
                    visualization TEXT NOT NULL DEFAULT 'combined',
                    stakan_enabled INTEGER NOT NULL DEFAULT 0,
                    bot_enabled INTEGER NOT NULL DEFAULT 1,
                    monitor_interval_minutes INTEGER NOT NULL DEFAULT 30
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
                CREATE TABLE IF NOT EXISTS monitor_state (
                    chat_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    next_check_at INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at INTEGER,
                    last_success_at INTEGER,
                    last_error TEXT,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(chat_id, symbol)
                );
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
            if "bot_enabled" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN bot_enabled INTEGER NOT NULL DEFAULT 1")
            if "monitor_interval_minutes" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN monitor_interval_minutes INTEGER NOT NULL DEFAULT 30")

    def ensure_user(self, chat_id: int) -> None:
        with self.lock, self._connection() as conn:
            conn.execute("INSERT OR IGNORE INTO users(chat_id) VALUES(?)", (chat_id,))

    def get_user(self, chat_id: int) -> dict[str, Any]:
        self.ensure_user(chat_id)
        with self.lock, self._connection() as conn:
            row = conn.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()
            return dict(row)

    def set_timeframe(self, chat_id: int, timeframe: str) -> None:
        self.ensure_user(chat_id)
        with self.lock, self._connection() as conn:
            conn.execute("UPDATE users SET timeframe=? WHERE chat_id=?", (timeframe, chat_id))

    def set_visualization(self, chat_id: int, mode: str) -> None:
        self.ensure_user(chat_id)
        with self.lock, self._connection() as conn:
            conn.execute("UPDATE users SET visualization=? WHERE chat_id=?", (mode, chat_id))

    def set_stakan(self, chat_id: int, enabled: bool) -> None:
        self.ensure_user(chat_id)
        with self.lock, self._connection() as conn:
            conn.execute("UPDATE users SET stakan_enabled=? WHERE chat_id=?", (1 if enabled else 0, chat_id))
        if enabled:
            self.schedule_monitor_now(chat_id)

    def set_bot_enabled(self, chat_id: int, enabled: bool) -> None:
        self.ensure_user(chat_id)
        with self.lock, self._connection() as conn:
            conn.execute("UPDATE users SET bot_enabled=? WHERE chat_id=?", (1 if enabled else 0, chat_id))
        if enabled:
            self.schedule_monitor_now(chat_id)

    def set_monitor_interval(self, chat_id: int, minutes: int) -> None:
        self.ensure_user(chat_id)
        with self.lock, self._connection() as conn:
            conn.execute("UPDATE users SET monitor_interval_minutes=? WHERE chat_id=?", (minutes, chat_id))
        self.schedule_monitor_now(chat_id)

    def add_symbol(self, chat_id: int, symbol: str) -> None:
        self.ensure_user(chat_id)
        with self.lock, self._connection() as conn:
            conn.execute("INSERT OR IGNORE INTO watchlist(chat_id, symbol) VALUES(?,?)", (chat_id, symbol))
            conn.execute(
                "INSERT INTO monitor_state(chat_id, symbol, next_check_at) VALUES(?,?,0) "
                "ON CONFLICT(chat_id, symbol) DO UPDATE SET next_check_at=0, last_error=NULL",
                (chat_id, symbol),
            )

    def del_symbol(self, chat_id: int, symbol: str) -> None:
        with self.lock, self._connection() as conn:
            conn.execute("DELETE FROM watchlist WHERE chat_id=? AND symbol=?", (chat_id, symbol))
            conn.execute("DELETE FROM orderbook_snapshots WHERE chat_id=? AND symbol=?", (chat_id, symbol))
            conn.execute("DELETE FROM monitor_state WHERE chat_id=? AND symbol=?", (chat_id, symbol))

    def clear_symbols(self, chat_id: int) -> None:
        with self.lock, self._connection() as conn:
            conn.execute("DELETE FROM watchlist WHERE chat_id=?", (chat_id,))
            conn.execute("DELETE FROM orderbook_snapshots WHERE chat_id=?", (chat_id,))
            conn.execute("DELETE FROM monitor_state WHERE chat_id=?", (chat_id,))

    def replace_symbols(self, chat_id: int, symbols: list[str]) -> None:
        self.ensure_user(chat_id)
        with self.lock, self._connection() as conn:
            conn.execute("DELETE FROM watchlist WHERE chat_id=?", (chat_id,))
            conn.execute("DELETE FROM monitor_state WHERE chat_id=?", (chat_id,))
            conn.execute("DELETE FROM orderbook_snapshots WHERE chat_id=?", (chat_id,))
            conn.executemany("INSERT OR IGNORE INTO watchlist(chat_id, symbol) VALUES(?,?)", [(chat_id, s) for s in symbols])
            conn.executemany("INSERT OR IGNORE INTO monitor_state(chat_id, symbol, next_check_at) VALUES(?,?,0)", [(chat_id, s) for s in symbols])

    def list_symbols(self, chat_id: int) -> list[str]:
        with self.lock, self._connection() as conn:
            rows = conn.execute("SELECT symbol FROM watchlist WHERE chat_id=? ORDER BY symbol", (chat_id,)).fetchall()
            return [r["symbol"] for r in rows]

    def enabled_watchlists(self) -> list[tuple[int, list[str]]]:
        with self.lock, self._connection() as conn:
            users = conn.execute("SELECT chat_id FROM users WHERE stakan_enabled=1 AND bot_enabled=1").fetchall()
            result: list[tuple[int, list[str]]] = []
            for user in users:
                rows = conn.execute("SELECT symbol FROM watchlist WHERE chat_id=? ORDER BY symbol", (user["chat_id"],)).fetchall()
                result.append((int(user["chat_id"]), [r["symbol"] for r in rows]))
            return result

    def get_snapshot(self, chat_id: int, symbol: str) -> dict[str, Any] | None:
        with self.lock, self._connection() as conn:
            row = conn.execute(
                "SELECT snapshot_json FROM orderbook_snapshots WHERE chat_id=? AND symbol=?",
                (chat_id, symbol),
            ).fetchone()
            return json.loads(row["snapshot_json"]) if row else None

    def get_snapshot_meta(self, chat_id: int, symbol: str) -> tuple[dict[str, Any] | None, int | None]:
        with self.lock, self._connection() as conn:
            row = conn.execute(
                "SELECT snapshot_json, updated_at FROM orderbook_snapshots WHERE chat_id=? AND symbol=?",
                (chat_id, symbol),
            ).fetchone()
            return (json.loads(row["snapshot_json"]), int(row["updated_at"])) if row else (None, None)

    def set_snapshot(self, chat_id: int, symbol: str, snapshot: dict[str, Any], updated_at: int) -> None:
        with self.lock, self._connection() as conn:
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

    def schedule_monitor_now(self, chat_id: int, symbol: str | None = None) -> None:
        with self.lock, self._connection() as conn:
            if symbol is not None:
                symbols = [symbol]
            else:
                rows = conn.execute("SELECT symbol FROM watchlist WHERE chat_id=?", (chat_id,)).fetchall()
                symbols = [r["symbol"] for r in rows]
            conn.executemany(
                "INSERT INTO monitor_state(chat_id, symbol, next_check_at) VALUES(?,?,0) "
                "ON CONFLICT(chat_id, symbol) DO UPDATE SET next_check_at=0, last_error=NULL",
                [(chat_id, item) for item in symbols],
            )

    def get_monitor_state(self, chat_id: int, symbol: str) -> dict[str, Any]:
        with self.lock, self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM monitor_state WHERE chat_id=? AND symbol=?",
                (chat_id, symbol),
            ).fetchone()
            if row:
                return dict(row)
            conn.execute("INSERT OR IGNORE INTO monitor_state(chat_id, symbol, next_check_at) VALUES(?,?,0)", (chat_id, symbol))
            return {
                "chat_id": chat_id, "symbol": symbol, "next_check_at": 0,
                "last_attempt_at": None, "last_success_at": None, "last_error": None,
                "consecutive_failures": 0,
            }

    def mark_monitor_attempt(self, chat_id: int, symbol: str, timestamp: int) -> None:
        with self.lock, self._connection() as conn:
            conn.execute(
                "INSERT INTO monitor_state(chat_id, symbol, next_check_at, last_attempt_at) VALUES(?,?,0,?) "
                "ON CONFLICT(chat_id, symbol) DO UPDATE SET last_attempt_at=excluded.last_attempt_at",
                (chat_id, symbol, timestamp),
            )

    def mark_monitor_success(self, chat_id: int, symbol: str, timestamp: int, next_check_at: int) -> None:
        with self.lock, self._connection() as conn:
            conn.execute(
                """
                INSERT INTO monitor_state(chat_id, symbol, next_check_at, last_attempt_at, last_success_at, last_error, consecutive_failures)
                VALUES(?,?,?,?,?,NULL,0)
                ON CONFLICT(chat_id, symbol) DO UPDATE SET
                    next_check_at=excluded.next_check_at,
                    last_attempt_at=excluded.last_attempt_at,
                    last_success_at=excluded.last_success_at,
                    last_error=NULL,
                    consecutive_failures=0
                """,
                (chat_id, symbol, next_check_at, timestamp, timestamp),
            )

    def mark_monitor_failure(self, chat_id: int, symbol: str, timestamp: int, retry_at: int, error: str) -> int:
        with self.lock, self._connection() as conn:
            row = conn.execute(
                "SELECT consecutive_failures FROM monitor_state WHERE chat_id=? AND symbol=?",
                (chat_id, symbol),
            ).fetchone()
            failures = (int(row["consecutive_failures"]) if row else 0) + 1
            conn.execute(
                """
                INSERT INTO monitor_state(chat_id, symbol, next_check_at, last_attempt_at, last_error, consecutive_failures)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(chat_id, symbol) DO UPDATE SET
                    next_check_at=excluded.next_check_at,
                    last_attempt_at=excluded.last_attempt_at,
                    last_error=excluded.last_error,
                    consecutive_failures=excluded.consecutive_failures
                """,
                (chat_id, symbol, retry_at, timestamp, error[:1500], failures),
            )
            return failures

    def monitor_summary(self, chat_id: int) -> dict[str, Any]:
        with self.lock, self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM monitor_state WHERE chat_id=? ORDER BY COALESCE(last_attempt_at, 0) DESC",
                (chat_id,),
            ).fetchall()
            if not rows:
                return {"last_success_at": None, "next_check_at": None, "last_error": None, "failures": 0}
            last_success = max((int(r["last_success_at"]) for r in rows if r["last_success_at"] is not None), default=None)
            next_check = min((int(r["next_check_at"]) for r in rows), default=None)
            error_row = next((r for r in rows if r["last_error"]), None)
            return {
                "last_success_at": last_success,
                "next_check_at": next_check,
                "last_error": str(error_row["last_error"]) if error_row else None,
                "failures": sum(int(r["consecutive_failures"] or 0) for r in rows),
            }
