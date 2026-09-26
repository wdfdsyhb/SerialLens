"""会话导出：把一次分析整理成可存档/可发帖求助的 Markdown."""

from __future__ import annotations

import time
from dataclasses import dataclass

import seriallens


@dataclass
class Session:
    title: str
    source: str          # COM3 / demo:nmea
    baud: str            # 9600 / 自动检测
    hexdump: str
    text_view: str
    heuristic: str       # 启发式识别结论
    ai_analysis: str     # AI 分析（可为空）
    extra: dict[str, str] | None = None


def _hexdump(data: bytes, width: int = 16) -> str:
    lines = []
    for off in range(0, len(data), width):
        chunk = data[off : off + width]
        hexpart = " ".join(f"{b:02X}" for b in chunk)
        asciipart = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in chunk)
        lines.append(f"{off:04X}  {hexpart:<{width * 3}}  {asciipart}")
    return "\n".join(lines)


def render(session: Session) -> str:
    """渲染成 Markdown 字符串."""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    parts = [
        f"# SerialLens 分析报告 — {session.title}",
        "",
        f"- 时间：{ts}",
        f"- 数据源：`{session.source}`",
        f"- 波特率：{session.baud}",
        f"- 工具版本：seriallens {seriallens.__version__}",
        "",
        "## 原始采样",
        "",
        "```",
        session.text_view,
        "```",
        "",
        "```",
        session.hexdump,
        "```",
        "",
        "## 本地启发式识别",
        "",
        session.heuristic,
        "",
    ]
    if session.ai_analysis:
        parts += ["## AI 分析", "", session.ai_analysis, ""]
    if session.extra:
        parts += ["## 备注", ""]
        parts += [f"- **{k}**：{v}" for k, v in session.extra.items()]
        parts.append("")
    parts += ["---", "*由 SerialLens 生成（AI 仅供辅助判断，以实测为准）*"]
    return "\n".join(parts)


def hexdump_of(data: bytes) -> str:
    return _hexdump(data)
