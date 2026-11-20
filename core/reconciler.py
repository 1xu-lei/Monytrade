"""对账：把四类检测结果落成工单，并维护总览与链路视图所需的数据形状。

两层职责：
1. reconcile()  —— 幂等重建工单集合：清空后重建，状态按 (订单号, 规则) 继承；
2. 只读查询    —— 总览四个数字、单笔资金链路的五节点模型。
"""

from datetime import datetime, timedelta
import json

import config
from core import detectors, models
from core.money import yuan_mark

STAGE_OF_LABEL = {
    "DUP_REFUND": 2,
    "FEE_ANOMALY": 3,
    "AMOUNT_MISMATCH": 4,
    "NOT_SETTLED": 4,
}

STAGE_NAMES = ("顾客支付", "平台受理", "退款发生", "手续费扣减", "结算到账")

SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}


def _inherited_status(conn) -> dict:
    rows = conn.execute(
        "SELECT order_id, label_code, status, note, first_seen_at FROM anomalies"
    ).fetchall()
    return {(row["order_id"], row["label_code"]): row for row in rows}


def reconcile(conn, *, reference: str = "") -> dict:
    """全量重算并落库。重复执行结果一致，不产生重复工单。"""
    started = datetime.now()
    reference = reference or models.reference_time(conn)
    inherited = _inherited_status(conn)
    findings = detectors.detect_all(conn, reference)

    stamp = models.now_text()
    carried = 0
    payload = []
    for finding in findings:
        key = (finding["order_id"], finding["label_code"])
        previous = inherited.get(key)
        if previous:
            carried += 1
            status = previous["status"]
            note = previous["note"]
            first_seen = previous["first_seen_at"]
        else:
            status, note, first_seen = "pending", "", stamp
        payload.append({
            "anomaly_id": finding["anomaly_id"],
            "order_id": finding["order_id"],
            "label_code": finding["label_code"],
            "severity": finding["severity"],
            "expected_fen": finding["expected_fen"],
            "actual_fen": finding["actual_fen"],
            "diff_fen": finding["diff_fen"],
            "gap_fen": finding["gap_fen"],
            "evidence_json": detectors.evidence_json(conn, finding, reference),
            "status": status,
            "note": note,
            "first_seen_at": first_seen,
            "updated_at": stamp,
        })

    with conn:  # 「先清空后重建」放在同一事务里，中途不会留下空表
        conn.execute("DELETE FROM anomalies")
        models.insert_rows(conn, "anomalies", payload)

    counts = {label: 0 for label in config.LABELS}
    for finding in findings:
        counts[finding["label_code"]] += 1

    elapsed = (datetime.now() - started).total_seconds() * 1000
    return {
        "reference_time": reference,
        "total": len(findings),
        "counts": counts,
        "gap_fen": sum(finding["gap_fen"] for finding in findings),
        "carried": carried,
        "created": len(findings) - carried,
        "duration_ms": round(elapsed, 1),
    }


# ---------------------------------------------------------------- 总览

def stats(conn) -> dict:
    """四个核心数字 + 分类计数。金额全部由 SQL 聚合，不经 Python 推算。"""
    order_row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(amount_fen), 0) AS total FROM orders"
    ).fetchone()
    settled_row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(net_fen), 0) AS total,"
        "       COALESCE(SUM(fee_fen), 0) AS fee, COALESCE(SUM(refund_fen), 0) AS refund"
        " FROM settlements"
    ).fetchone()
    refund_row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(amount_fen), 0) AS total FROM refunds"
    ).fetchone()
    placeholders = ", ".join("?" for _ in config.OPEN_STATUSES)
    open_row = conn.execute(
        f"SELECT COUNT(*) AS n, COALESCE(SUM(gap_fen), 0) AS gap FROM anomalies "
        f"WHERE status IN ({placeholders})",
        list(config.OPEN_STATUSES),
    ).fetchone()
    total_row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(gap_fen), 0) AS gap FROM anomalies"
    ).fetchone()
    by_label = {label: 0 for label in config.LABELS}
    for row in conn.execute("SELECT label_code, COUNT(*) AS n FROM anomalies GROUP BY label_code"):
        by_label[row["label_code"]] = row["n"]
    # 严重度计数同样由 SQL 给出：视图不再自己猜「哪类规则算高危」，
    # 规则表（detectors）改档位时这里自动跟着变。
    by_severity = {level: 0 for level in config.SEVERITY_TEXT}
    for row in conn.execute("SELECT severity, COUNT(*) AS n FROM anomalies GROUP BY severity"):
        by_severity[row["severity"]] = row["n"]

    return {
        "order_count": order_row["n"],
        "order_total_fen": order_row["total"],
        "settlement_count": settled_row["n"],
        "received_fen": settled_row["total"],
        "fee_fen": settled_row["fee"],
        "refund_fen": refund_row["total"],
        "refund_count": refund_row["n"],
        "open_gap_fen": open_row["gap"],
        "open_count": open_row["n"],
        "all_gap_fen": total_row["gap"],
        "all_count": total_row["n"],
        "by_label": by_label,
        "by_severity": by_severity,
        "reference_time": models.reference_time(conn),
    }


def top_open(conn, limit: int = 5) -> list:
    """总览页的「待处理异常」。没有未结工单时返回空列表 —— 不回退成已处理工单，
    否则表格里会混进 resolved/ignored 的行，与旁边的「共 0 笔未结」自相矛盾。"""
    return models.list_anomalies(conn, status="pending", limit=limit)


# ---------------------------------------------------------------- 资金链路

def _expected_settle(paid_at: str) -> str:
    try:
        moment = datetime.strptime(paid_at, "%Y-%m-%d %H:%M:%S") + timedelta(days=1)
        return moment.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return paid_at


