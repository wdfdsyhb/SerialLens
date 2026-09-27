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

    def test_split_drops_trailing_partial_frame(self):
        """读到一半的残帧不进帧池，否则污染对齐."""
        sensor = TempSensorSource()
        stream = sensor._frame() * 3 + sensor._frame()[:4]
        frames, mode, header, _rule = split_frames(stream)
        assert mode == "header" and header == "AA55"
        assert len(frames) == 3
        assert all(len(f) == 9 for f in frames)


class TestRawBlockLearning:
    def test_learn_from_raw_blocks_continuous_stream(self):
        """真实设备路径：每轮读到的是连续原始字节块（起点任意），learn 应自动切帧.

        旧实现把整块当「一帧」传给 learn：连续流设备逐字节错位 + 真值只看首块，
        产出高置信度错误画像。
        """
        readings = [(25.3, 60.0), (27.0, 62.5), (30.1, 65.0)]
        sensor = TempSensorSource()
        blocks: list[bytes] = []
        truths: dict[str, float] = {}
        for i, (temp, humi) in enumerate(readings):
            sensor.set_reading(temp, humi)
            stream = b"".join(sensor._frame() for _ in range(30))  # 270 字节连续流
            start = 1 + i * 3  # 每块从帧中间开始，模拟任意时刻开始读
            blocks.append(stream[start : start + 200])
            truths.setdefault("温度", temp)
            truths.setdefault("湿度", humi)
        profile, log = learn(blocks, name="raw", truths=truths)

        assert profile.header == "AA55", f"帧头学习错误: {log}"
        decoded = {f.name: f for f in profile.fields}
        assert decoded["温度"].offset == 3 and decoded["温度"].type == "u16le"
        assert abs(decoded["温度"].scale - 0.01) < 1e-9
        assert decoded["湿度"].offset == 5 and decoded["湿度"].type == "u16be"
        assert abs(decoded["湿度"].scale - 0.1) < 1e-9
        # 终极验证：画像能解码未参与学习的帧
        probe = TempSensorSource(28.7, 63.0)._frame()
        values = profile.decode_frame(probe)
        assert abs(values["温度"] - 28.7) < 0.01
        assert abs(values["湿度"] - 63.0) < 0.01

    def test_modal_length_filter_ignores_leading_partial_frame(self):
        """等长过滤以众数长度为基准：首帧是残帧时不应全军覆没（旧实现按首帧过滤）."""
        good = [TempSensorSource()._frame() for _ in range(3)]
        partial = good[0][:4]
        profile, _log = learn([partial] + good, name="modal", truths={})
        assert profile.fields
        assert all(f.offset >= 2 for f in profile.fields)

    def test_length_rule_not_clobbered_by_fixed_fallback(self):
        """推导出的 byte@N+K 规则不应被 fixed:N 无条件覆盖（旧代码条件恒真）."""
        frames, truths = _collect_rounds([(25.3, 60.0), (27.0, 62.5), (30.1, 65.0)])
        profile, _log = learn(frames, name="rule", truths=truths)
        assert profile.length_rule.startswith("byte@")


class TestLengthRule:
    def test_profile_decodes_stream(self, tmp_path):
        frames, truths = _collect_rounds([(25.3, 60.0), (27.0, 62.5), (30.1, 65.0)])
        profile, _ = learn(frames, name="stream", truths=truths)
        sensor = TempSensorSource(26.0, 61.0)
        stream = sensor._frame() + sensor._frame() + sensor._frame()
        results = profile.decode_stream(stream)
        assert len(results) == 3
        assert all(abs(r["温度"] - 26.0) < 0.01 for r in results)

    def test_chunked_stream_decode_no_frame_loss(self):
        """流式分块解码：合成流分 64 字节块喂入，跨块边界的帧不丢（尾部结转）.

        旧 watch 实现每块独立 decode_stream，跨块帧被静默丢弃。
        """
        frames, truths = _collect_rounds([(25.3, 60.0), (27.0, 62.5), (30.1, 65.0)])
        profile, _ = learn(frames, name="chunked", truths=truths)
        sensor = TempSensorSource(temp_c=26.0, humi_pct=61.0)
        stream = b"".join(sensor._frame() for _ in range(50))  # 450 字节，64 不整除
        total, buf = 0, bytearray()
        for i in range(0, len(stream), 64):
            buf.extend(stream[i : i + 64])
            results, tail = profile.split_stream(bytes(buf))
            buf = bytearray(tail)
            assert all(abs(r["温度"] - 26.0) < 0.01 for r in results)
            total += len(results)
        assert total == 50
        assert not buf  # 全部消费完，无残留

    def test_split_stream_keeps_partial_frame_tail(self):
        """split_stream 应把不完整帧作为尾部返回，拼上剩余字节后能解出."""
        frames, truths = _collect_rounds([(25.3, 60.0), (27.0, 62.5), (30.1, 65.0)])
        profile, _ = learn(frames, name="tail", truths=truths)
        whole = TempSensorSource(26.0, 61.0)._frame()
        head_part, tail = profile.split_stream(whole[:5])
        assert head_part == []
        assert tail == whole[:5]
        rest, tail2 = profile.split_stream(tail + whole[5:])
        assert len(rest) == 1
        assert tail2 == b""
        assert abs(profile.decode_frame(rest[0])["温度"] - 26.0) < 0.01

    def test_crc8_known_vector(self):
        # CRC-8/ATM 标准测试向量："123456789" -> 0xF4
        assert _crc8(b"123456789") == 0xF4
