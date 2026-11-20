"""页面组装：把查询结果拼成完整的服务端渲染页面。

零 JavaScript：所有交互都是<form>提交 + 服务端重定向，页面在禁用 JS 的
环境里也能完整使用。
"""

from urllib.parse import urlencode

import config
from core import assistant, importer, models, reconciler
from core.money import yuan_mark
from web import components as c
from web import svg
from web.render import render


class NotFound(Exception):
    """页面级 404：由 app.py 转成带状态码的错误页。"""


def _page(template: str, title: str, active: str, env: str, **ctx) -> str:
    body = render(template, **ctx)
    return render(
        "layout.html",
        title=title,
        nav=nav(active),
        env=env,
        body=body,
        version=config.VERSION,
    )


def nav(active: str) -> str:
    items = [
        ("/", "总览", "overview"),
        ("/anomalies", "异常中心", "anomalies"),
        ("/import", "账单导入", "import"),
        ("/assistant", "问一句", "assistant"),
    ]
    links = "".join(
        f'<a class="nav-link{" active" if key == active else ""}" href="{href}">{text}</a>'
        for href, text, key in items
    )
    return links


def environment(conn) -> str:
    stats = reconciler.stats(conn)
    status = assistant.ai_status(conn)
    ai_text = "AI 已接入" if status["available"] else "AI 未接入 · 已降级"
    reference = stats["reference_time"] or "无数据"
    return (f'T = {reference} · 订单 {stats["order_count"]} / 结算 '
            f'{stats["settlement_count"]} / 工单 {stats["all_count"]} · {ai_text}')


def with_evidence(rows) -> list:
    out = []
    for row in rows:
        item = dict(row)
        evidence = reconciler.evidence_of(row)
        item["evidence"] = evidence
        item["reason"] = evidence.get("reason", "")
        item["suggestion"] = evidence.get("suggestion", "")
        out.append(item)
    return out


# ---------------------------------------------------------------- 总览

def overview(conn, flash: str = "") -> str:
    stats = reconciler.stats(conn)
    env = environment(conn)

    if stats["order_count"] == 0:
        body = c.empty_state(
            "还没有任何台账数据",
            "先运行 python gen_data.py 生成演示数据，或到「账单导入」上传三类 CSV。",
            '<p><a class="btn" href="/import">前往账单导入</a></p>',
        )
        return _page("overview.html", "总览", "overview", env,
                     hero_title="资金链路核查台",
                     hero_sub="把「订单 → 支付 → 退款 → 手续费 → 结算到账」重建为一条链路，"
                              "自动定位这笔钱断在哪一环。",
                     cards=body, top_rows="", top_note="", label_bars="",
                     reference=stats["reference_time"], quick_links="", flash=flash,
                     export_links="")

    cards = "".join([
        c.stat_card("订单总额", yuan_mark(stats["order_total_fen"]),
                    f'{stats["order_count"]} 笔订单', "card-ink"),
        c.stat_card("实际到账", yuan_mark(stats["received_fen"]),
                    f'{stats["settlement_count"]} 笔结算净额', "card-ok"),
        c.stat_card("待核差异", yuan_mark(stats["open_gap_fen"]),
                    f'{stats["open_count"]} 笔未结工单的缺口合计', "card-gap"),
        c.stat_card("异常笔数", f'{stats["all_count"]} 笔',
                    "高 {} / 中 {} / 低 {}".format(
                        sum(1 for label in ("NOT_SETTLED", "DUP_REFUND")
                            for _ in range(stats["by_label"][label])),
                        sum(1 for _ in range(stats["by_label"]["AMOUNT_MISMATCH"])),
                        sum(1 for _ in range(stats["by_label"]["FEE_ANOMALY"]))),
                    "card-ink"),
    ])

    top_rows = c.anomaly_rows(
        reconciler.top_open(conn, 5), return_to=urlencode({}))

    label_bars = _label_bars(stats)

    quick_links = (
        '<div class="quick">'
        '<form method="post" action="/reconcile" class="inline">'
        '<button class="btn btn-primary" type="submit">重新对账</button></form>'
        '<a class="btn" href="/anomalies">进入异常中心</a>'
        '<a class="btn" href="/export/batch.md">导出全部核查单（Markdown）</a>'
        '<a class="btn" href="/export/batch.csv">导出全部核查单（CSV）</a>'
        "</div>"
    )

    return _page("overview.html", "总览", "overview", env,
                 hero_title="资金链路核查台",
                 hero_sub="把「订单 → 支付 → 退款 → 手续费 → 结算到账」重建为一条链路，"
                          "自动定位这笔钱断在哪一环。",
                 cards=cards,
                 top_rows=top_rows,
                 top_note=f"按严重程度排序，共 {stats['open_count']} 笔未结",
                 label_bars=label_bars,
                 reference=stats["reference_time"],
                 quick_links=quick_links,
                 export_links="",
                 flash=flash)


