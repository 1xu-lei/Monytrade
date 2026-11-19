"""core/assistant.py：意图识别、数字校验、降级与拒答。

这组用例守的是「模型不算数」这条铁律：即使模型胡编一个金额，
也必须被数字校验拦下来并回落到模板答案。
"""

import json
import os
import unittest

import config
from core import assistant, reconciler
from tests import support


class KeywordIntentTest(unittest.TestCase):
    """降级路径的意图识别：不依赖模型也必须认得出三类问法。"""

    def test_unsettled_phrasings(self):
        for question in ("今天哪几笔没到账", "有没有没到卡的", "哪些订单未结算"):
            self.assertEqual(assistant.keyword_intent(question)["intent"], "unsettled_orders")

    def test_fee_phrasings(self):
        for question in ("这个月手续费多收了多少", "费率是不是被调高了", "平台抽成对不上"):
            self.assertEqual(assistant.keyword_intent(question)["intent"], "fee_overcharge")

    def test_order_id_wins_over_keywords(self):
        intent = assistant.keyword_intent("MT20260921-001 这笔为什么没到账")
        self.assertEqual(intent["intent"], "explain_order")
        self.assertEqual(intent["order_id"], "MT20260921-001")

    def test_out_of_scope(self):
        for question in ("今天天气怎么样", "帮我写首诗", ""):
            self.assertEqual(assistant.keyword_intent(question)["intent"], "out_of_scope")


class IntentJsonTest(unittest.TestCase):
    """模型返回的意图 JSON 必须严格校验，越界一律当成没答。"""

    def test_accepts_valid_json(self):
        reply = '{"intent":"explain_order","order_id":"MT20260921-001","confidence":0.9}'
        intent = assistant._parse_intent_json(reply)
        self.assertEqual(intent["intent"], "explain_order")
        self.assertEqual(intent["order_id"], "MT20260921-001")

    def test_rejects_unknown_intent(self):
        self.assertIsNone(assistant._parse_intent_json('{"intent":"refund_everything"}'))

    def test_rejects_bad_order_id(self):
        self.assertIsNone(assistant._parse_intent_json(
            '{"intent":"explain_order","order_id":"随便一个字符串"}'))

    def test_rejects_non_json(self):
        for reply in ("好的，我来帮你看看", "", None, "{不是 json}"):
            self.assertIsNone(assistant._parse_intent_json(reply))

    def test_strips_code_fence(self):
        reply = '```json\n{"intent":"fee_overcharge","month":"2026-09"}\n```'
        intent = assistant._parse_intent_json(reply)
        self.assertEqual(intent["intent"], "fee_overcharge")
        self.assertEqual(intent["month"], "2026-09")


class VerifyNumbersTest(unittest.TestCase):
    """第三段措辞的数字白名单：只允许出现事实清单里的数字。"""

    def setUp(self):
        self.result = {
            "facts": [{"order_id": "MT20260921-001", "paid_at": "2026-09-21 09:14:00",
                       "gap_fen": 13981, "refund_fen": 0}],
            "total_gap_fen": 13981,
            "month": "2026-09",
        }

    def test_accepts_numbers_from_facts(self):
        self.assertTrue(assistant.verify_numbers(
            "MT20260921-001 缺口 ¥139.81，即 13981 分。", self.result))

    def test_rejects_hallucinated_amount(self):
        self.assertFalse(assistant.verify_numbers("缺口 ¥139.82。", self.result))
        self.assertFalse(assistant.verify_numbers("缺口一共 13982 分。", self.result))

    def test_dates_and_times_are_ignored(self):
        self.assertTrue(assistant.verify_numbers(
            "以 2026-09-22 10:20:55 为基准，缺口 ¥139.81。", self.result))

    def test_empty_text_rejected(self):
        self.assertFalse(assistant.verify_numbers("", self.result))


