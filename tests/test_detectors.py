"""core/detectors.py：四条规则各一个命中用例 + 一个不该命中的反例。"""

import unittest
from unittest import mock

import config
from core import detectors, models, money

from .support import REFERENCE_TIME, TempDBTest, add_order, add_refund, add_settlement


# ---------------------------------------------------------------- 规则一


class NotSettledRuleTest(TempDBTest):
    """规则一 · 已支付未到账：结算表查无订单号。"""

    def test_hit_when_settlement_missing(self):
        add_order(self.conn, "MT-900", channel="wechat",
                  paid_at="2026-09-19 09:00:00", amount_fen=10000)
        add_refund(self.conn, "RF-900", "MT-900",
                   refunded_at="2026-09-19 18:00:00", amount_fen=1000)

        findings = detectors.detect_not_settled(self.conn, REFERENCE_TIME)

        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding["label_code"], "NOT_SETTLED")
        self.assertEqual(finding["anomaly_id"], "AN-NS-MT-900")
        self.assertEqual(finding["severity"], "high")
        self.assertEqual(finding["expected_fen"], 10000 - 60 - 1000)  # 8940
        self.assertEqual(finding["actual_fen"], 0)
        self.assertEqual(finding["diff_fen"], -8940)
        self.assertEqual(finding["gap_fen"], 8940)
        self.assertEqual(finding["extra"]["agreed_fee_fen"], 60)
        self.assertEqual(finding["extra"]["refund_fen"], 1000)
        self.assertEqual(finding["extra"]["age"], "已支付 3 天 1 小时")
        self.assertIn("未到账", finding["reason"])
        self.assertTrue(finding["steps"])

    def test_miss_when_settlement_exists(self):
        add_order(self.conn, "MT-901", channel="wechat", amount_fen=10000)
        add_settlement(self.conn, "ST-901", "MT-901", gross_fen=10000, fee_fen=60)

        self.assertEqual(detectors.detect_not_settled(self.conn, REFERENCE_TIME), [])

    def test_miss_when_orders_table_is_empty(self):
        add_settlement(self.conn, "ST-902", "MT-902", gross_fen=10000, fee_fen=60)
        self.assertEqual(detectors.detect_not_settled(self.conn, REFERENCE_TIME), [])

    def test_expected_amount_follows_rate_table(self):
        """约定手续费取自 channel_rates，改费率会改变应收净额。"""
        add_order(self.conn, "MT-903", channel="meituan", amount_fen=38000)
        models.seed_rates(self.conn, {"meituan": 250})  # 2.5%

        findings = detectors.detect_not_settled(self.conn, REFERENCE_TIME)

        self.assertEqual(findings[0]["extra"]["agreed_fee_fen"], 950)
        self.assertEqual(findings[0]["gap_fen"], 38000 - 950)


# ---------------------------------------------------------------- 规则二


class DupRefundRuleTest(TempDBTest):
    """规则二 · 重复退款：同额 + 时间窗。"""

    def setUp(self):
        super().setUp()
        add_order(self.conn, "MT-910", channel="wechat",
                  paid_at="2026-09-19 17:20:19", amount_fen=6000)

    def test_hit_same_amount_inside_window(self):
        add_refund(self.conn, "RF-1", "MT-910",
                   refunded_at="2026-09-19 18:00:00", amount_fen=3000, reason="退一半")
        add_refund(self.conn, "RF-2", "MT-910",
                   refunded_at="2026-09-19 18:06:00", amount_fen=3000, reason="退一半")

        findings = detectors.detect_dup_refund(self.conn, REFERENCE_TIME)

        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding["label_code"], "DUP_REFUND")
        self.assertEqual(finding["anomaly_id"], "AN-DR-MT-910")
        self.assertEqual(finding["severity"], "high")
        self.assertEqual(finding["actual_fen"], 6000)  # 两笔退款合计
        self.assertEqual(finding["expected_fen"], 3000)  # 只应退一笔
        self.assertEqual(finding["diff_fen"], 3000)
        self.assertEqual(finding["gap_fen"], 3000)  # 多退的部分
        self.assertEqual(finding["extra"]["refund_ids"], ["RF-2"])
        self.assertEqual(finding["extra"]["refund_sum_fen"], 6000)
        self.assertIn("多退", finding["reason"])

    def test_miss_same_amount_outside_window(self):
        add_refund(self.conn, "RF-1", "MT-910",
                   refunded_at="2026-09-19 18:00:00", amount_fen=3000)
        add_refund(self.conn, "RF-2", "MT-910",
                   refunded_at="2026-09-19 18:20:00", amount_fen=3000)  # 超出 10 分钟窗

        self.assertEqual(detectors.detect_dup_refund(self.conn, REFERENCE_TIME), [])

    def test_miss_different_amount_inside_window(self):
        add_refund(self.conn, "RF-1", "MT-910",
                   refunded_at="2026-09-19 18:00:00", amount_fen=1000)
        add_refund(self.conn, "RF-2", "MT-910",
                   refunded_at="2026-09-19 18:03:00", amount_fen=2000)

        self.assertEqual(detectors.detect_dup_refund(self.conn, REFERENCE_TIME), [])

    def test_miss_single_refund(self):
        add_refund(self.conn, "RF-1", "MT-910", amount_fen=3000)
        self.assertEqual(detectors.detect_dup_refund(self.conn, REFERENCE_TIME), [])


