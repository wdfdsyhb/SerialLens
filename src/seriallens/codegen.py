"""C 代码生成：把协议画像变成嵌入式可用的解析器源码.

产出两个文件：<name>_parser.h / <name>_parser.c，特点：
- 零依赖：不 include 任何系统头（stdint 等价物内联 typedef），
  裸机 / RTOS / 主机 any compiler 都能编
- 状态机流式解析：feed 任意分块，跨块帧不丢（尾部结转在内部缓冲）
- 支持两种帧长规则：fixed:N 与 byte@N+K（长度字节动态）
- 字段物理值 = 原始值 × scale，端序按画像显式生成
- 诚实原则：不生成校验代码（画像没有推断出校验算法），注释里明说

非 ASCII 字段名（如「温度」）自动转成 f{offset}_{type} 形式的合法 C 标识符，
语义名保留在注释里 —— C99 标识符可移植性优先。
"""

from __future__ import annotations

import re

from .profile import ProtocolProfile

_INT_TYPEDEF = """\
typedef unsigned char       sl_u8;
typedef signed char         sl_i8;
typedef unsigned short      sl_u16;
typedef short               sl_i16;
typedef unsigned int        sl_u32;
typedef int                 sl_i32;
typedef float               sl_f32;
"""


def _c_ident(name: str, fallback: str) -> str:
    """把任意字段名转成合法 C 标识符（非字母数字下划线转下划线）；转不出来用 fallback."""
    cand = re.sub(r"[^0-9a-zA-Z_]+", "_", name).strip("_")
    if not cand or cand[0].isdigit():
        cand = fallback
    return cand


def _numeric_expr(field: dict, buf: str = "sbuf", base: str = "pos") -> str:
    """生成一个字段的原始值读取表达式（按类型与端序）."""
    off, t = field["offset"], field["type"]
    o = f"{base} + {off}" if base == "pos" else f"{base}"
    if t == "u8":
        return f"({buf}[{o}])"
    if t == "i8":
        return f"((sl_i8)({buf}[{o}]))"
    if t == "u16le":
        return f"((sl_u16)({buf}[{o}]) | ((sl_u16)({buf}[{o} + 1]) << 8))"
    if t == "u16be":
        return f"(((sl_u16)({buf}[{o}]) << 8) | (sl_u16)({buf}[{o} + 1]))"
    if t == "i16le":
        return f"((sl_i16)((sl_u16)({buf}[{o}]) | ((sl_u16)({buf}[{o} + 1]) << 8)))"
    if t == "i16be":
        return f"((sl_i16)(((sl_u16)({buf}[{o}]) << 8) | (sl_u16)({buf}[{o} + 1])))"
    if t == "u32le":
        return (f"((sl_u32)({buf}[{o}]) | ((sl_u32)({buf}[{o} + 1]) << 8) | "
                f"((sl_u32)({buf}[{o} + 2]) << 16) | ((sl_u32)({buf}[{o} + 3]) << 24)))")
    if t == "u32be":
        return (f"(((sl_u32)({buf}[{o}]) << 24) | ((sl_u32)({buf}[{o} + 1]) << 16) | "
                f"((sl_u32)({buf}[{o} + 2]) << 8) | (sl_u32)({buf}[{o} + 3])))")
    if t == "i32le" or t == "i32be":
        raw = _numeric_expr(field, buf, base).replace("sl_u32", "sl_u32")
        return f"((sl_i32)({raw}))"
    if t == "f32le":
        return f"__sl_f32_from_bits(__sl_u32le({buf} + {o}))"
    if t == "f32be":
        return f"__sl_f32_from_bits(__sl_u32be({buf} + {o}))"
    return "0"


