"""core/money.py：分→元的格式化边界（负数、千分位、不产生浮点）。"""

import unittest

from core import money


class FenToYuanTest(unittest.TestCase):
    """fen_to_yuan：唯一的「分 → 展示元」出口，必须全程整数运算。"""

    def test_positive_basic(self):
        self.assertEqual(money.fen_to_yuan(1423), "14.23")
        self.assertEqual(money.fen_to_yuan(0), "0.00")
        self.assertEqual(money.fen_to_yuan(100), "1.00")
        self.assertEqual(money.fen_to_yuan(5), "0.05")  # 角分不足两位要补零

    def test_thousands_separator_toggle(self):
        self.assertEqual(money.fen_to_yuan(123456789), "1,234,567.89")
        self.assertEqual(money.fen_to_yuan(123456789, thousands=False), "1234567.89")
        self.assertEqual(money.fen_to_yuan(99999), "999.99")  # 四位以上才分节
        self.assertEqual(money.fen_to_yuan(100000), "1,000.00")

    def test_negative_keeps_sign(self):
        self.assertEqual(money.fen_to_yuan(-1423), "-14.23")
        self.assertEqual(money.fen_to_yuan(-5), "-0.05")
        self.assertEqual(money.fen_to_yuan(-5, thousands=False), "-0.05")
        self.assertEqual(money.fen_to_yuan(-123456789), "-1,234,567.89")

    def test_no_float_rounding_loss(self):
        """超过 float 精确整数范围（2^53）的分值也必须原样还原。"""
        fen = 2 ** 53 + 1  # 9007199254740993
        self.assertEqual(money.fen_to_yuan(fen, thousands=False), "90071992547409.93")
        self.assertNotEqual(money.fen_to_yuan(fen, thousands=False), "90071992547409.92")
        self.assertEqual(money.fen_to_yuan(fen), "90,071,992,547,409.93")

    def test_result_is_text_and_accepts_integral_string(self):
        self.assertIsInstance(money.fen_to_yuan(1), str)
        self.assertEqual(money.fen_to_yuan("1423"), "14.23")  # int() 兼容整数字符串


class YuanMarkTest(unittest.TestCase):
    """带符号展示文本：负号统一用 U+2212，正数前缀 +¥。"""

    def test_positive(self):
        self.assertEqual(money.yuan_mark(1423), "¥14.23")
        self.assertEqual(money.yuan_mark(123456789), "¥1,234,567.89")
        self.assertEqual(money.yuan_mark(0), "¥0.00")

    def test_negative_uses_unicode_minus(self):
        self.assertEqual(money.yuan_mark(-1423), "\u2212¥14.23")
        self.assertNotIn("-", money.yuan_mark(-1423))  # 不是 ASCII 连字符
        self.assertEqual(money.yuan_mark(-1423, thousands=False), "\u2212¥14.23")

    def test_signed_positive_gets_plus(self):
        self.assertEqual(money.signed_yuan_mark(1423), "+¥14.23")
        self.assertEqual(money.signed_yuan_mark(1), "+¥0.01")

    def test_signed_negative_and_zero(self):
        self.assertEqual(money.signed_yuan_mark(-1423), "\u2212¥14.23")
        self.assertEqual(money.signed_yuan_mark(0), "¥0.00")  # 零不带正负号


class RoundHalfUpDivTest(unittest.TestCase):
    def test_rounds_half_up(self):
        self.assertEqual(money.round_half_up_div(5, 2), 3)
        self.assertEqual(money.round_half_up_div(4, 2), 2)
        self.assertEqual(money.round_half_up_div(1, 3), 0)
        self.assertEqual(money.round_half_up_div(2, 3), 1)
        self.assertEqual(money.round_half_up_div(0, 7), 0)

    def test_negative_numerator_rejected(self):
        with self.assertRaises(ValueError):
            money.round_half_up_div(-1, 2)


class FeeFromBpTest(unittest.TestCase):
    def test_matches_fixture_values(self):
        self.assertEqual(money.fee_from_bp(14065, 60), 84)  # data/settlements.csv ST0000001
        self.assertEqual(money.fee_from_bp(38000, 200), 760)  # 美团 2%
        self.assertEqual(money.fee_from_bp(10000, 60), 60)
        self.assertEqual(money.fee_from_bp(0, 60), 0)

    def test_half_fen_rounds_up(self):
        self.assertEqual(money.fee_from_bp(5000, 1), 1)  # 0.5 分进 1 分
        self.assertEqual(money.fee_from_bp(4999, 1), 0)  # 0.4999 分舍去


if __name__ == "__main__":
    unittest.main()
