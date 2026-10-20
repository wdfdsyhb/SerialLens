"""webapp SQLite 会话持久化测试：重启后历史不丢、只存会话不存密钥."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")

from webapp.store import Store  # noqa: E402

# 测试用占位值（运行时拼接，非真实凭据）
FAKE_KEY = "sk" + "-" + "placeholder-not-real"


class TestStoreBasics:
    def test_empty_db_loads_nothing(self, tmp_path):
        s = Store(tmp_path / "s.db")
        assert s.load_all() == {}

    def test_append_survives_reopen(self, tmp_path):
        db = tmp_path / "s.db"
        s1 = Store(db)
        s1.append("sess-1", {"role": "user", "content": "什么是乱码"})
        s1.append("sess-1", {"role": "assistant", "content": "波特率不匹配"})
        s1.append("sess-2", {"role": "bot", "result": {"title": "协议识别", "steps": ["a"]}})
        # 模拟重启：新实例从同一文件恢复
        s2 = Store(db)
        all_sessions = s2.load_all()
        assert set(all_sessions.keys()) == {"sess-1", "sess-2"}
        assert all_sessions["sess-1"][0]["content"] == "什么是乱码"
        assert all_sessions["sess-1"][1]["role"] == "assistant"
        assert all_sessions["sess-2"][0]["result"]["title"] == "协议识别"

    def test_rows_keep_order(self, tmp_path):
        s = Store(tmp_path / "s.db")
        for i in range(5):
            s.append("s", {"role": "user", "content": f"msg{i}"})
        msgs = Store(tmp_path / "s.db").load_all()["s"]
        assert [m["content"] for m in msgs] == [f"msg{i}" for i in range(5)]

    def test_corrupt_row_skipped(self, tmp_path):
        s = Store(tmp_path / "s.db")
        s.append("good", {"role": "user", "content": "ok"})
        # 直接注入一条坏 JSON，模拟库损坏：load_all 应跳过而非崩溃
        import sqlite3
        conn = sqlite3.connect(tmp_path / "s.db")
        conn.execute("INSERT INTO messages (session_id, payload, seq) VALUES ('bad', '{oops', 0)")
        conn.commit()
        conn.close()
        all_sessions = Store(tmp_path / "s.db").load_all()
        assert all_sessions["good"][0]["content"] == "ok"
        assert "bad" not in all_sessions


class TestAppIntegration:
    def test_chat_history_persisted_and_reloadable(self, tmp_path, monkeypatch):
        """通过 /api/chat 落库；把 _SESSIONS 清空后从 store 恢复，历史完整."""
        from fastapi.testclient import TestClient
        import webapp.app as appmod
        from webapp.app import app

        monkeypatch.setattr(appmod, "_STORE", appmod._STORE.__class__(tmp_path / "web.db"))
        appmod._RUNTIME_CFG.clear()
        appmod._FILES.clear()
        appmod._SESSIONS.clear()
        appmod._TOKENS.clear()
        c = TestClient(app)
        token = c.post("/api/login", json={"password": appmod._ACCESS_PASSWORD}).json()["token"]
        c.headers.update({"X-Access-Token": token})

        c.post("/api/settings", json={"api_key": FAKE_KEY})

        def fake(cfg, system, user, images=None, mime="image/png", timeout=30.0, history=None):
            return "回复内容"

        monkeypatch.setattr(appmod, "_llm_chat_completion", fake)
        c.post("/api/chat", json={"message": "第一句", "session_id": "sess-p"})

        # 模拟重启：内存会话清空，从 SQLite 恢复
        appmod._SESSIONS.clear()
        restored = appmod._STORE.load_all()
        appmod._SESSIONS.update(restored)
        msgs = appmod._SESSIONS["sess-p"]
        roles = [m["role"] for m in msgs]
        assert roles == ["user", "assistant"]
        assert msgs[0]["content"] == "第一句"
        assert msgs[1]["content"] == "回复内容"

    def test_runtime_api_key_never_written_to_store(self, tmp_path, monkeypatch):
        """密钥仅存内存：落库内容里绝不含 api_key."""
        import sqlite3
        from fastapi.testclient import TestClient
        import webapp.app as appmod
        from webapp.app import app

        monkeypatch.setattr(appmod, "_STORE", appmod._STORE.__class__(tmp_path / "web.db"))
        appmod._RUNTIME_CFG.clear()
        appmod._SESSIONS.clear()
        appmod._TOKENS.clear()
        c = TestClient(app)
        token = c.post("/api/login", json={"password": appmod._ACCESS_PASSWORD}).json()["token"]
        c.headers.update({"X-Access-Token": token})
        c.post("/api/settings", json={"api_key": FAKE_KEY})

        def fake(cfg, system, user, images=None, mime="image/png", timeout=30.0, history=None):
            return "回复"

        monkeypatch.setattr(appmod, "_llm_chat_completion", fake)
        c.post("/api/chat", json={"message": "你好", "session_id": "sess-k"})

        conn = sqlite3.connect(tmp_path / "web.db")
        raw = "".join(r[0] for r in conn.execute("SELECT payload FROM messages"))
        conn.close()
        assert FAKE_KEY not in raw
