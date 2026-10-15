"""webapp.engine 场景引擎测试：意图路由 + 四个场景的确定性输出."""

from __future__ import annotations

import pytest

from webapp.engine import SCENARIOS, route_intent, run_scenario

# ---------------------------------------------------------------- 意图路由


class TestRouteIntent:
    def test_garbled_diagnosis(self):
        assert route_intent("我的串口收到一堆乱码，完全看不懂") == "diagnose"

    def test_garbled_english_word(self):
        assert route_intent("串口输出乱码怎么排查") == "diagnose"

    def test_protocol_analysis(self):
        assert route_intent("帮我分析这段传感器数据是什么协议") == "analyze"

    def test_codegen(self):
        assert route_intent("学习这个协议并生成 C 解析器代码") == "codegen"

    def test_codegen_variant(self):
        assert route_intent("能给我生成单片机上用的解析代码吗") == "codegen"

    def test_capability_fallback(self):
        assert route_intent("你能做什么？") == "assist"

    def test_unknown_fallback(self):
        assert route_intent("今天天气不错") == "assist"


# ---------------------------------------------------------------- 场景执行


class TestDiagnoseScenario:
    def test_returns_structured_result(self):
        res = run_scenario("diagnose")
        assert res["scenario"] == "diagnose"
        assert res["title"]
        assert res["reply"]
        data = res["data"]
        assert data["normal"]["hexdump"]
        assert data["garbled"]["hexdump"]
        # 乱码段的文本视图必须与正常段明显不同（位错位生效）
        assert data["garbled"]["text_view"] != data["normal"]["text_view"]

    def test_deterministic(self):
        a = run_scenario("diagnose")
        b = run_scenario("diagnose")
        assert a["data"]["garbled"]["hexdump"] == b["data"]["garbled"]["hexdump"]


class TestAnalyzeScenario:
    def test_returns_frames_and_heuristic(self):
        res = run_scenario("analyze")
        data = res["data"]
        assert data["frames_count"] >= 2
        assert data["hexdump"]
        assert data["frame_len"] > 0

    def test_deterministic(self):
        assert run_scenario("analyze")["data"]["hexdump"] == run_scenario("analyze")["data"]["hexdump"]


class TestCodegenScenario:
    def test_returns_c_code(self):
        res = run_scenario("codegen")
        data = res["data"]
        assert "sl_u8" in data["c_header"]          # 零依赖 typedef 特征
        assert "AA55" in data["c_header"]           # 帧头出现在生成代码
        assert data["c_source"]
        assert data["fields"]                        # 推断出的字段表

    def test_profile_saved_locally(self):
        res = run_scenario("codegen")
        assert res["data"]["profile_name"] == "web_demo_sensor"
        from pathlib import Path

        saved = Path(res["data"]["saved_to"])
        assert saved.exists()


class TestAssistScenario:
    def test_self_intro_guides_three_scenarios(self):
        res = run_scenario("assist")
        for kw in ("乱码", "协议", "C"):
            assert kw in res["reply"]


class TestRunScenarioContract:
    def test_unknown_scenario_raises(self):
        with pytest.raises(ValueError):
            run_scenario("nope")

    def test_all_scenarios_have_reply_and_steps(self):
        for name in SCENARIOS:
            res = run_scenario(name)
            assert res["reply"], name
            assert isinstance(res["steps"], list) and res["steps"], name