def _label_bars(stats) -> str:
    total = max(stats["all_count"], 1)
    rows = []
    for label in config.LABELS:
        count = stats["by_label"][label]
        width = max(int(count / total * 100), 2 if count else 0)
        rows.append(
            f'<div class="bar-row"><span class="bar-label">{c.label_badge(label)}</span>'
            f'<span class="bar-track"><span class="bar-fill bar-{label.lower()}" '
            f'style="width:{width}%"></span></span>'
            f'<span class="bar-count">{count}</span></div>'
        )
    return "".join(rows)


# ---------------------------------------------------------------- 异常中心

# 每页工单数。list_anomalies 的 limit/offset 就是给这里用的；
# 导出链接始终按筛选条件取全量，不受分页影响。
PAGE_SIZE = 20


def anomalies_page(conn, filters: dict, flash: str = "", page: int = 1) -> str:
    env = environment(conn)
    total = models.count_anomalies(conn, **filters)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(page, 1), pages)
    rows = with_evidence(models.list_anomalies(
        conn, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE, **filters))
    query = urlencode({key: value for key, value in filters.items() if value})
    return_to = "/anomalies" + (f"?{query}" if query else "")

    form = _filter_form(filters)
    if not rows:
        empty = c.empty_state(
            "当前筛选条件下没有异常",
            "换一个类型 / 状态 / 时间范围试试，或清空筛选看全部工单。",
            '<p><a class="btn" href="/anomalies">清空筛选</a></p>',
        )
        table = ""
    else:
        empty = ""
        table = (
            '<table class="grid"><thead><tr>'
            "<th>订单号</th><th>类型</th><th>严重度</th><th>缺口</th>"
            "<th>支付时间</th><th>处理状态</th><th></th>"
            "</tr></thead><tbody>"
            + c.anomaly_rows(rows, return_to=return_to)
            + "</tbody></table>"
        )

    page_note = f"（第 {page} / {pages} 页，缺口为本页合计）" if pages > 1 else ""
    summary = (
        f'<div class="count-line">共 <b>{total}</b> 笔工单，'
        f'缺口合计 <b>{yuan_mark(sum(int(row["gap_fen"]) for row in rows))}</b>'
        f'{page_note}'
        f'{"（已按筛选条件过滤）" if query else ""}</div>'
    )

    export_links = (
        '<div class="quick">'
        f'<a class="btn" href="/export/batch.md?{query}">导出当前筛选（Markdown）</a>'
        f'<a class="btn" href="/export/batch.csv?{query}">导出当前筛选（CSV）</a>'
        "</div>"
    )

    return _page("anomalies.html", "异常中心", "anomalies", env,
                 filters=form, summary=summary, pager=_pager(page, pages, query),
                 table=table, empty=empty,
                 export_links=export_links, flash=flash)


