"""演示数据生成器（固定随机种子，结果可复现）。

生成三个 CSV 台账 + 一份 ground_truth.json 标注，然后（可选）直接把数据
灌进 SQLite 并跑一次对账，使 `python app.py` 打开即有数据。

    python gen_data.py                # 写 data/ + 初始化 moneytrail.db
    python gen_data.py --csv-only     # 只写 CSV，不碰数据库
    python gen_data.py --out DIR --db PATH

模型：单店 3 个营业日（2026-09-19 ~ 2026-09-21），T+1 批量结算，
判定基准时点 T = 数据集中最新流水时间（2026-09-22 上午）。
"""

import argparse
import csv
import json
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

import config
from core import importer, models, reconciler
from core.money import fee_from_bp

SEED = 20261119
SETTLE_TIME = "10:20:00"

# 场景表：显式声明每一笔订单的角色，金额与角色都不依赖随机数。
#   refunds         : [(退款时间, 金额分, 原因), ...]
#   fee_override_fen: 渠道实际扣的手续费（用于制造费率异常）
#   net_adjust_fen  : 结算净额相对「订单额 − 手续费 − 退款」的偏差
#   unsettled       : 结算表中故意不出现（制造断链）
ORDERS = [
    # ---- 2026-09-19 ----
    {"order_id": "MT20260919-001", "channel": "wechat", "paid_at": "2026-09-19 09:06:12",
     "amount_fen": 14065},
    {"order_id": "MT20260919-002", "channel": "wechat", "paid_at": "2026-09-19 09:41:33",
     "amount_fen": 12800, "net_adjust_fen": -176},          # 规则三：平台少付 ¥1.76
    {"order_id": "MT20260919-003", "channel": "alipay", "paid_at": "2026-09-19 10:22:05",
     "amount_fen": 9600,
     "refunds": [("2026-09-19 16:40:05", 1000, "少送一件，退差额")]},
    {"order_id": "MT20260919-004", "channel": "meituan", "paid_at": "2026-09-19 11:58:47",
     "amount_fen": 23500, "unsettled": True},                # 规则一：最久的一笔断链
    {"order_id": "MT20260919-005", "channel": "alipay", "paid_at": "2026-09-19 12:30:11",
     "amount_fen": 6800},
    {"order_id": "MT20260919-006", "channel": "meituan", "paid_at": "2026-09-19 14:05:52",
     "amount_fen": 15420,
     "refunds": [("2026-09-19 19:12:00", 3000, "口味问题，部分退款")]},
    {"order_id": "MT20260919-007", "channel": "wechat", "paid_at": "2026-09-19 17:20:19",
     "amount_fen": 8800,
     "refunds": [("2026-09-19 18:04:11", 3000, "顾客取消，退一半"),
                 ("2026-09-19 18:11:47", 3000, "顾客取消，退一半")]},   # 规则二：7 分钟内重复
    {"order_id": "MT20260919-008", "channel": "alipay", "paid_at": "2026-09-19 20:11:03",
     "amount_fen": 11250},

    # ---- 2026-09-20 ----
    {"order_id": "MT20260920-001", "channel": "meituan", "paid_at": "2026-09-20 09:12:40",
     "amount_fen": 38000, "fee_override_fen": 950},          # 规则四：2.50% vs 约定 2.00%
    {"order_id": "MT20260920-002", "channel": "alipay", "paid_at": "2026-09-20 10:05:26",
     "amount_fen": 24295, "unsettled": True},                # 规则一
    {"order_id": "MT20260920-003", "channel": "alipay", "paid_at": "2026-09-20 11:33:58",
     "amount_fen": 5400},
    {"order_id": "MT20260920-004", "channel": "wechat", "paid_at": "2026-09-20 13:14:07",
     "amount_fen": 7650,
     "refunds": [("2026-09-20 14:02:33", 2400, "漏送，退差价")]},
    {"order_id": "MT20260920-005", "channel": "alipay", "paid_at": "2026-09-20 15:47:31",
     "amount_fen": 14300,
     "refunds": [("2026-09-20 16:20:05", 5000, "部分退"),
                 ("2026-09-20 16:22:58", 5000, "部分退")]},    # 规则二：3 分钟内重复
    {"order_id": "MT20260920-006", "channel": "wechat", "paid_at": "2026-09-20 18:02:55",
     "amount_fen": 2600},
    {"order_id": "MT20260920-007", "channel": "alipay", "paid_at": "2026-09-20 19:25:14",
     "amount_fen": 17800},
    {"order_id": "MT20260920-008", "channel": "alipay", "paid_at": "2026-09-20 20:48:39",
     "amount_fen": 9900,
     "refunds": [("2026-09-20 21:03:12", 2500, "打包费重复收取")]},

    # ---- 2026-09-21 ----
    {"order_id": "MT20260921-001", "channel": "wechat", "paid_at": "2026-09-21 09:14:00",
     "amount_fen": 14065, "unsettled": True},                # 规则一：演示主线那一笔
    {"order_id": "MT20260921-002", "channel": "alipay", "paid_at": "2026-09-21 10:38:22",
     "amount_fen": 4500,
     "refunds": [("2026-09-21 11:15:40", 1200, "取消部分菜品")]},
    {"order_id": "MT20260921-003", "channel": "meituan", "paid_at": "2026-09-21 12:07:45",
     "amount_fen": 16800},
    {"order_id": "MT20260921-004", "channel": "wechat", "paid_at": "2026-09-21 13:51:09",
     "amount_fen": 7300},
    {"order_id": "MT20260921-005", "channel": "wechat", "paid_at": "2026-09-21 15:22:36",
     "amount_fen": 26000, "fee_override_fen": 210},          # 规则四：0.81% vs 约定 0.60%
    {"order_id": "MT20260921-006", "channel": "alipay", "paid_at": "2026-09-21 17:03:18",
     "amount_fen": 11100, "net_adjust_fen": 88},             # 规则三：平台多付 ¥0.88
    {"order_id": "MT20260921-007", "channel": "wechat", "paid_at": "2026-09-21 18:44:51",
     "amount_fen": 8900},
    {"order_id": "MT20260921-008", "channel": "alipay", "paid_at": "2026-09-21 20:26:30",
     "amount_fen": 32000},
]


