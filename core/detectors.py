"""四类异常检测。

每条规则独立可测、可单独开关；阈值来自 config.py，不硬编码在逻辑里。
同一订单可被多条规则命中，结果中通过 label_code 区分主因。

所有金额加减乘除均为整数「分」运算。`CAST(... AS REAL)` 只出现在规则四的
费率比值比较中，不参与任何金额计算。
"""

import json
from datetime import datetime

import config
from core import models
from core.money import fee_from_bp, yuan_mark

# ---------------------------------------------------------------- 可展示 SQL

RULE_SQL = {
    "NOT_SETTLED": """-- 规则一 · 已支付未到账
SELECT o.order_id, o.channel, o.paid_at, o.amount_fen
FROM orders o
LEFT JOIN settlements s ON s.order_id = o.order_id
WHERE s.order_id IS NULL;""",
    "DUP_REFUND": """-- 规则二 · 重复退款（同额 + 时间窗由程序二次判定）
SELECT r.order_id, COUNT(*) AS n, SUM(r.amount_fen) AS refund_sum
FROM refunds r
GROUP BY r.order_id
HAVING COUNT(*) > 1;""",
    "AMOUNT_MISMATCH": """-- 规则三 · 到账金额不符
WITH rf AS (
  SELECT order_id, SUM(amount_fen) AS refund_sum
  FROM refunds GROUP BY order_id
)
SELECT s.settlement_id, s.order_id, s.net_fen,
       s.gross_fen - s.fee_fen - COALESCE(rf.refund_sum, 0) AS expected_net,
       s.net_fen - (s.gross_fen - s.fee_fen - COALESCE(rf.refund_sum, 0)) AS diff_fen
FROM settlements s
LEFT JOIN rf ON rf.order_id = s.order_id
WHERE s.net_fen <> s.gross_fen - s.fee_fen - COALESCE(rf.refund_sum, 0);""",
    "FEE_ANOMALY": f"""-- 规则四 · 手续费异常（费率比值比较，不参与金额运算）
SELECT s.settlement_id, s.order_id, s.channel, s.gross_fen, s.fee_fen,
       CAST(s.fee_fen AS REAL) / s.gross_fen         AS actual_rate,
       r.rate                                        AS agreed_rate
FROM settlements s
JOIN channel_rates r ON r.channel = s.channel
WHERE s.gross_fen > 0
  AND ABS(CAST(s.fee_fen AS REAL) / s.gross_fen - r.rate)
      > r.rate * {config.FEE_TOLERANCE_RATIO};""",
}

RULE_THRESHOLDS = {
    "NOT_SETTLED": {"基准时点": "T = 数据集中最新流水时间", "匹配方式": "订单号精确匹配"},
    "DUP_REFUND": {"同额判定": "两笔退款金额完全相等",
                   "时间窗": f"{config.DUP_REFUND_WINDOW_MINUTES} 分钟"},
    "AMOUNT_MISMATCH": {"期望净额": "gross_fen − fee_fen − Σrefunds.amount_fen",
                        "容差": "0 分（整数分严格相等）"},
    "FEE_ANOMALY": {"费率容差": f"{config.FEE_TOLERANCE_RATIO:.0%}",
                    "比较方式": "|实际费率 − 约定费率| > 约定费率 × 容差"},
}


def executable_sql(label_code: str) -> str:
    """从带注释的展示 SQL 中取出可直接执行的语句。

    规则 SQL 与证据抽屉里展示的 SQL 是同一份字符串 —— 展示的就是执行的，
    避免「文档一套、代码另一套」。
    """
    lines = [line for line in RULE_SQL[label_code].splitlines() if not line.strip().startswith("--")]
    return "\n".join(lines).strip().rstrip(";")


# ---------------------------------------------------------------- 小工具

