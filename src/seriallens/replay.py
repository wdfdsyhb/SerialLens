"""文件回放：把现成的串口捕获文件变成分析素材.

支持格式（自动识别，不靠扩展名）：
- 二进制捕获（.bin 等直接读字节）
- 纯 HEX 文本：空格 / 冒号 / 逗号 / 换行分隔均可，如 "AA55 04E2" 或 "AA 55 04 E2"
- hexdump 带偏移格式（逻辑分析仪 / SerialLens / hexdump -C 导出）：
      0000  AA 55 04 E2 09 02 58 00 8F   *..)..X..
      0009  AA 55 04 8C 0A 02 71 01 5F   *..)..
  解析规则：剥掉行首偏移，取中段 hex 域，忽略 ASCII 尾注与注释行（# 或 // 开头）。
- 空行与纯注释行跳过

诚实原则：解析不出任何字节时明确报错，不猜测文件语义。
"""

from __future__ import annotations

from pathlib import Path

_HEX_CHARS = set("0123456789abcdefABCDEF")


class ReplayError(ValueError):
    """文件无法解析为串口数据。"""


def _is_hexdump_line(line: str) -> bool:
    """形如「偏移:  hex... ascii...」的行（偏移后通常有两个以上空格）。"""
    stripped = line.strip()
    head = stripped.split(None, 1)
    if not head:
        return False
    token = head[0].rstrip(":")
    return len(token) >= 4 and all(c in _HEX_CHARS for c in token) and len(head) == 2 and "  " in stripped


def _parse_hexdump_line(line: str) -> bytes:
    """从 hexdump 行提取 hex 域：去掉偏移与 ASCII 尾注。"""
    stripped = line.strip()
    parts = stripped.split(None, 1)
    body = parts[1] if len(parts) == 2 else ""
    # ASCII 尾注：以两个以上空格分隔；仅保留 hex 域
    if "  " in body:
        body = body.split("  ", 1)[0]
    return _hex_text_to_bytes(body)


def _hex_text_to_bytes(text: str) -> bytes:
    cleaned = (
        text.replace(":", " ").replace(",", " ").replace(";", " ")
        .replace("-", " ").replace("0x", " ").replace("0X", " ")
    )
    tokens = [t for t in cleaned.split() if t]
    out = bytearray()
    for t in tokens:
        if not all(c in _HEX_CHARS for c in t):
            raise ReplayError(f"非 HEX 词元：{t!r}")
        if len(t) % 2 != 0:
            raise ReplayError(f"HEX 词元长度为奇数：{t!r}")
        out.extend(bytes.fromhex(t))
    return bytes(out)


def load_capture(path: str | Path) -> tuple[bytes, str]:
    """读取捕获文件，返回 (字节数据, 实际使用的解析格式描述).

    - 先按二进制读；若文件是文本且内容像 HEX/hexdump，按文本解析
    - 判别依据：解码为 utf-8（宽松）是否可读且含 HEX 词元
    """
    p = Path(path)
    if not p.exists():
        raise ReplayError(f"文件不存在：{p}")
    raw = p.read_bytes()
    if not raw:
        raise ReplayError(f"文件为空：{p}")

    # 尝试按文本解析（HEX/hexdump 优先：二进制文件很少恰好全是可打印字符）
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = ""

    if text:
        lines = [ln.strip() for ln in text.splitlines()]
        meaningful = [ln for ln in lines if ln and not ln.startswith(("#", "//"))]
        if meaningful:
            hexdump_mode = any(_is_hexdump_line(ln) for ln in meaningful)
            try:
                if hexdump_mode:
                    chunks = []
                    for ln in meaningful:
                        if not _is_hexdump_line(ln):
                            continue  # hexdump 文件里的注释/杂行
                        chunks.append(_parse_hexdump_line(ln))
                    data = b"".join(chunks)
                else:
                    data = _hex_text_to_bytes(" ".join(meaningful))
            except ReplayError as exc:
                # 可读文本但不是 HEX：明确拒绝，绝不退回二进制（那会把日志当数据）
                raise ReplayError(f"文本文件无法解析为 HEX 数据（{exc}）") from exc
            if data:
                fmt = "hexdump 文本" if hexdump_mode else "纯 HEX 文本"
                return data, fmt
            raise ReplayError("文本文件未解析出任何字节")

        # 全是注释行：也按无效文本拒绝
        raise ReplayError("文本文件没有有效内容行")

    # 二进制路径（仅当文件不是可读文本时才到达这里）
    return raw, "二进制"
