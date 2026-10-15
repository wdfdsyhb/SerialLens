"""SerialLens Web 智能体：FastAPI 对话壳.

架构（答辩叙事：LLM 只做语言层，确定性计算在引擎）：
  前端 → /api/chat → 意图路由 → 引擎场景（确定性） → LLM（可选，失败降级模板）

LLM 接入两路：
  - 运行时设置（/api/settings，页面内配置，存内存不落盘，重启失效）
  - 环境变量兜底（SERIALLENS_API_KEY / DEEPSEEK_API_KEY / OPENAI_API_KEY）

对话路由（按优先级）：
  1. 附件：用户上传的真实数据 → LLM 分析（无 LLM 则给引擎数据视图）
  2. 「演示」触发词：跑引擎内置虚拟数据场景（+ LLM 解读）
  3. 其余：LLM 自由对话（无 LLM 时引导上传/演示/配置，不跑虚拟数据冒充分析）

运行：uvicorn webapp.app:app --host 0.0.0.0 --port 8300
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from seriallens.analyst import LLMConfig
from seriallens.export import hexdump_of
from webapp.engine import _text_view, _view, route_intent, run_scenario

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="SerialLens Agent", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

MAX_UPLOAD = 5 * 1024 * 1024

# 运行时 LLM 配置（页面内设置；仅存内存，key 永不落盘、永不回显）
_RUNTIME_CFG: dict = {}
# 已上传文件（内存态：file_id → {name, kind, mime, raw}）
_FILES: dict = {}


class ChatRequest(BaseModel):
    message: str
    file_ids: list[str] = []


class SettingsRequest(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""


_LLM_SYSTEM = (
    "你是 SerialLens——面向嵌入式初学者与评审演示的串口诊断智能体。"
    "你会收到用户的提问和引擎已计算出的结构化结果（summary/steps/data）。"
    "规则：只基于给定结果回答，禁止编造数据或能力；简体中文、markdown、"
    "克制篇幅（300 字内）；语气像一位耐心的工程师同事；结尾可给不超过 3 条"
    "可操作建议或下一步引导。"
)

_FREE_SYSTEM = (
    "你是 SerialLens——AI 串口侦探，让 AI Agent 也能「看懂」串口数据。"
    "你的三大能力：1) 乱码诊断（位错位分析、波特率定位）；2) 协议识别（帧头/"
    "结构线索）；3) 协议学习（多帧推断字段并生成单片机 C 解析器）。"
    "用户现在与你自由对话：与能力相关的问题请自然介绍并引导用户上传数据文件"
    "（📎）或说「演示」查看内置示例；与能力无关的问题礼貌简答并把话题引回串口"
    "调试。简体中文、markdown、不超过 200 字，禁止编造你不具备的能力。"
)

_FILE_SYSTEM = (
    "你是 SerialLens——AI 串口侦探。用户上传了真实数据文件，引擎已给出"
    "HEX/文本视图与启发式识别结果。请基于给定内容分析：可能是什么协议/问题、"
    "关键特征、下一步建议。禁止编造文件中不存在的内容；证据不足就明说。"
    "简体中文、markdown、300 字内。"
)

_DEMO_WORDS = ("演示", "示例", "demo", "Demo")


# ---------------------------------------------------------------- LLM 层


def _runtime_llm() -> LLMConfig:
    """构造生效的 LLM 配置：运行时设置优先，环境变量兜底."""
    cfg = LLMConfig()
    if _RUNTIME_CFG.get("api_key"):
        cfg.api_key = _RUNTIME_CFG["api_key"]
        cfg.base_url = (_RUNTIME_CFG.get("base_url") or cfg.base_url).rstrip("/")
        cfg.model = _RUNTIME_CFG.get("model") or cfg.model
    return cfg


def _runtime_llm_available() -> bool:
    return _runtime_llm().available


def _llm_chat_completion(cfg: LLMConfig, system: str, user: str, timeout: float = 30.0) -> str:
    """统一 LLM 调用入口（OpenAI 兼容 /chat/completions）。异常向上抛，由调用方降级."""
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(
            f"{cfg.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {cfg.api_key}"},
            json={
                "model": cfg.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.3,
                "max_tokens": 800,
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


def _llm_polish(message: str, result: dict) -> str | None:
    """场景命中时：把引擎结构化结果转成自然对话回复。失败返回 None 走状态说明."""
    cfg = _runtime_llm()
    if not cfg.available:
        return None
    brief = {
        "scenario": result["scenario"],
        "title": result["title"],
        "steps": result["steps"],
        "data_brief": {
            k: v for k, v in result["data"].items()
            if k in ("frames_count", "frame_len", "header", "length_rule", "fields", "heuristic")
        },
    }
    try:
        return _llm_chat_completion(
            cfg, _LLM_SYSTEM,
            f"用户提问：{message}\n\n引擎结果：{json.dumps(brief, ensure_ascii=False)}",
        )
    except Exception:
        return None  # 演示稳定性优先：LLM 任何故障都降级到状态说明


# ---------------------------------------------------------------- 文件处理


def _file_kind(name: str, mime: str, raw: bytes) -> str:
    if mime.startswith("image/"):
        return "image"
    if name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")):
        return "image"
    printable = sum(1 for b in raw if 0x20 <= b < 0x7F or b in (9, 10, 13))
    return "text" if raw and printable / len(raw) > 0.7 else "binary"


def _file_prompt(message: str, files: list[dict]) -> str:
    parts = [f"用户提问：{message or '帮我分析这个文件'}"]
    for f in files[:3]:
        raw = f["raw"]
        if f["kind"] == "text":
            body = raw.decode("utf-8", errors="replace")[:2000]
            parts.append(f"附件 {f['name']}（文本，{len(raw)} 字节）：\n```\n{body}\n```")
        elif f["kind"] == "image":
            parts.append(
                f"附件 {f['name']}（图片，{len(raw)} 字节）。当前接口未启用视觉输入，"
                "请如实告知用户：该模型暂不支持读图，建议转述图片内容或改传文本/数据文件。"
            )
        else:
            dump = hexdump_of(raw[:256])
            parts.append(f"附件 {f['name']}（二进制，{len(raw)} 字节，前 256 字节 HEX）：\n```\n{dump}\n```")
    return "\n\n".join(parts)


def _result(scenario: str, title: str, reply: str, *, llm: bool, data: dict,
             steps: list | None = None, needs_data: bool = False) -> dict:
    return {
        "scenario": scenario,
        "title": title,
        "reply": reply,
        "summary": "",
        "steps": steps or [],
        "data": data,
        "llm_polished": llm,
        "needs_data": needs_data,
    }


# ---------------------------------------------------------------- API


@app.post("/api/chat")
def chat(req: ChatRequest) -> dict:
    scenario = route_intent(req.message)
    cfg = _runtime_llm()
    files = [_FILES[f] for f in (req.file_ids or []) if f in _FILES]

    # 1) 附件优先：真实数据分析
    if files:
        first = files[0]
        if cfg.available:
            try:
                reply = _llm_chat_completion(cfg, _FILE_SYSTEM, _file_prompt(req.message, files))
                return _result("analyze", "文件数据分析", reply, llm=True, data={"hexdump": hexdump_of(first["raw"][:128]) if first["kind"] != "text" else ""})
            except Exception:
                pass  # 降级：引擎数据视图
        view = _view(first["raw"]) if first["kind"] != "image" else {"hexdump": "", "text_view": "", "heuristic": "图片文件（暂不支持视觉分析）"}
        return _result(
            "analyze", "文件数据分析",
            "引擎已生成数据视图（见下）。在右上角 ⚙ 配置 AI 后可获得对话式分析。",
            llm=False, data=view,
        )

    # 2) 「演示」触发：引擎跑内置虚拟数据
    if any(w in req.message for w in _DEMO_WORDS):
        target = scenario if scenario != "assist" else "diagnose"
        result = run_scenario(target, req.message)
        polished = _llm_polish(req.message, result)
        if polished:
            result["reply"] = polished
            result["llm_polished"] = True
        else:
            result["llm_polished"] = False
        result["needs_data"] = False
        return result

    # 3) 其余：LLM 自由对话；无 LLM 时不跑虚拟数据冒充分析
    if cfg.available:
        try:
            reply = _llm_chat_completion(cfg, _FREE_SYSTEM, req.message)
            return _result(scenario or "assist", "对话", reply, llm=True, data={})
        except Exception:
            pass
    if scenario != "assist":
        return _result(
            scenario, "等待数据",
            "要分析你的真实数据请用 📎 上传文件；查看内置示例请说「演示」。",
            llm=False, data={}, needs_data=True,
        )
    result = run_scenario("assist")
    result["llm_polished"] = False
    result["needs_data"] = False
    return result


@app.post("/api/upload")
async def upload(file: UploadFile) -> dict:
    raw = await file.read()
    if len(raw) > MAX_UPLOAD:
        raise HTTPException(status_code=400, detail="文件超过 5MB 上限")
    if not raw:
        raise HTTPException(status_code=400, detail="空文件")
    kind = _file_kind(file.filename or "", file.content_type or "", raw)
    if kind == "text":
        preview = raw.decode("utf-8", errors="replace")[:500]
    elif kind == "image":
        preview = f"（图片，{len(raw)} 字节）"
    else:
        preview = hexdump_of(raw[:128])
    file_id = "f" + uuid.uuid4().hex[:12]
    _FILES[file_id] = {"name": file.filename or file_id, "kind": kind, "mime": file.content_type or "", "raw": raw}
    return {"file_id": file_id, "name": file.filename, "kind": kind, "size": len(raw), "preview": preview}


@app.get("/api/settings")
def get_settings() -> dict:
    return {
        "configured": _runtime_llm_available(),
        "base_url": _RUNTIME_CFG.get("base_url") or "",
        "model": _RUNTIME_CFG.get("model") or "",
    }


@app.post("/api/settings")
def save_settings(req: SettingsRequest) -> dict:
    if not (req.api_key or _RUNTIME_CFG.get("api_key")):
        raise HTTPException(status_code=400, detail="api_key 不能为空")
    if req.api_key:
        _RUNTIME_CFG["api_key"] = req.api_key
    if req.base_url:
        _RUNTIME_CFG["base_url"] = req.base_url
    if req.model:
        _RUNTIME_CFG["model"] = req.model
    return {
        "configured": _runtime_llm_available(),
        "base_url": _RUNTIME_CFG.get("base_url") or "",
        "model": _RUNTIME_CFG.get("model") or "",
    }


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "scenarios": ["diagnose", "analyze", "codegen", "assist"]}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
