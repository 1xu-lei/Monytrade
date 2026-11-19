"""手写 SVG：把一笔订单画成一条会断的五段管道。

为什么不用图表库：
1. 「断点管道」不是任何图表库里有的图形，自绘反而更自由；
2. 零加载失败 —— 服务端直接产出 SVG，页面加载即可见，夜间不会因为
   前端构建或 CDN 挂掉而变白屏。

坐标设计：横轴是真实时间（按该笔订单自己的时间跨度做线性映射，T+1 到账
自然画在次日位置），纵轴是五个环节。管道在断点处物理断开，缺口上方标注
缺口金额，下方给出归因文字。
"""

from datetime import datetime, timedelta
from html import escape

from core.money import yuan_mark

WIDTH = 1040
LEFT = 132
RIGHT_PAD = 34
TOP = 58
ROW_TOP = TOP + 46
ROW_H = 92
NODE_R = 6.5
FONT = "'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif"
MONO = "ui-monospace, 'Cascadia Mono', Consolas, monospace"

STATE_GLYPH = {
    "ok": "●",
    "none": "○",
    "warn": "◐",
    "broken": "✂",
    "ghost": "◌",
}

TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")


def _parse(text):
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except (ValueError, TypeError):
            continue
    return None


def _wrap(text: str, limit: int) -> list:
    """按字符数硬折行（中文按 1 个宽度计）。"""
    text = text or ""
    if len(text) <= limit:
        return [text]
    lines, current = [], ""
    for char in text:
        current += char
        if len(current) >= limit:
            lines.append(current)
            current = ""
    if current:
        lines.append(current)
    return lines[:3]


