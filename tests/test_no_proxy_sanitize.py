"""回归测试：NO_PROXY 里出现带方括号的 IPv6 时，httpx 会抛 InvalidURL 而非网络错误.

背景（2026-10-20）：某些 Windows 环境把 NO_PROXY 设成
`localhost,127.0.0.1,::1,[::1]`，httpx 把 "[::1]" 当成 `host:port` 解析，
抛出 `InvalidURL: Invalid port: ':1]'`。该异常被调用方的
`except Exception` 静默吞掉，表现为「配了 AI 却永远返回模板回复」。

修复：analyst._sanitize_no_proxy() 只剔除这一种非法写法，其余代理配置保留。
"""

from __future__ import annotations

import os

import pytest

from seriallens.analyst import _sanitize_no_proxy

BAD = "localhost,127.0.0.1,::1,[::1]"


@pytest.fixture()
def restore_env():
    """测试后恢复 NO_PROXY/no_proxy，避免污染其他用例."""
    saved = {k: os.environ.get(k) for k in ("NO_PROXY", "no_proxy")}
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


class TestSanitizeNoProxy:
    def test_removes_bracketed_ipv6(self, restore_env):
        os.environ["NO_PROXY"] = BAD
        os.environ["no_proxy"] = BAD
        _sanitize_no_proxy()
        for var in ("NO_PROXY", "no_proxy"):
            value = os.environ[var]
            assert "[::1]" not in value, f"{var} 仍含非法写法 [::1]"
            # 其余条目必须保留：外网访问依赖代理配置
            assert "localhost" in value
            assert "127.0.0.1" in value
            assert "::1" in value

    def test_noop_when_absent(self, restore_env):
        os.environ.pop("NO_PROXY", None)
        os.environ.pop("no_proxy", None)
        _sanitize_no_proxy()  # 不应抛异常，也不应凭空创建变量
        assert os.environ.get("NO_PROXY") is None
        assert os.environ.get("no_proxy") is None

    def test_noop_when_clean(self, restore_env):
        clean = "localhost,127.0.0.1,::1"
        os.environ["NO_PROXY"] = clean
        os.environ["no_proxy"] = clean
        _sanitize_no_proxy()
        assert os.environ["NO_PROXY"] == clean
        assert os.environ["no_proxy"] == clean

    def test_only_bracketed_entry_falls_back(self, restore_env):
        """极端情况：NO_PROXY 只有 "[::1]" 一项，清空后应回落到安全默认值."""
        os.environ["NO_PROXY"] = "[::1]"
        os.environ["no_proxy"] = "[::1]"
        _sanitize_no_proxy()
        for var in ("NO_PROXY", "no_proxy"):
            assert os.environ[var].strip(), f"{var} 被清空，本地回环会走代理"
            assert "[::1]" not in os.environ[var]

    def test_httpx_actually_works_after_sanitize(self, restore_env):
        """端到端：清洗后 httpx 不再抛 InvalidURL（不依赖任何外部服务）."""
        httpx = pytest.importorskip("httpx")
        os.environ["NO_PROXY"] = BAD
        os.environ["no_proxy"] = BAD
        _sanitize_no_proxy()
        # 构造 client 就会读取代理配置；此前这一步之后的请求会因非法 URL 崩溃。
        with httpx.Client(timeout=1.0) as client:
            assert client is not None
