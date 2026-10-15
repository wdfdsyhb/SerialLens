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

import base64
import json
import os
import secrets
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
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
# 会话消息历史（内存态：session_id → [{role, content}]）——多轮上下文
_SESSIONS: dict = {}
_HISTORY_LIMIT = 10

# 访问门禁：密码优先级 = 环境变量 > webapp/.env.local（本地文件，不入库） > 自动生成
_ENV_LOCAL = Path(__file__).parent / ".env.local"


def _load_password() -> str:
    env_pw = os.environ.get("SERIALLENS_ACCESS_PASSWORD")
    if env_pw:
        return env_pw
    if _ENV_LOCAL.exists():
        val = _ENV_LOCAL.read_text(encoding="utf-8").strip()
        if val:
            return val
    return secrets.token_urlsafe(6)


_ACCESS_PASSWORD: str = _load_password()
# 已签发的会话令牌（内存态）
_TOKENS: set = set()
_PUBLIC_API = {"/api/login", "/api/health"}

if os.environ.get("SERIALLENS_ACCESS_PASSWORD") is None and not _ENV_LOCAL.exists():
    # 自动生成时打印一次，便于部署者记录并提交给评审
    print(f"[SerialLens] 本次运行的访问密码（提交评审用）：{_ACCESS_PASSWORD}  "
          f"（设置 SERIALLENS_ACCESS_PASSWORD 环境变量可自定义固定密码）", flush=True)


class ChatRequest(BaseModel):
    message: str
    file_ids: list[str] = []
    session_id: str = ""


class LoginRequest(BaseModel):
    password: str = ""


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
    "你是 SerialLens——AI 串口侦探。用户上传了真实数据（文本日志、HEX/二进制或图片），"
    "引擎已给出 HEX/文本视图与启发式识别结果。请基于给定内容分析：可能是什么协议/问题、"
    "关键特征、下一步建议。若附带图片，请直接基于图片实际内容观察与描述（如板卡、"
    "接线、示波器/逻辑分析仪截图、报错界面）。禁止编造不存在的内容；证据不足就明说。"
    "简体中文、markdown、300 字内。"
)

_DEMO_WORDS = ("演示", "示例", "demo", "Demo")


# ---------------------------------------------------------------- LLM 层


def _guess_image_mime(raw: bytes) -> str:
    if raw.startswith(b"\x89PNG"):
        return "image/png"
    if raw.startswith(b"\xFF\xD8"):
        return "image/jpeg"
    if raw.startswith((b"GIF8",)):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def _vision_messages(system: str, user: str, images: list[bytes] | None, mime: str = "image/png",
                     history: list[dict] | None = None) -> list[dict]:
    """构造 OpenAI 兼容消息：无图=纯文本；有图=content 数组（text + image_url base64）。历史插在当前消息之前."""
    msgs: list[dict] = [{"role": "system", "content": system}]
    msgs.extend(history or [])
    if not images:
        msgs.append({"role": "user", "content": user})
    else:
        content: list[dict] = [{"type": "text", "text": user}]
        for raw in images:
            b64 = base64.b64encode(raw).decode()
            content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
        msgs.append({"role": "user", "content": content})
    return msgs


def _session_history(sid: str) -> list[dict]:
    if not sid:
        return []
    return list(_SESSIONS.get(sid, []))[-_HISTORY_LIMIT:]


def _append_history(sid: str, role: str, content: str) -> None:
    if not sid:
        return
    _SESSIONS.setdefault(sid, []).append({"role": role, "content": content})
    hist = _SESSIONS[sid]
    if len(hist) > _HISTORY_LIMIT * 3:      # 硬上限防内存膨胀
        del hist[: len(hist) - _HISTORY_LIMIT * 2]


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


