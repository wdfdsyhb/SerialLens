"""SerialLens 命令行入口.

子命令：
  ports     列出系统串口
  watch     实时监视（HEX/文本双视图）
  detect    自动波特率检测
  analyze   采样 + 启发式 + AI 分析 -> Markdown 报告
  demo      无硬件演示（内置合成数据源）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .analyst import LLMConfig, analyze, build_prompt
from .baudrate import COMMON_BAUDRATES, detect_baudrate
from .export import Session, hexdump_of, render
from .protocols import detect
from .serial_port import RealSerial, demo_factory, list_ports

console = Console()
err = Console(stderr=True)

BAUD_CHOICES = [str(b) for b in COMMON_BAUDRATES]


def _text_view(data: bytes, limit: int = 600) -> str:
    txt = "".join(chr(b) if 0x20 <= b < 0x7F or b in (9, 10, 13) else "." for b in data)
    return txt[:limit]


# ---------------------------------------------------------------- ports

def cmd_ports(args: argparse.Namespace) -> int:
    ports = list_ports()
    if not ports:
        console.print("[yellow]未发现串口设备（或驱动未装）。[/yellow]")
        return 1
    table = Table(title="可用串口")
    table.add_column("端口", style="cyan")
    for p in ports:
        table.add_row(p)
    console.print(table)
    return 0


# ---------------------------------------------------------------- demo

def cmd_demo(args: argparse.Namespace) -> int:
    """无硬件演示：合成 GPS 流或乱码流，走完整检测+分析管线."""
    mode = "garbled" if args.garbled else "nmea"
    source = f"demo:{mode}"
    console.print(f"[dim]演示模式：{source}（合成数据，无需硬件）[/dim]\n")

    from .serial_port import DemoSource

    with DemoSource(mode=mode) as src:
        data = src.read(nbytes=args.sample, timeout=0.5)

    result = detect(data)
    console.print(Panel(result.summary(), title="本地启发式识别", border_style="cyan"))

    if args.detect:
        scores = detect_baudrate(factory, nbytes=args.sample)
        table = Table(title="波特率试错扫描（demo）")
        table.add_column("波特率", style="cyan")
        table.add_column("可读性分", justify="right")
        table.add_column("证据")
        for s in scores[:5]:
            style = "green" if s.readable else "dim"
            table.add_row(str(s.baud), f"{s.score:.2f}", s.hint, style=style)
        console.print(table)

    return _finish_analysis(data, source, str(args.baud), result.summary(), args)


# ---------------------------------------------------------------- detect

def cmd_detect(args: argparse.Namespace) -> int:
    factory = _real_factory(args.port)
    console.print(f"[dim]正在扫描 {args.port} 的波特率（每档采样 {args.sample} 字节）…[/dim]")
    scores = detect_baudrate(factory, nbytes=args.sample)
    table = Table(title=f"{args.port} 波特率检测")
    table.add_column("波特率", style="cyan")
    table.add_column("可读性分", justify="right")
    table.add_column("证据")
    best = None
    for s in scores:
        if s.score < 0:
            table.add_row(str(s.baud), "-", s.hint, style="dim")
            continue
        if best is None:
            best = s
        style = "green bold" if s is best else ""
        table.add_row(str(s.baud), f"{s.score:.2f}", s.hint, style=style)
    console.print(table)
    if best and best.readable:
        console.print(f"\n[green]建议波特率：{best.baud}[/green]")
        return 0
    console.print("\n[yellow]所有档位可读性都低：设备可能没在发数据，或流控/电平有问题。[/yellow]")
    return 1


# ---------------------------------------------------------------- watch

def cmd_watch(args: argparse.Namespace) -> int:
    profile = None
    if args.profile:
        from .profile import ProtocolProfile

        profile = ProtocolProfile.load(args.profile)
        console.print(f"[dim]解码画像「{profile.name}」已加载：{len(profile.fields)} 个字段[/dim]")
    factory = _real_factory(args.port)
    console.print(f"[dim]监视 {args.port} @ {args.baud}，Ctrl+C 停止[/dim]")
    try:
        with factory(args.baud) as src:
            while True:
                data = src.read(nbytes=args.chunk, timeout=0.5)
                if data:
                    if profile:
                        for values in profile.decode_stream(data):
                            pretty = "  ".join(f"[cyan]{k}[/cyan]={v}" for k, v in values.items())
                            console.print(f"[green]●[/green] {pretty}")
                    else:
                        text = _text_view(data).replace("\r", "")
                        hexv = data.hex(" ")
                        console.print(f"[green]TEXT[/green] {text}")
                        console.print(f"[dim]HEX  {hexv}[/dim]")
    except KeyboardInterrupt:
        console.print("\n[dim]已停止[/dim]")
    return 0


# ---------------------------------------------------------------- analyze

def cmd_analyze(args: argparse.Namespace) -> int:
    factory = _real_factory(args.port)
    console.print(f"[dim]从 {args.port} @ {args.baud} 采样 {args.sample} 字节…[/dim]")
    with factory(args.baud) as src:
        data = src.read(nbytes=args.sample, timeout=2.0)
    if not data:
        err.print("[red]没有收到任何数据。检查接线/设备是否在发数据。[/red]")
        return 1
    result = detect(data)
    console.print(Panel(result.summary(), title="本地启发式识别", border_style="cyan"))
    return _finish_analysis(data, args.port, str(args.baud), result.summary(), args)


# ---------------------------------------------------------------- learn

def cmd_learn(args: argparse.Namespace) -> int:
    """协议学习：多帧采样 -> 逐字节变化分析 -> 真值绑定 -> 保存画像."""
    from .learn import learn
    from .profile import ProtocolProfile
    from .serial_port import TempSensorSource

    if args.demo_sensor:
        console.print("[dim]学习靶场：虚拟温湿度传感器（AA55 帧）多轮采样，每轮改变真值[/dim]")
        sensor = TempSensorSource(temp_c=25.3, humi_pct=60.0)
        frames: list[bytes] = []
        truths: dict[str, float] = {}
        for rnd in range(args.rounds):
            console.print(f"  第 {rnd + 1} 轮：温度 [cyan]{sensor.temp_c}°C[/cyan] 湿度 [cyan]{sensor.humi_pct}%[/cyan]")
            frames.append(sensor._frame())
            truths.setdefault("温度", sensor.temp_c)
            truths.setdefault("湿度", sensor.humi_pct)
            sensor.set_reading(sensor.temp_c + 1.7, sensor.humi_pct + 2.5)
        source_desc = "demo-sensor"
    else:
        if not args.port:
            err.print("[red]需要 --port 或 --demo-sensor[/red]")
            return 1
        truth_pairs = _parse_truths(args.truth or [])
        if len(truth_pairs) < 1:
            err.print("[red]至少标注一个真值：--truth \"温度=25.3\"（采样时设备的实际读数）[/red]")
            return 1
        factory = _real_factory(args.port)
        frames = []
        console.print(f"[dim]从 {args.port} @ {args.baud} 采 {args.rounds} 帧…[/dim]")
        with factory(args.baud) as src:
            for i in range(args.rounds):
                if i > 0 and args.gap:
                    console.print(f"  第 {i + 1} 轮前请改变设备读数（等待 {args.gap}s）…")
                    import time as _t

                    _t.sleep(args.gap)
                frames.append(src.read(nbytes=args.sample, timeout=2.0))
        truths = {k: v for k, v in truth_pairs.items()}
        source_desc = args.port

    profile, log = learn(frames, name=args.name, truths=truths)
    profile.notes = f"数据源: {source_desc}; 真值标注: {truths or '无'}"

    console.print(Panel("\n".join(log), title=f"学习日志 · {args.name}", border_style="cyan"))
    path = profile.save()
    console.print(f"\n[green]画像已保存：[/green]{path}")

    # 用画像解码首帧做验证展示
    if profile.fields:
        decoded = profile.decode_frame(frames[0])
        table = Table(title="画像解码验证（首帧）")
        table.add_column("字段")
        table.add_column("值", justify="right")
        for k, v in decoded.items():
            table.add_row(str(k), str(v))
        console.print(table)
    return 0


def _parse_truths(items: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for item in items:
        if "=" not in item:
            continue
        k, v = item.split("=", 1)
        try:
            out[k.strip()] = float(v)
        except ValueError:
            continue
    return out


# ---------------------------------------------------------------- shared

def _real_factory(port: str):
    def factory(baud: int) -> RealSerial:
        return RealSerial(port, baud)
    return factory


def _finish_analysis(data: bytes, source: str, baud: str, heuristic: str, args) -> int:
    hexdump = hexdump_of(data)
    cfg = LLMConfig()
    ai_text = ""
    if args.ai:
        if not cfg.available:
            err.print(
                "[yellow]未配置 API key，跳过 AI 分析。[/yellow]\n"
                "[dim]设置任意一组即可：SERIALLENS_API_KEY / DEEPSEEK_API_KEY / OPENAI_API_KEY\n"
                "自定义端点：SERIALLENS_BASE_URL + SERIALLENS_MODEL[/dim]"
            )
        else:
            console.print(f"[dim]请求 {cfg.model} 分析…[/dim]")
            try:
                prompt = build_prompt(hexdump, _text_view(data), heuristic, f"{baud}（来源：{source}）")
                ai_text = analyze(prompt, cfg)
                console.print(Panel(ai_text, title=f"AI 分析 · {cfg.model}", border_style="magenta"))
            except Exception as exc:
                err.print(f"[red]AI 分析失败：{exc}[/red]")

    session = Session(
        title=args.title or source,
        source=source,
        baud=baud + ("（demo 合成）" if source.startswith("demo") else ""),
        hexdump=hexdump,
        text_view=_text_view(data),
        heuristic=heuristic,
        ai_analysis=ai_text,
    )
    out = Path(args.out or f"seriallens-report-{int(__import__('time').time())}.md")
    out.write_text(render(session), encoding="utf-8")
    console.print(f"\n[green]报告已保存：[/green]{out.resolve()}")
    return 0


# ---------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="seriallens",
        description="AI 串口侦探 — 会看懂串口数据的调试助手",
    )
    ap.add_argument("--version", action="version", version=f"seriallens {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ports", help="列出系统串口")
    p.set_defaults(fn=cmd_ports)

    p = sub.add_parser("demo", help="无硬件演示（合成 GPS 流 / 乱码流）")
    p.add_argument("--garbled", action="store_true", help="演示乱码场景（位错位合成）")
    p.add_argument("--baud", default="9600", choices=BAUD_CHOICES)
    p.add_argument("--sample", type=int, default=192, help="采样字节数")
    p.add_argument("--detect", action="store_true", help="附带波特率扫描演示")
    p.add_argument("--ai", action="store_true", help="调用 AI 分析（需 API key）")
    p.add_argument("--out", default=None, help="报告输出路径")
    p.add_argument("--title", default=None, help="报告标题")
    p.set_defaults(fn=cmd_demo)

    p = sub.add_parser("detect", help="自动波特率检测")
    p.add_argument("port")
    p.add_argument("--sample", type=int, default=96)
    p.set_defaults(fn=cmd_detect)

    p = sub.add_parser("learn", help="协议学习：多帧采样 -> 字段推断 -> 保存画像")
    p.add_argument("--port", default=None, help="串口号（与 --demo-sensor 二选一）")
    p.add_argument("--demo-sensor", action="store_true", help="用内置虚拟温湿度传感器演示")
    p.add_argument("-b", "--baud", default="115200", choices=BAUD_CHOICES)
    p.add_argument("--rounds", type=int, default=5, help="采样轮数（>=3 效果好）")
    p.add_argument("--sample", type=int, default=256)
    p.add_argument("--gap", type=float, default=5.0, help="真实设备：两轮采样间隔秒数（用来改变读数）")
    p.add_argument("--truth", action="append", default=[], help='真值标注，如 --truth "温度=25.3" 可多次')
    p.add_argument("--name", default="my-device", help="画像保存名")
    p.set_defaults(fn=cmd_learn)

    p = sub.add_parser("watch", help="实时监视串口")
    p.add_argument("port")
    p.add_argument("-b", "--baud", default="115200", choices=BAUD_CHOICES)
    p.add_argument("--chunk", type=int, default=64)
    p.add_argument("--profile", default=None, help="加载协议画像实时解码（learn 的产物名）")
    p.set_defaults(fn=cmd_watch)

    p = sub.add_parser("analyze", help="采样 + 启发式 + AI 分析 -> Markdown 报告")
    p.add_argument("port")
    p.add_argument("-b", "--baud", default="115200", choices=BAUD_CHOICES)
    p.add_argument("--sample", type=int, default=256)
    p.add_argument("--ai", action="store_true", help="调用 AI 分析（需 API key）")
    p.add_argument("--out", default=None)
    p.add_argument("--title", default=None)
    p.set_defaults(fn=cmd_analyze)

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        err.print("\n[dim]已中断[/dim]")
        return 130
    except Exception as exc:
        err.print(f"[red]错误：{exc}[/red]")
        return 1


if __name__ == "__main__":
    sys.exit(main())
