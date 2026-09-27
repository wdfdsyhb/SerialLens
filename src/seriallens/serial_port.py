"""数据源抽象：真实串口与离线 demo 源共用同一套接口.

demo 源存在的原因：GitHub 上没插硬件的人也应该能 30 秒体验核心功能。
乱码模式不是假装的——真实错波特率会造成位错位，这里按位错位合成，效果一致。
"""

from __future__ import annotations

import random
import struct
import time
from typing import Iterator


class DataSource:
    """串口数据源协议：with 语法 + read()."""

    def read(self, nbytes: int = 64, timeout: float = 0.5) -> bytes:
        raise NotImplementedError

    def write(self, data: bytes) -> int:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def __enter__(self) -> "DataSource":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class RealSerial(DataSource):
    """pyserial 包装。pyserial 仅在此模块 import，无硬件环境也能跑 demo."""

    def __init__(self, port: str, baud: int, timeout: float = 0.5):
        import serial  # 延迟导入

        self._ser = serial.Serial(port, baud, timeout=timeout)

    def read(self, nbytes: int = 64, timeout: float = 0.5) -> bytes:
        self._ser.timeout = timeout
        return self._ser.read(nbytes)

    def write(self, data: bytes) -> int:
        return self._ser.write(data)

    def close(self) -> None:
        self._ser.close()


def list_ports() -> list[str]:
    """列出系统可用串口，失败返回空列表."""
    try:
        from serial.tools import list_ports as _lp

        return [p.device for p in _lp.comports()]
    except Exception:
        return []


# ---------------------------------------------------------------- demo 源

_NMEA_TEMPLATE = [
    "$GPGGA,{t},{lat},N,{lon},E,1,08,0.9,5.7,M,-3.2,M,,*",
    "$GPRMC,{t},A,{lat},N,{lon},E,0.02,84.4,260926,,,A*",
    "$GPGSV,3,1,11,03,03,111,00,04,15,270,00,06,01,010,00,13,06,292,00*",
    "$GPVTG,84.4,T,83.3,M,0.02,N,0.04,K,A*",
]


def _nmea_sentence(template: str, rnd: random.Random) -> bytes:
    """填一条完整 NMEA 语句，并算好 *XX 校验和."""
    now = time.strftime("%H%M%S", time.gmtime())
    lat = f"3014.{rnd.randint(0, 9999):04d}"
    lon = f"12006.{rnd.randint(0, 9999):04d}"
    body = template.format(t=now, lat=lat, lon=lon)
    if body.endswith("*"):
        body += "00"  # 先占位
        star = body.rfind("*")
        checksum = 0
        for ch in body[1:star]:
            checksum ^= ord(ch)
        body = body[: star + 1] + f"{checksum:02X}"
    return (body + "\r\n").encode("ascii")


def bit_shift(data: bytes, bits: int) -> bytes:
    """整流按位平移——模拟波特率不匹配时的位错位乱码.

    例：右移 2 位后，原字节 0x24 ('$') 会变成完全不同的字节，
    与真实「9600 的数据用 115200 收」的观感一致：偶见零星可读字符，整体乱码。
    """
    if bits == 0 or not data:
        return bytes(data)
    if bits > 0:  # 左移
        out = bytearray()
        carry = 0
        for b in data:
            out.append(((b << bits) & 0xFF) | carry)
            carry = b >> (8 - bits)
        return bytes(out)
    # 右移
    bits = -bits
    out = bytearray()
    carry = 0
    for b in data:
        out.append((b >> bits) | carry)
        carry = (b & ((1 << bits) - 1)) << (8 - bits)
    return bytes(out)


class DemoSource(DataSource):
    """合成数据源：mode='nmea' 正常 GPS 流 / 'garbled' 位错位乱码流."""

    def __init__(self, mode: str = "nmea", wrong_bits: int = 2, seed: int | None = None):
        if mode not in ("nmea", "garbled"):
            raise ValueError(f"未知 demo 模式: {mode}")
        self.mode = mode
        self.wrong_bits = wrong_bits
        self._rnd = random.Random(seed)

    def read(self, nbytes: int = 64, timeout: float = 0.5) -> bytes:
        buf = bytearray()
        while len(buf) < nbytes:
            template = self._rnd.choice(_NMEA_TEMPLATE)
            buf += _nmea_sentence(template, self._rnd)
        data = bytes(buf[:nbytes])
        if self.mode == "garbled":
            data = bit_shift(data, self.wrong_bits)
        return data

    def write(self, data: bytes) -> int:
        return len(data)  # demo 源只进不出


def demo_factory(mode: str) -> "type[DemoSource]":
    """给 detect_baudrate 的 port_factory 协议：callable(baud) -> source.

    合成流本身与波特率无关，但为了演示真实观感，
    只有「正确档位」返回好数据，其余档位返回位错位乱码。
    """

    class _BaudDemo(DemoSource):
        def __init__(self, baud: int):
            # 9600 视为「设备真实波特率」
            super().__init__(mode="nmea" if baud == 9600 else "garbled")

    return _BaudDemo


def iter_demo(mode: str = "nmea", interval: float = 1.0) -> Iterator[bytes]:
    """持续产出 demo 数据行，供 watch 使用."""
    src = DemoSource(mode)
    try:
        while True:
            yield src.read(nbytes=96, timeout=interval)
            time.sleep(interval)
    finally:
        src.close()


# ------------------------------------------------------ 虚拟温度传感器（学习模式靶子）

def _crc8(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


class TempSensorSource(DataSource):
    """虚拟温湿度传感器：AA 55 | len | temp u16le(×0.01°C) | humi u16be(×0.1%) | seq u8 | crc8.

    学习模式的标准靶子：帧头/长度/小端温度/大端湿度/递增序号/校验，五脏俱全。
    temp_c / humi_pct 可在多轮采样间用 set_reading() 改变，模拟真实环境变化。
    """

    def __init__(self, temp_c: float = 25.3, humi_pct: float = 60.0, seq: int = 0):
        self.temp_c = temp_c
        self.humi_pct = humi_pct
        self.seq = seq

    def set_reading(self, temp_c: float, humi_pct: float) -> None:
        self.temp_c = temp_c
        self.humi_pct = humi_pct

    def read(self, nbytes: int = 64, timeout: float = 0.5) -> bytes:
        return self._frame()

    def _frame(self) -> bytes:
        # round() 而非 int()：int 截断会让 8.2*100=819.99... 变成 819（差 0.01）
        payload = struct.pack("<H", round(self.temp_c * 100)) + struct.pack(">H", round(self.humi_pct * 10))
        body = bytes([len(payload)]) + payload + bytes([self.seq & 0xFF])
        frame = b"\xAA\x55" + body + bytes([_crc8(body)])
        self.seq += 1
        return frame

    def write(self, data: bytes) -> int:
        return len(data)
