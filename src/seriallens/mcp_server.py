"""SerialLens MCP server：把串口侦探能力暴露给任何 MCP 客户端（ZCode / Claude Desktop 等）.

启动：seriallens mcp          （stdio 传输，供 MCP 客户端拉起）
工具集（全部无副作用 except capture）：
  serial__ports        列出系统串口
  serial__analyze      分析一份 HEX 采样（内置启发式，可选 AI）
  serial__detect_baud  对真实串口做波特率试错扫描
  serial__capture      从真实串口采样一段（唯一有副作用的工具）
  serial__demo_frames  生成虚拟温湿度传感器帧（演示/测试靶场）
  serial__learn        从多帧采样学习协议画像并保存
  serial__decode       用已保存的画像解码一段 HEX 流
  serial__profiles     列出已保存的画像

设计原则：
- 无硬件也全流程可用（demo 数据源）；capture 是唯一触碰真实设备的工具
- 不暴露文件系统/任意命令；AI 走 env 配置，与 CLI 一致
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import __version__
from .analyst import LLMConfig, analyze, build_prompt
from .baudrate import detect_baudrate
from .export import hexdump_of
from .learn import learn
from .profile import PROFILES_DIR, ProtocolProfile
from .protocols import detect
from .serial_port import RealSerial, TempSensorSource, list_ports

mcp = FastMCP(
    "seriallens",
    instructions=(
        "SerialLens：AI 串口侦探。分析串口数据、自动波特率、学习协议字段、"
        "生成解码画像。无硬件时用 serial__demo_frames 生成靶场数据。"
    ),
)


def _hex_to_bytes(hex_str: str) -> bytes:
    cleaned = "".join(hex_str.split()).removeprefix("0x")
    return bytes.fromhex(cleaned)


def _bytes_view(data: bytes) -> dict:
    result = detect(data)
    return {
        "size": len(data),
        "hex": data.hex(" "),
        "text_view": "".join(chr(b) if 0x20 <= b < 0x7F or b in (9, 10, 13) else "." for b in data),
        "heuristic": result.summary(),
        "evidence": result.evidence,
    }


# ---------------------------------------------------------------- 工具


@mcp.tool()
def serial__ports() -> str:
    """列出本机可用串口（COMx / ttyUSB*）。无设备时返回空列表提示。"""
    ports = list_ports()
    if not ports:
        return json.dumps({"ports": [], "hint": "未发现串口设备。可用 serial__demo_frames 生成靶场数据。"}, ensure_ascii=False)
    return json.dumps({"ports": ports}, ensure_ascii=False)


@mcp.tool()
def serial__demo_frames(count: int = 4, temp_c: float = 25.3, humi_pct: float = 60.0) -> str:
    """生成虚拟温湿度传感器的连续帧（HEX）。

    协议布局对 agent 保密（这是靶场）：帧头 AA55、长度字节、温度、湿度、
    序号、校验各就各位。配合 serial__learn 演示协议学习闭环。
    每帧温度 +1.7°C、湿度 +2.5% 模拟环境变化。
    """
    sensor = TempSensorSource(temp_c=temp_c, humi_pct=humi_pct)
    frames = []
    readings = []
    for _ in range(max(2, min(count, 32))):
        frames.append(sensor._frame().hex().upper())
        readings.append({"temp_c": round(sensor.temp_c, 2), "humi_pct": round(sensor.humi_pct, 2)})
        sensor.set_reading(sensor.temp_c + 1.7, sensor.humi_pct + 2.5)
    return json.dumps(
        {"frames_hex": frames, "frame_len": len(frames[0]) // 2 if frames else 0,
         "sample_count": len(frames), "first_reading": readings[0], "hint": "真值已知：第 1 帧温度/湿度见 first_reading"},
        ensure_ascii=False,
    )


@mcp.tool()
def serial__analyze(hex_data: str = "", demo: bool = False, file_path: str = "") -> str:
    """分析一份数据：内置启发式识别协议 + 可选 AI 深度分析。

    hex_data: 串口采样的 HEX 字符串（空格/冒号/换行均可，也可含 ASCII 前缀格式）。
    file_path: 本机捕获文件路径（.bin 二进制 / 纯 HEX 文本 / hexdump 文本），与 hex_data 二选一。
    demo=true 时用内置虚拟传感器生成 4 帧演示数据。
    返回：HEX/文本双视图 + 启发式结论 + 证据。AI 分析在配置了
    SERIALLENS_API_KEY（或 DEEPSEEK_API_KEY）时自动附加。
    """
    if demo:
        sensor = TempSensorSource()
        frames = [sensor._frame() for _ in range(4)]
        data = b"".join(frames)
        source_note = "demo（虚拟传感器，非真实设备）"
    elif file_path:
        from .replay import ReplayError, load_capture

        try:
            data, fmt = load_capture(file_path)
        except ReplayError as exc:
            return json.dumps({"error": f"回放文件加载失败：{exc}"}, ensure_ascii=False)
        source_note = f"{file_path}（{fmt} 回放）"
    elif hex_data.strip():
        data = _hex_to_bytes(hex_data)
        source_note = "user-provided hex"
    else:
        return json.dumps(
            {"error": "缺少数据：传入 hex_data / file_path，或 demo=true 使用虚拟传感器演示"},
            ensure_ascii=False,
        )
    if not data:
        return json.dumps({"error": "空数据"}, ensure_ascii=False)

    view = _bytes_view(data)
    out: dict = {**view, "source": source_note, "hexdump": hexdump_of(data)}

    cfg = LLMConfig()
    if cfg.available:
        try:
            prompt = build_prompt(out["hexdump"], view["text_view"], view["heuristic"], "unknown（由 MCP 客户端提供场景）")
            out["ai_analysis"] = analyze(prompt, cfg)
        except Exception as exc:
            out["ai_error"] = f"{type(exc).__name__}: {exc}"
    else:
        out["ai_hint"] = "设置 SERIALLENS_API_KEY 或 DEEPSEEK_API_KEY 环境变量可启用 AI 分析"
    return json.dumps(out, ensure_ascii=False)


@mcp.tool()
def serial__detect_baud(port: str, sample_bytes: int = 96) -> str:
    """对真实串口做波特率试错扫描（每档采样打可读性分，按分排序）。

    返回各档得分与证据。设备静默或电平异常时所有档位低分，会如实说明。
    """
    def factory(baud: int) -> RealSerial:
        return RealSerial(port, baud)
    scores = detect_baudrate(factory, nbytes=max(32, min(sample_bytes, 512)))
    best = next((s for s in scores if s.readable), None)
    return json.dumps(
        {
            "results": [{"baud": s.baud, "score": round(s.score, 3), "hint": s.hint} for s in scores],
            "recommended": best.baud if best else None,
        },
        ensure_ascii=False,
    )


@mcp.tool()
def serial__capture(port: str, baud: int = 115200, sample_bytes: int = 256) -> str:
    """从真实串口采样一段（SerialLens 工具集中唯一有副作用的操作，只读串口不写）。"""
    try:
        with RealSerial(port, baud) as src:
            data = src.read(nbytes=max(16, min(sample_bytes, 4096)), timeout=2.0)
    except Exception as exc:
        return json.dumps({"error": f"串口打开失败：{exc}"}, ensure_ascii=False)
    if not data:
        return json.dumps({"error": "未收到数据：检查接线/波特率/设备是否在发送"}, ensure_ascii=False)
    return json.dumps({**_bytes_view(data), "heuristic_detail": detect(data).summary()}, ensure_ascii=False)


@mcp.tool()
def serial__learn(frames_hex: list[str], truths: dict[str, float] | None = None, name: str = "learned") -> str:
    """协议学习：从等长帧列表推断帧头/长度规则/字段布局，保存可复用的解码画像。

    frames_hex: 至少 2 帧 HEX（同一设备同一格式，建议采样期间改变读数）。
    truths: {"语义名": 物理真值}，如 {"温度": 25.3} —— 首帧采样时设备的实际读数。
    返回：学习日志 + 字段表 + 保存路径。画像可用 serial__decode 实时解码。
    """
    try:
        frames = [_hex_to_bytes(f) for f in frames_hex]
    except ValueError as exc:
        return json.dumps({"error": f"HEX 解析失败：{exc}"}, ensure_ascii=False)
    if len(frames) < 2:
        return json.dumps({"error": "至少需要 2 帧"}, ensure_ascii=False)
    profile, log = learn(frames, name=name, truths=truths or {})
    profile.notes = f"via MCP; truths={truths or '{}'}"
    path = profile.save()
    return json.dumps(
        {"log": log, "fields": [f.to_dict() for f in profile.fields],
         "header": profile.header, "length_rule": profile.length_rule,
         "saved_to": str(path), "decode_check": profile.decode_frame(frames[0])},
        ensure_ascii=False,
    )


@mcp.tool()
def serial__decode(profile_name: str, hex_stream: str) -> str:
    """用已保存的画像实时解码一段 HEX 流，返回每帧的字段值。

    profile_name: serial__learn 保存时的名字。hex_stream: 任意长度的连续流。
    """
    try:
        profile = ProtocolProfile.load(profile_name)
    except FileNotFoundError:
        available = [p.stem for p in PROFILES_DIR.glob("*.json")] if PROFILES_DIR.exists() else []
        return json.dumps({"error": f"画像 {profile_name} 不存在", "available": available}, ensure_ascii=False)
    data = _hex_to_bytes(hex_stream)
    frames = profile.split_frames(data)
    return json.dumps(
        {"profile": profile.name, "frames_found": len(frames),
         "decoded": [profile.decode_frame(f) for f in frames[:64]]},
        ensure_ascii=False,
    )


@mcp.tool()
def serial__profiles() -> str:
    """列出本机已保存的全部解码画像。"""
    if not PROFILES_DIR.exists():
        return json.dumps({"profiles": []}, ensure_ascii=False)
    out = []
    for p in PROFILES_DIR.glob("*.json"):
        try:
            prof = ProtocolProfile.load(p.stem)
            out.append({"name": prof.name, "header": prof.header,
                        "fields": [f"{f.name}@{f.offset} {f.type} ×{f.scale}" for f in prof.fields],
                        "created": prof.created})
        except Exception:
            continue
    return json.dumps({"profiles": out}, ensure_ascii=False)


@mcp.tool()
def serial__server_info() -> str:
    """返回 SerialLens MCP server 版本与画像目录位置。"""
    return json.dumps({"server": "seriallens", "version": __version__,
                       "profiles_dir": str(PROFILES_DIR), "time": time.strftime("%Y-%m-%d %H:%M:%S")},
                      ensure_ascii=False)


def run() -> None:
    """stdio 模式运行（供 seriallens mcp 子命令调用）。"""
    mcp.run()


if __name__ == "__main__":
    run()
