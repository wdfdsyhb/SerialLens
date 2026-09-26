"""波特率检测单测：用合成 port_factory 验证试错扫描逻辑."""

from seriallens.baudrate import detect_baudrate, score_sample
from seriallens.serial_port import demo_factory


def test_demo_baudrate_detection_finds_9600():
    """demo 源里「设备真实波特率」是 9600，扫描应把它排第一."""
    factory = demo_factory("nmea")
    scores = detect_baudrate(factory, candidates=(115200, 9600, 57600), nbytes=128)
    assert scores[0].baud == 9600
    assert scores[0].score > 0.8
    assert scores[1].score < scores[0].score


def test_scores_sorted_desc():
    factory = demo_factory("nmea")
    scores = detect_baudrate(factory, candidates=(9600, 115200, 38400), nbytes=128)
    assert all(scores[i].score >= scores[i + 1].score for i in range(len(scores) - 1))


def test_score_sample_empty():
    score, hint = score_sample(b"")
    assert score == 0.0


def test_score_sample_garbled_low():
    from seriallens.serial_port import DemoSource

    src = DemoSource(mode="garbled", seed=1)
    score, _ = score_sample(src.read(192))
    assert score < 0.5


def test_score_sample_nmea_high():
    from seriallens.serial_port import DemoSource

    src = DemoSource(mode="nmea", seed=1)
    score, _ = score_sample(src.read(192))
    assert score > 0.6
