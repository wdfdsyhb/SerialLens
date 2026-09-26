"""协议学习闭环单测：虚拟传感器 -> learn() -> 画像 -> 解码还原真值."""

from seriallens.learn import learn, split_frames, align, cluster_dynamic
from seriallens.profile import ProtocolProfile
from seriallens.serial_port import TempSensorSource, _crc8


def _collect_rounds(temp_seq: list[tuple[float, float]]) -> tuple[list[bytes], dict[str, float]]:
    """按 (温度, 湿度) 序列生成多轮采样帧，返回 (帧列表, 真值表)."""
    sensor = TempSensorSource()
    frames, truths = [], {}
    for temp, humi in temp_seq:
        sensor.set_reading(temp, humi)
        frames.append(sensor._frame())
        truths.setdefault("温度", temp)
        truths.setdefault("湿度", humi)
    return frames, truths


class TestLearnClosedLoop:
    def test_full_loop_recovers_fields(self):
        frames, truths = _collect_rounds([(25.3, 60.0), (27.0, 62.5), (30.1, 65.0), (21.4, 55.5)])
        profile, log = learn(frames, name="demo-sensor", truths=truths)

        # 帧头学习正确
        assert profile.header == "AA55"
        # 静态帧头不进动态字段
        assert all(f.offset >= 2 for f in profile.fields)

        decoded = {f.name: f for f in profile.fields}
        # 温度：u16le @3，scale 0.01 —— 绑定到用户真值
        assert "温度" in decoded, f"温度未绑定: {log}"
        temp = decoded["温度"]
        assert temp.type == "u16le" and temp.offset == 3
        assert abs(temp.scale - 0.01) < 1e-9
        # 湿度：u16be @5，scale 0.1
        assert "湿度" in decoded, f"湿度未绑定: {log}"
        humi = decoded["湿度"]
        assert humi.type == "u16be" and humi.offset == 5
        assert abs(humi.scale - 0.1) < 1e-9

        # 终极验证：画像解码任意一帧应还原该轮真值
        sensor = TempSensorSource(temp_c=28.7, humi_pct=63.0)
        frame = sensor._frame()
        values = profile.decode_frame(frame)
        assert abs(values["温度"] - 28.7) < 0.01
        assert abs(values["湿度"] - 63.0) < 0.01

    def test_profile_save_load_decode_roundtrip(self, tmp_path):
        frames, truths = _collect_rounds([(25.3, 60.0), (27.0, 62.5), (30.1, 65.0)])
        profile, _ = learn(frames, name="roundtrip", truths=truths)
        path = profile.save(directory=tmp_path)
        loaded = ProtocolProfile.load("roundtrip", directory=tmp_path)
        assert path.exists()
        assert loaded.header == profile.header
        assert len(loaded.fields) == len(profile.fields)
        values = loaded.decode_frame(frames[0])
        assert abs(values["温度"] - 25.3) < 0.01

    def test_too_few_frames_warns(self):
        frames, _ = _collect_rounds([(25.3, 60.0)])
        profile, log = learn(frames, name="thin", truths={})
        assert any("不足 2 帧" in line for line in log)

    def test_identical_frames_no_fields(self):
        frames = [TempSensorSource(25.3, 60.0)._frame()] * 4  # 同一帧重复
        profile, log = learn(frames, name="static", truths={})
        assert not profile.fields
        assert any("完全一致" in line for line in log)


class TestFrameSplitting:
    def test_split_by_discovered_header(self):
        sensor = TempSensorSource()
        stream = sensor._frame() + sensor._frame() + sensor._frame()
        frames, mode, header, rule = split_frames(stream)
        assert len(frames) == 3
        assert mode == "header"
        assert header == "AA55"
        assert all(len(f) == 9 for f in frames)

    def test_split_lines(self):
        data = b"OK\r\nERROR\r\nOK\r\n"
        frames, mode, _, _ = split_frames(data)
        assert mode == "line"
        assert len(frames) == 3


class TestLengthRule:
    def test_profile_decodes_stream(self, tmp_path):
        frames, truths = _collect_rounds([(25.3, 60.0), (27.0, 62.5), (30.1, 65.0)])
        profile, _ = learn(frames, name="stream", truths=truths)
        sensor = TempSensorSource(26.0, 61.0)
        stream = sensor._frame() + sensor._frame() + sensor._frame()
        results = profile.decode_stream(stream)
        assert len(results) == 3
        assert all(abs(r["温度"] - 26.0) < 0.01 for r in results)

    def test_crc8_known_vector(self):
        # CRC-8/ATM 标准测试向量："123456789" -> 0xF4
        assert _crc8(b"123456789") == 0xF4
