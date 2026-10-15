"""webapp 场景引擎：意图路由 + 确定性场景执行.

设计（评委演示优先）：
- 四个场景全部基于内置虚拟数据（评委无硬件可全流程体验）
- 场景输出确定性：固定 seed / 固定初始读数，两次执行结果一致
- 引擎只做确定性计算；LLM 润色是 app 层的可选增强，引擎不依赖网络

场景故事线（对应 PPT 三个提示词示例）：
  diagnose  乱码诊断 —— 正常帧 vs 位错位帧对比，指向波特率不匹配
  analyze   协议分析 —— 虚拟传感器采样，启发式识别帧头/等长帧线索
  codegen   协议学习 —— 多帧学习出字段画像，生成零依赖 C 解析器
  assist    自述引导 —— 角色说明 + 三个场景的体验指引
"""

from __future__ import annotations

from pathlib import Path

from seriallens.analyst import LLMConfig
from seriallens.codegen import generate_c
from seriallens.export import hexdump_of
from seriallens.learn import learn
from seriallens.profile import ProtocolProfile
from seriallens.protocols import detect
from seriallens.serial_port import DemoSource, TempSensorSource

SCENARIOS = ("diagnose", "analyze", "codegen", "assist")

WEBAPP_PROFILES_DIR = Path(__file__).parent / ".profiles"

# 关键词路由：diagnose 优先（高频痛点），codegen 先于 analyze
# （「学习协议并生成代码」同时命中两者，应按更具体的意图走）
_KEYWORDS: dict[str, tuple[str, ...]] = {
    "diagnose": ("乱码", "看不懂", "错位", "花屏", "没输出", "没反应"),
    "codegen": ("生成", "c 代码", "c代码", "解析器", "解析代码", "单片机", "学习这个协议", "解码器"),
    "analyze": ("分析", "协议", "识别", "帧结构", "传感器数据", "这是什么"),
}

_GARBLED_SEED = 7


def route_intent(message: str) -> str:
    """关键词意图路由。未命中返回 assist（自述+引导），不猜."""
    text = (message or "").strip().lower()
    for name in ("diagnose", "codegen", "analyze"):
        if any(k in text for k in _KEYWORDS[name]):
            return name
    return "assist"


def _text_view(data: bytes) -> str:
    return "".join(chr(b) if 0x20 <= b < 0x7F or b in (9, 10, 13) else "." for b in data)


def _view(data: bytes) -> dict:
    det = detect(data)
    return {
        "hexdump": hexdump_of(data),
        "text_view": _text_view(data),
        "heuristic": det.summary(),
    }


def _printable_ratio(text_view: str) -> float:
    if not text_view:
        return 0.0
    printable = sum(1 for ch in text_view if ch not in ".")
    return round(printable / len(text_view), 2)


# ---------------------------------------------------------------- 场景实现


def _scenario_diagnose() -> dict:
    normal = DemoSource(mode="nmea", seed=_GARBLED_SEED)
    garbled = DemoSource(mode="garbled", wrong_bits=2, seed=_GARBLED_SEED)
    normal_data = normal.read(96)
    garbled_data = garbled.read(96)

    normal_view = _view(normal_data)
    garbled_view = _view(garbled_data)
    ratio = _printable_ratio(garbled_view["text_view"])

    steps = [
        "采样两段数据：同参数下「正常接收」与「疑似乱码」各一段（虚拟演示源）",
        "对两段分别做文本视图渲染 + 启发式识别",
        f"对比可打印率：乱码段可打印字符占比骤降至 {ratio:.0%}",
        "结论：位错位特征 → 首查波特率是否匹配",
    ]
    summary = (
        "### 诊断结论\n\n"
        f"- 正常段：NMEA 语句完整可读，`$GPGGA…` 结构清晰\n"
        f"- 乱码段：可打印率仅 **{ratio:.0%}**，字节分布无任何协议特征\n"
        f"- 特征：典型的**位错位**——与「波特率不匹配（如 9600 的数据用 115200 收）」的观感一致\n\n"
        "### 建议下一步\n\n"
        "1. 逐一尝试常见波特率（9600 / 115200 / 38400 / 19200）\n"
        "2. 用 SerialLens 的自动波特率扫描：逐档试收 + 可读性打分，按分排序\n"
        "3. 若所有档位都低分，检查 TX/RX 是否接反、设备供电是否稳定"
    )
    reply = (
        "我看了一下你这两段数据：同样参数下，一段是完整的 NMEA 语句，"
        f"另一段几乎全是乱码（可读字符只剩 {ratio:.0%}）。这种「整体乱、偶见零星可读字符」"
        "是典型的位错位特征——最常见的原因就是**波特率不匹配**：设备在 9600 发，"
        "你却用 115200 收（或反过来）。先对调这两个值试试；不行的话我可以跑一轮"
        "自动波特率扫描，把常见档位全部试收打分。"
    )
    return {
        "scenario": "diagnose",
        "title": "串口乱码诊断",
        "reply": reply,
        "summary": summary,
        "steps": steps,
        "data": {"normal": normal_view, "garbled": garbled_view},
    }


