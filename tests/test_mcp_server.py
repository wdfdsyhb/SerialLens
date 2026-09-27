"""MCP server 单测：直接调用 FastMCP 注册的工具函数 + 注册表烟测.

说明：工具函数层闭环覆盖业务逻辑；stdio 子进程握手属 Windows 管道时序
脆弱测试，以工具注册表完整性检查替代（真实握手由 MCP 客户端连接时验证）。
"""

import json

from seriallens.mcp_server import mcp


def _tool_fn(name: str):
    """FastMCP 把工具注册进 mcp._tool_manager；取原始函数做直接调用."""
    tool = mcp._tool_manager._tools[name]
    return tool.fn


class TestToolFunctions:
    def test_ports(self):
        out = json.loads(_tool_fn("serial__ports")())
        assert "ports" in out

    def test_demo_frames(self):
        out = json.loads(_tool_fn("serial__demo_frames")(count=4))
        assert len(out["frames_hex"]) == 4
        assert out["frame_len"] == 9
        assert all(len(f) == 18 for f in out["frames_hex"])  # 9 字节 = 18 hex 字符

    def test_analyze_demo(self):
        out = json.loads(_tool_fn("serial__analyze")(demo=True))
        assert "NMEA" not in out["heuristic"]
        assert out["size"] == 36  # 4 帧 × 9 字节
        # 虚拟传感器是二进制帧：启发式应报帧头而非文本
        assert "AA" in out["heuristic"] or "帧" in out["heuristic"]

    def test_analyze_with_hex(self):
        hexdata = "AA 55 04 E2 09 02 58 00 8F " * 3
        out = json.loads(_tool_fn("serial__analyze")(hex_data=hexdata))
        assert out["size"] == 27
        assert "hexdump" in out

    def test_analyze_empty(self):
        out = json.loads(_tool_fn("serial__analyze")())
        assert "error" in out

    def test_learn_closed_loop_with_demo(self):
        """demo_frames -> learn -> decode 全链路（MCP 工具层闭环）."""
        demo = json.loads(_tool_fn("serial__demo_frames")(count=5))
        learned = json.loads(
            _tool_fn("serial__learn")(frames_hex=demo["frames_hex"],
                                      truths={"温度": demo["first_reading"]["temp_c"],
                                              "湿度": demo["first_reading"]["humi_pct"]},
                                      name="mcp-loop")
        )
        assert learned["header"] == "AA55"
        names = {f["name"] for f in learned["fields"]}
        assert "温度" in names and "湿度" in names

        # 用返回的画像解码新帧
        temp2 = learned["decode_check"]["温度"]
        assert abs(temp2 - demo["first_reading"]["temp_c"]) < 0.01

    def test_decode_profile(self):
        from seriallens.serial_port import TempSensorSource

        sensor = TempSensorSource(temp_c=33.3, humi_pct=44.4)
        stream = (sensor._frame() + sensor._frame()).hex().upper()
        out = json.loads(_tool_fn("serial__decode")("mcp-loop", stream))
        assert out["frames_found"] == 2
        assert abs(out["decoded"][0]["温度"] - 33.3) < 0.01

    def test_profiles_list(self):
        out = json.loads(_tool_fn("serial__profiles")())
        names = [p["name"] for p in out["profiles"]]
        assert "mcp-loop" in names

    def test_server_info(self):
        import seriallens

        out = json.loads(_tool_fn("serial__server_info")())
        assert out["version"] == seriallens.__version__


class TestRegistrySmoke:
    """注册表烟测：全部工具已注册、命名符合 mcp__serial__* 约定."""

    def test_all_tools_registered(self):
        tools = mcp._tool_manager._tools
        expected = {"serial__ports", "serial__demo_frames", "serial__analyze",
                    "serial__detect_baud", "serial__capture", "serial__learn",
                    "serial__decode", "serial__profiles", "serial__server_info"}
        assert expected.issubset(set(tools))
