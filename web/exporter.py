"""核查单导出：单笔 / 批量 × Markdown / CSV。

导出的东西是「能直接发给平台客服的凭据」，因此必须自洽：
订单信息 → 五段链路 → 计算过程 → 原始流水 → 规则依据 → 建议动作，缺一不可。
"""

import csv
import io
from datetime import datetime

import config
from core.money import yuan_mark

CSV_HEADER = ("order_id", "label_code", "label_text", "severity", "expected_fen",
              "actual_fen", "diff_fen", "gap_fen", "status", "reason",
              "suggestion", "reference_time")


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _stage_table(trail: dict) -> list:
    rows = ["| 环节 | 状态 | 时间 | 金额 | 原始流水号 |", "|---|---|---|---|---|"]
    state_text = {"ok": "✅ 已记录", "none": "— 无记录", "warn": "⚠ 异常",
                  "broken": "✂ 断链", "ghost": "◌ 预计"}
    for index, stage in enumerate(trail["stages"], start=1):
        amount = "—" if stage.get("amount_fen") is None else yuan_mark(stage["amount_fen"])
        rows.append(
            f"| {index}. {stage['name']} | {state_text.get(stage['state'], stage['state'])} "
            f"| {stage.get('time') or '—'} | {amount} | {stage.get('ref') or '—'} |"
        )
    return rows


def order_markdown(conn, order_id: str, trail: dict, anomaly_rows: list) -> str:
    order = trail["order"]
    lines = [
        f"# 资金链路核查单 · {order_id}",
        "",
        f"- 订单号：`{order_id}`",
        f"- 收款渠道：{config.CHANNEL_TEXT.get(order['channel'], order['channel'])}",
        f"- 支付时间：{order['paid_at']}",
        f"- 订单金额：{yuan_mark(int(order['amount_fen']))}（{order['amount_fen']} 分）",
        f"- 判定基准时点 T：{trail['reference']}（数据集中最新流水时间）",
        f"- 生成时间：{_stamp()}",
        f"- 生成工具：MoneyTrail {config.VERSION} · 资金链路核查台",
        "",
        "## 一、资金链路",
        "",
    ]
    lines += _stage_table(trail)
    lines += [""]
    if trail["broken_index"] >= 0:
        lines += [
            f"**断点**：第 {trail['broken_index'] + 1} 环「"
            f"{trail['stages'][trail['broken_index']]['name']}」"
            f" —— {trail['gap_text']}",
            "",
            f"**归因**：{trail['reason']}",
            "",
        ]
    else:
        lines += ["**断点**：无，五段链路全部走通。", ""]

    for number, anomaly in enumerate(anomaly_rows, start=1):
        evidence = anomaly["evidence"]
        lines += [
            f"## 二.{number} 差异明细 · {config.LABEL_TEXT[anomaly['label_code']]}",
            "",
            f"- 规则：{config.RULE_NAME[anomaly['label_code']]}",
            f"- 严重程度：{config.SEVERITY_TEXT[anomaly['severity']]}",
            f"- 期望金额：{yuan_mark(anomaly['expected_fen'])}",
            f"- 实际金额：{yuan_mark(anomaly['actual_fen'])}",
            f"- 差额：{yuan_mark(anomaly['diff_fen'])}",
            f"- 缺口：{yuan_mark(anomaly['gap_fen'])}",
            "",
            "```text",
        ]
        lines += evidence.get("steps", [])
        lines += ["```", "", "### 原始流水", "", "| 来源 | 原文 |", "|---|---|"]
        for row in evidence.get("raw_rows", []):
            lines.append(f"| {row['source']} | `{row['text']}` |")
        lines += [
            "",
            "### 判定依据",
            "",
            "```sql",
            evidence.get("rule_sql", "").strip(),
            "```",
            "",
            "阈值快照："
            + "；".join(f"{key} = {value}"
                        for key, value in evidence.get("rule_thresholds", {}).items()),
            "",
            "### 建议后续动作",
            "",
            anomaly["suggestion"] or evidence.get("suggestion", ""),
            "",
        ]

    if not anomaly_rows:
        lines += ["## 二、差异明细", "", "本笔订单未命中任何检测规则。", ""]

    lines += [
        "---",
        "",
        "> 本核查单由 MoneyTrail 依据本地台账自动生成；所有金额均由 SQL 聚合，"
        "以整数「分」计算，不含浮点运算。",
        "",
    ]
    return "\n".join(lines)


def batch_markdown(conn, trails: list, generated_at: str, filters: str = "") -> str:
    total_gap = sum(sum(int(a["gap_fen"]) for a in trail["anomalies"]) for trail in trails)
    lines = [
        "# 资金链路核查单 · 批量",
        "",
        f"- 生成时间：{generated_at}",
        f"- 筛选条件：{filters or '全部未结异常'}",
        f"- 涉及订单：{len(trails)} 笔",
        f"- 缺口合计：{yuan_mark(total_gap)}（{total_gap} 分）",
        f"- 生成工具：MoneyTrail {config.VERSION} · 资金链路核查台",
        "",
        "## 汇总",
        "",
        "| 订单号 | 渠道 | 支付时间 | 订单金额 | 命中规则 | 缺口 | 断点 |",
        "|---|---|---|---|---|---|---|",
    ]
    for trail in trails:
        order = trail["order"]
        labels = "、".join(config.LABEL_TEXT[a["label_code"]] for a in trail["anomalies"]) or "—"
        gap = sum(int(a["gap_fen"]) for a in trail["anomalies"])
        broken = (trail["stages"][trail["broken_index"]]["name"]
                  if trail["broken_index"] >= 0 else "—")
        lines.append(
            f"| {order['order_id']} | "
            f"{config.CHANNEL_TEXT.get(order['channel'], order['channel'])} | "
            f"{order['paid_at']} | {yuan_mark(int(order['amount_fen']))} | "
            f"{labels} | {yuan_mark(gap)} | {broken} |"
        )
    lines.append("")
    for trail in trails:
        lines.append("---")
        lines.append("")
        lines.append(order_markdown(conn, trail["order"]["order_id"], trail, trail["anomalies"]))
    return "\n".join(lines)


def batch_csv(trails: list, reference_time: str) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    for trail in trails:
        for anomaly in trail["anomalies"]:
            writer.writerow([
                anomaly["order_id"],
                anomaly["label_code"],
                config.LABEL_TEXT[anomaly["label_code"]],
                anomaly["severity"],
                anomaly["expected_fen"],
                anomaly["actual_fen"],
                anomaly["diff_fen"],
                anomaly["gap_fen"],
                config.STATUS_TEXT[anomaly["status"]],
                anomaly.get("reason", ""),
                anomaly.get("suggestion", ""),
                reference_time,
            ])
    return "\ufeff" + buffer.getvalue()

