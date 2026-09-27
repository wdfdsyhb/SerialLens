"""协议学习引擎：多帧对比 -> 字段推断 -> 协议画像.

算法分四步，全部纯函数、离线可测：

1. split  帧切分：行模式 / 帧头发现 / 定长
2. align  多帧逐字节对齐：每个偏移统计变化集合
3. infer  静态字段(帧头/ID/填充) vs 动态字段分区，相邻动态字节聚类
4. bind   用户真值绑定：遍历 (类型, 比例尺) 组合，找出能算出真值的解释

诚实原则：推断结果全部带 confidence 与 label；绑定不上的字段标「动态-未知」，
不编造语义 —— 进一步解释交给 AI 分析（--ai）或人工确认。
"""

from __future__ import annotations

import struct
from collections import Counter
from dataclasses import dataclass

from .profile import FIELD_TYPES, FieldSpec, ProtocolProfile

TRUTH_SCALES = (1.0, 0.1, 0.01, 0.001, 0.5, 0.02, 0.25, 10.0, 100.0, 3600.0, 60.0)
TRUTH_TOLERANCE = 0.002  # 真实设备编码是精确的（2530*0.01==25.3），容差只盖浮点舍入


# ---------------------------------------------------------------- 1. 帧切分

def split_frames(data: bytes) -> tuple[list[bytes], str, str, str]:
    """把采样切成帧。返回 (帧列表, framing_mode, header, length_rule).

    策略：
    a) 行式：可打印率高且有行尾 -> line
    b) 帧头发现：出现 >=2 次的 2 字节前缀，且各次间距一致 -> header 模式，
       长度规则从帧内静态字节推断（byte@N+K），失败退化为 fixed:N
    c) 否则 fixed（按间距最大公约数切）
    """
    ratio = _printable(data)
    parts = data.replace(b"\r\n", b"\n").split(b"\n")
    # 结尾没有行尾 => 最后一行大概率被截断（读到一半），不作为帧
    if not data.endswith((b"\n", b"\r\n")) and len(parts) > 1:
        parts = parts[:-1]
    lines = [ln for ln in parts if ln.strip()]
    if ratio > 0.85 and len(lines) >= 2:
        return lines, "line", "", ""

    # 帧头发现：取流中出现次数最多的高频 2 字节组合（限定前缀位置出现的才可信）
    for head_len in (2, 1, 3):
        head = _discover_header(data, head_len)
        if head is None:
            continue
        positions = _find_all(data, head)
        if len(positions) < 2:
            continue
        gaps = {positions[i + 1] - positions[i] for i in range(len(positions) - 1)}
        if len(gaps) == 1:
            gap = gaps.pop()
            # 只收完整帧：末尾不足一个间距的是被截断的残帧，丢弃
            frames = [data[p : p + gap] for p in positions if p + gap <= len(data)]
            mode, rule = _infer_length_rule(data, positions, head, gap)
            return frames, mode, head.hex().upper(), rule

    # 定长兜底：用最小重复间距
    return _split_fixed(data)


def _printable(data: bytes) -> float:
    if not data:
        return 0.0
    return sum(1 for b in data if 0x20 <= b < 0x7F or b in (9, 10, 13)) / len(data)


def _find_all(data: bytes, sub: bytes) -> list[int]:
    out, idx = [], 0
    while True:
        pos = data.find(sub, idx)
        if pos == -1:
            return out
        out.append(pos)
        idx = pos + 1


def _discover_header(data: bytes, head_len: int) -> bytes | None:
    """发现帧头：出现 >=2 次且等间距的首选前缀。按出现次数排序取最常见."""
    cands = _header_candidates(data, head_len)
    return cands[0] if cands else None


def _header_candidates(data: bytes, head_len: int) -> list[bytes]:
    """枚举所有「出现 >=2 次且等间距」的候选帧头，按采样出现次数降序.

    连续流里固定偏移的窗口都会等间距重复（真帧头和部分负载窗口从单块内看
    是同构的），所以调用方需要跨块交叉验证 + 恒定前缀打分来定夺。
    """
    if len(data) < head_len * 2:
        return []
    counter = Counter(data[i : i + head_len] for i in range(0, len(data) - head_len, max(head_len, 4)))
    # 采样步进避免把随机内容当帧头；真实帧头会以稳定步长重复出现
    out: list[bytes] = []
    for cand, _n in counter.most_common(8):
        positions = _find_all(data, cand)
        if len(positions) >= 2:
            gaps = {positions[i + 1] - positions[i] for i in range(len(positions) - 1)}
            if len(gaps) == 1 and next(iter(gaps)) >= head_len + 1:
                out.append(cand)
    return out


