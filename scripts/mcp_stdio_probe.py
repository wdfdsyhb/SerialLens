"""MCP stdio 全流程调试脚本：spawn 真实 server 子进程，走完整 JSON-RPC 会话.

用法：python scripts/mcp_stdio_probe.py
对全部 9 个工具发起真实 tools/call，验证响应合法性（不校验业务细节——那是单测的活）。
关注：协议合规性、编码、超时、stdout 纯净度（混入非 JSON 行 = 协议破坏）。
"""

import json
import subprocess
import sys
import threading
import time

REQS = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
     "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "probe", "version": "0"}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    # tools/call 逐个来
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
     "params": {"name": "serial__server_info", "arguments": {}}},
    {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
     "params": {"name": "serial__ports", "arguments": {}}},
    {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
     "params": {"name": "serial__demo_frames", "arguments": {"count": 5}}},
    {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
     "params": {"name": "serial__analyze", "arguments": {"demo": True}}},
    {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
     "params": {"name": "serial__analyze", "arguments": {}}},
    {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
     "params": {"name": "serial__profiles", "arguments": {}}},
    {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
     "params": {"name": "serial__decode", "arguments": {"profile_name": "不存在的画像", "hex_stream": "AA55"}}},
]


def main() -> int:
    p = subprocess.Popen(
        [sys.executable, "-c", "from seriallens.mcp_server import run; run()"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    responses: dict[int, dict] = {}
    junk: list[str] = []

    def reader():
        for raw in p.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                junk.append(line[:100])
                continue
            if isinstance(msg.get("id"), int):
                responses[msg["id"]] = msg

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    payload = b"".join((json.dumps(r) + "\n").encode("utf-8") for r in REQS)
    p.stdin.write(payload)
    p.stdin.flush()

    deadline = time.time() + 60
    while time.time() < deadline and len(responses) < len(REQS) - 1:
        time.sleep(0.2)

    p.kill()

    ok, bad = 0, 0
    for i, req in enumerate(REQS):
        rid = req.get("id")
        if rid is None:
            continue
        r = responses.get(rid)
        name = req.get("method") if "method" in req else req.get("params", {}).get("name", "?")
        if r is None:
            print(f"  [{rid}] {name}: NO RESPONSE  ❌")
            bad += 1
            continue
        if "error" in r:
            print(f"  [{rid}] {name}: JSON-RPC error {r['error'].get('code')} {str(r['error'].get('message'))[:80]}  ❌")
            bad += 1
            continue
        result = r.get("result", {})
        if name == "tools/call" or ("name" in req.get("params", {})):
            content = result.get("content", [])
            text = content[0].get("text", "") if content else ""
            # 结果必须是合法 JSON（我们的工具全部返回 json.dumps）
            try:
                json.loads(text)
                print(f"  [{rid}] {req['params']['name']}: ok, {len(text)} chars  ✓")
                ok += 1
            except (json.JSONDecodeError, IndexError):
                print(f"  [{rid}] {req['params']['name']}: content 不是合法 JSON  ❌ head={text[:80]!r}")
                bad += 1
        else:
            info = result.get("serverInfo", {})
            ntools = len(result.get("tools", []))
            print(f"  [{rid}] {name}: ok  server={info.get('name')} tools={ntools}  ✓")
            ok += 1

    print(f"\nstdout 纯净度: {len(junk)} 行非 JSON 噪声 {('[' + junk[0] + ']') if junk else ''}")
    print(f"结果: {ok} ok / {bad} bad")
    return 1 if bad or junk else 0


if __name__ == "__main__":
    sys.exit(main())