def parse_time(text: str):
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def human_age(reference: str, since: str) -> str:
    """'已支付 3 天 2 小时'。"""
    a, b = parse_time(reference), parse_time(since)
    if not a or not b:
        return ""
    minutes = int((a - b).total_seconds() // 60)
    if minutes < 0:
        minutes = 0
    days, rest = divmod(minutes, 24 * 60)
    hours = rest // 60
    if days:
        return f"已支付 {days} 天{f' {hours} 小时' if hours else ''}"
    if hours:
        return f"已支付 {hours} 小时"
    return f"已支付 {max(minutes, 1)} 分钟"


def anomaly_id(label_code: str, order_id: str) -> str:
    """确定性 id：同一订单同一规则永远得到同一个工单号，天然幂等。"""
    return f"AN-{config.LABEL_SHORT[label_code]}-{order_id}"


def raw_rows(conn, order_id: str) -> list:
    """evidence 第①段：把库里的行还原成 CSV 原文，供人肉眼比对。"""
    lines = []
    order = models.get_order(conn, order_id)
    if order:
        lines.append({"source": "orders",
                      "text": f"{order['order_id']},{order['channel']},{order['paid_at']},"
                              f"{order['amount_fen']},{order['status']}"})
    for refund in models.get_refunds(conn, order_id):
        lines.append({"source": "refunds",
                      "text": f"{refund['refund_id']},{refund['order_id']},{refund['refunded_at']},"
                              f"{refund['amount_fen']},{refund['reason']}"})
    for settlement in conn.execute(
            "SELECT * FROM settlements WHERE order_id = ? ORDER BY settled_at, settlement_id",
            (order_id,)):
        lines.append({"source": "settlements",
                      "text": f"{settlement['settlement_id']},{settlement['order_id']},"
                              f"{settlement['channel']},{settlement['settled_at']},"
                              f"{settlement['gross_fen']},{settlement['fee_fen']},"
                              f"{settlement['refund_fen']},{settlement['net_fen']}"})
    return lines


def _finding(label_code: str, order_id: str, *, expected: int, actual: int,
             diff: int, gap: int, steps: list, reason: str, suggestion: str,
             extra: dict = None) -> dict:
    return {
        "anomaly_id": anomaly_id(label_code, order_id),
        "order_id": order_id,
        "label_code": label_code,
        "severity": config.SEVERITY[label_code],
        "expected_fen": int(expected),
        "actual_fen": int(actual),
        "diff_fen": int(diff),
        "gap_fen": int(gap),
        "steps": steps,
        "reason": reason,
        "suggestion": suggestion,
        "extra": extra or {},
    }


def _sum_refunds(conn, order_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_fen), 0) AS s FROM refunds WHERE order_id = ?", (order_id,)
    ).fetchone()
    return int(row["s"])


def agreed_rate_bp(conn, channel: str) -> int:
    """约定费率（基点）。优先取 channel_rates 表，缺省回落到 config 的出厂值。"""
    rates = models.load_rates(conn)
    rate_bp = rates.get(channel, {}).get("rate_bp")
    if rate_bp is None:
        rate_bp = config.DEFAULT_RATE_BP.get(channel, 0)
    return int(rate_bp)


def agreed_fee(conn, channel: str, amount_fen: int) -> int:
    return fee_from_bp(amount_fen, agreed_rate_bp(conn, channel))


# ---------------------------------------------------------------- 规则一

def detect_not_settled(conn, reference: str) -> list:
    sql = executable_sql("NOT_SETTLED")
    rows = conn.execute(sql).fetchall()
    findings = []
    for row in rows:
        order_id = row["order_id"]
        gross = int(row["amount_fen"])
        fee = agreed_fee(conn, row["channel"], gross)
        refund = _sum_refunds(conn, order_id)
        expected = gross - fee - refund
        if expected <= 0:
            # 退款已覆盖全部应收（含手续费）：平台本就不欠这笔钱，不开工单。
            continue
        age = human_age(reference, row["paid_at"])
        steps = [
            f"gross        = {gross:>7} 分   （订单金额）",
            f"fee（约定）  = {fee:>7} 分   （{config.CHANNEL_TEXT.get(row['channel'], row['channel'])}"
            f" 约定费率）",
            f"refund       = {refund:>7} 分   （Σ refunds.amount_fen）",
            f"应收净额     = {gross} − {fee} − {refund} = {expected} 分",
            f"实际到账     = {0:>7} 分   （settlements 中查无该订单号）",
            f"缺口         = {expected} − 0 = {expected} 分（{yuan_mark(expected)}）",
            f"判定         = NOT_SETTLED（规则一）",
        ]
        findings.append(_finding(
            "NOT_SETTLED", order_id,
            expected=expected, actual=0, diff=-expected, gap=expected,
            steps=steps,
            reason=f"结算表中查无该订单号，{age or '已支付一段时间'}，应收净额 {yuan_mark(expected)} 未到账",
            suggestion="向收单渠道提交订单号与支付时间，要求补结算或出具未结算原因",
            extra={"paid_at": row["paid_at"], "channel": row["channel"],
                   "age": age, "agreed_fee_fen": fee, "refund_fen": refund},
        ))
    return findings


# ---------------------------------------------------------------- 规则二