def build(rng: random.Random):
    """返回 (orders, refunds, settlements) 三个 list[dict]。"""
    orders, refunds, settlements = [], [], []
    refund_seq = 0
    per_day_index = {}

    for spec in ORDERS:
        paid = datetime.strptime(spec["paid_at"], "%Y-%m-%d %H:%M:%S")
        day_key = spec["paid_at"][:10]

        orders.append({
            "order_id": spec["order_id"],
            "channel": spec["channel"],
            "paid_at": spec["paid_at"],
            "amount_fen": int(spec["amount_fen"]),
            "status": "PAID",
        })

        refund_sum = 0
        for refunded_at, amount_fen, reason in spec.get("refunds", []):
            refund_seq += 1
            refund_sum += int(amount_fen)
            refunds.append({
                "refund_id": f"RF{refund_seq:04d}",
                "order_id": spec["order_id"],
                "refunded_at": refunded_at,
                "amount_fen": int(amount_fen),
                "reason": reason,
            })

        if spec.get("unsettled"):
            continue

        rate_bp = config.DEFAULT_RATE_BP[spec["channel"]]
        fee = spec.get("fee_override_fen")
        if fee is None:
            fee = fee_from_bp(int(spec["amount_fen"]), rate_bp)
        fee = int(fee)
        net = int(spec["amount_fen"]) - fee - refund_sum + int(spec.get("net_adjust_fen", 0))

        index = per_day_index.get(day_key, 0)
        per_day_index[day_key] = index + 1
        next_day = (paid + timedelta(days=1)).strftime("%Y-%m-%d ")
        settled = datetime.strptime(next_day + SETTLE_TIME, "%Y-%m-%d %H:%M:%S")
        settled += timedelta(seconds=rng.randrange(0, 60))

        settlements.append({
            "settlement_id": f"ST{len(settlements) + 1:07d}",
            "order_id": spec["order_id"],
            "channel": spec["channel"],
            "settled_at": settled.strftime("%Y-%m-%d %H:%M:%S"),
            "gross_fen": int(spec["amount_fen"]),
            "fee_fen": fee,
            "refund_fen": refund_sum,
            "net_fen": net,
        })

    return orders, refunds, settlements


