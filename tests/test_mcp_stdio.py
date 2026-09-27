"""MCP stdio 协议级烟测：spawn 真实子进程，验证握手、工具调用与 stdout 纯净度.

这是单测（工具函数直调）之上的协议层保险：确保 entry point 能被真实
MCP 客户端拉起、响应合法、stdout 没有混入破坏 JSON-RPC 的日志噪声。
"""

import json
import subprocess
import sys
import threading
import time

import pytest

EXPECTED_TOOLS = {
    "serial__ports", "serial__demo_frames", "serial__analyze",
    "serial__detect_baud", "serial__capture", "serial__learn",
    "serial__decode", "serial__profiles", "serial__server_info",
}


def _probe(calls: list[dict], timeout: float = 45.0) -> tuple[dict[int, dict], list[str]]:
    proc = subprocess.Popen(
        [sys.executable, "-c", "from seriallens.mcp_server import run; run()"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    responses: dict[int, dict] = {}
    junk: list[str] = []

    def reader():
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                junk.append(line)
                continue
            if isinstance(msg.get("id"), int):
                responses[msg["id"]] = msg

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        payload = b"".join((json.dumps(r) + "\n").encode("utf-8") for r in calls)
        proc.stdin.write(payload)
        proc.stdin.flush()
        deadline = time.time() + timeout
        expected_ids = {r["id"] for r in calls if "id" in r}
        while time.time() < deadline and not expected_ids.issubset(responses):
            time.sleep(0.1)
    finally:
        proc.kill()
    return responses, junk


def test_stdio_handshake_and_safe_tools():
    """握手 + tools/list + 无副作用工具全部真实调用成功，stdout 纯净."""
    calls = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "serial__server_info", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "serial__demo_frames", "arguments": {"count": 3}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "serial__analyze", "arguments": {"demo": True}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
         "params": {"name": "serial__analyze", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
         "params": {"name": "serial__decode", "arguments": {"profile_name": "无", "hex_stream": "AA55"}}},
    ]
    responses, junk = _probe(calls)
    assert not junk, f"stdout 被日志污染：{junk[:2]}"

    init = responses[1]["result"]["serverInfo"]
    assert init["name"] == "seriallens"
    tool_names = {t["name"] for t in responses[2]["result"]["tools"]}
    assert tool_names == EXPECTED_TOOLS

    # 无副作用工具：结果 content[0].text 必须是合法 JSON
    for rid in (3, 4, 5, 7):
        text = responses[rid]["result"]["content"][0]["text"]
        json.loads(text)  # 抛错即失败
    # analyze 空输入应给出指引性错误而不是崩溃
    assert "error" in json.loads(responses[6]["result"]["content"][0]["text"])
    # decode 不存在的画像：结构化错误 + available 列表
    assert "error" in json.loads(responses[7]["result"]["content"][0]["text"])


def test_stdio_learn_decode_roundtrip():
    """协议层闭环：demo_frames -> learn -> decode 经真实 stdio 完成."""
    calls: list[dict] = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "serial__demo_frames", "arguments": {"count": 5}}},
    ]
    responses, _junk = _probe(calls)
    demo = json.loads(responses[2]["result"]["content"][0]["text"])
    assert len(demo["frames_hex"]) == 5

    # 第二个会话：learn + decode（同一进程内连续调用）
    calls2 = calls[:2] + [
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "serial__learn", "arguments": {
             "frames_hex": demo["frames_hex"],
             "truths": {"温度": demo["first_reading"]["temp_c"],
                        "湿度": demo["first_reading"]["humi_pct"]},
             "name": "stdio-loop"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "serial__decode", "arguments": {
             "profile_name": "stdio-loop",
             "hex_stream": "".join(demo["frames_hex"])}}},
    ]
    responses2, junk2 = _probe(calls2)
    assert not junk2
    learned = json.loads(responses2[2]["result"]["content"][0]["text"])
    names = {f["name"] for f in learned["fields"]}
    assert {"温度", "湿度"}.issubset(names)
    decoded = json.loads(responses2[3]["result"]["content"][0]["text"])
    assert decoded["frames_found"] == 5
    assert abs(decoded["decoded"][0]["温度"] - demo["first_reading"]["temp_c"]) < 0.01