def _pager(page: int, pages: int, query: str) -> str:
    """分页条：只改 page 参数，筛选条件原样带回。"""
    if pages <= 1:
        return ""

    def link(target: int, text: str) -> str:
        params = f"{query}&amp;" if query else ""
        return f'<a class="btn" href="/anomalies?{params}page={target}">{text}</a>'

    parts = []
    if page > 1:
        parts.append(link(page - 1, "上一页"))
    parts.append(f'<span class="muted small">第 {page} / {pages} 页</span>')
    if page < pages:
        parts.append(link(page + 1, "下一页"))
    return '<div class="quick">' + "".join(parts) + "</div>"


def _filter_form(filters: dict) -> str:
    def select(name, options, current, blank_text):
        opts = [f'<option value="">{blank_text}</option>']
        for value, text in options:
            selected = " selected" if value == current else ""
            opts.append(f'<option value="{value}"{selected}>{text}</option>')
        return f'<select name="{name}">{"" .join(opts)}</select>'

    label_options = [(label, config.LABEL_TEXT[label]) for label in config.LABELS]
    severity_options = [("high", "高"), ("medium", "中"), ("low", "低")]
    status_options = [(code, config.STATUS_TEXT[code]) for code in config.STATUSES]

    return (
        '<form class="filters" method="get" action="/anomalies">'
        f'<label>类型{select("label", label_options, filters.get("label", ""), "全部")}</label>'
        f'<label>严重度{select("severity", severity_options, filters.get("severity", ""), "全部")}</label>'
        f'<label>状态{select("status", status_options, filters.get("status", ""), "全部")}</label>'
        f'<label>起始日期<input type="date" name="date_from" value="{c.esc(filters.get("date_from", ""))}"></label>'
        f'<label>截止日期<input type="date" name="date_to" value="{c.esc(filters.get("date_to", ""))}"></label>'
        '<button class="btn btn-primary" type="submit">筛选</button>'
        '<a class="btn" href="/anomalies">重置</a>'
        "</form>"
    )


# ---------------------------------------------------------------- 资金链路

def trail_page(conn, order_id: str, flash: str = "") -> str:
    env = environment(conn)
    trail = reconciler.build_trail(conn, order_id)
    if not trail:
        raise NotFound(f"账本中没有订单 {order_id}")

    items = with_evidence(trail["anomalies"])
    main_id = trail["main"]["anomaly_id"] if trail["main"] else ""
    drawers = "".join(
        c.evidence_drawer(item, open_by_default=(item["anomaly_id"] == main_id))
        for item in items
    ) or (
        '<div class="ok-banner">本笔订单五段链路全部走通，未命中任何规则。</div>'
    )

    order = trail["order"]
    head = (
        f'<div class="trail-head">'
        f'<div class="trail-id mono">{c.esc(order_id)}</div>'
        f'<div class="trail-meta">'
        f'<span>{c.esc(config.CHANNEL_TEXT.get(order["channel"], order["channel"]))}</span>'
        f'<span class="dot">·</span><span>{c.esc(order["paid_at"])}</span>'
        f'<span class="dot">·</span><span>订单额 {yuan_mark(int(order["amount_fen"]))}</span>'
        f'<span class="dot">·</span><span>退款 {yuan_mark(trail["refund_sum_fen"])}</span>'
        f"</div></div>"
    )
    badges = "".join(c.label_badge(item["label_code"]) for item in items)
    head += f'<div class="trail-badges">{badges}</div>' if badges else ""

    if trail["broken_index"] >= 0:
        main_summary = (
            f'<div class="break-summary">'
            f'<span class="break-icon">✂</span>'
            f'<div><div class="break-title">断在第 {trail["broken_index"] + 1} 环 · '
            f'{c.esc(trail["stages"][trail["broken_index"]]["name"])}'
            f'　{trail["gap_text"]}</div>'
            f'<div class="break-reason">{c.esc(trail["reason"])}</div></div>'
            "</div>"
        )
    else:
        main_summary = ""

    export_buttons = (
        '<div class="quick">'
        f'<a class="btn btn-primary" href="/export/order/{c.esc(order_id)}.md">导出核查单（Markdown）</a>'
        f'<a class="btn" href="/export/order/{c.esc(order_id)}.csv">导出明细（CSV）</a>'
        '<a class="btn" href="/anomalies">返回异常中心</a>'
        "</div>"
    )

    related = _related_orders(conn, order_id)

    return _page("trail.html", f"链路 {order_id}", "anomalies", env,
                 head=head, svg=svg.build(trail, title=f"{order_id} 资金链路"),
                 main_summary=main_summary, drawers=drawers,
                 export_buttons=export_buttons, related=related, flash=flash,
                 reference=trail["reference"])