def primary_anomaly(rows: list):
    return min(rows, key=lambda r: (SEVERITY_RANK.get(r["severity"], 9), -abs(r["gap_fen"]))) \
        if rows else None


def build_trail(conn, order_id: str, anomalies: list = None, reference: str = "") -> dict:
    """把一笔订单还原成五段管道 + 断点位置 + 缺口 + 归因文字。"""
    order = models.get_order(conn, order_id)
    if order is None:
        return {}
    reference = reference or models.reference_time(conn)
    anomalies = list(anomalies) if anomalies is not None else list(
        conn.execute("SELECT * FROM anomalies WHERE order_id = ?", (order_id,))
    )
    refunds = models.get_refunds(conn, order_id)
    settlement = models.get_settlement(conn, order_id)
    extra_settlements = max(models.count_settlements(conn, order_id) - 1, 0)

    gross = int(order["amount_fen"])
    refund_sum = sum(int(r["amount_fen"]) for r in refunds)
    agreed = detectors.agreed_fee(conn, order["channel"], gross)

    stages = [
        {"key": "pay", "name": STAGE_NAMES[0], "state": "ok", "time": order["paid_at"],
         "amount_fen": gross, "ref": order_id, "note": config.CHANNEL_TEXT.get(order["channel"],
                                                                              order["channel"])},
        {"key": "accept", "name": STAGE_NAMES[1], "state": "ok", "time": order["paid_at"],
         "amount_fen": gross, "ref": "—", "note": "渠道已受理"},
    ]

    if refunds:
        stages.append({
            "key": "refund", "name": STAGE_NAMES[2], "state": "ok",
            "time": refunds[-1]["refunded_at"], "amount_fen": refund_sum,
            "ref": "、".join(r["refund_id"] for r in refunds),
            "note": f"共 {len(refunds)} 笔",
        })
    else:
        stages.append({
            "key": "refund", "name": STAGE_NAMES[2], "state": "none", "time": "",
            "amount_fen": 0, "ref": "—", "note": "无退款记录",
        })

    if settlement:
        stages.append({
            "key": "fee", "name": STAGE_NAMES[3], "state": "ok",
            "time": settlement["settled_at"], "amount_fen": int(settlement["fee_fen"]),
            "ref": settlement["settlement_id"],
            "note": f"结算 gross {yuan_mark(int(settlement['gross_fen']))}",
        })
        stages.append({
            "key": "settle", "name": STAGE_NAMES[4], "state": "ok",
            "time": settlement["settled_at"], "amount_fen": int(settlement["net_fen"]),
            "ref": settlement["settlement_id"],
            "note": "已到账" + (f"（共 {extra_settlements} 笔结算，链路只展开首笔）"
                              if extra_settlements else ""),
        })
    else:
        stages.append({
            "key": "fee", "name": STAGE_NAMES[3], "state": "none", "time": "",
            "amount_fen": agreed, "ref": "—", "note": f"无结算记录（应收约 {yuan_mark(agreed)}）",
        })
        stages.append({
            "key": "settle", "name": STAGE_NAMES[4], "state": "ghost",
            "time": _expected_settle(order["paid_at"]), "amount_fen": None,
            "ref": "—", "note": "预计 T+1 到账",
        })

    main = primary_anomaly(anomalies)
    broken_index = -1
    gap_fen = 0
    gap_text = ""
    reason = ""
    if main is not None:
        broken_index = STAGE_OF_LABEL.get(main["label_code"], 4)
        stage = stages[broken_index]
        stage["state"] = "broken"
        gap_fen = abs(int(main["gap_fen"]))
        entry = _evidence(main)
        reason = entry.get("reason") or ""
        if main["label_code"] == "NOT_SETTLED":
            gap_text = f"断链 · 缺 {yuan_mark(gap_fen)}"
        elif main["label_code"] == "AMOUNT_MISMATCH":
            gap_text = f"金额不符 · 差 {yuan_mark(gap_fen)}"
        elif main["label_code"] == "FEE_ANOMALY":
            gap_text = f"费率异常 · 多收 {yuan_mark(gap_fen)}"
        else:
            gap_text = f"重复退款 · 多退 {yuan_mark(gap_fen)}"

    return {
        "order": dict(order),
        "stages": stages,
        "anomalies": [dict(row) for row in anomalies],
        "main": dict(main) if main is not None else None,
        "broken_index": broken_index,
        "gap_fen": gap_fen,
        "gap_text": gap_text,
        "reason": reason,
        "reference": reference,
        "refund_sum_fen": refund_sum,
        "agreed_fee_fen": agreed,
        "settlement": dict(settlement) if settlement else None,
        "refunds": [dict(row) for row in refunds],
    }


def _evidence(anomaly_row) -> dict:
    try:
        return json.loads(anomaly_row["evidence_json"])
    except (TypeError, ValueError):
        return {}


def evidence_of(anomaly_row) -> dict:
    return _evidence(anomaly_row)


def set_status(conn, anomaly_id: str, status: str, note: str = None) -> bool:
    if status not in config.STATUSES:
        return False
    anomaly = models.get_anomaly(conn, anomaly_id)
    if anomaly is None:
        return False
    with conn:
        if note is None:
            conn.execute(
                "UPDATE anomalies SET status = ?, updated_at = ? WHERE anomaly_id = ?",
                (status, models.now_text(), anomaly_id),
            )
        else:
            conn.execute(
                "UPDATE anomalies SET status = ?, note = ?, updated_at = ? WHERE anomaly_id = ?",
                (status, note, models.now_text(), anomaly_id),
            )
    return True
