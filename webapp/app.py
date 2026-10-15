"""SerialLens Web 智能体：FastAPI 对话壳.

架构（答辩叙事：LLM 只做语言层，确定性计算在引擎）：
  前端 → /api/chat → 意图路由 → 引擎场景（确定性） → LLM 润色（可选，失败降级模板）

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


class ChatRequest(BaseModel):
    message: str


_LLM_SYSTEM = (
    "你是 SerialLens——面向嵌入式初学者与评审演示的串口诊断智能体。"
    "你会收到用户的提问和引擎已计算出的结构化结果（summary/steps/data）。"
    "规则：只基于给定结果回答，禁止编造数据或能力；简体中文、markdown、"
    "克制篇幅（300 字内）；语气像一位耐心的工程师同事；结尾可给不超过 3 条"
    "可操作建议或下一步引导。"
)


def _llm_polish(message: str, result: dict) -> str | None:
    """可选 LLM 润色：把引擎结构化结果转成自然对话回复。失败返回 None 走模板."""
    cfg = LLMConfig()
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
        with httpx.Client(timeout=20.0) as client:
            resp = client.post(
                f"{cfg.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {cfg.api_key}"},
                json={
                    "model": cfg.model,
                    "messages": [
                        {"role": "system", "content": _LLM_SYSTEM},
                        {"role": "user", "content": f"用户提问：{message}\n\n引擎结果：{json.dumps(brief, ensure_ascii=False)}"},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 800,
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
    except Exception:
        return None  # 演示稳定性优先：LLM 任何故障都降级到模板回复


@app.post("/api/chat")
def chat(req: ChatRequest) -> dict:
    scenario = route_intent(req.message)
    result = run_scenario(scenario, req.message)
    polished = _llm_polish(req.message, result)
    if polished:
        result["reply"] = polished
        result["llm_polished"] = True
    else:
        result["llm_polished"] = False
    return result


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "scenarios": ["diagnose", "analyze", "codegen", "assist"]}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