def _infer_length_rule(data: bytes, positions: list[int], head: bytes, gap: int) -> tuple[str, str]:
    """在帧头后 1..4 字节里找「静态长度字节」：值 + K == 帧长 -> byte@N+K."""
    frame_len = gap
    for n in range(1, 5):
        values = {data[p + n] for p in positions if p + n < len(data)}
        if len(values) == 1:
            v = values.pop()
            if 0 < v and v + (frame_len - n - 1) == frame_len and v < frame_len:
                return "header", f"byte@{n}+{frame_len - v}"
    return "header", f"fixed:{frame_len}"


def _split_fixed(data: bytes) -> tuple[list[bytes], str, str, str]:
    for n in (8, 16, 32, 64, 4):
        if len(data) >= n * 2 and len(data) % n == 0:
            return [data[i : i + n] for i in range(0, len(data), n)], "header", "", f"fixed:{n}"
    return [], "header", "", ""


# ---------------------------------------------------------------- 2-3. 对齐与推断

@dataclass
class ByteStat:
    offset: int
    values: set[int]
    constant: bool

    @property
    def change_ratio_note(self) -> str:
        return f"{len(self.values)} 种取值" if not self.constant else "恒定"


def align(frames: list[bytes]) -> list[ByteStat]:
    """逐偏移统计：等长帧才参与."""
    if not frames:
        return []
    width = min(len(f) for f in frames)
    stats = []
    for i in range(width):
        vals = {f[i] for f in frames if len(f) == len(frames[0])}
        stats.append(ByteStat(i, vals, len(vals) == 1))
    return stats


def cluster_dynamic(stats: list[ByteStat]) -> list[list[int]]:
    """相邻的动态字节合并成候选字段区间（连续 >=1 个动态偏移为一段）."""
    runs: list[list[int]] = []
    cur: list[int] = []
    for s in stats:
        if not s.constant:
            cur.append(s.offset)
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return runs


# ---------------------------------------------------------------- 4. 真值绑定

def _candidates_for_width(width: int) -> list[str]:
    return [t for t, sz in FIELD_TYPES.items() if sz == width]


def bind_truth(raw_chunks: list[bytes], truth: float) -> tuple[str, float, float] | None:
    """尝试 (类型, scale) 组合，返回 (类型, scale, 置信度) 或 None.

    raw_chunks: 该字段在各帧的原始字节。每个候选类型用 struct 按自身端序解码
    （真实设备就是这么编的）。命中要同时满足两条，避免只看首帧的巧合误绑：
    1. 首帧解码值 * scale ≈ truth（首帧对应真值标注时刻的读数）
    2. 全帧解码一致性：所有帧的解码值都落在真值同一量级的合理带内
       ——真实传感器的相邻采样会变但不会跳量级；错位/巧合解释会炸出野值
    """
    if not raw_chunks:
        return None
    widths = (4, 2, 1) if len(raw_chunks[0]) >= 4 else ((2, 1) if len(raw_chunks[0]) >= 2 else (1,))
    for width in widths:
        for ftype in _candidates_for_width(width):
            try:
                values = [_decode(chunk, ftype) for chunk in raw_chunks]
            except struct.error:
                continue
            for scale in TRUTH_SCALES:
                if values[0] * scale == 0:
                    continue
                if abs(values[0] * scale - truth) <= TRUTH_TOLERANCE * max(abs(truth), 1e-9) and all(
                    _in_plausible_band(v * scale, truth) for v in values
                ):
                    return ftype, scale, 0.95
    return None


def _in_plausible_band(value: float, truth: float) -> bool:
    """物理合理性带（诚实启发式）：后续读数允许变化，但不跳出一阶量级."""
    limit = max(20.0 * abs(truth), 10.0)
    return abs(value - truth) <= limit


def _decode(raw: bytes, ftype: str) -> float:
    fmt = {"u8": "B", "i8": "b", "u16le": "<H", "u16be": ">H", "i16le": "<h", "i16be": ">h",
           "u32le": "<I", "u32be": ">I", "f32le": "<f", "f32be": ">f"}[ftype]
    return float(struct.unpack(fmt, raw)[0])


