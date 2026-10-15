"""webapp 附件上传与对话上下文测试：文件上传、真实数据分析、无数据不自动跑场景."""

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
    return TestClient(app)


class TestUpload:
    def test_upload_text_file(self, client):
        resp = client.post("/api/upload", files={"file": ("uart.log", b"$GPGGA,1234*56\r\n$GPRMC,5678*9A\r\n", "text/plain")})
        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "text"
        assert body["name"] == "uart.log"
        assert "GPGGA" in body["preview"]
        assert body["file_id"]

    def test_upload_binary_file(self, client):
        resp = client.post("/api/upload", files={"file": ("capture.bin", b"\xAA\x55\x04\xE2", "application/octet-stream")})
        body = resp.json()
        assert body["kind"] == "binary"
        assert "AA 55" in body["preview"]

    def test_upload_image(self, client):
        resp = client.post("/api/upload", files={"file": ("board.png", b"\x89PNG fake", "image/png")})
        body = resp.json()
        assert body["kind"] == "image"

    def test_upload_rejects_oversize(self, client):
        resp = client.post("/api/upload", files={"file": ("big.bin", b"x" * (6 * 1024 * 1024), "application/octet-stream")})
        assert resp.status_code == 400


class TestChatWithFile:
    def test_text_file_content_reaches_llm(self, client, monkeypatch):
        client.post("/api/settings", json={"api_key": "sk-x"})
        seen = []
        monkeypatch.setattr(appmod, "_llm_chat_completion",
                            lambda cfg, system, user, images=None, mime="image/png": seen.append(user) or "基于你日志的分析")

        fid = client.post("/api/upload", files={"file": ("uart.log", b"ERR: timeout at 0x1F", "text/plain")}).json()["file_id"]
        resp = client.post("/api/chat", json={"message": "帮我分析这个日志", "file_ids": [fid]})
        assert resp.json()["reply"] == "基于你日志的分析"
        assert "timeout at 0x1F" in seen[0]

    def test_image_sent_as_vision_input(self, client, monkeypatch):
        """图片附件走视觉消息通道（base64），不再拼「不支持」占位文案."""
        client.post("/api/settings", json={"api_key": "sk-x"})
        seen = []
        monkeypatch.setattr(appmod, "_llm_chat_completion",
                            lambda cfg, system, user, images=None, mime="image/png": seen.append((user, images)) or "图中是一块开发板")

        fid = client.post("/api/upload", files={"file": ("board.png", b"\x89PNG fake-image", "image/png")}).json()["file_id"]
        resp = client.post("/api/chat", json={"message": "看看这张图", "file_ids": [fid]})
        assert resp.json()["reply"] == "图中是一块开发板"
        user, images = seen[0]
        assert images and images[0] == b"\x89PNG fake-image"   # 原始字节进视觉通道
        assert "不支持" not in user                              # prompt 里没有降级占位

    def test_vision_messages_structure(self):
        """OpenAI 兼容视觉消息格式：text + image_url(base64 data URI)."""
        msgs = appmod._vision_messages("sys-prompt", "user-text", [b"\x89PNG"])
        assert msgs[0] == {"role": "system", "content": "sys-prompt"}
        content = msgs[1]["content"]
        assert isinstance(content, list) and content[0]["type"] == "text"
        img = content[1]
        assert img["type"] == "image_url"
        assert img["image_url"]["url"].startswith("data:image/png;base64,")

    def test_vision_messages_without_images_falls_back_to_plain(self):
        msgs = appmod._vision_messages("sys-prompt", "user-text", None)
        assert msgs[1]["content"] == "user-text"


class TestNoAutoScenario:
    def test_keyword_without_demo_word_or_file_does_not_run_engine(self, client):
        """没说'演示'、没传数据：不自动跑虚拟数据分析."""
        resp = client.post("/api/chat", json={"message": "串口乱码怎么办"})
        body = resp.json()
        assert body["scenario"] == "diagnose"
        assert body.get("needs_data") is True
        assert body["data"] == {}          # 引擎没跑

    def test_demo_word_triggers_engine(self, client):
        resp = client.post("/api/chat", json={"message": "演示一下串口乱码诊断"})
        body = resp.json()
        assert body["scenario"] == "diagnose"
        assert body["data"], "demo 触发应跑引擎"
        assert body["needs_data"] is False

    def test_canned_reply_removed_without_llm(self, client):
        """无 LLM 时不得输出长篇内置话术（避免冒充 AI 回答）."""
        resp = client.post("/api/chat", json={"message": "演示一下串口乱码诊断"})
        reply = resp.json()["reply"]
        assert len(reply) <= 80, f"无 LLM 回复应为一行状态说明，实际 {len(reply)} 字"
        assert "配置" in reply or "AI" in reply


class TestFileContextWithoutLLM:
    def test_file_without_llm_gets_engine_view(self, client):
        """无 LLM 时上传文件：返回数据视图提示而非伪 AI 话术."""
        fid = client.post("/api/upload", files={"file": ("uart.log", b"hello uart", "text/plain")}).json()["file_id"]
        resp = client.post("/api/chat", json={"message": "帮我分析", "file_ids": [fid]})
        body = resp.json()
        assert body["llm_polished"] is False
        assert body.get("data", {}).get("hexdump") or "数据" in body["reply"]
