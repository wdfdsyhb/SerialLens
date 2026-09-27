"""文件回放测试：三种捕获格式 + 错误路径."""

import pytest

from seriallens.replay import ReplayError, load_capture
from seriallens.serial_port import TempSensorSource


def _sensor_stream(n: int = 3, temp: float = 25.3, humi: float = 60.0) -> bytes:
    sensor = TempSensorSource(temp_c=temp, humi_pct=humi)
    return b"".join(sensor._frame() for _ in range(n))


def _hexdump_text(data: bytes, width: int = 9) -> str:
    lines = []
    for off in range(0, len(data), width):
        chunk = data[off : off + width]
        hexpart = " ".join(f"{b:02X}" for b in chunk)
        ascii_part = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in chunk)
        lines.append(f"{off:04X}  {hexpart}  {ascii_part}")
    return "\n".join(lines) + "\n"


class TestFormats:
    def test_binary_file(self, tmp_path):
        expected = _sensor_stream(3)
        f = tmp_path / "capture.bin"
        f.write_bytes(expected)
        data, fmt = load_capture(f)
        assert data == expected
        assert fmt == "二进制"

    def test_pure_hex_text(self, tmp_path):
        expected = _sensor_stream(2)
        f = tmp_path / "capture.hex"
        f.write_text(" ".join(f"{b:02X}" for b in expected) + "\n", encoding="ascii")
        data, fmt = load_capture(f)
        assert data == expected
        assert "HEX" in fmt

    def test_hexdump_with_offsets_and_ascii(self, tmp_path):
        expected = _sensor_stream(4)
        f = tmp_path / "capture_dump.txt"
        f.write_text(_hexdump_text(expected), encoding="ascii")
        data, fmt = load_capture(f)
        assert data == expected
        assert "hexdump" in fmt

    def test_hexdump_with_comments_and_separators(self, tmp_path):
        expected = _sensor_stream(2)
        body = " ".join(f"{b:02X}" for b in expected)
        f = tmp_path / "mixed.log"
        f.write_text(
            f"# 设备日志导出\n"
            f"0000:  {body}  //第一帧起\n"
            f"\n"
            f"{body}\n",
            encoding="utf-8",
        )
        data, _fmt = load_capture(f)
        assert data == expected

    def test_colon_separated_hex(self, tmp_path):
        expected = b"\xaa\x55\x04"
        f = tmp_path / "colon.txt"
        f.write_text("AA:55:04\n", encoding="ascii")
        data, _ = load_capture(f)
        assert data == expected

    def test_empty_file_rejected(self, tmp_path):
        f = tmp_path / "empty.bin"
        f.write_bytes(b"")
        with pytest.raises(ReplayError, match="为空"):
            load_capture(f)

    def test_missing_file_rejected(self, tmp_path):
        with pytest.raises(ReplayError, match="不存在"):
            load_capture(tmp_path / "nope.bin")

    def test_garbage_text_rejected(self, tmp_path):
        """纯非 HEX 文本（如英文日志）应报错而非猜语义。"""
        f = tmp_path / "words.txt"
        f.write_text("hello world this is not serial data\n", encoding="ascii")
        with pytest.raises(ReplayError):
            load_capture(f)