# ---------------------------------------------------------------- 主入口

def _pool_frames(blocks: list[bytes], log: list[str]) -> tuple[list[bytes], str]:
    """把输入整理成候选帧池，返回 (帧列表, framing_mode).

    输入有两种形态，先判别再处理：
    - 已是单帧列表（demo 传感器路径 / 既有调用方式）-> 原样保留
    - 每轮一大块连续采样的原始字节块（CLI 真实设备路径 read 到的整块）
      -> 必须先切帧；直接把块当帧会逐字节错位，产出高置信度错误画像

    判别：任一块能独立切出 >=2 帧即视为原始块。二进制连续流用「跨块帧头
    投票 + 恒定前缀打分」统一切帧基准：单块内固定偏移的负载窗口和真帧头
    同构（都等间距重复），只有跨块比较（真值随轮次变化 vs 帧头恒定）
    才能分辨，并保证所有块按同一旋转对齐。
    """
    if not blocks:
        return [], "header"
    probes: list[tuple[list[bytes], str]] = []
    looks_raw = False
    for b in blocks:
        fs, mode, _header, _rule = split_frames(b)
        probes.append((fs, mode))
        if len(fs) >= 2:
            looks_raw = True
    if not looks_raw:
        return list(blocks), "header"

    if any(mode == "line" for _fs, mode in probes):
        frames = [f for fs, _mode in probes for f in fs]
        log.append(f"输入为连续文本采样块：已按行切分，共 {len(frames)} 行。")
        return frames, "line"

    # 二进制连续流：收集各块的候选帧头，用同一帧头切所有块再打分
    cand_set: dict[bytes, None] = {}
    for b in blocks:
        for head_len in (2, 1, 3):
            for cand in _header_candidates(b, head_len):
                cand_set.setdefault(cand, None)
    best: tuple[tuple[int, int], bytes, list[bytes]] | None = None
    for head in cand_set:
        pool, nblocks = _cut_blocks_with_header(blocks, head)
        if nblocks < 2 or len(pool) < 2:
            continue
        score = (_constant_prefix_len(pool), len(pool))
        if best is None or score > best[0]:
            best = (score, head, pool)
    if best is None:
        frames = [f for fs, _mode in probes for f in fs]
        log.append("未能跨块确定一致帧头：退回按块独立切帧（建议增加采样轮数）。")
        return frames, "header"
    _score, head, pool = best
    log.append(
        f"输入为连续采样块：已自动切帧（帧头 {head.hex().upper()}，"
        f"等长过滤前共 {len(pool)} 个候选帧）。"
    )
    return pool, "header"


def _cut_blocks_with_header(blocks: list[bytes], head: bytes) -> tuple[list[bytes], int]:
    """用同一帧头切所有块。要求各块内等间距且间距一致（同一协议才成立）.

    返回 (完整帧池, 参与切帧的块数)。块尾不足一个间距的残帧丢弃。
    """
    pool: list[bytes] = []
    gap: int | None = None
    used = 0
    for b in blocks:
        positions = _find_all(b, head)
        if len(positions) < 2:
            continue
        gaps = {positions[i + 1] - positions[i] for i in range(len(positions) - 1)}
        if len(gaps) != 1:
            continue
        g = gaps.pop()
        if g < len(head) + 1:
            continue
        if gap is None:
            gap = g
        elif g != gap:
            continue
        used += 1
        pool.extend(b[p : p + g] for p in positions if p + g <= len(b))
    return pool, used


def _constant_prefix_len(frames: list[bytes]) -> int:
    """池化对齐后的恒定前缀长度：真帧头随轮次恒定，负载窗口会变."""
    if not frames:
        return 0
    width = min(len(f) for f in frames)
    n = 0
    for i in range(width):
        if len({f[i] for f in frames}) != 1:
            break
        n += 1
    return n


def _filter_modal_length(frames: list[bytes]) -> list[bytes]:
    """等长过滤以众数长度为基准：首帧若是残帧，按首帧过滤会全军覆没."""
    if not frames:
        return []
    modal = Counter(len(f) for f in frames).most_common(1)[0][0]
    return [f for f in frames if len(f) == modal]


