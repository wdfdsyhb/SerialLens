"""webapp 会话导出：把一次对话与引擎结果渲成可下载的 Markdown."""

from __future__ import annotations

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
    appmod._TOKENS.clear()
    c = TestClient(app)
    token = c.post("/api/login", json={"password": appmod._ACCESS_PASSWORD}).json()["token"]
    c.headers.update({"X-Access-Token": token})
    return c


class TestExportGate:
    def test_export_requires_token(self):
        appmod._RUNTIME_CFG.clear()
        appmod._FILES.clear()
        appmod._SESSIONS.clear()
        appmod._TOKENS.clear()
        anon = TestClient(app)
        assert anon.get("/api/export/sess-1").status_code == 401

    def test_export_missing_session_404(self, client):
        assert client.get("/api/export/no-such-session").status_code == 404


class TestExportContent:
    def test_export_returns_markdown(self, client):
        appmod._SESSIONS["sess-exp"] = [
            {"role": "user", "content": "帮我分析一下这段数据"},
            {"role": "assistant", "content": "发现稳定帧头与等长帧结构。"},
            {
                "role": "bot",
                "result": {
                    "scenario": "analyze",
                    "title": "协议识别分析",
                    "reply": "发现稳定帧头与等长帧结构。",
                    "steps": ["采样 4 帧", "启发式识别帧头"],
                    "data": {
                        "frames_count": 4,
                        "frame_len": 8,
                        "hexdump": "00 01 02 03",
                        "heuristic": "自定义二进制帧",
                    },
                    "llm_polished": False,
                    "needs_data": False,
                },
            },
        ]
        resp = client.get("/api/export/sess-exp")
        assert resp.status_code == 200
        assert "text/markdown" in resp.headers["content-type"]
        body = resp.text
        assert "# SerialLens" in body
        assert "帮我分析一下这段数据" in body
        assert "协议识别分析" in body
        assert "00 01 02 03" in body

    def test_export_empty_session(self, client):
        appmod._SESSIONS["sess-empty"] = []
        resp = client.get("/api/export/sess-empty")
        assert resp.status_code == 200
        assert "text/markdown" in resp.headers["content-type"]
        assert "暂无内容" in resp.text
