"""协议画像（Profile）：协议学习模式的产出物.

一次 learn() 生成一份画像（JSON），之后 watch/decode 用它做实时解码。
画像结构：

{
  "name": "my-sensor",
  "created": "2026-09-26T21:00:00",
  "framing": {"mode": "header", "header": "AA55", "length_rule": "byte@2+2"},
  "fields": [
    {"name": "temp", "offset": 3, "type": "u16le", "scale": 0.01,
     "unit": "?", "label": "温度(用户标注)", "confidence": 0.95},
    ...
  ]
}

设计原则：
- 画像是「可纠正的假设」：每个字段带 confidence，用户可手改 JSON 后重用
- 与 jinja2 等任何渲染引擎无关：纯数据结构 + struct 解码
- decode() 只做画像声明的事，不越权解释
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

PROFILES_DIR = Path.home() / ".seriallens" / "profiles"

# 支持的字段类型：字节数
FIELD_TYPES: dict[str, int] = {
    "u8": 1,
    "i8": 1,
    "u16le": 2,
    "u16be": 2,
    "i16le": 2,
    "i16be": 2,
    "u32le": 4,
    "u32be": 4,
    "f32le": 4,
    "f32be": 4,
}


@dataclass
class FieldSpec:
    name: str
    offset: int
    type: str
    scale: float = 1.0   # 物理值 = 原始值 * scale
    unit: str = ""
    label: str = ""      # 语义标注：如「温度(用户标注)」/「动态-未知」
    confidence: float = 0.5

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "FieldSpec":
        keys = ("name", "offset", "type", "scale", "unit", "label", "confidence")
        return FieldSpec(**{k: d[k] for k in keys if k in d})


@dataclass
class ProtocolProfile:
    name: str
    framing_mode: str  # header | line | fixed
    header: str = ""   # hex 字符串，如 "AA55"
    length_rule: str = ""  # "" / "byte@N+K" (帧长 = 帧头后第 N 字节 + K) / "fixed:N"
    fields: list[FieldSpec] = field(default_factory=list)
    notes: str = ""
    created: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))

    # ------------------------------------------------ 序列化

    def save(self, directory: Path | None = None) -> Path:
        directory = directory or PROFILES_DIR
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.name}.json"
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @staticmethod
    def load(name: str, directory: Path | None = None) -> "ProtocolProfile":
        path = (directory or PROFILES_DIR) / f"{name}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return ProtocolProfile.from_dict(data)

    @staticmethod
    def from_dict(d: dict) -> "ProtocolProfile":
        return ProtocolProfile(
            name=d["name"],
            framing_mode=d.get("framing", {}).get("mode", "header"),
            header=d.get("framing", {}).get("header", ""),
            length_rule=d.get("framing", {}).get("length_rule", ""),
            fields=[FieldSpec.from_dict(f) for f in d.get("fields", [])],
            notes=d.get("notes", ""),
            created=d.get("created", ""),
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "created": self.created,
            "framing": {"mode": self.framing_mode, "header": self.header, "length_rule": self.length_rule},
            "fields": [f.to_dict() for f in self.fields],
            "notes": self.notes,
        }

    # ------------------------------------------------ 解码

    def split_frames(self, data: bytes) -> list[bytes]:
        """按画像帧结构把流切成帧（尽力而为，返回完整帧列表）."""
        frames: list[bytes] = []
        if self.framing_mode == "header" and self.header:
            head = bytes.fromhex(self.header)
            idx = 0
            while True:
                pos = data.find(head, idx)
                if pos == -1:
                    break
                frame_len = self._frame_len(data, pos)
                if frame_len and pos + frame_len <= len(data):
                    frames.append(data[pos : pos + frame_len])
                    idx = pos + frame_len
                else:
                    break
            return frames
        if self.framing_mode == "line":
            for ln in data.replace(b"\r\n", b"\n").split(b"\n"):
                if ln.strip():
                    frames.append(ln)
            return frames
        # fixed:N
        try:
            n = int(self.length_rule.split(":")[1])
        except (IndexError, ValueError):
            return []
        return [data[i : i + n] for i in range(0, len(data) - n + 1, n)]

    def _frame_len(self, data: bytes, pos: int) -> int | None:
        rule = self.length_rule
        if not rule:
            return None
        if rule.startswith("fixed:"):
            return int(rule.split(":")[1])
        # byte@N+K：帧长 = [pos+N] + K（N 相对帧头）
        try:
            spec = rule.removeprefix("byte@")
            n_str, k_str = spec.split("+")
            n, k = int(n_str), int(k_str)
            if pos + n < len(data):
                return data[pos + n] + k
        except (ValueError, IndexError):
            pass
        return None

    def decode_frame(self, frame: bytes) -> dict[str, object]:
        """按字段表解码一帧，返回 {字段名: 物理值}。超界的字段标记而非抛错."""
        out: dict[str, object] = {}
        for f in self.fields:
            size = FIELD_TYPES.get(f.type)
            if size is None or f.offset + size > len(frame):
                out[f.name] = "<越界>"
                continue
            raw = frame[f.offset : f.offset + size]
            out[f.name] = round(_decode_value(raw, f.type) * f.scale, 4)
        return out

    def decode_stream(self, data: bytes) -> list[dict[str, object]]:
        return [self.decode_frame(fr) for fr in self.split_frames(data)]


def _decode_value(raw: bytes, ftype: str) -> float:
    import struct

    if ftype == "u8":
        return float(raw[0])
    if ftype == "i8":
        return float(struct.unpack("b", raw)[0])
    fmt = {"u16le": "<H", "u16be": ">H", "i16le": "<h", "i16be": ">h",
           "u32le": "<I", "u32be": ">I", "f32le": "<f", "f32be": ">f"}[ftype]
    return float(struct.unpack(fmt, raw)[0])
