"""协议识别启发式：从一串字节里猜「这是什么」.

只做事实层面的启发式判断（可打印率、行结构、帧头、校验和），
不做超过证据的结论 —— 结论性的判断交给 AI 分析或人工确认。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# 常见自定义帧头（字节序列 -> 叫法）
KNOWN_PREFIXES: dict[bytes, str] = {
    b"\xaa\x55": "AA 55 (常见传感器帧头)",
    b"\x55\xaa": "55 AA (常见帧头/MBR 标记)",
    b"\xa5\x5a": "A5 5A",
    b"\xa5": "A5 (单字节帧头，误报率高)",
    b"\x02": "STX (0x02 文本帧起始)",
    b"\x7e": "0x7E (HDLC 类帧界)",
}

NMEA_LINE_ENDS = (b"\r\n", b"\n", b"\r")


@dataclass
class DetectResult:
    """一次识别的结论与证据."""

    protocol: str
    confidence: float  # 0.0 - 1.0
    evidence: list[str] = field(default_factory=list)
    printable_ratio: float = 0.0
    sample_size: int = 0

    def summary(self) -> str:
        ev = "；".join(self.evidence) if self.evidence else "无显著特征"
        return f"{self.protocol}（置信度 {self.confidence:.0%}）— {ev}"


def printable_ratio(data: bytes) -> float:
    if not data:
        return 0.0
    printable = sum(1 for b in data if 0x20 <= b < 0x7F or b in (0x09, 0x0A, 0x0D))
    return printable / len(data)


def _nmea_check(lines: list[bytes]) -> tuple[int, int]:
    """返回 (带 *校验和 的行数, 校验和通过的行数)."""
    total = passed = 0
    for line in lines:
        star = line.rfind(b"*")
        if star == -1 or star + 3 > len(line):
            continue
        hexpart = line[star + 1 : star + 3]
        try:
            expected = int(hexpart, 16)
        except ValueError:
            continue
        actual = 0
        for b in line[1:star]:
            actual ^= b
        total += 1
        passed += int(actual == expected)
    return total, passed


def _detect_nmea(data: bytes, lines: list[bytes]) -> DetectResult | None:
    starts = sum(1 for ln in lines if ln[:1] in (b"$", b"!"))
    if not lines or starts < max(1, len(lines) // 2):
        return None
    total, passed = _nmea_check(lines)
    evidence = [f"{starts}/{len(lines)} 行以 $/! 开头"]
    if total:
        evidence.append(f"NMEA 校验和 {passed}/{total} 通过")
        confidence = 0.6 + 0.35 * (passed / total)
    else:
        confidence = 0.6
    talkers = sorted({ln[1:3].decode("ascii", "replace") for ln in lines if len(ln) > 3})
    if talkers:
        evidence.append(" Talker ID: " + ", ".join(talkers[:4]))
    return DetectResult("NMEA 0183（GPS/北斗等导航语句）", min(confidence, 0.98), evidence, printable_ratio(data), len(data))


def _detect_json(data: bytes) -> DetectResult | None:
    text = data.decode("utf-8", "replace")
    ok = 0
    for chunk in text.splitlines():
        s = chunk.strip()
        if not s:
            continue
        try:
            json.loads(s)
            ok += 1
        except json.JSONDecodeError:
            pass
    if ok == 0:
        return None
    return DetectResult(
        "JSON（设备/网关常见调试输出）",
        min(0.5 + 0.4 * ok / max(1, len(text.splitlines())), 0.95),
        [f"{ok} 行可解析为 JSON"],
        printable_ratio(data),
        len(data),
    )


def _detect_framing(data: bytes) -> DetectResult | None:
    ratio = printable_ratio(data)
    if ratio > 0.85:
        return None  # 更像文本，交给文本分支
    for prefix, name in KNOWN_PREFIXES.items():
        hits = data.count(prefix)
        if hits >= 2 and (hits * len(prefix)) / len(data) < 0.5:
            return DetectResult(
                f"自定义二进制帧（疑似 {name}）",
                0.55,
                [f"帧头 {name} 出现 {hits} 次", f"可打印字节占比 {ratio:.0%}（非纯文本）"],
                ratio,
                len(data),
            )
    return DetectResult(
        "未知二进制流",
        0.3,
        [f"可打印字节占比仅 {ratio:.0%}，未命中常见帧头"],
        ratio,
        len(data),
    )


def _detect_text(data: bytes, lines: list[bytes]) -> DetectResult | None:
    ratio = printable_ratio(data)
    if ratio < 0.85 or not lines:
        return None
    crlf = data.count(b"\r\n")
    evidence = [f"可打印占比 {ratio:.0%}，{len(lines)} 行"]
    if crlf:
        evidence.append(f"行尾 CRLF x{crlf}")
    return DetectResult("纯文本输出（调试打印/AT 响应等）", 0.7, evidence, ratio, len(data))


def detect(data: bytes) -> DetectResult:
    """主入口：给一段采样，返回最可能的协议结论."""
    if not data:
        return DetectResult("无数据", 0.0, ["采样为空"], 0.0, 0)
    lines = [ln for ln in data.replace(b"\r\n", b"\n").split(b"\n") if ln.strip()]
    candidates = []
    for fn in (_detect_nmea, _detect_json, _detect_text, _detect_framing):
        try:
            r = fn(data, lines) if fn in (_detect_nmea, _detect_text) else fn(data)
        except Exception:  # 启发式绝不让主流程崩
            continue
        if r:
            candidates.append(r)
    if not candidates:
        return DetectResult("未知", 0.0, ["所有启发式未命中"], printable_ratio(data), len(data))
    return max(candidates, key=lambda r: r.confidence)
