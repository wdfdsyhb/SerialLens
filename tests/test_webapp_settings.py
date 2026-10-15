"""webapp 设置与自由对话测试：运行时 API 配置 + LLM 自由对话路由."""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import webapp.app as appmod
from webapp.app import app


@pytest.fixture()
def client():
    appmod._RUNTIME_CFG.clear()
    c = TestClient(app)
    token = c.post("/api/login", json={"password": appmod._ACCESS_PASSWORD}).json()["token"]
    c.headers.update({"X-Access-Token": token})
    return c


class TestSettings:
    def test_default_not_configured(self, client):
        resp = client.get("/api/settings")
        assert resp.status_code == 200
        body = resp.json()
        assert body["configured"] is False

    def test_post_persists_and_masks_key(self, client):
        resp = client.post("/api/settings", json={
            "base_url": "https://api.deepseek.com", "api_key": "sk-test-123", "model": "deepseek-chat",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["configured"] is True
        assert body["base_url"] == "https://api.deepseek.com"
        assert body["model"] == "deepseek-chat"
        assert "api_key" not in body          # key 永不回显
        assert "sk-test-123" not in resp.text

    def test_empty_key_rejects(self, client):
        resp = client.post("/api/settings", json={"api_key": ""})
        assert resp.status_code in (400, 422)

    def test_runtime_cfg_flips_llm_availability(self, client):
        assert appmod._runtime_llm_available() is False
        client.post("/api/settings", json={"api_key": "sk-x"})
        assert appmod._runtime_llm_available() is True


class TestFreeChat:
    def test_assist_without_llm_uses_template(self, client, monkeypatch):
        # 无 LLM：保持模板自述
        resp = client.post("/api/chat", json={"message": "随便聊聊"})
        body = resp.json()
        assert body["scenario"] == "assist"
        assert body["llm_polished"] is False

    def test_assist_with_llm_calls_model(self, client, monkeypatch):
        client.post("/api/settings", json={"api_key": "sk-x"})
        calls = []
        monkeypatch.setattr(appmod, "_llm_chat_completion", lambda cfg, system, user, history=None: calls.append(user) or "这是模型的自由回复")

        resp = client.post("/api/chat", json={"message": "你能处理 Modbus 吗？"})
        body = resp.json()
        assert body["llm_polished"] is True
        assert body["reply"] == "这是模型的自由回复"
        assert calls and "Modbus" in calls[0]

    def test_scenario_with_llm_polishes_reply(self, client, monkeypatch):
        client.post("/api/settings", json={"api_key": "sk-x"})
        monkeypatch.setattr(appmod, "_llm_chat_completion", lambda cfg, system, user, history=None: "模型润色后的诊断回复")

        resp = client.post("/api/chat", json={"message": "演示一下串口乱码"})
        body = resp.json()
        assert body["scenario"] == "diagnose"
        assert body["reply"] == "模型润色后的诊断回复"
        assert body["llm_polished"] is True
        # 引擎数据不受 LLM 影响
        assert body["data"]["garbled"]["hexdump"]