def _related_orders(conn, order_id: str) -> str:
    rows = conn.execute(
        "SELECT order_id FROM anomalies WHERE order_id <> ? "
        "ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, "
        "ABS(gap_fen) DESC LIMIT 4", (order_id,)
    ).fetchall()
    if not rows:
        return ""
    links = "".join(
        f'<a class="chip" href="/trail/{c.esc(row["order_id"])}">{c.esc(row["order_id"])}</a>'
        for row in rows
    )
    return f'<div class="related"><span class="muted small">其它异常订单</span>{links}</div>'


# ---------------------------------------------------------------- 问答

EXAMPLES = (
    ("今天哪几笔没到账", "今天哪几笔没到账"),
    ("MT20260921-001 这笔为什么对不上", "MT20260921-001 为什么对不上"),
    ("这个月手续费多收了多少", "这个月手续费多收了多少"),
)


def assistant_page(conn, question: str = "", flash: str = "") -> str:
    env = environment(conn)
    status = assistant.ai_status(conn)
    result = assistant.ask(conn, question) if question else None

    if status["available"]:
        status_detail = f'{status.get("model", "")} · {status.get("endpoint", "")}'
    else:
        status_detail = status["reason"]
    status_box = (
        f'<div class="ai-status {"" if status["available"] else "off"}">'
        f'<span class="ai-dot"></span>'
        f'<b>{"模型已接入" if status["available"] else "模型不可用"}</b>'
        f'<span class="muted">{c.esc(status_detail)}</span>'
        "</div>"
    )
    toggle_text = "关闭熔断开关" if assistant.kill_switch(conn) else "模拟 AI 不可用"
    toggle = (
        '<form class="inline" method="post" action="/ai/toggle">'
        f'<button class="btn" type="submit">{toggle_text}</button></form>'
    )

    if result:
        facts_html = _facts_table(result)
        answer_panel = (
            '<section class="answer">'
            f'<div class="answer-mode">回答方式：'
            f'{"模型措辞（已通过数字校验）" if result["mode"] == "model" else "模板兜底"}'
            f'　·　意图来源：{"模型" if result["intent"].get("by") == "model" else "关键词"}'
            f'</div>'
            f'<div class="answer-text">{c.esc(result["answer"]).replace(chr(10), "<br>")}</div>'
            f"{facts_html}"
            "</section>"
        )
        transparency = (
            '<section class="transparency">'
            "<h3>确定性证据 · 程序实际执行的每一步</h3>"
            '<div class="triplet">'
            f'<div><h4>① 意图 JSON</h4><pre class="sql">{c.esc(_pretty(result["intent"]))}</pre></div>'
            f'<div><h4>② 程序生成的 SQL</h4><pre class="sql">{c.esc(result["sql"] or "（拒答，未执行 SQL）")}</pre></div>'
            f'<div><h4>③ 事实清单来源</h4><pre class="sql">{c.esc(_sources_text(result))}</pre></div>'
            "</div>"
            f'<p class="muted small">{c.esc(result["note"])}</p>'
            "</section>"
        )
    else:
        answer_panel = (
            '<section class="answer placeholder">'
            '<p class="muted">在上方提出一个关于账本的问题，或点下面的例子。'
            "回答里的所有数字都来自数据库查询，模型只负责组织措辞。</p>"
            "</section>"
        )
        transparency = ""

    examples = "".join(
        f'<a class="chip" href="/assistant?{urlencode({"q": question})}">{text}</a>'
        for question, text in EXAMPLES
    )
    ask_form = (
        '<form class="ask" method="get" action="/assistant">'
        f'<input type="text" name="q" value="{c.esc(question)}" '
        'placeholder="例如：今天哪几笔没到账" autocomplete="off">'
        '<button class="btn btn-primary" type="submit">提问</button>'
        "</form>"
    )

    return _page("assistant.html", "问一句", "assistant", env,
                 status_box=status_box + toggle, ask_form=ask_form,
                 examples=examples, answer_panel=answer_panel,
                 transparency=transparency, flash=flash)