class AskDegradedTest(support.TempDBTest):
    """没有 API Key：三类问法都要给出带订单号的模板答案，其余拒答。"""

    def _seed(self):
        support.add_order(self.conn, "MT20260921-001", amount_fen=14065,
                          paid_at="2026-09-21 09:14:00")
        support.add_order(self.conn, "MT20260921-002", amount_fen=10000,
                          paid_at="2026-09-21 10:00:00")
        support.add_settlement(self.conn, "ST0000001", "MT20260921-002",
                               gross_fen=10000, fee_fen=60, net_fen=9940)
        support.add_order(self.conn, "MT20260921-003", channel="meituan",
                          amount_fen=38000, paid_at="2026-09-21 11:00:00")
        support.add_settlement(self.conn, "ST0000002", "MT20260921-003",
                               channel="meituan", gross_fen=38000, fee_fen=950,
                               net_fen=37050)
        reconciler.reconcile(self.conn)

    def setUp(self):
        super().setUp()
        self._ai_env = support.ai_offline()
        self.addCleanup(support.ai_env_restore, self._ai_env)
        assistant.set_kill_switch(self.conn, False)
        self._seed()

    def test_no_key_means_unavailable(self):
        self.assertFalse(assistant.ai_status(self.conn)["available"])

    def test_unsettled_answer_lists_order_ids(self):
        result = assistant.ask(self.conn, "今天哪几笔没到账")
        self.assertEqual(result["mode"], "template")
        self.assertEqual(result["intent"]["intent"], "unsettled_orders")
        self.assertIn("MT20260921-001", result["answer"])
        self.assertNotIn("MT20260921-002", result["answer"])  # 已到账的不该出现
        self.assertEqual(result["sources"], ["MT20260921-001"])

    def test_explain_order_answer(self):
        result = assistant.ask(self.conn, "MT20260921-001 这笔为什么对不上")
        self.assertEqual(result["mode"], "template")
        self.assertIn("MT20260921-001", result["answer"])
        self.assertIn("结算到账", result["answer"])

    def test_explain_unknown_order_refuses_gracefully(self):
        result = assistant.ask(self.conn, "MT20990101-999 为什么对不上")
        self.assertIn("没有订单", result["answer"])
        self.assertEqual(result["facts"], [])

    def test_fee_overcharge_answer(self):
        result = assistant.ask(self.conn, "这个月手续费多收了多少")
        self.assertEqual(result["intent"]["intent"], "fee_overcharge")
        self.assertIn("MT20260921-003", result["answer"])
        self.assertIn("1.90", result["answer"])  # 950 - 760 = 190 分

    def test_out_of_scope_is_refused(self):
        result = assistant.ask(self.conn, "今天天气怎么样")
        self.assertEqual(result["mode"], "refuse")
        self.assertEqual(result["sql"], "")

    def test_kill_switch_reported_in_status(self):
        assistant.set_kill_switch(self.conn, True)
        self.assertFalse(assistant.ai_status(self.conn)["available"])
        self.assertIn("熔断", assistant.ai_status(self.conn)["reason"])

    def test_answer_never_contains_foreign_numbers(self):
        """铁律 1 的端到端证据：回答里的数字必须来自 SQL 结果。"""
        result = assistant.ask(self.conn, "今天哪几笔没到账")
        self.assertTrue(assistant.verify_numbers(result["answer"], result))


class AskWithKeyButBrokenServiceTest(support.TempDBTest):
    """有 Key 但服务连不上：必须静默回落模板，不抛异常。"""

    def setUp(self):
        super().setUp()
        self._saved = {key: os.environ.get(key) for key in
                       (config.BAIZE_API_KEY_ENV, config.BAIZE_BASE_URL_ENV)}
        os.environ[config.BAIZE_API_KEY_ENV] = "test-key-not-real"
        os.environ[config.BAIZE_BASE_URL_ENV] = "http://127.0.0.1:1/v1"
        self.addCleanup(self._restore)
        assistant.set_kill_switch(self.conn, False)

    def _restore(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_falls_back_without_raising(self):
        self.assertTrue(assistant.ai_status(self.conn)["available"])
        self.assertIsNone(assistant._chat([{"role": "user", "content": "ping"}]))
        result = assistant.ask(self.conn, "今天哪几笔没到账")
        self.assertEqual(result["mode"], "template")
        self.assertTrue(result["answer"].strip())

    def test_intent_json_rejects_garbage_from_model(self):
        """模型返回非 JSON 时，意图识别要退回关键词而不是报错。"""
        self.assertIsNone(assistant._parse_intent_json("我觉得你想问没到账"))
        intent = assistant.parse_intent(self.conn, "这个月手续费多收了多少")
        self.assertEqual(intent["intent"], "fee_overcharge")
        self.assertEqual(intent["by"], "keyword")


class SqlCatalogTest(unittest.TestCase):
    """三条意图 SQL 都必须是参数化 / 无字符串拼接的定稿语句。"""

    def test_expected_intents_have_sql(self):
        for name in assistant.SUPPORTED:
            self.assertIn(name, assistant.SQL)
            self.assertTrue(assistant.SQL[name].strip().upper().startswith("SELECT"))
            self.assertNotIn("{}", assistant.SQL[name])
            self.assertNotIn("%s", assistant.SQL[name])

    def test_intent_schema_is_valid_json(self):
        self.assertIsInstance(json.loads(assistant.INTENT_SCHEMA), dict)


if __name__ == "__main__":
    unittest.main()
