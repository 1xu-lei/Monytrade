"""core/importer.py：正常导入、逐行报错、主键重复、哈希去重。"""

import unittest

from core import importer, models

from .support import DATA_DIR, TempDBTest, csv_text


class ImportHappyPathTest(TempDBTest):
    def test_import_orders_normalizes_time_and_status(self):
        text = csv_text("orders", [
            {"order_id": "MT-001", "channel": "wechat", "paid_at": "2026-09-19 09:06:12",
             "amount_fen": "14065", "status": "PAID"},
            {"order_id": "MT-002", "channel": "alipay", "paid_at": "2026-09-19T09:41:33",
             "amount_fen": "12800", "status": "paid"},
        ])
        result = importer.import_csv(self.conn, "orders", text, "orders.csv")

        self.assertEqual(result["rows"], 2)
        self.assertFalse(result["skipped"])
        self.assertEqual(result["reason"], "")
        self.assertIsNotNone(result["batch_id"])
        self.assertEqual(models.count(self.conn, "orders"), 2)
        self.assertEqual(models.count(self.conn, "import_batches"), 1)

        row = models.get_order(self.conn, "MT-002")
        self.assertEqual(row["paid_at"], "2026-09-19 09:41:33")  # 'T' 分隔符被规范化
        self.assertEqual(row["status"], "PAID")  # 小写状态被大写化
        self.assertEqual(row["amount_fen"], 12800)  # 金额落库为 int
        self.assertEqual(row["batch_id"], result["batch_id"])

    def test_import_refunds_allows_empty_reason_and_zero_amount(self):
        text = csv_text("refunds", [
            {"refund_id": "RF0001", "order_id": "MT-001", "refunded_at": "2026-09-19 16:40:05",
             "amount_fen": "1000", "reason": "少送一件，退差额"},
            {"refund_id": "RF0002", "order_id": "MT-002", "refunded_at": "2026/09/19 17:00:00",
             "amount_fen": "0", "reason": ""},
        ])
        result = importer.import_csv(self.conn, "refunds", text, "refunds.csv")

        self.assertEqual(result["rows"], 2)
        self.assertEqual(models.count(self.conn, "refunds"), 2)
        refund = self.conn.execute(
            "SELECT * FROM refunds WHERE refund_id = 'RF0002'"
        ).fetchone()
        self.assertEqual(refund["reason"], "")
        self.assertEqual(refund["amount_fen"], 0)
        self.assertEqual(refund["refunded_at"], "2026-09-19 17:00:00")  # '/' 写法被规范化

    def test_import_data_fixture_counts(self):
        """data/ 下的演示 CSV 只读导入，行数应与 ground_truth 一致。"""
        for kind, expected in (("orders", 24), ("refunds", 9), ("settlements", 21)):
            content = (DATA_DIR / f"{kind}.csv").read_bytes()
            result = importer.import_csv(self.conn, kind, content, f"{kind}.csv")
            self.assertFalse(result["skipped"])
            self.assertEqual(result["rows"], expected)
            self.assertEqual(models.count(self.conn, kind), expected)
        self.assertEqual(models.count(self.conn, "import_batches"), 3)