def _pretty(data: dict) -> str:
    import json
    return json.dumps(data, ensure_ascii=False, indent=2)


def _facts_table(result: dict) -> str:
    if not result["facts"]:
        return ""
    rows = "".join(
        f'<tr><td class="mono">{c.order_link(item.get("order_id", ""))}</td>'
        f'<td>{c.esc(item.get("channel", ""))}</td>'
        f'<td class="mono">{c.esc(item.get("paid_at", ""))}</td>'
        f'<td class="mono">{yuan_mark(int(item.get("gap_fen", 0)))}</td></tr>'
        for item in result["facts"]
    )
    return (
        '<table class="grid compact"><thead><tr><th>订单号</th><th>渠道</th>'
        f"<th>支付时间</th><th>缺口</th></tr></thead><tbody>{rows}</tbody></table>"
    )


def _sources_text(result: dict) -> str:
    if not result["facts"]:
        return "（无事实行，未访问数据表）"
    return "\n".join(
        f'{item.get("order_id")} ← {", ".join(sorted(item.keys()))}'
        for item in result["facts"]
    )


# ---------------------------------------------------------------- 导入

def import_page(conn, message: str = "", errors: list = None) -> str:
    env = environment(conn)
    errors = errors or []
    banners = ""
    if message:
        banners += c.alert("ok", message)
    if errors:
        banners += c.alert("error", f"导入被拒，整份文件未写入（共 {len(errors)} 处问题）",
                           c.error_table(errors))
    forms = "".join(
        f'<form class="upload" method="post" action="/import" enctype="multipart/form-data">'
        f'<h4>{c.esc(importer.KIND_TEXT[name])}</h4>'
        f'<input type="hidden" name="kind" value="{name}">'
        f'<input type="file" name="file" accept=".csv,text/csv" required>'
        f'<div class="muted small mono">{", ".join(importer.COLUMNS[name])}</div>'
        '<button class="btn" type="submit">导入</button>'
        "</form>"
        for name in importer.KINDS
    )
    batches = importer.recent_batches(conn, 10)
    if batches:
        rows = "".join(
            f'<tr><td class="mono">#{row["batch_id"]}</td>'
            f'<td>{c.esc(importer.KIND_TEXT.get(row["kind"], row["kind"]))}</td>'
            f'<td class="mono">{c.esc(row["file_hash"][:12])}…</td>'
            f'<td class="mono">{row["rows"]}</td>'
            f'<td class="mono">{c.esc(row["imported_at"])}</td></tr>'
            for row in batches
        )
        history = (
            '<table class="grid compact"><thead><tr><th>批次</th><th>类型</th>'
            f"<th>文件哈希</th><th>行数</th><th>导入时间</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
    else:
        history = c.empty_state("还没有导入记录",
                                "上传三类 CSV 后，这里会留下可追溯的批次记录。")

    return _page("import.html", "账单导入", "import", env,
                 banners=banners, forms=forms, history=history)


# ---------------------------------------------------------------- 404

def error_page(message: str, env: str = "", hint: str = "") -> str:
    env = env or "MoneyTrail"
    # message 可能来自 URL（例如未知订单号），必须转义后再进模板。
    return _page("error.html", "页面不存在", "", env,
                 message=c.esc(message),
                 hint=c.esc(hint or "检查一下地址，或回到总览重新开始。"))