def _scenario_analyze() -> dict:
    sensor = TempSensorSource(temp_c=25.3, humi_pct=60.0)
    frames = []
    for _ in range(4):
        frames.append(sensor._frame())
        sensor.set_reading(sensor.temp_c + 1.7, sensor.humi_pct + 2.5)
    data = b"".join(frames)
    view = _view(data)
    frame_len = len(frames[0])

    steps = [
        f"采样 4 帧（虚拟温湿度传感器，帧长 {frame_len} 字节，等长）",
        "启发式识别：帧头统计 / JSON / NMEA / 文本判别",
        "发现稳定帧头 + 等长帧结构 → 判定为自定义二进制帧",
        "字段语义未定 → 需多帧采样学习（见「协议学习」场景）",
    ]
    summary = (
        "### 识别结果\n\n"
        f"- **帧数**：{len(frames)} 帧，等长 {frame_len} 字节\n"
        f"- **启发式**：{view['heuristic']}\n"
        "- 帧头稳定出现，判断为**自定义二进制协议**（非 NMEA / JSON / 纯文本）\n\n"
        "### 帧结构线索\n\n"
        "帧头之后存在长度字节与序号字节的特征，但字段边界、类型（温度/湿度？）、"
        "字节序与比例尺无法从单帧看出——继续用「协议学习」场景多帧推断。"
    )
    reply = (
        f"这段数据是 {len(frames)} 帧等长的自定义二进制数据，帧头稳定、结构规整——"
        "不是 NMEA 也不是 JSON，是典型的传感器私有协议。我能看出帧的骨架"
        "（帧头、长度、序号），但字段是什么物理量、什么字节序、带不带比例尺，"
        "需要多帧对比才能推断。你可以说「学习这个协议并生成 C 解析器」，"
        "我拿这段采样继续往下走。"
    )
    return {
        "scenario": "analyze",
        "title": "协议识别分析",
        "reply": reply,
        "summary": summary,
        "steps": steps,
        "data": {
            "frames_count": len(frames),
            "frame_len": frame_len,
            "hexdump": view["hexdump"],
            "text_view": view["text_view"],
            "heuristic": view["heuristic"],
        },
    }


def _scenario_codegen() -> dict:
    sensor = TempSensorSource(temp_c=25.3, humi_pct=60.0)
    frames = []
    for _ in range(8):
        frames.append(sensor._frame())
        sensor.set_reading(sensor.temp_c + 1.7, sensor.humi_pct + 2.5)
    truths = {"温度": 25.3, "湿度": 60.0}
    profile, log = learn(frames, name="web_demo_sensor", truths=truths)
    WEBAPP_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    saved = profile.save(directory=WEBAPP_PROFILES_DIR)
    c_header, c_source = generate_c(profile)
    fields = [f.to_dict() for f in profile.fields]

    steps = [
        "采样 8 帧（读数持续变化，保证字段可分）",
        "标注首帧真值：温度 25.3°C / 湿度 60.0%",
        "多帧差分 → 推断帧头、长度规则、字段偏移/类型/字节序/比例尺",
        f"画像保存为 web_demo_sensor（{saved}）",
        "generate_c：画像 → 零依赖流式状态机解析器（.h / .c）",
    ]
    summary = (
        "### 学习结果\n\n"
        f"- 帧头：`{profile.header}`\n"
        f"- 长度规则：`{profile.length_rule}`\n"
        f"- 字段（{len(fields)} 个）："
        + "、".join(f"{f['name']}@{f['offset']}({f['type']}" + (f"×{f['scale']}" if f.get("scale") != 1 else "") + ")" for f in fields)
        + "\n\n### 生成物\n\n"
        "- `web_demo_sensor_parser.h` / `.c`：零系统头依赖，流式状态机，跨块帧不丢\n"
        "- 诚实原则：未推断出校验算法就不生成校验代码，注释中说明"
    )
    reply = (
        f"学习完成。8 帧对比 + 首帧真值锚定，我推断出这是个 `{profile.header}` 帧头的协议，"
        f"包含 {len(fields)} 个字段（温度、湿度各自的偏移、类型、比例尺都已定位），"
        "画像已保存、可复用解码。同时我把画像编译成了两份 C 文件——零依赖、"
        "流式状态机，直接扔进单片机工程就能用。往下翻可以看到完整代码和字段表。"
    )
    return {
        "scenario": "codegen",
        "title": "协议学习与 C 解析器生成",
        "reply": reply,
        "summary": summary,
        "steps": steps,
        "data": {
            "frames_count": len(frames),
            "header": profile.header,
            "length_rule": profile.length_rule,
            "fields": fields,
            "learn_log": log,
            "profile_name": "web_demo_sensor",
            "saved_to": str(saved),
            "c_header": c_header,
            "c_source": c_source,
        },
    }


def _scenario_assist() -> dict:
    reply = (
        "我是 SerialLens——AI 串口侦探，让 AI Agent 也能「看懂」串口数据。"
        "我能做三类事：\n\n"
        "1. **乱码诊断**：串口收到乱码看不懂？说「我的串口全是乱码」，"
        "我分析位错位特征、定位波特率问题\n"
        "2. **协议分析**：一段陌生传感器数据？说「帮我分析这段数据是什么协议」，"
        "我识别帧头与结构线索\n"
        "3. **协议学习**：说「学习这个协议并生成 C 解析器」，"
        "我推断字段布局，生成能直接编译进单片机的解析代码\n\n"
        "（当前为无硬件演示模式，内置虚拟传感器数据；接入真实串口能力相同。）"
    )
    return {
        "scenario": "assist",
        "title": "能力自述",
        "reply": reply,
        "summary": "",
        "steps": ["解析提问意图 → 未命中技能关键词，转为能力自述与场景引导"],
        "data": {},
    }


_IMPL = {
    "diagnose": _scenario_diagnose,
    "analyze": _scenario_analyze,
    "codegen": _scenario_codegen,
    "assist": _scenario_assist,
}


def run_scenario(name: str, message: str = "") -> dict:
    """执行场景。name 必须在 SCENARIOS 内，否则 ValueError（调用方决定兜底）."""
    if name not in _IMPL:
        raise ValueError(f"未知场景: {name}（可用: {', '.join(SCENARIOS)}）")
    return _IMPL[name]()


def llm_enabled() -> bool:
    """LLM 增强层是否可用（app 层用；引擎本身不依赖）."""
    return LLMConfig().available
