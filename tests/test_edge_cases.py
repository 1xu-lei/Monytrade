"""回归用例：审查中发现的边界缺陷，逐条钉死。

覆盖三类问题：
1. 一单多结算：工单不重复、不崩溃，且证据取的是被判定那一条结算；
2. 退款覆盖全部应收：不该再开「未到账」工单；
3. 行号与输入清洗：空行不影响报错行号，URL/响应头里的订单号被清洗。
"""

import unittest

import app as app_module
from core import detectors, importer, models, reconciler
from tests import support


class MultiSettlementTest(support.TempDBTest):
    """一单两条结算：工单按订单去重，数值取缺口最大的那条。"""

    def setUp(self):
        super().setUp()
        support.add_order(self.conn, "MT20260921-001", amount_fen=10000,
                          paid_at="2026-09-21 09:00:00")
        # 第一条：净额少 100 分；第二条：净额少 400 分（更严重）
        support.add_settlement(self.conn, "ST0000001", "MT20260921-001",
                               settled_at="2026-09-22 10:00:00",
                               gross_fen=10000, fee_fen=60, net_fen=9840)
        support.add_settlement(self.conn, "ST0000002", "MT20260921-001",
                               settled_at="2026-09-22 11:00:00",
                               gross_fen=10000, fee_fen=60, net_fen=9540)

    def test_reconcile_does_not_crash_and_yields_one_ticket(self):
        result = reconciler.reconcile(self.conn)
        self.assertEqual(result["counts"]["AMOUNT_MISMATCH"], 1)
        rows = self.conn.execute(
            "SELECT * FROM anomalies WHERE label_code = 'AMOUNT_MISMATCH'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["gap_fen"], 400)

    def test_evidence_uses_the_matched_settlement(self):
        reconciler.reconcile(self.conn)
        row = self.conn.execute(
            "SELECT * FROM anomalies WHERE label_code = 'AMOUNT_MISMATCH'").fetchone()
        evidence = reconciler.evidence_of(row)
        self.assertEqual(evidence["extra"]["settlement_id"], "ST0000002")
        self.assertEqual(evidence["amounts"]["actual_fen"], 9540)
        self.assertTrue(any("ST0000002" in line for line in
                            (item["text"] for item in evidence["raw_rows"])))

    def test_fee_anomaly_uses_matched_settlement(self):
        support.add_order(self.conn, "MT20260921-002", channel="wechat",
                          amount_fen=10000, paid_at="2026-09-21 12:00:00")
        support.add_settlement(self.conn, "ST0000003", "MT20260921-002",
                               settled_at="2026-09-22 10:00:00",
                               gross_fen=10000, fee_fen=60, net_fen=9940)
        support.add_settlement(self.conn, "ST0000004", "MT20260921-002",
                               settled_at="2026-09-22 11:00:00",
                               gross_fen=20000, fee_fen=400, net_fen=19600)
        rows = detectors.detect_fee_anomaly(self.conn, support.REFERENCE_TIME)
        self.assertEqual(len(rows), 1)
        # 400 分 / 200 元 = 2% > 0.6% × 1.1，取这条；缺口 = 400 − 120 = 280
        self.assertEqual(rows[0]["extra"]["settlement_id"], "ST0000004")
        self.assertEqual(rows[0]["gap_fen"], 280)

    def test_trail_mentions_extra_settlements(self):
        trail = reconciler.build_trail(self.conn, "MT20260921-001")
        self.assertEqual(trail["stages"][4]["ref"], "ST0000001")
        self.assertIn("共 1 笔结算", trail["stages"][4]["note"])


class RefundCoversAmountTest(support.TempDBTest):
    """退款已覆盖全部应收：平台不欠钱，不该开「未到账」工单。"""

    def test_no_not_settled_when_fully_refunded(self):
        support.add_order(self.conn, "MT20260921-009", amount_fen=10000,
                          paid_at="2026-09-21 09:00:00")
        support.add_refund(self.conn, "RF0001", "MT20260921-009", amount_fen=10000,
                           refunded_at="2026-09-21 10:00:00")
        self.assertEqual(detectors.detect_not_settled(self.conn, support.REFERENCE_TIME), [])

    def test_gap_never_negative(self):
        support.add_order(self.conn, "MT20260921-010", amount_fen=10000,
                          paid_at="2026-09-21 09:00:00")
        support.add_refund(self.conn, "RF0002", "MT20260921-010", amount_fen=9000,
                           refunded_at="2026-09-21 10:00:00")
        rows = detectors.detect_not_settled(self.conn, support.REFERENCE_TIME)
        self.assertEqual(len(rows), 1)
        self.assertGreater(rows[0]["gap_fen"], 0)
        self.assertEqual(rows[0]["gap_fen"], 10000 - 60 - 9000)

    def test_stats_ignores_nothing_negative(self):
        support.add_order(self.conn, "MT20260921-011", amount_fen=5000,
                          paid_at="2026-09-21 09:00:00")
        support.add_refund(self.conn, "RF0003", "MT20260921-011", amount_fen=5000,
                           refunded_at="2026-09-21 10:00:00")
        reconciler.reconcile(self.conn)
        self.assertEqual(reconciler.stats(self.conn)["open_gap_fen"], 0)


class RowNumberTest(support.TempDBTest):

    def test_blank_lines_do_not_shift_reported_row(self):
        text = ("order_id,channel,paid_at,amount_fen,status\n"
                "MT20260919-001,wechat,2026-09-19 10:00:00,1000,PAID\n"
                "\n"
                "MT20260919-002,wechat,2026-09-19 11:00:00,abc,PAID\n")
        with self.assertRaises(importer.ImportValidationError) as ctx:
            importer.import_csv(self.conn, "orders", text, "x.csv", record_batch=False)
        self.assertEqual(ctx.exception.errors[0]["row"], 4)

    def test_duplicate_key_reports_real_line(self):
        support.add_order(self.conn, "MT20260919-001")
        text = ("order_id,channel,paid_at,amount_fen,status\n"
                "\n"
                "MT20260919-001,wechat,2026-09-19 10:00:00,1000,PAID\n")
        with self.assertRaises(importer.ImportValidationError) as ctx:
            importer.csv_rows = None
            importer.import_csv(self.conn, "orders", text, "y.csv", record_batch=False)
        self.assertEqual(ctx.exception.errors[0]["row"], 3)


class InputSanitisingTest(unittest.TestCase):
    """订单号会进 URL / 响应头 / 页面，必须过滤。"""

    def test_export_filename_is_sanitised(self):
        self.assertEqual(app_module._safe_name("MT20260921-001"), "MT20260921-001")
        cleaned = app_module._safe_name('a"\r\nSet-Cookie: x=1')
        self.assertNotIn('"', cleaned)
        self.assertNotIn("\r", cleaned)
        self.assertNotIn("\n", cleaned)
        self.assertNotIn("/", app_module._safe_name("../../etc/passwd"))

    def test_flash_parameter_is_escaped(self):
        escaped = app_module._first({"msg": ["<b>x</b>"]}, "msg")
        self.assertNotIn("<b>", escaped)


class TopOpenTest(support.TempDBTest):

    def test_no_pending_returns_empty_not_resolved(self):
        support.add_order(self.conn, "MT20260921-001", amount_fen=10000)
        reconciler.reconcile(self.conn)
        row = self.conn.execute("SELECT anomaly_id FROM anomalies").fetchone()
        reconciler.set_status(self.conn, row["anomaly_id"], "resolved")
        self.assertEqual(reconciler.top_open(self.conn), [])


class ThresholdSourceTest(support.TempDBTest):
    """规则四的约定费率必须与规则一同源（channel_rates），且阈值来自 config。"""

    def test_custom_rate_is_respected(self):
        models.seed_rates(self.conn, {"wechat": 200})  # 把微信费率改成 2%
        support.add_order(self.conn, "MT20260921-001", amount_fen=10000,
                          paid_at="2026-09-21 09:00:00")
        support.add_settlement(self.conn, "ST0000001", "MT20260921-001",
                               gross_fen=10000, fee_fen=60, net_fen=9940)
        rows = detectors.detect_fee_anomaly(self.conn, support.REFERENCE_TIME)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["expected_fen"], 200)

    def test_sql_uses_configured_tolerance(self):
        import config
        self.assertIn(f"r.rate * {config.FEE_TOLERANCE_RATIO}",
                      detectors.RULE_SQL["FEE_ANOMALY"])


if __name__ == "__main__":
    unittest.main()
