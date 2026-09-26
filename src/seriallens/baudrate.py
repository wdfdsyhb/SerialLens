"""波特率自动检测：同一份数据用不同波特率收，最「像话」的那个就是答案.

串口层拿不到电平宽度（那是示波器的活），但可以换一个思路：
用候选波特率各收一段，对结果打「可读性分」——
错波特率下位错位，收到的是乱码；对波特率下 ASCII 可读比例、
行完整性、NMEA 校验和都会显著更好。这是纯软件就能做的诚实方案。
"""

from __future__ import annotations

from dataclasses import dataclass

# 覆盖绝大多数实战场景（按出现频率排序，先试常见的省时间）
COMMON_BAUDRATES: tuple[int, ...] = (
    115200, 9600, 57600, 38400, 19200, 4800, 2400, 230400, 460800,
)

from .protocols import detect, printable_ratio


@dataclass
class BaudScore:
    baud: int
    score: float
    hint: str

    @property
    def readable(self) -> bool:
        return self.score >= 0.5


def score_sample(data: bytes) -> tuple[float, str]:
    """给一段收到的字节打可读性分（0-1），返回 (分数, 理由)."""
    if not data:
        return 0.0, "空"
    ratio = printable_ratio(data)
    result = detect(data)
    # 权重：可打印率是大头，协议置信度做加成
    score = ratio * 0.7 + result.confidence * 0.3
    return min(score, 1.0), result.summary()


def _read_via(port_factory, baud: int, nbytes: int, timeout: float) -> bytes:
    """用指定波特率开一次口、收一段、关掉。port_factory(baud) 需支持 with 语法."""
    with port_factory(baud) as port:
        return port.read(nbytes=nbytes, timeout=timeout)


def detect_baudrate(
    port_factory,
    candidates: tuple[int, ...] = COMMON_BAUDRATES,
    nbytes: int = 96,
    timeout: float = 0.6,
) -> list[BaudScore]:
    """逐个波特率试收并打分，按分数降序返回.

    port_factory: callable(baud:int) -> 上下文管理器，暴露 read(nbytes, timeout)。
    真实串口与 demo 源都实现这个协议，方便测试与离线演示。
    """
    results: list[BaudScore] = []
    for baud in candidates:
        try:
            data = _read_via(port_factory, baud, nbytes, timeout)
        except Exception as exc:  # 口被占用/无设备等，跳过该档
            results.append(BaudScore(baud, -1.0, f"打开失败: {exc}"))
            continue
        score, hint = score_sample(data)
        results.append(BaudScore(baud, score, hint))
    results.sort(key=lambda s: s.score, reverse=True)
    return results