def learn(
    frames: list[bytes],
    name: str = "learned",
    truths: dict[str, float] | None = None,
) -> tuple[ProtocolProfile, list[str]]:
    """从帧列表或原始采样块学习协议画像.

    frames: 每个元素可以是「一帧」，也可以是「一段连续采样的原始字节块」
    （CLI 真实设备路径每轮 read 到的整块）。后者先自动切帧成候选帧池
    （见 _pool_frames），再做等长过滤、结构推断与真值绑定。

    truths: {语义名: 物理真值}（用户在采样时标注，如 {"温度": 25.3}，
    对应第一轮采样时刻的读数，即池中第一帧）。帧数 <2 时动态字段无从
    谈起，日志里明说。返回 (画像, 学习日志)。
    """
    truths = truths or {}
    log: list[str] = []
    pooled, framing_mode = _pool_frames([f for f in frames if f], log)
    frames = _filter_modal_length(pooled)
    if len(frames) < 2:
        log.append("有效帧不足 2 帧，只能做静态分析，动态字段推断需要更多帧。")

    stats = align(frames)
    width = len(frames[0]) if frames else 0
    log.append(f"对齐 {len(frames)} 帧 × {width} 字节。")

    profile = ProtocolProfile(name=name, framing_mode=framing_mode)
    dynamic_runs = cluster_dynamic(stats)

    # 静态前缀 -> 帧头；若前缀里有「值 + K == 帧长」的字节，它更像长度字节，留在规则里
    if framing_mode == "header" and frames:
        static_head = 0
        while static_head < width and stats[static_head].constant:
            static_head += 1
        profile.header = frames[0][:static_head].hex().upper() if static_head >= 2 else ""
        for n in range(2, static_head):
            v = frames[0][n]
            if 0 < v < width and width - v >= 2:
                # 帧长 = v + (帧头2字节 + 长度字节1 + 尾部K) -> 生成 byte@N+K 规则
                profile.header = frames[0][:n].hex().upper()
                profile.length_rule = f"byte@{n}+{width - v}"
                log.append(f"偏移 {n} 恒定字节 {v:#04x} 与帧长 {width} 构成 byte@{n}+{width - v} 长度规则 -> 归为长度字节。")
                break
        if not profile.length_rule:
            # 没推导出更具体的 byte@N+K 规则时才回落定长，不覆盖已有规则
            profile.length_rule = f"fixed:{width}"
        if profile.header:
            log.append(f"帧头 {profile.header}（前 {len(bytes.fromhex(profile.header))} 字节恒定）。")

    # 动态区 -> 字段，尝试真值绑定
    used_truths: set[str] = set()
    field_index = 0
    for run in dynamic_runs:
        start, end = run[0], run[-1]
        seg = [f[start : end + 1] for f in frames]
        size = end - start + 1
        bound = bind_segment(seg, start, truths, used_truths)
        if bound:
            for spec in bound:
                profile.fields.append(spec)
                log.append(
                    f"字段 {spec.name} @ {spec.offset} {spec.type} scale={spec.scale}"
                    f" — {spec.label}（置信度 {spec.confidence:.0%}）"
                )
        else:
            label = "动态-未知（建议 AI 分析或人工确认）"
            conf = 0.3
            if all(seg[i] < seg[i + 1] for i in range(len(seg) - 1)):
                label, conf = "计数字段(推断：单调递增)", 0.6
            elif size == 1 and end == width - 1:
                label, conf = "疑似校验/序号(推断：帧尾单字节)", 0.4
            profile.fields.append(
                FieldSpec(name=f"field{field_index}", offset=start, type=_pick_type(size), label=label, confidence=conf)
            )
            log.append(f"字段 field{field_index} @ {start} ({size}B) — {label}")
        field_index += 1

    if not profile.fields:
        log.append("未发现动态字段：多帧内容完全一致，设备可能只在发同一帧。")

    # 二次搜索：量程窄的传感器会让多字节字段的部分字节恰好恒定（被判静态），
    # 导致动态区窗口错位。对剩余未绑定的真值，在含静态字节的区域滑动窗口补绑。
    # dyn_ 占位字段（无语义）允许被真值窗口覆盖，命中后移除。
    remaining = {k: v for k, v in truths.items() if k not in used_truths}
    if remaining and frames:
        for tname, truth in remaining.items():
            hard_covered = _covered_offsets([f for f in profile.fields if not f.name.startswith("dyn_")])
            spec = _search_whole_frame(frames, tname, truth, hard_covered, width)
            if spec:
                span = set(range(spec.offset, spec.offset + FIELD_TYPES.get(spec.type, 1)))
                before = len(profile.fields)
                profile.fields = [
                    f for f in profile.fields
                    if not f.name.startswith("dyn_") or not (set(range(f.offset, f.offset + FIELD_TYPES.get(f.type, 1))) & span)
                ]
                if len(profile.fields) < before:
                    log.append(f"窗口覆盖了占位字段（dyn），已替换为 {tname}。")
                profile.fields.append(spec)
                used_truths.add(tname)
                log.append(
                    f"字段 {spec.name} @ {spec.offset} {spec.type} scale={spec.scale}"
                    f" — {spec.label}（置信度 {spec.confidence:.0%}）"
                )

    profile.fields.sort(key=lambda f: f.offset)
    return profile, log