def detect_dup_refund(conn, reference: str) -> list:
    sql = executable_sql("DUP_REFUND")
    rows = conn.execute(sql).fetchall()
    window = config.DUP_REFUND_WINDOW_MINUTES
    findings = []
    for row in rows:
        order_id = row["order_id"]
        refunds = models.get_refunds(conn, order_id)
        duplicates = []
        used = set()
        for i, first in enumerate(refunds):
            if i in used:
                continue
            partner = None
            for j in range(i + 1, len(refunds)):
                if j in used:
                    continue
                second = refunds[j]
                if int(second["amount_fen"]) != int(first["amount_fen"]):
                    continue
                t1, t2 = parse_time(first["refunded_at"]), parse_time(second["refunded_at"])
                if not t1 or not t2:
                    continue
                if abs((t2 - t1).total_seconds()) <= window * 60:
                    partner = j
                    break
            if partner is None:
                continue
            used.update({i, partner})
            duplicates.append((first, refunds[partner]))

        if not duplicates:
            continue

        excess = sum(int(second["amount_fen"]) for _, second in duplicates)
        refund_sum = int(row["refund_sum"])
        order = models.get_order(conn, order_id)
        gross = int(order["amount_fen"]) if order else refund_sum
        steps = [
            f"refunds 笔数 = {int(row['n'])} 笔，合计 {refund_sum} 分",
            f"判定窗口     = 同额 + {window} 分钟内",
        ]
        for first, second in duplicates:
            steps.append(
                f"  命中重复   = {first['refund_id']}（{first['refunded_at']}）与 "
                f"{second['refund_id']}（{second['refunded_at']}），同为 "
                f"{yuan_mark(int(first['amount_fen']))}"
            )
        steps += [
            f"重复金额     = {excess} 分（{yuan_mark(excess)}）",
            f"订单金额     = {gross} 分，合计退款已占 "
            f"{(refund_sum * 100 // gross) if gross else 0}%",
            f"判定         = DUP_REFUND（规则二）",
        ]
        findings.append(_finding(
            "DUP_REFUND", order_id,
            expected=refund_sum - excess, actual=refund_sum, diff=excess, gap=excess,
            steps=steps,
            reason=f"同一订单在 {window} 分钟内出现同额退款各 {len(duplicates)} 组，"
                   f"疑似重复执行，多退 {yuan_mark(excess)}",
            suggestion="对照退款单号与顾客沟通记录，向渠道申请冲正多退部分",
            extra={"refund_ids": [second["refund_id"] for _, second in duplicates],
                   "refund_sum_fen": refund_sum, "window_minutes": window},
        ))
    return findings


# ---------------------------------------------------------------- 规则三

def detect_amount_mismatch(conn, reference: str) -> list:
    sql = executable_sql("AMOUNT_MISMATCH")
    rows = conn.execute(sql).fetchall()
    findings = []
    for row in rows:
        order_id = row["order_id"]
        # 必须取 SQL 命中的那一条结算行：一单多结算时按订单取首条会张冠李戴。
        settlement = models.get_settlement_by_id(conn, row["settlement_id"])
        if settlement is None:
            continue
        gross = int(settlement["gross_fen"])
        fee = int(settlement["fee_fen"])
        net = int(settlement["net_fen"])
        refund = _sum_refunds(conn, order_id)
        expected = int(row["expected_net"])
        diff = int(row["diff_fen"])
        short = diff < 0
        steps = [
            f"gross        = {gross:>7} 分",
            f"fee          = {fee:>7} 分",
            f"refund       = {refund:>7} 分",
            f"期望净额     = {gross} − {fee} − {refund} = {expected} 分",
            f"实际 net     = {net:>7} 分",
            f"差额         = {expected} − {net} = {abs(diff)} 分（{yuan_mark(abs(diff))}）"
            f"  {'平台少付' if short else '平台多付'}",
            f"判定         = AMOUNT_MISMATCH（规则三）",
        ]
        findings.append(_finding(
            "AMOUNT_MISMATCH", order_id,
            expected=expected, actual=net, diff=diff, gap=abs(diff),
            steps=steps,
            reason=(f"到账净额比「订单额 − 手续费 − 退款」{'少' if short else '多'} "
                    f"{yuan_mark(abs(diff))}，结算明细与订单未对齐"),
            suggestion=("以核查单向渠道申诉补付差额" if short
                        else "差额为平台多付，建议与渠道确认后冲抵，避免被后续扣回"),
            extra={"settlement_id": settlement["settlement_id"],
                   "settled_at": settlement["settled_at"],
                   "gross_fen": gross, "fee_fen": fee},
        ))
    return findings


# ---------------------------------------------------------------- 规则四