def _llm_chat_completion(cfg: LLMConfig, system: str, user: str, images: list[bytes] | None = None,
                         mime: str = "image/png", timeout: float = 30.0,
                         history: list[dict] | None = None) -> str:
    """统一 LLM 调用入口（OpenAI 兼容 /chat/completions，支持视觉与多轮历史）。异常向上抛，由调用方降级."""
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(
            f"{cfg.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {cfg.api_key}"},
            json={
                "model": cfg.model,
                "messages": _vision_messages(system, user, images, mime, history),
                "temperature": 0.3,
                "max_tokens": 800,
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


def _llm_stream_completion(cfg: LLMConfig, system: str, messages: list[dict], timeout: float = 40.0):
    """流式 LLM 调用：messages 为 [{role, content}]（不含 system），逐块 yield 文本增量。异常向上抛."""
    full = [{"role": "system", "content": system}] + list(messages)
    with httpx.Client(timeout=timeout) as client:
        with client.stream(
            "POST",
            f"{cfg.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {cfg.api_key}"},
            json={"model": cfg.model, "messages": full, "temperature": 0.3, "max_tokens": 800, "stream": True},
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                delta = (obj.get("choices") or [{}])[0].get("delta", {}).get("content")
                if delta:
                    yield delta


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
    parts = [f"用户提问：{message or '帮我分析这些数据'}"]
    for f in files[:3]:
        raw = f["raw"]
        if f["kind"] == "text":
            body = raw.decode("utf-8", errors="replace")[:2000]
            parts.append(f"附件 {f['name']}（文本，{len(raw)} 字节）：\n```\n{body}\n```")
        elif f["kind"] == "image":
            parts.append(f"附件 {f['name']}（图片，{len(raw)} 字节，已通过视觉输入通道附加给模型）")
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


# ---------------------------------------------------------------- 访问门禁


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """API 鉴权：除公开端点外，所有 /api/* 需要有效会话令牌."""
    path = request.url.path
    if path.startswith("/api/") and path not in _PUBLIC_API:
        token = request.headers.get("X-Access-Token", "")
        if not token or token not in _TOKENS:
            return JSONResponse({"detail": "未授权：请先输入访问密码"}, status_code=401)
    return await call_next(request)


@app.post("/api/login")
def login(req: LoginRequest) -> dict:
    if not req.password or not secrets.compare_digest(req.password, _ACCESS_PASSWORD):
        raise HTTPException(status_code=401, detail="访问密码不正确")
    token = secrets.token_urlsafe(24)
    _TOKENS.add(token)
    return {"token": token}


@app.get("/api/access")
def access_info() -> dict:
    """已登录用户查看当前访问密码（用于设置面板展示 / 提交给评委）."""
    return {"password": _ACCESS_PASSWORD}


# ---------------------------------------------------------------- API


@app.post("/api/chat")
def chat(req: ChatRequest) -> dict:
    scenario = route_intent(req.message)
    cfg = _runtime_llm()
    files = [_FILES[f] for f in (req.file_ids or []) if f in _FILES]

    # 1) 附件优先：真实数据分析（图片走视觉通道）
    if files:
        first = files[0]
        if cfg.available:
            images = [f["raw"] for f in files if f["kind"] == "image"][:3]
            img_file = next((f for f in files if f["kind"] == "image"), None)
            mime = (img_file["mime"] if img_file and img_file.get("mime") else None) or (img_file and _guess_image_mime(img_file["raw"])) or "image/png"
            try:
                reply = _llm_chat_completion(
                    cfg, _FILE_SYSTEM, _file_prompt(req.message, files),
                    images=images or None, mime=mime,
                    history=_session_history(req.session_id),
                )
                _append_history(req.session_id, "user", req.message or "（分析上传文件）")
                _append_history(req.session_id, "assistant", reply)
                return _result("analyze", "文件数据分析", reply, llm=True, data={"hexdump": hexdump_of(first["raw"][:128]) if first["kind"] != "text" else ""})
            except Exception:
                pass  # 降级：引擎数据视图
        view = _view(first["raw"]) if first["kind"] != "image" else {"hexdump": "", "text_view": "", "heuristic": "图片文件：在 ⚙ 配置支持视觉的模型后可分析"}
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

    # 3) 其余：LLM 自由对话（带会话历史）；无 LLM 时不跑虚拟数据冒充分析
    if cfg.available:
        history = _session_history(req.session_id)
        try:
            reply = _llm_chat_completion(cfg, _FREE_SYSTEM, req.message, history=history)
            _append_history(req.session_id, "user", req.message)
            _append_history(req.session_id, "assistant", reply)
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


def _sse(ev: str, payload) -> str:
    return f"event: {ev}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.post("/api/chat/stream")
def chat_stream(req: ChatRequest):
    """SSE 流式对话：meta → delta*（流式文本）→ result（完整结构）→ done."""

    def gen():
        scenario = route_intent(req.message)
        cfg = _runtime_llm()
        files = [_FILES[f] for f in (req.file_ids or []) if f in _FILES]

        # 1) 附件
        if files:
            yield _sse("meta", {"scenario": "analyze", "title": "文件数据分析"})
            first = files[0]
            data_view = {"hexdump": hexdump_of(first["raw"][:128]) if first["kind"] != "text" else ""}
            if cfg.available:
                images = [f["raw"] for f in files if f["kind"] == "image"][:3]
                img_file = next((f for f in files if f["kind"] == "image"), None)
                mime = (img_file["mime"] if img_file and img_file.get("mime") else None) or (img_file and _guess_image_mime(img_file["raw"])) or "image/png"
                hist = _session_history(req.session_id)
                try:
                    if images:
                        # 视觉消息走一次性调用（content 数组含图），不做逐字流
                        reply = _llm_chat_completion(cfg, _FILE_SYSTEM, _file_prompt(req.message, files),
                                                     images=images, mime=mime, history=hist)
                        yield _sse("delta", {"text": reply})
                    else:
                        parts = []
                        for chunk in _llm_stream_completion(cfg, _FILE_SYSTEM,
                                                            hist + [{"role": "user", "content": _file_prompt(req.message, files)}]):
                            parts.append(chunk)
                            yield _sse("delta", {"text": chunk})
                        reply = "".join(parts)
                    _append_history(req.session_id, "user", req.message or "（分析上传文件）")
                    _append_history(req.session_id, "assistant", reply)
                    yield _sse("result", _result("analyze", "文件数据分析", reply, llm=True, data=data_view))
                    yield _sse("done", {})
                    return
                except Exception:
                    pass
            fallback = "引擎已生成数据视图（见下）。在右上角 ⚙ 配置 AI 后可获得对话式分析。"
            view = _view(first["raw"]) if first["kind"] != "image" else {"hexdump": "", "text_view": "", "heuristic": "图片文件：在 ⚙ 配置支持视觉的模型后可分析"}
            yield _sse("delta", {"text": fallback})
            yield _sse("result", _result("analyze", "文件数据分析", fallback, llm=False, data=view))
            yield _sse("done", {})
            return

        # 2) 演示触发
        if any(w in req.message for w in _DEMO_WORDS):
            target = scenario if scenario != "assist" else "diagnose"
            result = run_scenario(target, req.message)
            yield _sse("meta", {"scenario": result["scenario"], "title": result["title"]})
            reply = result["reply"]
            if cfg.available:
                brief = {
                    "scenario": result["scenario"], "title": result["title"], "steps": result["steps"],
                    "data_brief": {k: v for k, v in result["data"].items()
                                   if k in ("frames_count", "frame_len", "header", "length_rule", "fields", "heuristic")},
                }
                try:
                    parts = []
                    for chunk in _llm_stream_completion(cfg, _LLM_SYSTEM,
                                                        [{"role": "user", "content": f"用户提问：{req.message}\n\n引擎结果：{json.dumps(brief, ensure_ascii=False)}"}]):
                        parts.append(chunk)
                        yield _sse("delta", {"text": chunk})
                    reply = "".join(parts)
                    result["llm_polished"] = True
                except Exception:
                    reply = result["reply"]           # 降级回状态说明
                    result["llm_polished"] = False
            else:
                result["llm_polished"] = False
                yield _sse("delta", {"text": reply})
            result["reply"] = reply
            result["needs_data"] = False
            yield _sse("result", result)
            yield _sse("done", {})
            return

        # 3) 自由对话 / 引导
        if cfg.available:
            yield _sse("meta", {"scenario": scenario or "assist", "title": "对话"})
            hist = _session_history(req.session_id)
            parts = []
            try:
                for chunk in _llm_stream_completion(cfg, _FREE_SYSTEM,
                                                    hist + [{"role": "user", "content": req.message}]):
                    parts.append(chunk)
                    yield _sse("delta", {"text": chunk})
                reply = "".join(parts)
                _append_history(req.session_id, "user", req.message)
                _append_history(req.session_id, "assistant", reply)
                yield _sse("result", _result(scenario or "assist", "对话", reply, llm=True, data={}))
            except Exception:
                # 流中断：已收文本尽量保留，降级为引导语收尾
                partial = "".join(parts)
                fallback = partial or "AI 服务暂时不可用。可上传数据文件（📎），或说「演示」查看内置示例。"
                yield _sse("delta", {"text": fallback if not partial else ""})
                yield _sse("result", _result(scenario or "assist", "对话", fallback, llm=False, data={}))
            yield _sse("done", {})
            return

        # 无 LLM：引导（不跑虚拟数据冒充分析）
        if scenario != "assist":
            reply = "要分析你的真实数据请用 📎 上传文件；查看内置示例请说「演示」。"
            yield _sse("meta", {"scenario": scenario, "title": "等待数据"})
            yield _sse("delta", {"text": reply})
            yield _sse("result", _result(scenario, "等待数据", reply, llm=False, data={}, needs_data=True))
        else:
            result = run_scenario("assist")
            result["llm_polished"] = False
            result["needs_data"] = False
            yield _sse("meta", {"scenario": "assist", "title": result["title"]})
            yield _sse("delta", {"text": result["reply"]})
            yield _sse("result", result)
        yield _sse("done", {})

    return StreamingResponse(gen(), media_type="text/event-stream")


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