class ImportValidationTest(TempDBTest):
    """校验失败必须带「行号 + 字段名」，且整体回滚。"""

    def valid_order(self, **overrides):
        row = {"order_id": "MT-001", "channel": "wechat", "paid_at": "2026-09-19 09:06:12",
               "amount_fen": "14065", "status": "PAID"}
        row.update(overrides)
        return row

    def assert_nothing_written(self):
        self.assertEqual(models.count(self.conn, "orders"), 0)
        self.assertEqual(models.count(self.conn, "refunds"), 0)
        self.assertEqual(models.count(self.conn, "import_batches"), 0)

    def test_missing_column_reports_header_row(self):
        text = csv_text("orders", [self.valid_order()],
                        header=["order_id", "channel", "paid_at", "amount_fen"])
        with self.assertRaises(importer.ImportValidationError) as ctx:
            importer.import_csv(self.conn, "orders", text, "orders.csv")

        errors = ctx.exception.errors
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["row"], 1)  # 表头就是第 1 行
        self.assertEqual(errors[0]["field"], "status")
        self.assertIn("缺少必需列", errors[0]["message"])
        self.assertIn("第 1 行", ctx.exception.summary())
        self.assert_nothing_written()

    def test_money_type_error_reports_data_row(self):
        rows = [self.valid_order(), self.valid_order(order_id="MT-002", amount_fen="14.65")]
        with self.assertRaises(importer.ImportValidationError) as ctx:
            importer.import_csv(self.conn, "orders", csv_text("orders", rows), "orders.csv")

        errors = ctx.exception.errors
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["row"], 3)  # 表头 1 行 + 第 1 条数据 → 第 3 行
        self.assertEqual(errors[0]["field"], "amount_fen")
        self.assertIn("必须是整数分", errors[0]["message"])
        self.assert_nothing_written()

    def test_negative_money_rejected(self):
        text = csv_text("orders", [self.valid_order(amount_fen="-100")])
        with self.assertRaises(importer.ImportValidationError) as ctx:
            importer.import_csv(self.conn, "orders", text, "orders.csv")

        errors = ctx.exception.errors
        self.assertEqual((errors[0]["row"], errors[0]["field"]), (2, "amount_fen"))
        self.assertIn("不能为负数", errors[0]["message"])

    def test_unparsable_time_rejected(self):
        text = csv_text("orders", [self.valid_order(paid_at="19/09/2026 09:06")])
        with self.assertRaises(importer.ImportValidationError) as ctx:
            importer.import_csv(self.conn, "orders", text, "orders.csv")

        errors = ctx.exception.errors
        self.assertEqual((errors[0]["row"], errors[0]["field"]), (2, "paid_at"))
        self.assertIn("时间格式无法识别", errors[0]["message"])

    def test_unknown_channel_rejected(self):
        text = csv_text("orders", [self.valid_order(channel="paypal")])
        with self.assertRaises(importer.ImportValidationError) as ctx:
            importer.import_csv(self.conn, "orders", text, "orders.csv")

        errors = ctx.exception.errors
        self.assertEqual((errors[0]["row"], errors[0]["field"]), (2, "channel"))
        self.assertIn("未知渠道", errors[0]["message"])

    def test_duplicate_order_id_inside_file(self):
        rows = [
            self.valid_order(),
            self.valid_order(order_id="MT-002"),
            self.valid_order(),  # 与第 2 行（第 1 条数据）订单号重复
        ]
        with self.assertRaises(importer.ImportValidationError) as ctx:
            importer.import_csv(self.conn, "orders", csv_text("orders", rows), "orders.csv")

        errors = ctx.exception.errors
        self.assertEqual(len(errors), 1)
        self.assertEqual((errors[0]["row"], errors[0]["field"]), (4, "order_id"))
        self.assertIn("文件内主键重复", errors[0]["message"])
        self.assert_nothing_written()

    def test_duplicate_refund_triplet_inside_file(self):
        rows = [
            {"refund_id": "RF1", "order_id": "MT-001", "refunded_at": "2026-09-19 18:00:00",
             "amount_fen": "3000", "reason": "退一半"},
            {"refund_id": "RF2", "order_id": "MT-001", "refunded_at": "2026-09-19 18:00:00",
             "amount_fen": "3000", "reason": "退一半"},
        ]
        with self.assertRaises(importer.ImportValidationError) as ctx:
            importer.import_csv(self.conn, "refunds", csv_text("refunds", rows), "refunds.csv")

        errors = ctx.exception.errors
        self.assertEqual((errors[0]["row"], errors[0]["field"]), (3, "refund_id"))
        self.assertIn("重复", errors[0]["message"])

    def test_row_number_matches_physical_line_after_blank_line(self):
        header = ",".join(importer.COLUMNS["orders"])
        text = "\n".join([
            header,
            "MT-001,wechat,2026-09-19 09:06:12,14065,PAID",
            "",  # 空行不报错，但行号必须按物理行算
            "MT-002,wechat,2026-09-19 10:00:00,abc,PAID",
        ]) + "\n"
        with self.assertRaises(importer.ImportValidationError) as ctx:
            importer.import_csv(self.conn, "orders", text, "orders.csv")

        errors = ctx.exception.errors
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["row"], 4)
        self.assert_nothing_written()

    def test_partial_failure_rolls_back_cleanly(self):
        rows = [self.valid_order(), self.valid_order(order_id="MT-002", amount_fen="oops")]
        with self.assertRaises(importer.ImportValidationError):
            importer.import_csv(self.conn, "orders", csv_text("orders", rows), "orders.csv")
        self.assert_nothing_written()
        self.assertIsNone(models.get_order(self.conn, "MT-001"))

    def test_empty_file_and_header_only(self):
        with self.assertRaises(importer.ImportValidationError) as ctx:
            importer.import_csv(self.conn, "orders", "", "orders.csv")
        self.assertEqual(ctx.exception.errors[0]["row"], 1)
        self.assertIn("缺少表头", ctx.exception.errors[0]["message"])

        with self.assertRaises(importer.ImportValidationError) as ctx:
            importer.import_csv(self.conn, "orders", csv_text("orders", []), "orders.csv")
        self.assertEqual(ctx.exception.errors[0]["row"], 2)
        self.assertIn("只有表头", ctx.exception.errors[0]["message"])

    def test_unknown_kind_rejected(self):
        with self.assertRaises(importer.ImportValidationError) as ctx:
            importer.import_csv(self.conn, "invoices", "order_id\nMT-001\n", "invoices.csv")
        self.assertEqual(ctx.exception.errors[0]["field"], "kind")
        self.assertIn("未知的台账类型", ctx.exception.errors[0]["message"])


