"""core/reconciler.py：对账幂等、工单状态继承、总览与资金链路。"""

import json
import unittest

import config
from core import importer, models, reconciler

from .support import DATA_DIR, TempDBTest


class FixtureReconcileTest(TempDBTest):
    """用 data/ 下的演示 CSV（只读）当夹具，跑完整对账。"""

    def setUp(self):
        super().setUp()
        for kind in importer.KINDS:
            content = (DATA_DIR / f"{kind}.csv").read_bytes()
            importer.import_csv(self.conn, kind, content, f"{kind}.csv")
        self.truth = json.loads((DATA_DIR / "ground_truth.json").read_text(encoding="utf-8"))

    def anomaly_ids(self):
        return [row["anomaly_id"] for row in models.list_anomalies(self.conn)]

    def test_reconcile_matches_ground_truth(self):
        result = reconciler.reconcile(self.conn)

        expected = {(item["label_code"], item["order_id"])
                    for item in self.truth["expected_anomalies"]}
        actual = {(row["label_code"], row["order_id"]) for row in models.list_anomalies(self.conn)}
        self.assertEqual(actual, expected)
        self.assertEqual(result["total"], len(expected))
        self.assertEqual(models.count(self.conn, "anomalies"), len(expected))

        counts = {label: 0 for label in config.LABELS}
        for label_code, _ in expected:
            counts[label_code] += 1
        self.assertEqual(result["counts"], counts)

        stats = reconciler.stats(self.conn)
        self.assertEqual(stats["order_count"], self.truth["counts"]["orders"])
        self.assertEqual(stats["settlement_count"], self.truth["counts"]["settlements"])
        self.assertEqual(stats["order_total_fen"], self.truth["totals"]["order_total_fen"])
        self.assertEqual(stats["received_fen"], self.truth["totals"]["received_fen"])
        self.assertEqual(stats["fee_fen"], self.truth["totals"]["fee_fen"])
        self.assertEqual(stats["refund_fen"], self.truth["totals"]["refund_fen"])
        self.assertEqual(result["gap_fen"], stats["all_gap_fen"])

    def test_reconcile_twice_keeps_work_order_count(self):
        first = reconciler.reconcile(self.conn)
        ids_first = self.anomaly_ids()
        count_first = models.count(self.conn, "anomalies")

        second = reconciler.reconcile(self.conn)
        ids_second = self.anomaly_ids()

        self.assertEqual(models.count(self.conn, "anomalies"), count_first)
        self.assertEqual(ids_second, ids_first)  # 工单号确定性生成，顺序也稳定
        self.assertEqual(len(ids_first), len(set(ids_first)))  # 无重复工单
        self.assertEqual(second["total"], first["total"])
        self.assertEqual(second["gap_fen"], first["gap_fen"])
        self.assertEqual(second["counts"], first["counts"])
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["carried"], second["total"])
        self.assertEqual(first["carried"], 0)
        self.assertEqual(first["created"], first["total"])

    def test_stats_counts_by_severity_matches_table(self):
        """总览卡片的「高/中/低」必须来自库里的 severity，而不是视图猜的 label 映射。"""
        reconciler.reconcile(self.conn)
        stats = reconciler.stats(self.conn)

        rows = {row["severity"]: row["n"] for row in self.conn.execute(
            "SELECT severity, COUNT(*) AS n FROM anomalies GROUP BY severity")}
        for level in config.SEVERITY_TEXT:
            self.assertEqual(stats["by_severity"][level], rows.get(level, 0), level)
        self.assertEqual(sum(stats["by_severity"].values()), stats["all_count"])
        # 档位表本身也不能和 config.SEVERITY 脱节
        for label, level in config.SEVERITY.items():
            self.assertIn(level, stats["by_severity"], label)

    def test_reconcile_keeps_status_note_and_first_seen(self):
        reconciler.reconcile(self.conn)
        target = models.list_anomalies(self.conn)[0]
        self.assertTrue(reconciler.set_status(self.conn, target["anomaly_id"],
                                             "resolved", "已联系渠道补结算"))
        before = models.get_anomaly(self.conn, target["anomaly_id"])

        result = reconciler.reconcile(self.conn)

        after = models.get_anomaly(self.conn, target["anomaly_id"])
        self.assertEqual(after["status"], "resolved")
        self.assertEqual(after["note"], "已联系渠道补结算")
        self.assertEqual(after["first_seen_at"], before["first_seen_at"])
        self.assertEqual(after["severity"], before["severity"])
        self.assertEqual(after["expected_fen"], before["expected_fen"])
        self.assertEqual(after["gap_fen"], before["gap_fen"])
        self.assertEqual(result["carried"], result["total"])

    def test_reconcile_drops_tickets_that_no_longer_apply(self):
        self.conn.execute(
            "INSERT INTO anomalies (anomaly_id, order_id, label_code, severity, evidence_json,"
            " first_seen_at, updated_at) VALUES"
            " ('AN-XX-STALE', 'MT-NOT-EXIST', 'DUP_REFUND', 'high', '{}',"
            "  '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        )
        self.conn.commit()

        result = reconciler.reconcile(self.conn)

        self.assertIsNone(models.get_anomaly(self.conn, "AN-XX-STALE"))
        self.assertEqual(models.count(self.conn, "anomalies"), result["total"])

    def test_stats_open_count_tracks_status_change(self):
        reconciler.reconcile(self.conn)
        stats = reconciler.stats(self.conn)
        self.assertEqual(stats["open_count"], stats["all_count"])
        self.assertEqual(stats["open_gap_fen"], stats["all_gap_fen"])

        target = models.list_anomalies(self.conn)[0]
        self.assertTrue(reconciler.set_status(self.conn, target["anomaly_id"], "ignored"))

        stats = reconciler.stats(self.conn)
        self.assertEqual(stats["open_count"], stats["all_count"] - 1)
        self.assertEqual(stats["open_gap_fen"], stats["all_gap_fen"] - abs(target["gap_fen"]))

    def test_evidence_json_is_stored_and_readable(self):
        reconciler.reconcile(self.conn)
        row = models.get_anomaly(self.conn, "AN-NS-MT20260919-004")
        self.assertIsNotNone(row)

        evidence = json.loads(row["evidence_json"])
        self.assertEqual(evidence["label_code"], "NOT_SETTLED")
        self.assertEqual(evidence["rule"], config.RULE_NAME["NOT_SETTLED"])
        self.assertTrue(evidence["steps"])
        self.assertEqual(evidence["amounts"]["gap"], "¥230.30")
        self.assertEqual(evidence["raw_rows"][0]["source"], "orders")
        self.assertIn("MT20260919-004", evidence["raw_rows"][0]["text"])

    def test_build_trail_marks_settlement_break(self):
        reconciler.reconcile(self.conn)
        trail = reconciler.build_trail(self.conn, "MT20260919-004")

        self.assertEqual(trail["order"]["order_id"], "MT20260919-004")
        self.assertEqual(len(trail["stages"]), 5)
        self.assertEqual([stage["key"] for stage in trail["stages"]],
                         ["pay", "accept", "refund", "fee", "settle"])
        self.assertEqual(trail["broken_index"], 4)  # 断在「结算到账」这一段
        self.assertEqual(trail["stages"][4]["state"], "broken")
        self.assertEqual(trail["main"]["label_code"], "NOT_SETTLED")
        self.assertEqual(trail["gap_fen"], 23030)  # 23500 − 470(美团 2%) − 0
        self.assertTrue(trail["gap_text"].startswith("断链"))
        self.assertEqual(trail["refund_sum_fen"], 0)

    def test_build_trail_returns_empty_for_unknown_order(self):
        self.assertEqual(reconciler.build_trail(self.conn, "MT-NOT-EXIST"), {})

    def test_set_status_rejects_unknown_status_and_order(self):
        reconciler.reconcile(self.conn)
        target = models.list_anomalies(self.conn)[0]
        self.assertFalse(reconciler.set_status(self.conn, target["anomaly_id"], "closed"))
        self.assertFalse(reconciler.set_status(self.conn, "AN-XX-NOPE", "resolved"))
        self.assertEqual(models.get_anomaly(self.conn, target["anomaly_id"])["status"], "pending")


class ReconcileOnEmptyDBTest(TempDBTest):
    """空库对账：不报错、不产单、不写脏数据。"""

    def test_reconcile_on_empty_db(self):
        result = reconciler.reconcile(self.conn)

        self.assertEqual(result["total"], 0)
        self.assertEqual(result["gap_fen"], 0)
        self.assertEqual(result["counts"], {label: 0 for label in config.LABELS})
        self.assertEqual(result["reference_time"], "")
        self.assertEqual(models.count(self.conn, "anomalies"), 0)

    def test_reconcile_twice_on_empty_db_stays_empty(self):
        reconciler.reconcile(self.conn)
        reconciler.reconcile(self.conn)
        self.assertEqual(models.count(self.conn, "anomalies"), 0)


if __name__ == "__main__":
    unittest.main()
