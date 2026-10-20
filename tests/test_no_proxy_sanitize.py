"""回归测试：NO_PROXY 里出现带方括号的 IPv6 时，httpx 会抛 InvalidURL 而非网络错误.

背景（2026-10-20）：某些 Windows 环境把 NO_PROXY 设成
`localhost,127.0.0.1,::1,[::1]`，httpx 把 "[::1]" 当成 `host:port` 解析，
抛出 `InvalidURL: Invalid port: ':1]'`。该异常被调用方的
`except Exception` 静默吞掉，表现为「配了 AI 却永远返回模板回复」。

修复演进：
- v1（已废弃）：修改 os.environ 剔除非法项 —— 进程级全局副作用
- v2（当前）：_make_client() 在检测到中毒时用 trust_env=False + 显式 proxy 建 client，
  不动 os.environ
"""

from __future__ import annotations

import os

import httpx
import pytest

from seriallens.analyst import _cleaned_no_proxy, _make_client, _no_proxy_is_poisoned

BAD = "localhost,127.0.0.1,::1,[::1]"
CLEAN = "localhost,127.0.0.1,::1"


@pytest.fixture()
def env(monkeypatch):
    """把代理相关环境变量隔离到测试内，避免污染其他用例."""

    def _set(**kwargs):
        for k, v in kwargs.items():
            if v is None:
                monkeypatch.delenv(k, raising=False)
            else:
                monkeypatch.setenv(k, v)

    return _set


class TestDetection:
    def test_detects_bracketed_ipv6(self, env):
        env(NO_PROXY=BAD, no_proxy=BAD)
        assert _no_proxy_is_poisoned() is True

    def test_clean_is_not_poisoned(self, env):
        env(NO_PROXY=CLEAN, no_proxy=CLEAN)
        assert _no_proxy_is_poisoned() is False

    def test_absent_is_not_poisoned(self, env):
        env(NO_PROXY=None, no_proxy=None)
        assert _no_proxy_is_poisoned() is False

    def test_lowercase_variant_detected(self, env):
        """Windows 上 no_proxy 与 NO_PROXY 可能只有一个被设置."""
        env(NO_PROXY=None, no_proxy=BAD)
        assert _no_proxy_is_poisoned() is True


class TestCleanedValue:
    def test_strips_only_bracketed(self, env):
        env(NO_PROXY=BAD, no_proxy=None)
        cleaned = _cleaned_no_proxy()
        assert "[::1]" not in cleaned
        # 其余条目必须保留：外网访问依赖代理配置
        assert "localhost" in cleaned
        assert "127.0.0.1" in cleaned
        assert "::1" in cleaned

    def test_only_bracketed_falls_back(self, env):
        """极端情况：NO_PROXY 只有 "[::1]"，清空后回落到安全默认值."""
        env(NO_PROXY="[::1]", no_proxy=None)
        cleaned = _cleaned_no_proxy()
        assert cleaned.strip()
        assert "[::1]" not in cleaned


class TestMakeClient:
    def test_clean_env_keeps_default_behavior(self, env):
        env(NO_PROXY=CLEAN, no_proxy=CLEAN)
        with _make_client(1.0) as client:
            # 未中毒时不加 trust_env=False，保留调用方环境
            assert client.trust_env is True

    def test_poisoned_env_does_not_mutate_os_environ(self, env):
        """关键回归：修复不得改动进程环境变量（v1 的问题）."""
        env(NO_PROXY=BAD, no_proxy=BAD)
        before = (os.environ.get("NO_PROXY"), os.environ.get("no_proxy"))
        with _make_client(1.0) as client:
            assert client.trust_env is False
        after = (os.environ.get("NO_PROXY"), os.environ.get("no_proxy"))
        assert before == after, "os.environ 被修改了，存在全局副作用"

    def test_poisoned_env_builds_usable_client(self, env):
        """端到端：中毒环境下 client 能正常构造（此前会在请求阶段崩）."""
        env(NO_PROXY=BAD, no_proxy=BAD)
        with _make_client(1.0) as client:
            assert isinstance(client, httpx.Client)
            assert client.trust_env is False
