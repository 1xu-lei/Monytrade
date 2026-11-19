"""MoneyTrail 自检脚本 · 10 条断言。

    python check.py            # 退出码 0 = 全绿，1 = 有 FAIL
    python check.py --verbose  # 额外打印检出率 / 误报率对照

设计约束：全部在临时目录里跑，不碰 data/ 与 moneytrail.db，
因此可以随时执行、可被夜间 Agent 反复调用而不会污染演示数据。
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import config
import gen_data
from core import assistant, detectors, importer, models, reconciler

PASS = "[PASS]"
FAIL = "[FAIL]"

MONEY_COLUMNS = (
    ("orders", ("amount_fen",)),
    ("refunds", ("amount_fen",)),
    ("settlements", ("gross_fen", "fee_fen", "refund_fen", "net_fen")),
    ("anomalies", ("expected_fen", "actual_fen", "diff_fen", "gap_fen")),
)


class Context:
    """各条断言共享的临时环境。"""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="moneytrail-check-"))
        self.csv_dir = self.tmp / "data"
        self.db_path = self.tmp / "check.db"
        self.conn = None
        self.truth = {}


def expected_of(ctx: Context, label_code: str) -> set:
    return {item["order_id"] for item in ctx.truth["expected_anomalies"]
            if item["label_code"] == label_code}


# ---------------------------------------------------------------- 断言

def check_01_data(ctx: Context) -> str:
    result = gen_data.generate(ctx.csv_dir, ctx.db_path, csv_only=False)
    ctx.conn = models.connect_ready(ctx.db_path)
    ctx.truth = json.loads((ctx.csv_dir / "ground_truth.json").read_text(encoding="utf-8"))
    counts = ctx.truth["counts"]
    actual = {kind: models.count(ctx.conn, kind) for kind in importer.KINDS}
    for kind in importer.KINDS:
        assert actual[kind] == counts[kind], f"{kind} 生成 {actual[kind]} 行，标注 {counts[kind]} 行"
    assert result["reconcile"]["total"] > 0, "对账没有产出任何工单"
    return (f"数据生成：orders {actual['orders']} / refunds {actual['refunds']} / "
            f"settlements {actual['settlements']}")


def check_02_validation(ctx: Context) -> str:
    missing_column = ("order_id,channel,paid_at,amount_fen\n"
                      "MT20260101-001,wechat,2026-01-01 10:00:00,100\n")
    try:
        importer.import_csv(ctx.conn, "orders", missing_column, "broken.csv", record_batch=False)
    except importer.ImportValidationError as exc:
        assert exc.errors, "抛异常但没有逐行错误"
        assert exc.errors[0]["row"] == 1, f"报错行号是 {exc.errors[0]['row']}，应为 1（表头行）"
        assert "status" in exc.errors[0]["message"], exc.errors[0]["message"]
    else:
        raise AssertionError("缺列文件没有被拒绝")

    negative = ("order_id,channel,paid_at,amount_fen,status\n"
                "MT20260101-009,wechat,2026-01-01 10:00:00,-100,PAID\n")
    try:
        importer.import_csv(ctx.conn, "orders", negative, "negative.csv", record_batch=False)
    except importer.ImportValidationError as exc:
        assert exc.errors[0]["row"] == 2, f"负金额报错行号 {exc.errors[0]['row']}，应为 2"
    else:
        raise AssertionError("负金额没有被拒绝")

    total = models.count(ctx.conn, "orders")
    assert total == ctx.truth["counts"]["orders"], f"校验失败却写入了数据（orders {total} 行）"
    return "导入校验：缺列文件被拒，报出第 1 行；负金额报出第 2 行；库内行数未变"


def check_03_idempotent(ctx: Context) -> str:
    content = (ctx.csv_dir / "orders.csv").read_bytes()
    before = models.count(ctx.conn, "orders")
    result = importer.import_csv(ctx.conn, "orders", content, "orders.csv")
    after = models.count(ctx.conn, "orders")
    assert result["skipped"] is True, "同文件二次导入没有被哈希识别"
    assert before == after, f"行数从 {before} 变成 {after}"
    return f"导入幂等：同文件二次导入，行数不变（{after} 行，skipped=True）"


def check_04_not_settled(ctx: Context) -> str:
    rows = detectors.detect_not_settled(ctx.conn, models.reference_time(ctx.conn))
    got = {row["order_id"] for row in rows}
    assert got == expected_of(ctx, "NOT_SETTLED"), f"命中 {sorted(got)}"
    for row in rows:
        assert row["gap_fen"] > 0, "缺口应大于 0"
    return (f"规则一 NOT_SETTLED   命中 {len(rows)} 笔，"
            f"缺口合计 {sum(r['gap_fen'] for r in rows)} 分")


def check_05_dup_refund(ctx: Context) -> str:
    rows = detectors.detect_dup_refund(ctx.conn, models.reference_time(ctx.conn))
    got = {row["order_id"] for row in rows}
    assert got == expected_of(ctx, "DUP_REFUND"), f"命中 {sorted(got)}"
    for row in rows:
        assert row["gap_fen"] > 0, "重复退款金额应大于 0"
    return (f"规则二 DUP_REFUND    命中 {len(rows)} 笔，"
            f"多退合计 {sum(r['gap_fen'] for r in rows)} 分")


def check_06_amount_mismatch(ctx: Context) -> str:
    rows = detectors.detect_amount_mismatch(ctx.conn, models.reference_time(ctx.conn))
    got = {row["order_id"] for row in rows}
    assert got == expected_of(ctx, "AMOUNT_MISMATCH"), f"命中 {sorted(got)}"
    diffs = {row["order_id"]: row["diff_fen"] for row in rows}
    assert diffs.get("MT20260919-002") == -176, f"少付应为 -176 分，实际 {diffs}"
    assert diffs.get("MT20260921-006") == 88, f"多付应为 +88 分，实际 {diffs}"
    total = sum(abs(row["diff_fen"]) for row in rows)
    return (f"规则三 AMOUNT_MISMATCH 命中 {len(rows)} 笔，"
            f"差额合计 {total} 分（少付 176 / 多付 88）")


def check_07_fee_anomaly(ctx: Context) -> str:
    rows = detectors.detect_fee_anomaly(ctx.conn, models.reference_time(ctx.conn))
    got = {row["order_id"] for row in rows}
    assert got == expected_of(ctx, "FEE_ANOMALY"), f"命中 {sorted(got)}"
    return (f"规则四 FEE_ANOMALY   命中 {len(rows)} 笔，"
            f"多收合计 {sum(r['gap_fen'] for r in rows)} 分")


def check_08_persistence(ctx: Context) -> str:
    row = ctx.conn.execute(
        "SELECT anomaly_id FROM anomalies ORDER BY anomaly_id LIMIT 1").fetchone()
    assert row, "库里没有工单"
    anomaly_id = row["anomaly_id"]
    assert reconciler.set_status(ctx.conn, anomaly_id, "resolved"), "状态写入失败"
    ctx.conn.close()
    ctx.conn = models.connect_ready(ctx.db_path)
    again = models.get_anomaly(ctx.conn, anomaly_id)
    assert again["status"] == "resolved", f"重连后状态是 {again['status']}"
    reconciler.reconcile(ctx.conn)
    kept = models.get_anomaly(ctx.conn, anomaly_id)
    assert kept["status"] == "resolved", "重新对账后状态丢了（幂等性被破坏）"
    return f"状态持久化：写 resolved 后重连仍在，重新对账后仍继承（{anomaly_id}）"


def check_09_degraded(ctx: Context) -> str:
    env_keys = (config.BAIZE_API_KEY_ENV, config.BAIZE_NO_AUTH_ENV, config.BAIZE_BASE_URL_ENV)
    saved = {key: os.environ.get(key) for key in env_keys}

    def restore():
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    try:
        # 一、没有凭据也没有免鉴权：助手不可用，直接走模板
        os.environ.pop(config.BAIZE_API_KEY_ENV, None)
        os.environ[config.BAIZE_NO_AUTH_ENV] = "0"
        assert assistant.ai_status(ctx.conn)["available"] is False
        for question in ("今天哪几笔没到账",
                        "MT20260921-001 这笔为什么对不上",
                        "这个月手续费多收了多少"):
            result = assistant.ask(ctx.conn, question)
            assert result["answer"].strip(), f"{question} 没有回答"
            assert result["mode"] == "template", f"{question} 未降级：{result['mode']}"
            assert result["intent"]["intent"] in assistant.SUPPORTED, result["intent"]
            assert result["sources"], f"{question} 的回答没有引用订单号"
        refused = assistant.ask(ctx.conn, "今天天气怎么样")
        assert refused["mode"] == "refuse", "超范围问题没有被拒答"
        assert models.count(ctx.conn, "anomalies") > 0, "降级状态下工单丢了"

        # 二、端点连不上：可用性判断不依赖网络，但请求失败必须静默回落模板
        os.environ[config.BAIZE_BASE_URL_ENV] = "http://127.0.0.1:1/v1"
        os.environ[config.BAIZE_NO_AUTH_ENV] = "1"
        assert assistant.ai_status(ctx.conn)["available"] is True
        assert assistant._chat([{"role": "user", "content": "ping"}]) is None
        broken = assistant.ask(ctx.conn, "今天哪几笔没到账")
        assert broken["mode"] == "template", f"端点不可达时未回落：{broken['mode']}"
        assert broken["sources"], "端点不可达时丢了订单号引用"

        return ("降级：无凭据或端点不可达时 3 类问法全部回落模板答案、超范围问题拒答、"
                "对账结果完好")
    finally:
        restore()


def check_10_integers(ctx: Context) -> str:
    offenders, fields = [], 0
    for table, columns in MONEY_COLUMNS:
        for column in columns:
            fields += 1
            for row in ctx.conn.execute(
                    f"SELECT DISTINCT typeof({column}) AS t FROM {table}"):
                if row["t"] != "integer":
                    offenders.append(f"{table}.{column}={row['t']}")
    assert not offenders, "存在非整数金额：" + "、".join(offenders)
    return f"整数校验：库内 {fields} 个金额字段全部为 INTEGER，无浮点参与"


CHECKS = [
    ("01", "数据生成", check_01_data),
    ("02", "导入校验", check_02_validation),
    ("03", "导入幂等", check_03_idempotent),
    ("04", "规则一 NOT_SETTLED", check_04_not_settled),
    ("05", "规则二 DUP_REFUND", check_05_dup_refund),
    ("06", "规则三 AMOUNT_MISMATCH", check_06_amount_mismatch),
    ("07", "规则四 FEE_ANOMALY", check_07_fee_anomaly),
    ("08", "状态持久化", check_08_persistence),
    ("09", "降级", check_09_degraded),
    ("10", "整数校验", check_10_integers),
]


def detection_rate(ctx: Context) -> str:
    """检出率 / 误报率：把落库工单和 ground_truth 标注逐条对照。"""
    truth = {(item["label_code"], item["order_id"])
             for item in ctx.truth.get("expected_anomalies", [])}
    found = {(row["label_code"], row["order_id"])
             for row in ctx.conn.execute("SELECT label_code, order_id FROM anomalies")}
    hit = truth & found
    rate = len(hit) / len(truth) if truth else 0.0
    return (f"检出率 {len(hit)}/{len(truth)} = {rate:.1%}　"
            f"误报 {len(found - truth)} 条　漏检 {len(truth - found)} 条")


def main(argv=None) -> int:
    flags = list(sys.argv[1:] if argv is None else argv)
    verbose = False
    for flag in flags:
        if flag in ("--verbose", "-v"):
            verbose = True
        else:
            print(f"未知参数：{flag}", file=sys.stderr)
            print("用法：python check.py [--verbose]", file=sys.stderr)
            return 2

    ctx = Context()
    print(f"MoneyTrail 自检  v{config.VERSION}")
    print("─" * 46)
    passed, failed = 0, 0
    for number, label, func in CHECKS:
        try:
            detail = func(ctx)
            passed += 1
            print(f"{PASS}  {number}  {detail}")
        except AssertionError as exc:
            failed += 1
            print(f"{FAIL}  {number}  {label} · {exc}")
        except Exception as exc:  # 任何异常都算 FAIL，不中断后续断言
            failed += 1
            print(f"{FAIL}  {number}  {label} · {type(exc).__name__}: {exc}")
    if verbose and ctx.conn is not None:
        print("─" * 46)
        print("      " + detection_rate(ctx))
    if ctx.conn is not None:
        ctx.conn.close()
        ctx.conn = None
    print("─" * 46)
    print(f"{passed} / {passed + failed} PASS")
    shutil.rmtree(ctx.tmp, ignore_errors=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main())