def _gen_field_code(field: dict, idx: int) -> tuple[str, str, str]:
    """返回 (成员声明, 赋值语句, 注释) 三元组."""
    t = field["type"]
    scale = float(field.get("scale", 1.0))
    label = field.get("label", "").replace("*/", "* /")
    unit = field.get("unit", "")
    cands = [
        _c_ident(field.get("name", ""), f"f{field['offset']}_{t}"),
    ]
    ident = next((c for c in cands if c), f"f{field['offset']}_{t}")
    comment = f"/* {field.get('name', ident)}: offset {field['offset']}, {t} x{scale:g} {unit} — {label} */"
    # 原始整数字段直接存整型；带 scale 的存 float
    if scale == 1.0 and t in ("u8", "i8", "u16le", "u16be", "i16le", "i16be"):
        ctype = {"u8": "sl_u8", "i8": "sl_i8", "u16le": "sl_u16", "u16be": "sl_u16",
                 "i16le": "sl_i16", "i16be": "sl_i16"}[t]
        decl = f"    {ctype} {ident};"
        expr = _numeric_expr(field)
    else:
        decl = f"    float {ident};"
        expr = f"(float)({_numeric_expr(field)}) * {scale:g}f"
    assign = f"    out->{ident} = {expr};"
    return decl, assign, comment