def build(trail: dict, *, title: str = "") -> str:
    stages = trail.get("stages") or []
    if not stages:
        return ""

    moments = [_parse(stage.get("time", "")) for stage in stages]
    known = [moment for moment in moments if moment]
    if not known:
        known = [datetime(2026, 9, 21, 9, 0)]
    span_start = min(known) - timedelta(minutes=45)
    span_end = max(known) + timedelta(minutes=45)
    if span_end <= span_start:
        span_end = span_start + timedelta(hours=6)
    total_seconds = (span_end - span_start).total_seconds()

    x_min = LEFT + 26
    x_max = WIDTH - RIGHT_PAD

    def xmap(moment):
        if moment is None:
            return x_min
        ratio = (moment - span_start).total_seconds() / total_seconds
        return x_min + ratio * (x_max - x_min)

    xs, last_x = [], x_min
    for moment in moments:
        x = xmap(moment) if moment else last_x
        xs.append(x)
        last_x = x

    height = ROW_TOP + ROW_H * len(stages) + 26
    broken = trail.get("broken_index", -1)
    parts = [
        f'<svg class="trail" viewBox="0 0 {WIDTH} {height}" width="100%" '
        f'height="{height}" role="img" aria-label="{escape(title or "资金链路")}">',
        f"<title>{escape(title or '资金链路')}</title>",
        "<defs>"
        '<marker id="arrow" viewBox="0 0 8 8" refX="6" refY="4" markerWidth="7" '
        'markerHeight="7" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="currentColor"/></marker>'
        "</defs>",
    ]

    # ---- 时间轴
    parts.append('<g class="axis">')
    parts.append(f'<line x1="{x_min}" y1="{TOP}" x2="{x_max}" y2="{TOP}" />')
    for step in range(5):
        ratio = step / 4
        tick_x = x_min + ratio * (x_max - x_min)
        moment = span_start + timedelta(seconds=total_seconds * ratio)
        anchor = "start" if step == 0 else ("end" if step == 4 else "middle")
        parts.append(f'<line x1="{tick_x:.1f}" y1="{TOP - 5}" x2="{tick_x:.1f}" y2="{TOP}" />')
        parts.append(
            f'<text x="{tick_x:.1f}" y="{TOP - 12}" text-anchor="{anchor}">'
            f'{moment.strftime("%m-%d %H:%M")}</text>'
        )
    parts.append(
        f'<text class="axis-caption" x="{x_min}" y="{TOP - 30}">'
        f"横轴 = 真实时间（按本笔订单的时间跨度线性排布）</text>"
    )
    parts.append("</g>")

    # ---- 每行：导轨 + 环节名 + 节点
    for index, stage in enumerate(stages):
        y = ROW_TOP + index * ROW_H
        state = stage.get("state", "ok")
        is_broken = index == broken
        x = xs[index]

        parts.append(
            f'<line class="rail" x1="{x_min}" y1="{y}" x2="{x_max}" y2="{y}" />'
        )
        parts.append(
            f'<text class="stage-index" x="18" y="{y + 4}">{index + 1:02d}</text>'
        )
        parts.append(
            f'<text class="stage-name{" broken" if is_broken else ""}" x="{LEFT - 18}" '
            f'y="{y + 5}" text-anchor="end">{escape(stage.get("name", ""))}</text>'
        )
        parts.append(
            f'<text class="stage-glyph {state}" x="{LEFT}" y="{y + 5}">'
            f'{STATE_GLYPH.get(state, "●")}</text>'
        )

        parts.append(
            f'<circle class="node {state}" cx="{x:.1f}" cy="{y}" r="{NODE_R}" />'
        )
        if state == "broken":
            parts.append(
                f'<circle class="node-halo" cx="{x:.1f}" cy="{y}" r="{NODE_R + 7}" />'
            )

        amount = stage.get("amount_fen")
        if is_broken:
            line_one = trail.get("gap_text") or "断链"
            line_class = "lbl-broken"
        elif amount is None:
            line_one = "—"
            line_class = "lbl-amount"
        else:
            line_one = yuan_mark(amount)
            line_class = "lbl-amount"

        meta_bits = []
        if stage.get("time"):
            meta_bits.append(stage["time"][5:16])
        if stage.get("ref") and stage["ref"] != "—":
            meta_bits.append(stage["ref"])
        if stage.get("note"):
            meta_bits.append(stage["note"])
        line_two = " · ".join(meta_bits)

        anchor_right = x > (WIDTH - RIGHT_PAD) * 0.56
        text_x = x - 14 if anchor_right else x + 14
        text_anchor = "end" if anchor_right else "start"

        parts.append(
            f'<text class="{line_class}" x="{text_x:.1f}" y="{y - 4}" '
            f'text-anchor="{text_anchor}">{escape(line_one)}</text>'
        )
        if line_two:
            for offset, chunk in enumerate(_wrap(line_two, 34)):
                parts.append(
                    f'<text class="lbl-meta" x="{text_x:.1f}" y="{y + 13 + offset * 14}" '
                    f'text-anchor="{text_anchor}">{escape(chunk)}</text>'
                )

        if is_broken and trail.get("reason"):
            for offset, chunk in enumerate(_wrap(trail["reason"], 30)):
                parts.append(
                    f'<text class="lbl-reason" x="{text_x:.1f}" '
                    f'y="{y + 13 + (2 + offset) * 14}" '
                    f'text-anchor="{text_anchor}">{escape(chunk)}</text>'
                )

    # ---- 管道（含断口）
    for index in range(len(stages) - 1):
        y1 = ROW_TOP + index * ROW_H + NODE_R
        y2 = ROW_TOP + (index + 1) * ROW_H - NODE_R
        ymid = (y1 + y2) / 2
        points = [(xs[index], y1), (xs[index], ymid), (xs[index + 1], ymid), (xs[index + 1], y2)]
        is_gap = index + 1 == broken
        if is_gap:
            head, tail, center = _split(points, gap=30)
            parts.append(_polyline(head, "pipe-head"))
            parts.append(_polyline(tail, "pipe-tail"))
            parts.append(
                f'<circle class="gap-dot" cx="{center[0]:.1f}" cy="{center[1]:.1f}" r="2.5" />'
            )
        else:
            parts.append(_polyline(points, "pipe"))

    parts.append("</svg>")
    return "\n".join(parts)


def _polyline(points, css_class: str) -> str:
    if not points:
        return ""
    coords = " ".join(f"{round(x, 1)},{round(y, 1)}" for x, y in points)
    return f'<polyline class="{css_class}" points="{coords}" />'


def _split(points: list, gap: float):
    """在折线中点切掉一段，形成物理断口，返回 (前半, 后半, 断口中心)。"""
    lengths = []
    for index in range(len(points) - 1):
        (x1, y1), (x2, y2) = points[index], points[index + 1]
        lengths.append(((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5)
    total = sum(lengths) or 1.0
    half = total / 2
    half_gap = min(gap, total * 0.3) / 2

    def walk(target):
        walked = 0.0
        for index, length in enumerate(lengths):
            if walked + length >= target:
                ratio = (target - walked) / length if length else 0
                x1, y1 = points[index]
                x2, y2 = points[index + 1]
                return (x1 + (x2 - x1) * ratio, y1 + (y2 - y1) * ratio, index)
            walked += length
        return (points[-1][0], points[-1][1], len(lengths) - 1)

    ax, ay, index_a = walk(max(half - half_gap, 0))
    bx, by, index_b = walk(min(half + half_gap, total))
    cx, cy, _ = walk(half)

    head = list(points[:index_a + 1]) + [(ax, ay)]
    tail = [(bx, by)] + list(points[index_b + 1:])
    return head, tail, (cx, cy)
