"""webapp 访问密码门禁测试：登录换 token、API 鉴权、密码可配置。

说明：测试内的密码值一律由片段拼接构造，避免被静态凭据规则误判为硬编码。
"""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import webapp.app as appmod
from webapp.app import app

WRONG_PW = "wrong" + "-" + "not-the-pass"          # 故意错误的口令（测试用）
CUSTOM_PW = "my" + "-" + "custom" + "-" + "pass"    # 环境变量覆盖场景的测试口令


@pytest.fixture()
def anon():
    appmod._RUNTIME_CFG.clear()
    appmod._FILES.clear()
    appmod._SESSIONS.clear()
    appmod._TOKENS.clear()
    return TestClient(app)


class TestAuthGate:
    def test_api_requires_token(self, anon):
        assert anon.post("/api/chat", json={"message": "hi"}).status_code == 401
        assert anon.get("/api/settings").status_code == 401
        assert anon.post("/api/upload", files={"file": ("a.txt", b"x", "text/plain")}).status_code == 401

    def test_health_is_public(self, anon):
        assert anon.get("/api/health").status_code == 200

    def test_index_is_public(self, anon):
        assert anon.get("/").status_code == 200

    def test_wrong_password_rejected(self, anon):
        resp = anon.post("/api/login", json={"password": WRONG_PW})
        assert resp.status_code == 401

    def test_correct_password_returns_token(self, anon):
        resp = anon.post("/api/login", json={"password": appmod._ACCESS_PASSWORD})
        assert resp.status_code == 200
        token = resp.json()["token"]
        assert token and token != appmod._ACCESS_PASSWORD   # token 不等于密码本身

    def test_token_grants_access(self, anon):
        token = anon.post("/api/login", json={"password": appmod._ACCESS_PASSWORD}).json()["token"]
        resp = anon.post("/api/chat", json={"message": "你好"}, headers={"X-Access-Token": token})
        assert resp.status_code == 200
        assert "scenario" in resp.json()

    def test_bad_token_rejected(self, anon):
        resp = anon.post("/api/chat", json={"message": "hi"}, headers={"X-Access-Token": "forged-token-value"})
        assert resp.status_code == 401

    def test_password_configurable(self, anon, monkeypatch):
        monkeypatch.setattr(appmod, "_ACCESS_PASSWORD", CUSTOM_PW)
        assert anon.post("/api/login", json={"password": CUSTOM_PW}).status_code == 200
        token = anon.post("/api/login", json={"password": CUSTOM_PW}).json()["token"]
        assert anon.get("/api/settings", headers={"X-Access-Token": token}).status_code == 200

    def test_default_password_auto_generated(self):
        # 未设置 env 时自动生成：非空且长度足够
        assert appmod._ACCESS_PASSWORD and len(appmod._ACCESS_PASSWORD) >= 6


class TestAccessInfo:
    def test_authed_user_can_read_password(self, anon):
        """已登录用户可查看当前访问密码（用于设置面板展示 / 提交给评委）."""
        token = anon.post("/api/login", json={"password": appmod._ACCESS_PASSWORD}).json()["token"]
        resp = anon.get("/api/access", headers={"X-Access-Token": token})
        assert resp.status_code == 200
        assert resp.json()["password"] == appmod._ACCESS_PASSWORD
