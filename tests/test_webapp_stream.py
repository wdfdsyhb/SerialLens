"""webapp 会话上下文与 SSE 流式测试."""

from __future__ import annotations

import json

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import webapp.app as appmod
from webapp.app import app


@pytest.fixture()
def client():
    appmod._RUNTIME_CFG.clear()
    appmod._FILES.clear()
    appmod._SESSIONS.clear()
    c = TestClient(app)
    token = c.post("/api/login", json={"password": appmod._ACCESS_PASSWORD}).json()["token"]
    c.headers.update({"X-Access-Token": token})
    return c


class TestSessionContext:
    def test_history_reaches_llm_on_second_turn(self, client, monkeypatch):
        client.post("/api/settings", json={"api_key": "sk-x"})
        seen = []

        def fake(cfg, system, user, images=None, mime="image/png", timeout=30.0, history=None):
            seen.append({"user": user, "history": history})
            return "第一轮回复" if len(seen) == 1 else "第二轮回复"

        monkeypatch.setattr(appmod, "_llm_chat_completion", fake)

        client.post("/api/chat", json={"message": "什么是串口乱码", "session_id": "sess-1"})
        resp = client.post("/api/chat", json={"message": "那怎么排查？", "session_id": "sess-1"})
        assert resp.json()["reply"] == "第二轮回复"
        assert seen[1]["history"] is not None
        hist = seen[1]["history"]
        roles = [m["role"] for m in hist]
        assert "user" in roles and "assistant" in roles
        assert "串口乱码" in json.dumps(hist, ensure_ascii=False)

    def test_sessions_are_isolated(self, client, monkeypatch):
        client.post("/api/settings", json={"api_key": "sk-x"})
        seen = []

        def fake(cfg, system, user, images=None, mime="image/png", timeout=30.0, history=None):
            seen.append({"user": user, "history": history or []})
            return "ok"

        monkeypatch.setattr(appmod, "_llm_chat_completion", fake)
        client.post("/api/chat", json={"message": "话题A", "session_id": "sess-A"})
        client.post("/api/chat", json={"message": "话题B", "session_id": "sess-B"})
        assert seen[1]["history"] == []          # B 会话不受 A 污染

    def test_history_truncated_to_recent(self, client, monkeypatch):
        client.post("/api/settings", json={"api_key": "sk-x"})
        seen = []

        def fake(cfg, system, user, images=None, mime="image/png", timeout=30.0, history=None):
            seen.append(history or [])
            return "r"

        monkeypatch.setattr(appmod, "_llm_chat_completion", fake)
        for i in range(8):
            client.post("/api/chat", json={"message": f"第{i}句", "session_id": "sess-T"})
        assert len(seen[-1]) <= 10               # 历史上限


class TestSSEStream:
    def test_stream_demo_scenario_full_protocol(self, client):
        with client.stream("POST", "/api/chat/stream", json={"message": "演示一下串口乱码诊断"}) as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers["content-type"]
            body = "".join(resp.iter_text())
        for ev in ("event: meta", "event: delta", "event: result", "event: done"):
            assert ev in body, f"缺少 {ev}"
        # result 事件携带引擎数据（hexdump）
        assert "hexdump" in body
        assert "garbled" in body

    def test_stream_llm_free_chat(self, client, monkeypatch):
        client.post("/api/settings", json={"api_key": "sk-x"})

        def fake_stream(cfg, system, messages, timeout=40.0):
            assert "你好" in messages[-1]["content"] or any("你好" in str(m.get("content")) for m in messages)
            yield "你"
            yield "好呀"

        monkeypatch.setattr(appmod, "_llm_stream_completion", fake_stream)
        with client.stream("POST", "/api/chat/stream", json={"message": "你好", "session_id": "sess-S"}) as resp:
            body = "".join(resp.iter_text())
        assert "event: delta" in body
        assert '"你"' in body or "你" in body

    def test_stream_llm_failure_falls_back(self, client, monkeypatch):
        client.post("/api/settings", json={"api_key": "sk-x"})

        def broken_stream(cfg, system, messages, timeout=40.0):
            yield "开头"
            raise RuntimeError("provider down")

        monkeypatch.setattr(appmod, "_llm_stream_completion", broken_stream)
        with client.stream("POST", "/api/chat/stream", json={"message": "随便聊聊"}) as resp:
            body = "".join(resp.iter_text())
        assert "event: result" in body           # 降级后仍完整收尾
        assert "event: done" in body
