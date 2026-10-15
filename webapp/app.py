"""SerialLens Web 智能体：FastAPI 对话壳.

架构（答辩叙事：LLM 只做语言层，确定性计算在引擎）：
  前端 → /api/chat → 意图路由 → 引擎场景（确定性） → LLM（可选，失败降级模板）

LLM 接入两路：
  - 运行时设置（/api/settings，页面内配置，存内存不落盘，重启失效）
  - 环境变量兜底（SERIALLENS_API_KEY / DEEPSEEK_API_KEY / OPENAI_API_KEY）

运行：uvicorn webapp.app:app --host 0.0.0.0 --port 8300
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from seriallens.analyst import LLMConfig
from webapp.engine import route_intent, run_scenario

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="SerialLens Agent", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# 运行时 LLM 配置（页面内设置；仅存内存，key 永不落盘、永不回显）
_RUNTIME_CFG: dict = {}


class ChatRequest(BaseModel):
    message: str


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
    "用户现在与你自由对话：与能力相关的问题请自然介绍并引导用户用对应说法触发"
    "（例如「我的串口收到一堆乱码」「学习这个协议并生成 C 解析器」）；"
    "与能力无关的问题礼貌简答并把话题引回串口调试。简体中文、markdown、"
    "不超过 200 字，禁止编造你不具备的能力。"
)


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


def _llm_chat_completion(cfg: LLMConfig, system: str, user: str, timeout: float = 20.0) -> str:
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
    """场景命中时：把引擎结构化结果转成自然对话回复。失败返回 None 走模板."""
    cfg = _runtime_llm()
    if not cfg.available:
        return None
    brief = {
        "scenario": result["scenario"],
        "title": result["title"],
        "summary": result["summary"],
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
        return None  # 演示稳定性优先：LLM 任何故障都降级到模板回复


# ---------------------------------------------------------------- API


@app.post("/api/chat")
def chat(req: ChatRequest) -> dict:
    scenario = route_intent(req.message)
    result = run_scenario(scenario, req.message)

    cfg = _runtime_llm()
    if cfg.available:
        try:
            if scenario == "assist":
                # 未命中技能 → LLM 自由对话（不再固定话术）
                result["reply"] = _llm_chat_completion(cfg, _FREE_SYSTEM, req.message)
            else:
                polished = _llm_polish(req.message, result)
                if polished:
                    result["reply"] = polished
            result["llm_polished"] = True
        except Exception:
            result["llm_polished"] = False
    else:
        result["llm_polished"] = False
    return result


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
        from fastapi import HTTPException

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
