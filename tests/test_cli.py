"""CLI 层冒烟测试：命令走通不崩、产物落盘（README 宣传的入口）."""

from seriallens import profile as profile_mod
from seriallens.cli import main


class TestDemoSmoke:
    def test_demo_detect_runs(self, tmp_path):
        """demo --detect 曾因引用未定义的 factory 必然 NameError，回归保护."""
        out = tmp_path / "report.md"
        rc = main(["demo", "--detect", "--out", str(out)])
        assert rc == 0
        assert out.exists()

    def test_demo_garbled_runs(self, tmp_path):
        out = tmp_path / "report.md"
        rc = main(["demo", "--garbled", "--out", str(out)])
        assert rc == 0
        assert out.exists()


class TestLearnSmoke:
    def test_learn_demo_sensor_cli(self, tmp_path, monkeypatch):
        """learn --demo-sensor 端到端：保存画像且字段绑定正确."""
        monkeypatch.setattr(profile_mod, "PROFILES_DIR", tmp_path)
        rc = main(["learn", "--demo-sensor", "--rounds", "5", "--name", "cli-smoke"])
        assert rc == 0
        loaded = profile_mod.ProtocolProfile.load("cli-smoke", directory=tmp_path)
        assert loaded.header == "AA55"
        assert loaded.length_rule.startswith("byte@")
        names = {f.name for f in loaded.fields}
        assert {"温度", "湿度"} <= names