def _covered_offsets(fields: list[FieldSpec]) -> set[int]:
    out: set[int] = set()
    for f in fields:
        size = FIELD_TYPES.get(f.type, 1)
        out.update(range(f.offset, f.offset + size))
    return out


def _search_whole_frame(
    frames: list[bytes], tname: str, truth: float, covered: set[int], width: int
) -> FieldSpec | None:
    """整帧滑动窗口找真值绑定；命中窗口允许包含恒定字节（量程窄字段）."""
    for w in (4, 2, 1):
        for off in range(0, width - w + 1):
            if any(o in covered for o in range(off, off + w)):
                continue
            chunk = [f[off : off + w] for f in frames]
            hit = bind_truth(chunk, truth)
            if hit:
                ftype, scale, conf = hit
                return FieldSpec(
                    name=tname,
                    offset=off,
                    type=ftype,
                    scale=scale,
                    label=f"{tname}(用户标注 truth；部分字节本轮未变化，建议复测)",
                    confidence=0.75,
                )
    return None


def bind_segment(seg: list[bytes], offset_abs: int, truths: dict[str, float], used: set[str]) -> list[FieldSpec]:
    """对一段连续动态字节（绝对起点 offset_abs）做贪心窗口绑定.

    从段起点开始，按 4 -> 2 -> 1 字节窗口尝试绑定未用的真值；
    命中则推进窗口，失败则该字节记为动态，继续。最后把连续的
    未绑定字节合并成段，套用计数/校验推断生成 dyn 字段。
    """
    specs: list[FieldSpec] = []
    size = len(seg[0])
    pos = 0
    dyn_positions: list[int] = []
    while pos < size:
        matched = False
        for width in (4, 2, 1):
            if pos + width > size:
                continue
            chunk = [f[pos : pos + width] for f in seg]
            for tname, truth in truths.items():
                if tname in used:
                    continue
                hit = bind_truth(chunk, truth)
                if hit:
                    ftype, scale, conf = hit
                    used.add(tname)
                    specs.append(
                        FieldSpec(
                            name=tname,
                            offset=offset_abs + pos,
                            type=ftype,
                            scale=scale,
                            label=f"{tname}(用户标注 truth)",
                            confidence=conf,
                        )
                    )
                    pos += width
                    matched = True
                    break
            if matched:
                break
        if not matched:
            dyn_positions.append(pos)
            pos += 1

    # 连续 dyn 字节合并成段，套推断
    for group in _consecutive_runs(dyn_positions):
        start, end = group[0], group[-1]
        gsize = end - start + 1
        gseg = [f[start : end + 1] for f in seg]
        label, conf, ftype = "动态-未知（建议 AI 分析或人工确认）", 0.3, _pick_type(gsize)
        if gsize == 1 and all(gseg[i] < gseg[i + 1] for i in range(len(gseg) - 1)):
            label, conf = "计数字段(推断：单调递增)", 0.6
        elif gsize == 1 and end == size - 1:
            label, conf = "疑似校验/序号(推断：帧尾单字节)", 0.4
        specs.append(FieldSpec(name=f"dyn_{offset_abs + start}", offset=offset_abs + start, type=ftype, label=label, confidence=conf))
    return specs


def _consecutive_runs(positions: list[int]) -> list[list[int]]:
    runs: list[list[int]] = []
    cur: list[int] = []
    for p in positions:
        if cur and p == cur[-1] + 1:
            cur.append(p)
        else:
            if cur:
                runs.append(cur)
            cur = [p]
    if cur:
        runs.append(cur)
    return runs


def _pick_type(size: int) -> str:
    return {1: "u8", 2: "u16be", 4: "u32be"}.get(size, "u8")
