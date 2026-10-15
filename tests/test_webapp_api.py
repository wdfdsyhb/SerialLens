"""webapp.app API 层测试：/api/chat、/api/health、静态首页."""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from webapp.app import app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


class TestChat:
    def test_diagnose_message(self, client):
        resp = client.post("/api/chat", json={"message": "演示一下串口乱码诊断"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["scenario"] == "diagnose"
        assert body["data"]["garbled"]["hexdump"]

    def test_keyword_without_demo_needs_data(self, client):
        resp = client.post("/api/chat", json={"message": "我的串口收到一堆乱码看不懂"})
        body = resp.json()
        assert body["scenario"] == "diagnose"
        assert body["needs_data"] is True
        assert body["data"] == {}

    def test_codegen_message(self, client):
        resp = client.post("/api/chat", json={"message": "演示学习这个协议并生成 C 解析器"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["scenario"] == "codegen"
        assert "sl_u8" in body["data"]["c_header"]

    def test_assist_fallback(self, client):
        resp = client.post("/api/chat", json={"message": "你能做什么"})
        assert resp.status_code == 200
        assert resp.json()["scenario"] == "assist"

    def test_empty_message_is_assist(self, client):
        resp = client.post("/api/chat", json={"message": ""})
        assert resp.status_code == 200
        assert resp.json()["scenario"] == "assist"

    def test_missing_body_422(self, client):
        resp = client.post("/api/chat", json={})
        assert resp.status_code == 422


class TestHealthAndStatic:
    def test_health(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_index_served(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "SerialLens" in resp.text

    def test_no_identity_leak_in_index(self, client):
        """盲评脱敏红线：首页不得出现作者身份线索（用户名/仓库名）."""
        resp = client.get("/")
        assert "wdfdsyhb" not in resp.text
        assert "github.com" not in resp.text.lower()