# ---------------------------------------------------------------- 规则三


class AmountMismatchRuleTest(TempDBTest):
    """规则三 · 到账金额不符：net ≠ gross − fee − Σrefunds。"""

    def test_hit_when_net_is_short(self):
        add_order(self.conn, "MT-920", channel="wechat", amount_fen=10000)
        add_settlement(self.conn, "ST-920", "MT-920",
                       gross_fen=10000, fee_fen=60, net_fen=9800)

        findings = detectors.detect_amount_mismatch(self.conn, REFERENCE_TIME)

        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding["label_code"], "AMOUNT_MISMATCH")
        self.assertEqual(finding["anomaly_id"], "AN-AM-MT-920")
        self.assertEqual(finding["severity"], "medium")
        self.assertEqual(finding["expected_fen"], 9940)
        self.assertEqual(finding["actual_fen"], 9800)
        self.assertEqual(finding["diff_fen"], -140)
        self.assertEqual(finding["gap_fen"], 140)  # gap 取绝对值
        self.assertIn("少 ¥1.40", finding["reason"])  # 平台少付

    def test_hit_when_net_is_over(self):
        add_order(self.conn, "MT-921", channel="alipay", amount_fen=11100)
        add_settlement(self.conn, "ST-921", "MT-921", channel="alipay",
                       gross_fen=11100, fee_fen=61, net_fen=11127)  # 多付 88 分

        findings = detectors.detect_amount_mismatch(self.conn, REFERENCE_TIME)

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["expected_fen"], 11039)
        self.assertEqual(findings[0]["diff_fen"], 88)
        self.assertEqual(findings[0]["gap_fen"], 88)
        self.assertIn("多 ¥0.88", findings[0]["reason"])  # 平台多付

    def test_miss_when_net_matches_formula(self):
        add_order(self.conn, "MT-922", channel="wechat", amount_fen=10000)
        add_settlement(self.conn, "ST-922", "MT-922",
                       gross_fen=10000, fee_fen=60, refund_fen=0, net_fen=9940)

        self.assertEqual(detectors.detect_amount_mismatch(self.conn, REFERENCE_TIME), [])

    def test_miss_when_refunds_are_deducted(self):
        add_order(self.conn, "MT-923", channel="wechat", amount_fen=10000)
        add_refund(self.conn, "RF-923", "MT-923", amount_fen=1000)
        add_settlement(self.conn, "ST-923", "MT-923",
                       gross_fen=10000, fee_fen=60, refund_fen=1000, net_fen=8940)

        self.assertEqual(detectors.detect_amount_mismatch(self.conn, REFERENCE_TIME), [])


# ---------------------------------------------------------------- 规则四


class FeeAnomalyRuleTest(TempDBTest):
    """规则四 · 手续费异常：|实际费率 − 约定费率| > 约定费率 × 10%。"""

    def test_hit_when_fee_rate_exceeds_tolerance(self):
        add_order(self.conn, "MT-930", channel="meituan", amount_fen=38000)
        add_settlement(self.conn, "ST-930", "MT-930", channel="meituan",
                       gross_fen=38000, fee_fen=950, net_fen=37050)  # 2.50% vs 约定 2.00%

        findings = detectors.detect_fee_anomaly(self.conn, REFERENCE_TIME)

        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding["label_code"], "FEE_ANOMALY")
        self.assertEqual(finding["anomaly_id"], "AN-FA-MT-930")
        self.assertEqual(finding["severity"], "low")
        self.assertEqual(finding["expected_fen"], 760)  # 约定 2%
        self.assertEqual(finding["actual_fen"], 950)
        self.assertEqual(finding["diff_fen"], 190)
        self.assertEqual(finding["gap_fen"], 190)  # 多收的手续费
        self.assertEqual(finding["extra"]["agreed_fee_fen"], 760)
        self.assertEqual(finding["extra"]["channel"], "meituan")
        self.assertIn("多收", finding["reason"])

    def test_miss_at_agreed_rate(self):
        add_order(self.conn, "MT-931", channel="meituan", amount_fen=38000)
        add_settlement(self.conn, "ST-931", "MT-931", channel="meituan",
                       gross_fen=38000, fee_fen=760, net_fen=37240)

        self.assertEqual(detectors.detect_fee_anomaly(self.conn, REFERENCE_TIME), [])

    def test_miss_just_inside_tolerance(self):
        add_order(self.conn, "MT-932", channel="meituan", amount_fen=38000)
        add_settlement(self.conn, "ST-932", "MT-932", channel="meituan",
                       gross_fen=38000, fee_fen=830, net_fen=37170)  # 2.18%，未超 2.2%

        self.assertEqual(detectors.detect_fee_anomaly(self.conn, REFERENCE_TIME), [])

    def test_miss_when_channel_rate_table_is_empty(self):
        """已知限制：规则四的 SQL 依赖 channel_rates 的 JOIN，缺费率配置时静默不产单。"""
        add_order(self.conn, "MT-933", channel="meituan", amount_fen=38000)
        add_settlement(self.conn, "ST-933", "MT-933", channel="meituan",
                       gross_fen=38000, fee_fen=950, net_fen=37050)
        self.conn.execute("DELETE FROM channel_rates")
        self.conn.commit()

        self.assertEqual(detectors.detect_fee_anomaly(self.conn, REFERENCE_TIME), [])


