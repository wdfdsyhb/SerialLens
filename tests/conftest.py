"""pytest 全局夹具：隔离 webapp 的持久化配置.

`webapp.app` 在导入时会从 `webapp/.llm.local` 恢复运行时 LLM 配置。开发机上这个
文件通常真实存在，若测试直接复用模块状态，就会出现「随机顺序下某些用例意外拿到
已配置的 LLM」这种依赖执行顺序的假失败。这里每个用例前把配置清空、把持久化路径
指向临时目录，测试之间互不影响，也不会读写开发者本机的真实配置。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_webapp_runtime(tmp_path, monkeypatch):
    """清空运行时 LLM 配置，并把 .llm.local 重定向到临时文件."""
    try:
        import webapp.app as appmod
    except Exception:  # fastapi 未安装时跳过，相关用例本身会 importorskip
        yield  # 生成器 fixture 不能直接 return，否则 pytest 报 did not yield a value
        return

    monkeypatch.setattr(appmod, "_LLM_LOCAL", tmp_path / ".llm.local", raising=False)
    saved = dict(appmod._RUNTIME_CFG)
    appmod._RUNTIME_CFG.clear()
    try:
        yield
    finally:
        appmod._RUNTIME_CFG.clear()
        appmod._RUNTIME_CFG.update(saved)