def ground_truth(orders, refunds, settlements) -> dict:
    """标注每笔订单的角色，用于算检出率 / 误报率。"""
    expected, annotations = [], {}
    note = {
        "MT20260919-002": "规则三：平台少付 176 分",
        "MT20260921-006": "规则三：平台多付 88 分",
        "MT20260919-007": "规则二：7 分 36 秒内同额退款 3000 分两笔",
        "MT20260920-005": "规则二：2 分 53 秒内同额退款 5000 分两笔",
        "MT20260920-001": "规则四：美团 2.50% 超出约定 2.00% 容差",
        "MT20260921-005": "规则四：微信 0.81% 超出约定 0.60% 容差",
        "MT20260919-004": "规则一：结算表查无该订单号",
        "MT20260920-002": "规则一：结算表查无该订单号",
        "MT20260921-001": "规则一：结算表查无该订单号（演示主线）",
    }
    settled_ids = {row["order_id"] for row in settlements}
    for order in orders:
        if order["order_id"] not in settled_ids:
            expected.append({"label_code": "NOT_SETTLED", "order_id": order["order_id"]})
    for spec in ORDERS:
        if spec.get("net_adjust_fen"):
            expected.append({"label_code": "AMOUNT_MISMATCH", "order_id": spec["order_id"]})
        if spec.get("fee_override_fen"):
            expected.append({"label_code": "FEE_ANOMALY", "order_id": spec["order_id"]})
        if len(spec.get("refunds", [])) > 1:
            expected.append({"label_code": "DUP_REFUND", "order_id": spec["order_id"]})
    for item in expected:
        annotations[f"{item['label_code']}@{item['order_id']}"] = note.get(item["order_id"], "")

    return {
        "seed": SEED,
        "generated_by": "gen_data.py",
        "counts": {"orders": len(orders), "refunds": len(refunds),
                   "settlements": len(settlements)},
        "totals": {
            "order_total_fen": sum(row["amount_fen"] for row in orders),
            "received_fen": sum(row["net_fen"] for row in settlements),
            "fee_fen": sum(row["fee_fen"] for row in settlements),
            "refund_fen": sum(row["amount_fen"] for row in refunds),
        },
        "expected_anomalies": expected,
        "annotations": annotations,
        "t_reference_hint": "判定基准时点 T = 数据集中最新流水时间",
    }


CSV_HEADERS = {
    "orders": ("order_id", "channel", "paid_at", "amount_fen", "status"),
    "refunds": ("refund_id", "order_id", "refunded_at", "amount_fen", "reason"),
    "settlements": ("settlement_id", "order_id", "channel", "settled_at",
                    "gross_fen", "fee_fen", "refund_fen", "net_fen"),
}


def write_csv(path: Path, kind: str, rows: list) -> None:
    headers = CSV_HEADERS[kind]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row[key] for key in headers})


def generate(out_dir: Path, db_path=None, *, csv_only: bool = False) -> dict:
    rng = random.Random(SEED)
    orders, refunds, settlements = build(rng)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(out_dir / "orders.csv", "orders", orders)
    write_csv(out_dir / "refunds.csv", "refunds", refunds)
    write_csv(out_dir / "settlements.csv", "settlements", settlements)

    truth = ground_truth(orders, refunds, settlements)
    (out_dir / "ground_truth.json").write_text(
        json.dumps(truth, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    result = {"csv_dir": str(out_dir), "db": "", "imported": [], "reconcile": None}
    if not csv_only:
        db_path = db_path or config.DB_PATH
        conn = models.connect_ready(db_path, reset=True)
        models.seed_rates(conn)
        for kind in importer.KINDS:
            result["imported"].append(importer.import_path(conn, kind, out_dir / f"{kind}.csv"))
        result["reconcile"] = reconciler.reconcile(conn)
        result["db"] = str(db_path)
        conn.close()
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="MoneyTrail 演示数据生成器")
    parser.add_argument("--out", default=str(config.DATA_DIR), help="CSV 输出目录")
    parser.add_argument("--db", default=str(config.DB_PATH), help="SQLite 文件路径")
    parser.add_argument("--csv-only", action="store_true", help="只写 CSV，不初始化数据库")
    args = parser.parse_args(argv)

    result = generate(Path(args.out), Path(args.db), csv_only=args.csv_only)
    counts = json.loads((Path(args.out) / "ground_truth.json").read_text(encoding="utf-8"))["counts"]
    print(f"MoneyTrail 演示数据已生成（seed={SEED}）")
    print(f"  CSV 目录 : {result['csv_dir']}")
    print(f"  orders {counts['orders']} / refunds {counts['refunds']} / "
          f"settlements {counts['settlements']}")
    if result["reconcile"]:
        rec = result["reconcile"]
        print(f"  数据库   : {result['db']}")
        print(f"  对账完成 : 工单 {rec['total']} 笔，"
              f"缺口合计 {rec['gap_fen']} 分，耗时 {rec['duration_ms']} ms")
        for label in config.LABELS:
            print(f"    - {label:<16} {rec['counts'][label]:>2} 笔")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main())