# ---------------------------------------------------------------- 公共工具


class DetectorHelpersTest(TempDBTest):
    def test_executable_sql_drops_comments_and_semicolon(self):
        for label_code in config.LABELS:
            sql = detectors.executable_sql(label_code)
            self.assertTrue(sql)
            self.assertNotIn("--", sql)
            self.assertFalse(sql.endswith(";"))
            self.conn.execute(sql).fetchall()  # 取出后必须能直接执行

    def test_anomaly_id_is_deterministic(self):
        first = detectors.anomaly_id("DUP_REFUND", "MT-001")
        self.assertEqual(first, "AN-DR-MT-001")
        self.assertEqual(first, detectors.anomaly_id("DUP_REFUND", "MT-001"))
        self.assertEqual(detectors.anomaly_id("FEE_ANOMALY", "MT-001"), "AN-FA-MT-001")

    def test_parse_time(self):
        self.assertIsNone(detectors.parse_time(""))
        self.assertIsNone(detectors.parse_time("2026-09-19"))
        self.assertEqual(detectors.parse_time("2026-09-19 09:06:12").year, 2026)

    def test_human_age(self):
        self.assertEqual(
            detectors.human_age("2026-09-22 10:00:00", "2026-09-19 08:00:00"),
            "已支付 3 天 2 小时")
        self.assertEqual(
            detectors.human_age("2026-09-22 10:00:00", "2026-09-21 08:00:00"),
            "已支付 1 天 2 小时")
        self.assertEqual(
            detectors.human_age("2026-09-19 10:47:00", "2026-09-19 10:15:00"),
            "已支付 32 分钟")
        self.assertEqual(
            detectors.human_age("2026-09-19 10:00:00", "2026-09-19 10:00:00"),
            "已支付 1 分钟")
        self.assertEqual(  # 未来时间不产生负数
            detectors.human_age("2026-09-19 10:00:00", "2026-09-19 12:00:00"),
            "已支付 1 分钟")
        self.assertEqual(detectors.human_age("", "2026-09-19 12:00:00"), "")

    def test_agreed_fee_falls_back_to_config_defaults(self):
        self.conn.execute("DELETE FROM channel_rates")
        self.conn.commit()
        # 库里没有费率时退回 config.DEFAULT_RATE_BP
        self.assertEqual(detectors.agreed_fee(self.conn, "wechat", 10000), 60)
        self.assertEqual(detectors.agreed_fee(self.conn, "unknown", 10000), 0)

    def test_fen_display(self):
        # 金额展示统一走 core.money，detectors 只做检测不再自带格式化函数
        self.assertEqual(money.fen_to_yuan(1423, thousands=False), "14.23")
        self.assertEqual(money.fen_to_yuan(-5, thousands=False), "-0.05")

    def test_detect_all_respects_rules_enabled(self):
        add_order(self.conn, "MT-940", channel="wechat", amount_fen=10000)
        findings = detectors.detect_all(self.conn, REFERENCE_TIME)
        self.assertEqual([item["label_code"] for item in findings], ["NOT_SETTLED"])

        with mock.patch.dict(config.RULES_ENABLED, {"NOT_SETTLED": False}):
            self.assertEqual(detectors.detect_all(self.conn, REFERENCE_TIME), [])
        self.assertEqual(len(detectors.detect_all(self.conn, REFERENCE_TIME)), 1)


if __name__ == "__main__":
    unittest.main()
