"""AI 分析：把 HEX 采样与启发式结论交给 OpenAI 兼容接口的大模型.

设计原则：
- provider 无关：任何 OpenAI 兼容端点（DeepSeek / GLM / OpenRouter / 本地 vLLM）都能接
- 全部走环境变量，绝不把 key 写进代码或日志
- 无 key 时优雅降级：启发式结论照常输出，AI 部分显示配置指引
"""

from __future__ import annotations

import os

import httpx

SYSTEM_PROMPT = """\
你是一名嵌入式串口协议分析专家。用户会给你一段串口采样（HEX 与文本两种视图）、
基本的启发式识别结果、以及可能的波特率信息。请用简体中文、markdown、克制篇幅回答：

1. **协议判断**：最可能是什么协议（NMEA 0183 / JSON / Modbus RTU / AT 命令 / 自定义帧 / 纯文本调试）？
   说明判断依据。证据不足就明说不足，不要编。
2. **帧结构**：若能看出帧头/长度/字段/校验，给出推测的字节布局表；看不出就给「下一步抓什么数据才能确认」。
3. **异常**：乱码、断帧、错位、校验失败等，指出最可能的原因（如波特率不匹配、接反 TX/RX、供电不稳）。
4. **下一步**：给出最多 3 条具体可操作的建议。

约束：只基于给定数据推理；禁止捏造具体设备型号；总长不超过 300 字。\
"""


class LLMConfig:
    def __init__(self) -> None:
        self.api_key = (
            os.environ.get("SERIALLENS_API_KEY")
            or os.environ.get("DEEPSEEK_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or ""
        )
        self.base_url = (
            os.environ.get("SERIALLENS_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or "https://api.deepseek.com"
        ).rstrip("/")
        self.model = os.environ.get("SERIALLENS_MODEL", "deepseek-chat")

    @property
    def available(self) -> bool:
        return bool(self.api_key)


def build_prompt(hexdump: str, text_view: str, heuristic: str, baud_info: str) -> str:
    return (
        f"## 串口采样（{len(text_view)} 字符视图 / HEX 见下）\n\n"
        f"### 文本视图\n```\n{text_view}\n```\n\n"
        f"### HEX 视图\n```\n{hexdump}\n```\n\n"
        f"### 本地启发式识别\n{heuristic}\n\n"
        f"### 波特率\n{baud_info}\n"
    )


def analyze(prompt: str, cfg: LLMConfig, timeout: float = 60.0) -> str:
    """调用 OpenAI 兼容 /chat/completions，返回模型文本。异常向上抛，由 CLI 统一处理."""
    url = f"{cfg.base_url}/chat/completions"
    payload = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 1200,
    }
    headers = {"Authorization": f"Bearer {cfg.api_key}"}
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    return data["choices"][0]["message"]["content"]
