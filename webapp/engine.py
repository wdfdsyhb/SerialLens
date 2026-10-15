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
    # 不内置长篇话术：无 LLM 时只给状态说明，对话式解读交给配置后的 AI
    reply = (
        f"引擎完成对比分析（正常段 vs 乱码段，数据视图见下，乱码段可读率 {ratio:.0%}）。"
        "在右上角 ⚙ 配置 AI 后，可获得对话式解读与建议。"
    )
    return {
        "scenario": "diagnose",
        "title": "串口乱码诊断",
        "reply": reply,
        "summary": "",
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
    reply = "引擎完成协议识别（帧头与结构线索，数据视图见下）。配置 AI 后可获得对话式解读。"
    return {
        "scenario": "analyze",
        "title": "协议识别分析",
        "reply": reply,
        "summary": "",
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
    reply = "协议学习与 C 解析器生成完成（字段表与代码见下）。配置 AI 后可获得对话式解读。"
    return {
        "scenario": "codegen",
        "title": "协议学习与 C 解析器生成",
        "reply": reply,
        "summary": "",
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
        "请先在右上角 ⚙ 配置 AI，即可开始真实对话。"
        "也可以说「演示乱码诊断」查看内置示例，或用 📎 上传串口数据文件体验协议分析与 C 代码生成。"
    )
    return {
        "scenario": "assist",
        "title": "能力自述",
        "reply": reply,
        "summary": "",
        "steps": ["解析提问意图 → 未配置 AI，转为配置引导"],
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
