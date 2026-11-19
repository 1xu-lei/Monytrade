"""页面片段：徽章、表格行、证据抽屉等可复用 HTML 块。

所有循环与条件在这里展开成字符串，模板文件里只有 {{占位符}}。
"""

from html import escape

import config
from core.money import signed_yuan_mark, yuan_mark

SEVERITY_CLASS = {"high": "sev-high", "medium": "sev-medium", "low": "sev-low"}


def esc(value) -> str:
    return escape("" if value is None else str(value))


def money(fen) -> str:
    return yuan_mark(int(fen or 0))


def signed(fen) -> str:
    return signed_yuan_mark(int(fen or 0))


def label_badge(label_code: str) -> str:
    return (f'<span class="badge badge-{label_code.lower().replace("_", "-")}">'
            f"{esc(config.LABEL_TEXT.get(label_code, label_code))}</span>")


def severity_badge(severity: str) -> str:
    return (f'<span class="sev {SEVERITY_CLASS.get(severity, "")}">'
            f"{esc(config.SEVERITY_TEXT.get(severity, severity))}</span>")


def status_badge(status: str) -> str:
    return f'<span class="status status-{esc(status)}">{esc(config.STATUS_TEXT.get(status, status))}</span>'


def order_link(order_id: str, text: str = "") -> str:
    return f'<a class="mono order-link" href="/trail/{esc(order_id)}">{esc(text or order_id)}</a>'


def stat_card(label: str, value: str, hint: str = "", tone: str = "") -> str:
    return (
        f'<div class="card stat {tone}">'
        f'<div class="stat-label">{esc(label)}</div>'
        f'<div class="stat-value">{value}</div>'
        f'<div class="stat-hint">{esc(hint)}</div>'
        "</div>"
    )


def anomaly_rows(rows, *, show_status_form: bool = True, return_to: str = "") -> str:
    if not rows:
        return ""
    out = []
    for row in rows:
        anomaly_id = row["anomaly_id"]
        status_cell = status_badge(row["status"])
        if show_status_form:
            options = "".join(
                f'<option value="{code}"{" selected" if code == row["status"] else ""}>'
                f"{esc(config.STATUS_TEXT[code])}</option>"
                for code in config.STATUSES
            )
            status_cell = (
                f'<form class="inline status-form" method="post" '
                f'action="/anomalies/{esc(anomaly_id)}/status">'
                f'<input type="hidden" name="return_to" value="{esc(return_to)}">'
                f'<select name="status" aria-label="处理状态">{options}</select>'
                f'<button type="submit" class="btn btn-mini">更新</button>'
                "</form>"
            )
        out.append(
            "<tr>"
            f'<td class="col-order">{order_link(row["order_id"])}</td>'
            f'<td>{label_badge(row["label_code"])}</td>'
            f'<td>{severity_badge(row["severity"])}</td>'
            f'<td class="col-amount gap">{signed(row["gap_fen"])}</td>'
            f'<td class="col-time mono">{esc((row["paid_at"] or "")[:16])}</td>'
            f'<td class="col-status">{status_cell}</td>'
            f'<td class="col-actions"><a class="btn btn-mini" '
            f'href="/trail/{esc(row["order_id"])}">看链路</a></td>'
            "</tr>"
        )
    return "\n".join(out)


def evidence_drawer(anomaly: dict, *, open_by_default: bool = False) -> str:
    """证据抽屉固定三段：① 原始流水 ② 逐行计算 ③ 判定依据。"""
    evidence = anomaly.get("evidence") or {}
    raw_rows = "".join(
        f'<tr><td class="mono src">{esc(row["source"])}</td>'
        f'<td class="mono raw">{esc(row["text"])}</td></tr>'
        for row in evidence.get("raw_rows", [])
    ) or '<tr><td colspan="2" class="muted">该订单在库中没有对应的原始行</td></tr>'

    steps = "\n".join(esc(line) for line in evidence.get("steps", []))
    thresholds = "".join(
        f"<li><span>{esc(key)}</span><b>{esc(value)}</b></li>"
        for key, value in (evidence.get("rule_thresholds") or {}).items()
    )

    return (
        f'<details class="drawer"{" open" if open_by_default else ""}>'
        "<summary>"
        f'{label_badge(anomaly["label_code"])}'
        f'<span class="drawer-title">{esc(config.RULE_NAME.get(anomaly["label_code"], ""))}</span>'
        f'<span class="drawer-gap">缺口 {money(anomaly["gap_fen"])}</span>'
        "<span class=\"drawer-hint\">展开证据</span>"
        "</summary>"
        '<div class="drawer-body">'
        '<section class="evidence">'
        "<h4><i>①</i> 原始流水对照</h4>"
        f'<table class="raw-table"><tbody>{raw_rows}</tbody></table>'
        "</section>"
        '<section class="evidence">'
        "<h4><i>②</i> 逐行计算过程</h4>"
        f'<pre class="calc">{steps}</pre>'
        "</section>"
        '<section class="evidence">'
        "<h4><i>③</i> 判定依据</h4>"
        f'<p class="reason-text">{esc(evidence.get("reason", ""))}</p>'
        f'<ul class="thresholds">{thresholds}</ul>'
        f'<p class="muted small">判定基准时点 T = '
        f'{esc(evidence.get("reference_time", ""))}</p>'
        '<details class="sql-block"><summary>查看本次判定使用的完整 SQL</summary>'
        f'<pre class="sql">{esc(evidence.get("rule_sql", ""))}</pre></details>'
        "</section>"
        '<section class="evidence">'
        "<h4>建议后续动作</h4>"
        f'<p class="suggest">{esc(evidence.get("suggestion", ""))}</p>'
        "</section>"
        "</div>"
        "</details>"
    )


def empty_state(title: str, hint: str, action_html: str = "") -> str:
    return (
        '<div class="empty">'
        f'<div class="empty-mark">◌</div>'
        f"<h3>{esc(title)}</h3>"
        f'<p class="muted">{esc(hint)}</p>'
        f"{action_html}"
        "</div>"
    )


def alert(kind: str, title: str, body_html: str = "") -> str:
    return (
        f'<div class="alert alert-{esc(kind)}">'
        f'<strong>{esc(title)}</strong>'
        f'<div>{body_html}</div>'
        "</div>"
    )


def error_table(errors: list) -> str:
    rows = "".join(
        f'<tr><td class="mono">第 {int(item["row"])} 行</td>'
        f'<td class="mono">{esc(item["field"])}</td>'
        f'<td>{esc(item["message"])}</td></tr>'
        for item in errors
    )
    return (
        '<table class="error-table"><thead><tr><th>位置</th><th>字段</th><th>问题</th></tr>'
        f"</thead><tbody>{rows}</tbody></table>"
    )
