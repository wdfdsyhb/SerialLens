"""SQLite 会话持久化：服务重启后对话历史可恢复.

设计原则：
- 只存会话消息（user/assistant/bot result），API key 等敏感配置绝不落盘
- 单文件、零依赖（标准库 sqlite3），WAL 模式提高并发下的读写表现
- 损坏行跳过不崩溃：演示现场容错优先
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path


class Store:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS messages ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " session_id TEXT NOT NULL,"
            " payload TEXT NOT NULL,"
            " seq INTEGER NOT NULL)"
        )
        self._conn.commit()

    def append(self, session_id: str, message: dict) -> None:
        """追加一条消息到会话（跨线程安全）."""
        payload = json.dumps(message, ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages (session_id, payload, seq) VALUES (?, ?, ?)",
                (session_id, payload, self._next_seq(session_id)),
            )
            self._conn.commit()

    def _next_seq(self, session_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 FROM messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return int(row[0])

    def load_all(self) -> dict[str, list[dict]]:
        """加载全部会话：session_id → [message]。坏行所在的会话整体丢弃."""
        sessions: dict[str, list[dict]] = {}
        bad: set[str] = set()
        with self._lock:
            rows = self._conn.execute(
                "SELECT session_id, payload FROM messages ORDER BY session_id, seq, id"
            ).fetchall()
        for sid, payload in rows:
            if sid in bad:
                continue
            try:
                sessions.setdefault(sid, []).append(json.loads(payload))
            except json.JSONDecodeError:
                bad.add(sid)
                sessions.pop(sid, None)  # 损坏会话整体丢弃：演示稳定性优先
        return sessions