def generate_c(profile: ProtocolProfile) -> tuple[str, str]:
    """生成 (header_text, source_text)。字段名不合法时自动替换并保留原语义注释."""
    name = _c_ident(profile.name, "profile")
    sym = name.upper()
    header_hex = profile.header or ""
    header_bytes = bytes.fromhex(header_hex) if header_hex else b""
    hlen = len(header_bytes)

    length_rule = profile.length_rule
    if length_rule.startswith("fixed:"):
        fixed_len = int(length_rule.split(":")[1])
        dynamic = False
        nbyte, kadd = 0, 0
    elif length_rule.startswith("byte@"):
        spec = length_rule.removeprefix("byte@")
        n_str, k_str = spec.split("+")
        nbyte, kadd = int(n_str), int(k_str)
        fixed_len = 0
        dynamic = True
    else:
        # 无长度规则：退化为仅按帧头扫描的定长 0 —— 不生成解析体，生成头文件骨架
        fixed_len, dynamic, nbyte, kadd = 0, False, 0, 0

    max_frame = max(fixed_len, (nbyte + kadd if dynamic else 0), hlen, 16)
    stream_cap = max(max_frame * 4, 256)

    # 字段生成
    decls: list[str] = []
    assigns: list[str] = []
    notes: list[str] = []
    for i, f in enumerate(profile.fields):
        fd = f.to_dict() if hasattr(f, "to_dict") else dict(f)
        decl, assign, comment = _gen_field_code(fd, i)
        decls.append(comment)
        decls.append(decl)
        assigns.append(comment)
        assigns.append(assign)

    fields_block = "\n".join(decls) if decls else "    /* 画像没有动态字段（静态帧） */"
    assign_block = "\n".join(assigns) if assigns else "    /* no dynamic fields */"

    # 帧头数组
    header_arr = ", ".join(f"0x{b:02X}" for b in header_bytes) if header_bytes else "0"
    header_typedef = "" if header_bytes else "/* 无帧头（line/fixed 模式未生成扫描逻辑） */\n"

    # 长度判定代码
    if dynamic:
        len_code = f"""\
            /* 动态长度：byte@{nbyte}+{kadd} */
            if (p->slen < pos + {nbyte + 1}) break;             /* 长度字节未到齐 */
            flen = (int)p->sbuf[pos + {nbyte}] + {kadd};
            if (flen < {hlen + 1} || flen > SL_{sym}_MAX_FRAME) {{
                pos += 1;                                       /* 非法长度：跳过该帧头继续扫 */
                continue;
            }}"""
    else:
        len_code = f"""\
            flen = {fixed_len};                                 /* 定长帧 */"""

    h = f"""\
/* {name}_parser.h — Generated by SerialLens (protocol profile "{profile.name}")
 *
 * Frame: header {header_hex or '(none)'} | length {profile.length_rule or '(unknown)'}
 * Notes: {profile.notes or '-'}
 * Created: {profile.created}
 *
 * This file is self-contained (no #include). Regenerate with:
 *   seriallens export-c {profile.name}
 * Field semantics come from the profile's truth-binding; verify against
 * real hardware before production use. Checksums are NOT validated here.
 */
#ifndef SL_{sym}_H
#define SL_{sym}_H

{_INT_TYPEDEF}
{header_typedef}
#define SL_{sym}_HEADER_LEN   {hlen}
#define SL_{sym}_MAX_FRAME    {max_frame}
#define SL_{sym}_STREAM_CAP   {stream_cap}

/* 解码出的帧字段（物理值；语义名见各行注释） */
typedef struct {{
{fields_block}
}} sl_{name}_frame;

/* 流式解析器状态（跨块结转，帧边界自动保持） */
typedef struct {{
    sl_u8  sbuf[SL_{sym}_STREAM_CAP];
    unsigned slen;
}} sl_{name}_parser;

void   sl_{name}_init(sl_{name}_parser *p);
/* 喂入任意分块的字节流，解出的帧写入 out（最多 out_max 帧），返回帧数 */
unsigned sl_{name}_feed(sl_{name}_parser *p, const sl_u8 *data, unsigned len,
                        sl_{name}_frame *out, unsigned out_max);

#endif /* SL_{sym}_H */
"""

    c = f"""\
/* {name}_parser.c — Generated by SerialLens. See {name}_parser.h. */

#include "{name}_parser.h"

/* 零依赖小工具：位组装与 memmove 替代（不引 string.h） */
static sl_u32 __sl_u32le(const sl_u8 *b) {{
    return (sl_u32)b[0] | ((sl_u32)b[1] << 8) | ((sl_u32)b[2] << 16) | ((sl_u32)b[3] << 24);
}}
static sl_u32 __sl_u32be(const sl_u8 *b) {{
    return ((sl_u32)b[0] << 24) | ((sl_u32)b[1] << 16) | ((sl_u32)b[2] << 8) | (sl_u32)b[3];
}}
static float __sl_f32_from_bits(sl_u32 bits) {{
    union {{ sl_u32 u; float f; }} conv;
    conv.u = bits;
    return conv.f;
}}
static void __sl_buf_shift(sl_u8 *buf, unsigned len, unsigned n) {{
    unsigned i;
    if (n == 0 || n > len) return;
    for (i = 0; i + n < len; i++) buf[i] = buf[i + n];
}}

static const sl_u8 SL_{sym}_HEADER[{hlen or 1}] = {{ {header_arr} }};

void sl_{name}_init(sl_{name}_parser *p) {{
    p->slen = 0;
}}

unsigned sl_{name}_feed(sl_{name}_parser *p, const sl_u8 *data, unsigned len,
                        sl_{name}_frame *out, unsigned out_max) {{
    unsigned produced = 0;
    unsigned pos = 0;
    unsigned i;
    int flen;

    /* 追加进内部流缓冲（不满则丢最旧——流式采集宁丢旧不阻塞） */
    for (i = 0; i < len; i++) {{
        if (p->slen >= SL_{sym}_STREAM_CAP) {{
            __sl_buf_shift(p->sbuf, p->slen, len ? SL_{sym}_STREAM_CAP / 4 : 0);
            p->slen -= (p->slen >= SL_{sym}_STREAM_CAP / 4 + 1) ? SL_{sym}_STREAM_CAP / 4 : p->slen;
        }}
        p->sbuf[p->slen++] = data[i];
    }}

    while (produced < out_max) {{
        /* 1) 扫描帧头 */
        long found = -1;
        for (i = pos; i + SL_{sym}_HEADER_LEN <= p->slen; i++) {{
            unsigned j;
            int match = 1;
            for (j = 0; j < SL_{sym}_HEADER_LEN; j++) {{
                if (p->sbuf[i + j] != SL_{sym}_HEADER[j]) {{ match = 0; break; }}
            }}
            if (match) {{ found = (long)i; break; }}
        }}
        if (found < 0) {{
            /* 没有完整帧头：保留尾部可能被撕裂的前缀字节 */
            unsigned keep = SL_{sym}_HEADER_LEN - 1;
            if (p->slen > keep) {{
                __sl_buf_shift(p->sbuf, p->slen, p->slen - keep);
                p->slen = keep;
            }}
            break;
        }}
        pos = (unsigned)found;

        /* 2) 帧长判定 */
{len_code}

        /* 3) 帧不完整：等待下一块（从帧头起保留） */
        if (p->slen < pos + (unsigned)flen) break;

        /* 4) 字段提取（物理值 = 原始值 x scale；校验未生成——画像未推断出校验算法） */
        {{
            sl_{name}_frame f;
{assign_block}
            out[produced++] = f;
        }}
        pos += (unsigned)flen;
    }}

    /* 5) 消费完毕：把剩余字节挪到缓冲头部 */
    if (pos > 0) {{
        __sl_buf_shift(p->sbuf, p->slen, pos);
        p->slen -= pos;
    }}
    return produced;
}}
"""
    return h, c