def detect_fee_anomaly(conn, reference: str) -> list:
    sql = executable_sql("FEE_ANOMALY")
    rows = conn.execute(sql).fetchall()
    findings = []
    for row in rows:
        order_id = row["order_id"]
        settlement = models.get_settlement_by_id(conn, row["settlement_id"])
        if settlement is None:
            continue
        gross = int(settlement["gross_fen"])
        fee = int(settlement["fee_fen"])
        if gross <= 0:
            continue
        # 约定费率与规则一同源（channel_rates），避免「展示的阈值 ≠ 判定的阈值」。
        rate_bp = agreed_rate_bp(conn, row["channel"])
        agreed = fee_from_bp(gross, rate_bp)
        diff = fee - agreed
        overcharge = max(diff, 0)
        actual_rate = fee / gross
        steps = [
            f"gross        = {gross:>7} 分",
            f"fee（实际）  = {fee:>7} 分，实际费率 = {fee} / {gross} = {actual_rate:.4%}",
            f"fee（约定）  = {agreed:>7} 分，约定费率 = {rate_bp / 100:.2f}%",
            f"费率偏差     = |{actual_rate:.4%} − {rate_bp / 100:.2f}%| = "
            f"{abs(actual_rate - rate_bp / 10000):.4%}，"
            f"容差 {config.FEE_TOLERANCE_RATIO:.0%} × {rate_bp / 100:.2f}% = "
            f"{rate_bp / 10000 * config.FEE_TOLERANCE_RATIO:.4%}",
            f"金额偏差     = {fee} − {agreed} = {diff} 分（{yuan_mark(abs(diff))}）",
            f"判定         = FEE_ANOMALY（规则四）",
        ]
        findings.append(_finding(
            "FEE_ANOMALY", order_id,
            expected=agreed, actual=fee, diff=diff, gap=overcharge,
            steps=steps,
            reason=(f"实际费率 {actual_rate:.2%} 偏离约定费率 {rate_bp / 100:.2f}% 超过 "
                    f"{config.FEE_TOLERANCE_RATIO:.0%} 容差，"
                    + (f"多收手续费 {yuan_mark(overcharge)}" if overcharge else "实际费率低于约定")),
            suggestion="向渠道索要费率备案文件，要求按约定费率重算并退还多收部分",
            extra={"settlement_id": settlement["settlement_id"], "channel": row["channel"],
                   "agreed_fee_fen": agreed, "actual_rate": round(actual_rate, 6),
                   "agreed_rate": rate_bp / 10000},
        ))
    return findings


DETECTORS = {
    "NOT_SETTLED": detect_not_settled,
    "DUP_REFUND": detect_dup_refund,
    "AMOUNT_MISMATCH": detect_amount_mismatch,
    "FEE_ANOMALY": detect_fee_anomaly,
}


def detect_all(conn, reference: str = "") -> list:
    """按 config.RULES_ENABLED 依次跑规则，返回去重后的全部 finding。"""
    reference = reference or models.reference_time(conn)
    findings = []
    for label_code in config.LABELS:
        if not config.RULES_ENABLED.get(label_code, False):
            continue
        findings.extend(DETECTORS[label_code](conn, reference))
    return dedupe(findings)


def dedupe(findings: list) -> list:
    """同一 (订单, 规则) 只保留缺口最大的一条。

    工单以「订单 × 规则」为主键（anomaly_id 由二者决定），而规则三/四的 SQL
    是「一条结算一行」。一单多结算时若不去重，就会出现两条同 id 的工单，
    写入时触发主键冲突。保留缺口最大的一条，等于按订单取最严重的那笔。
    """
    best = {}
    for finding in findings:
        key = finding["anomaly_id"]
        current = best.get(key)
        if current is None or abs(finding["gap_fen"]) > abs(current["gap_fen"]):
            best[key] = finding
    return list(best.values())


def build_evidence(conn, finding: dict, reference: str) -> dict:
    """组装证据抽屉的三段内容：原始流水 / 逐行计算 / 判定依据。"""
    label_code = finding["label_code"]
    return {
        "label_code": label_code,
        "rule": config.RULE_NAME[label_code],
        "rule_thresholds": RULE_THRESHOLDS[label_code],
        "rule_sql": RULE_SQL[label_code],
        "reference_time": reference,
        "raw_rows": raw_rows(conn, finding["order_id"]),
        "steps": finding["steps"],
        "reason": finding["reason"],
        "suggestion": finding["suggestion"],
        "amounts": {
            "expected_fen": finding["expected_fen"],
            "actual_fen": finding["actual_fen"],
            "diff_fen": finding["diff_fen"],
            "gap_fen": finding["gap_fen"],
            "expected": yuan_mark(finding["expected_fen"]),
            "actual": yuan_mark(finding["actual_fen"]),
            "gap": yuan_mark(finding["gap_fen"]),
        },
        "extra": finding.get("extra", {}),
    }


def evidence_json(conn, finding: dict, reference: str) -> str:
    return json.dumps(build_evidence(conn, finding, reference), ensure_ascii=False)