class ImportDedupTest(TempDBTest):
    """同文件重复导入按 SHA-256 跳过；跨文件的同订单号按主键查重报错。"""

    def orders_text(self, second_id="MT-002"):
        return csv_text("orders", [
            {"order_id": "MT-001", "channel": "wechat", "paid_at": "2026-09-19 09:06:12",
             "amount_fen": "14065", "status": "PAID"},
            {"order_id": second_id, "channel": "alipay", "paid_at": "2026-09-19 09:41:33",
             "amount_fen": "12800", "status": "PAID"},
        ])

    def test_same_file_second_import_skipped_by_hash(self):
        text = self.orders_text()
        first = importer.import_csv(self.conn, "orders", text, "orders.csv")
        second = importer.import_csv(self.conn, "orders", text, "orders.csv")

        self.assertFalse(first["skipped"])
        self.assertTrue(second["skipped"])
        self.assertEqual(second["rows"], 0)
        self.assertEqual(second["hash"], first["hash"])
        self.assertEqual(second["batch_id"], first["batch_id"])  # 复用原批次
        self.assertEqual(second["reason"], "同一文件已导入过（哈希一致）")
        self.assertEqual(models.count(self.conn, "orders"), 2)
        self.assertEqual(models.count(self.conn, "import_batches"), 1)

    def test_bom_and_plain_text_share_hash(self):
        text = self.orders_text()
        importer.import_csv(self.conn, "orders", b"\xef\xbb\xbf" + text.encode("utf-8"),
                            "with_bom.csv")
        second = importer.import_csv(self.conn, "orders", text, "plain.csv")
        self.assertTrue(second["skipped"])  # BOM 在解码阶段就被剥掉，哈希一致

    def test_duplicate_batch_is_per_kind(self):
        importer.import_csv(self.conn, "orders", self.orders_text(), "orders.csv")
        other = importer.import_csv(
            self.conn, "refunds",
            csv_text("refunds", [{"refund_id": "RF1", "order_id": "MT-001",
                                  "refunded_at": "2026-09-19 18:00:00",
                                  "amount_fen": "1000", "reason": ""}]),
            "refunds.csv")
        self.assertFalse(other["skipped"])
        self.assertEqual(models.count(self.conn, "import_batches"), 2)

    def test_existing_order_id_in_db_reports_row_2(self):
        importer.import_csv(self.conn, "orders", self.orders_text(), "orders.csv")
        clash = self.orders_text(second_id="MT-003")  # MT-001 已存在，MT-003 是新的
        with self.assertRaises(importer.ImportValidationError) as ctx:
            importer.import_csv(self.conn, "orders", clash, "orders_v2.csv")

        errors = ctx.exception.errors
        self.assertEqual(len(errors), 1)
        self.assertEqual((errors[0]["row"], errors[0]["field"]), (2, "order_id"))
        self.assertIn("主键已存在于库中", errors[0]["message"])
        self.assertEqual(models.count(self.conn, "orders"), 2)  # 一行都没新增
        self.assertIsNone(models.get_order(self.conn, "MT-003"))

    def test_record_batch_false_skips_batch_table_and_hash_dedup(self):
        text = self.orders_text()
        first = importer.import_csv(self.conn, "orders", text, record_batch=False)
        self.assertIsNone(first["batch_id"])
        self.assertEqual(models.count(self.conn, "import_batches"), 0)

        # 同一份文件因为没有哈希记录，会改由「主键已存在于库中」拦下
        with self.assertRaises(importer.ImportValidationError) as ctx:
            importer.import_csv(self.conn, "orders", text, record_batch=False)
        self.assertIn("主键已存在于库中", ctx.exception.errors[0]["message"])


if __name__ == "__main__":
    unittest.main()
