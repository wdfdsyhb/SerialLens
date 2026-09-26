"""协议识别单测：合成数据验证启发式."""

from seriallens.protocols import detect, printable_ratio
from seriallens.serial_port import DemoSource, bit_shift


def _nmea_line(body: str, checksum: str) -> bytes:
    return f"${body}*{checksum}\r\n".encode()


def _correct_nmea(body: str) -> bytes:
    cs = 0
    for ch in body:
        cs ^= ord(ch)
    return f"${body}*{cs:02X}\r\n".encode()


class TestNmea:
    def test_valid_nmea_detected(self):
        data = _correct_nmea("GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,") * 3
        r = detect(data)
        assert "NMEA" in r.protocol
        assert r.confidence > 0.8
        # 校验和应该全部通过
        assert any("通过" in ev for ev in r.evidence)

    def test_bad_checksum_lowers_confidence(self):
        good = _correct_nmea("GPRMC,123519,A,4807.038,N,01131.000,E,0.02,84.4,260926,,,A")
        bad = _nmea_line("GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,", "FF")
        r_good = detect(good * 4)
        r_mixed = detect(good * 2 + bad * 2)
        assert r_good.confidence > r_mixed.confidence

    def test_demo_nmea_source_detected(self):
        src = DemoSource(mode="nmea", seed=42)
        data = src.read(nbytes=256)
        r = detect(data)
        assert "NMEA" in r.protocol
        assert r.confidence > 0.7


class TestBinary:
    def test_known_framing(self):
        payload = bytes([0xAA, 0x55, 0x03, 0x1F, 0x40]) + bytes(range(10)) + b"\xAA\x55\x02\x00"
        r = detect(payload * 2)
        assert "AA 55" in r.protocol or "帧" in r.protocol

    def test_garbled_is_unknown_binary(self):
        src = DemoSource(mode="garbled", seed=42)
        data = src.read(nbytes=192)
        r = detect(data)
        assert r.printable_ratio < 0.85
        # 乱码不应被认成 NMEA
        assert "NMEA" not in r.protocol

    def test_bit_shift_corrupts(self):
        original = b"$GPGGA,hello world,12345\r\n" * 3
        shifted = bit_shift(original, 2)
        assert shifted != original
        assert printable_ratio(shifted) < printable_ratio(original)


class TestText:
    def test_plain_text(self):
        data = b"Temperature: 25.3 C\r\nHumidity: 60 %\r\nOK\r\n"
        r = detect(data)
        assert "文本" in r.protocol

    def test_json(self):
        data = b'{"temp":25.3,"ok":true}\n{"temp":25.4,"ok":true}\n'
        r = detect(data)
        assert "JSON" in r.protocol

    def test_empty(self):
        r = detect(b"")
        assert r.confidence == 0.0
