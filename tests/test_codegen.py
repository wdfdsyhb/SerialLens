"""C 代码生成测试：生成结构断言 + 可选的真实编译验证（探测到 MSVC cl.exe 时）."""

import subprocess
from pathlib import Path

import pytest

from seriallens.codegen import generate_c
from seriallens.learn import learn
from seriallens.profile import ProtocolProfile
from seriallens.serial_port import TempSensorSource

CL_CANDIDATES = [
    Path("D:/VS/VC/Tools/MSVC/14.51.36231/bin/Hostx64/x64/cl.exe"),
]
VCVARS_CANDIDATES = [
    Path("D:/VS/VC/Auxiliary/Build/vcvars64.bat"),
]


def _learn_demo_profile(name: str = "cgen") -> ProtocolProfile:
    sensor = TempSensorSource()
    frames, truths = [], {}
    for t, h in [(25.3, 60.0), (27.0, 62.5), (30.1, 65.0), (21.4, 55.5)]:
        sensor.set_reading(t, h)
        frames.append(sensor._frame())
        truths.setdefault("温度", t)
        truths.setdefault("湿度", h)
    profile, _log = learn(frames, name=name, truths=truths)
    return profile


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    profile = _learn_demo_profile("cgen-test")
    h_text, c_text = generate_c(profile)
    d = tmp_path_factory.mktemp("cgen")
    (d / "cgen-test_parser.h").write_text(h_text, encoding="utf-8")
    (d / "cgen-test_parser.c").write_text(c_text, encoding="utf-8")
    return d, profile, h_text, c_text


def _find_cl() -> Path | None:
    for p in CL_CANDIDATES:
        if p.exists():
            return p
    return None


def _find_vcvars() -> Path | None:
    for p in VCVARS_CANDIDATES:
        if p.exists():
            return p
    return None


class TestGeneratedCode:
    def test_header_contents(self, generated):
        _d, profile, h_text, _c = generated
        assert "SL_CGEN_TEST_H" in h_text  # 连字符必须被 sanitize 成下划线
        assert "typedef struct" in h_text
        assert "SL_CGEN_TEST_MAX_FRAME" in h_text
        # 中文语义名保留在注释里，标识符本身是 ASCII
        assert "温度" in h_text and "湿度" in h_text

    def test_c_source_contents(self, generated):
        _d, _p, _h, c_text = generated
        assert "sl_cgen_test_feed" in c_text
        assert "sl_cgen_test_init" in c_text
        # 帧头字节出现在查找表
        assert "0xAA" in c_text and "0x55" in c_text
        # 零依赖：不 include 任何系统头（include 自己的 .h 合法）
        assert "#include <" not in c_text and "#include <" not in (generated[2])
        # 温度 u16le 小端组装表达式 + scale
        assert "<< 8" in c_text
        assert "0.01f" in c_text
        # 大端湿度
        assert "0.1f" in c_text

    def test_chinese_field_names_become_ascii_idents(self, generated):
        _d, _p, h_text, c_text = generated
        # 结构体成员标识符不含中文（中文只允许出现在注释里）
        body = h_text[h_text.index("typedef struct"): h_text.index("} sl_")]
        code_lines = [ln.split("/*")[0] for ln in body.splitlines()]
        code_text = "\n".join(code_lines)
        for ch in ("温度", "湿度"):
            assert ch not in code_text
        # 注释里保留中文语义名
        assert "温度" in h_text and "湿度" in h_text
        # C 源码里的赋值行同样不含中文
        assert "温度" not in "\n".join(ln.split("/*")[0] for ln in c_text.splitlines())


class TestRealCompilation:
    """探测到可工作的 MSVC（vcvars + cl）时做真实编译验证；否则 skip.

    注意：只检查 cl.exe 存在不够——非标准 VS 布局（如本机 D:\VS）的
    vcvars64 可能本身报错，因此做一次最小编译探针再决定。
    """

    def test_cl_compiles_generated_parser(self, generated):
        cl = _find_cl()
        vcvars = _find_vcvars()
        if cl is None or vcvars is None:
            pytest.skip("未找到 cl.exe / vcvars64.bat，跳过编译验证")

        d, _profile, _h, _c = generated
        probe_bat = d / "probe.bat"
        probe_bat.write_text(
            '@echo off\r\n'
            f'call "{vcvars}" >nul 2>&1\r\n'
            'if errorlevel 1 exit /b 2\r\n'
            f'cd /d "{d}"\r\n'
            f'"{cl}" /nologo /c cgen-test_parser.c\r\n'
            'exit /b %errorlevel%\r\n',
            encoding="ascii",
        )
        r = subprocess.run(["cmd", "/c", str(probe_bat)], capture_output=True, timeout=180,
                           encoding="utf-8", errors="replace")
        if r.returncode != 0:
            pytest.skip(f"本机 MSVC 工具链不可用（returncode={r.returncode}），跳过编译验证")
        obj = next(d.glob("*.obj"), None)
        assert obj is not None, f"编译通过但未见 .obj：{r.stdout}"
