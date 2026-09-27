"""数据源编码单测：虚拟传感器字段编码应四舍五入而非截断."""

from struct import unpack

from seriallens.serial_port import TempSensorSource


def test_temp_sensor_encoding_rounds_not_truncates():
    """8.2*100=819.99...，int() 截断成 819（差 0.01），round() 才是正确编码."""
    sensor = TempSensorSource(temp_c=8.2, humi_pct=61.7)
    frame = sensor._frame()
    temp_raw = unpack("<H", frame[3:5])[0]
    humi_raw = unpack(">H", frame[5:7])[0]
    assert temp_raw == 820
    assert humi_raw == 617
