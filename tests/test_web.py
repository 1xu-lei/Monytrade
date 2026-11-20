"""web/：模板渲染、手写 SVG、核查单导出、multipart 解析与页面组装。

这组用例守的是「界面坏不坏」这条线：core/ 的算法有 check.py 与
test_detectors.py 守着，这里只断言渲染层本身。
"""

import json
import re
import tempfile
import unittest
from pathlib import Path

import gen_data
from core import models, reconciler
from tests import support
from web import components, exporter, multipart, svg, views
from web.render import Renderer, render

NODE_PATTERN = re.compile(r'<circle class="node (ok|none|ghost|broken)"')


class RendererTest(unittest.TestCase):
    """模板引擎只做 {{name}} 替换，缺参数必须立刻报错而不是静默留空。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        (self.dir / "demo.html").write_text("<b>{{title}}</b>{{body}}", encoding="utf-8")
        self.renderer = Renderer(self.dir)

    def test_simple_substitution(self):
        self.assertEqual(self.renderer.render("demo.html", title="标题", body="正文"),
                         "<b>标题</b>正文")

    def test_missing_key_raises(self):
        with self.assertRaises(KeyError):
            self.renderer.render("demo.html", title="只有标题")

    def test_none_becomes_empty(self):
        self.assertEqual(self.renderer.render("demo.html", title=None, body=None), "<b></b>")

    def test_layout_renders(self):
        html = render("layout.html", title="总览", nav="", env="T = -", body="<p>x</p>",
                      version="1.0")
        self.assertIn("MoneyTrail", html)
        self.assertIn("<p>x</p>", html)
        self.assertNotIn("{{", html)


class DemoTests(unittest.TestCase):
    """一次性生成演示库，供 SVG / 视图 / 导出三类用例共用。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls._tmp.name) / "web.db"
        gen_data.generate(Path(cls._tmp.name) / "data", cls.db_path, csv_only=False)
        cls.conn = models.connect_ready(cls.db_path)
        cls._ai_env = support.ai_offline()

    @classmethod
    def tearDownClass(cls):
        support.ai_env_restore(cls._ai_env)
        cls.conn.close()
        cls._tmp.cleanup()


class SvgTest(DemoTests):

    def test_five_nodes_and_timeline_axis(self):
        trail = reconciler.build_trail(self.conn, "MT20260921-001")
        markup = svg.build(trail, title="MT20260921-001")
        self.assertEqual(len(NODE_PATTERN.findall(markup)), 5)
        self.assertIn("顾客支付", markup)
        self.assertIn("结算到账", markup)
        self.assertIn("横轴 = 真实时间", markup)

    def test_broken_pipe_has_physical_gap(self):
        trail = reconciler.build_trail(self.conn, "MT20260921-001")
        markup = svg.build(trail)
        self.assertEqual(trail["broken_index"], 4)
        self.assertIn("断链 · 缺 ¥139.81", markup)
        self.assertIn("pipe-head", markup)   # 断口前
        self.assertIn("pipe-tail", markup)   # 断口后
        self.assertIn("gap-dot", markup)

    def test_healthy_order_has_no_gap(self):
        trail = reconciler.build_trail(self.conn, "MT20260919-001")
        self.assertEqual(trail["broken_index"], -1)
        markup = svg.build(trail)
        self.assertNotIn("pipe-head", markup)
        self.assertNotIn("gap-dot", markup)

    def test_break_lands_on_the_right_stage(self):
        cases = {"MT20260919-007": 2, "MT20260920-001": 3,
                 "MT20260921-006": 4, "MT20260919-004": 4}
        stages = {"MT20260919-007": "退款发生", "MT20260920-001": "手续费扣减",
                  "MT20260921-006": "结算到账", "MT20260919-004": "结算到账"}
        for order_id, expected in cases.items():
            trail = reconciler.build_trail(self.conn, order_id)
            self.assertEqual(trail["broken_index"], expected, order_id)
            self.assertEqual(trail["stages"][expected]["name"], stages[order_id])
            self.assertEqual(trail["stages"][expected]["state"], "broken")

    def test_escaping_in_labels(self):
        """归因文字里的 HTML 元字符必须被转义，不能直接注入 SVG。"""
        trail = reconciler.build_trail(self.conn, "MT20260921-001")
        trail["reason"] = '<script>alert(1)</script>'
        markup = svg.build(trail)
        self.assertNotIn("<script>", markup)
        self.assertIn("&lt;script&gt;", markup)

    def test_empty_trail_is_safe(self):
        self.assertEqual(svg.build({}), "")


class ExporterTest(DemoTests):

    def _trail_with_evidence(self, order_id):
        trail = reconciler.build_trail(self.conn, order_id)
        return dict(trail, anomalies=views.with_evidence(trail["anomalies"]))

    def test_single_markdown_has_all_three_evidence_sections(self):
        trail = self._trail_with_evidence("MT20260921-001")
        text = exporter.order_markdown(self.conn, "MT20260921-001", trail, trail["anomalies"])
        self.assertIn("资金链路核查单", text)
        self.assertIn("## 一、资金链路", text)
        self.assertIn("原始流水", text)
        self.assertIn("```text", text)      # 逐行计算
        self.assertIn("```sql", text)       # 判定依据
        self.assertIn("13981", text)        # 缺口计算过程可复核
        self.assertIn("建议后续动作", text)

    def test_csv_rows_match_anomaly_count(self):
        rows = models.list_anomalies(self.conn)
        trails = [self._trail_with_evidence(row["order_id"]) for row in rows]
        text = exporter.batch_csv(trails, models.reference_time(self.conn))
        lines = [line for line in text.splitlines() if line.strip()]
        self.assertEqual(lines[0].lstrip("\ufeff").split(",")[0], "order_id")
        self.assertEqual(len(lines) - 1, len(rows))

    def test_batch_markdown_counts_every_order(self):
        rows = models.list_anomalies(self.conn)
        trails = [self._trail_with_evidence(row["order_id"]) for row in rows]
        text = exporter.batch_markdown(self.conn, trails, "2026-11-19 12:00:00", "")
        self.assertIn(f"涉及订单：{len(trails)} 笔", text)
        self.assertEqual(text.count("# 资金链路核查单 · MT"), len(trails))


class MultipartTest(unittest.TestCase):

    def test_parses_field_and_file(self):
        body = b"".join([
            b"--BOUND\r\nContent-Disposition: form-data; name=\"kind\"\r\n\r\norders\r\n",
            b"--BOUND\r\nContent-Disposition: form-data; name=\"file\"; "
            b"filename=\"orders.csv\"\r\nContent-Type: text/csv\r\n\r\n",
            "a,b\n1,2\n".encode("utf-8"),
            b"\r\n--BOUND--\r\n",
        ])
        fields = multipart.parse(body, "multipart/form-data; boundary=BOUND")
        self.assertEqual(fields["kind"]["data"], b"orders")
        self.assertEqual(fields["file"]["filename"], "orders.csv")
        self.assertEqual(fields["file"]["data"], "a,b\n1,2\n".encode("utf-8"))

    def test_quoted_boundary(self):
        body = b'--X\r\nContent-Disposition: form-data; name="k"\r\n\r\nv\r\n--X--\r\n'
        fields = multipart.parse(body, 'multipart/form-data; boundary="X"')
        self.assertEqual(fields["k"]["data"], b"v")

    def test_missing_boundary_raises(self):
        with self.assertRaises(multipart.MultipartError):
            multipart.parse(b"", "multipart/form-data")

    def test_empty_body_raises(self):
        with self.assertRaises(multipart.MultipartError):
            multipart.parse(b"", "multipart/form-data; boundary=X")


class ViewTest(DemoTests):

    def test_overview_has_four_cards(self):
        html = views.overview(self.conn)
        for label in ("订单总额", "实际到账", "待核差异", "异常笔数"):
            self.assertIn(label, html)
        self.assertIn("¥3,428.45", html)
        self.assertNotIn("{{", html)

    def test_anomalies_filter_keeps_only_that_label(self):
        filters = {"label": "FEE_ANOMALY", "severity": "", "status": "",
                   "date_from": "", "date_to": ""}
        html = views.anomalies_page(self.conn, filters)
        self.assertIn("badge-fee-anomaly", html)
        self.assertNotIn("badge-dup-refund", html)

    def test_anomalies_empty_state(self):
        filters = {"label": "", "severity": "", "status": "resolved",
                   "date_from": "", "date_to": ""}
        html = views.anomalies_page(self.conn, filters)
        self.assertIn("当前筛选条件下没有异常", html)

    def test_anomalies_pagination_splits_and_clamps(self):
        original = views.PAGE_SIZE
        self.addCleanup(setattr, views, "PAGE_SIZE", original)
        views.PAGE_SIZE = 4
        filters = {"label": "", "severity": "", "status": "",
                   "date_from": "", "date_to": ""}
        first = views.anomalies_page(self.conn, filters, page=1)
        self.assertIn("第 1 / 3 页", first)
        self.assertIn("下一页", first)
        self.assertNotIn("上一页", first)
        # 越界页回落到最后一页，而不是渲染成「没有异常」
        last = views.anomalies_page(self.conn, filters, page=99)
        self.assertIn("第 3 / 3 页", last)
        self.assertIn("上一页", last)
        self.assertNotIn("当前筛选条件下没有异常", last)

    def test_anomalies_pager_keeps_filters(self):
        original = views.PAGE_SIZE
        self.addCleanup(setattr, views, "PAGE_SIZE", original)
        views.PAGE_SIZE = 2
        filters = {"label": "NOT_SETTLED", "severity": "", "status": "",
                   "date_from": "", "date_to": ""}
        html = views.anomalies_page(self.conn, filters, page=1)
        self.assertIn("label=NOT_SETTLED&amp;page=2", html)
        self.assertIn("第 1 / 2 页", html)

    def test_assistant_example_links_are_url_encoded(self):
        html = views.assistant_page(self.conn, "")
        self.assertIn("/assistant?q=%E4%BB%8A%E5%A4%A9", html)
        self.assertNotIn("q=今天", html)

    def test_trail_page_contains_pipeline_and_evidence(self):
        html = views.trail_page(self.conn, "MT20260921-001")
        self.assertIn("<svg", html)
        self.assertIn("凭什么这样判断", html)
        self.assertIn("逐行计算过程", html)
        self.assertIn("本次判定使用的完整 SQL", html)

    def test_trail_page_unknown_order_raises_not_found(self):
        with self.assertRaises(views.NotFound):
            views.trail_page(self.conn, "MT20990101-999")

    def test_assistant_page_shows_intent_and_sql(self):
        html = views.assistant_page(self.conn, "这个月手续费多收了多少")
        self.assertIn("意图 JSON", html)
        self.assertIn("程序生成的 SQL", html)
        self.assertIn("fee_overcharge", html)

    def test_assistant_page_without_question(self):
        html = views.assistant_page(self.conn, "")
        self.assertIn("在上方提出一个关于账本的问题", html)

    def test_import_page_lists_three_upload_forms(self):
        html = views.import_page(self.conn)
        self.assertEqual(html.count('enctype="multipart/form-data"'), 3)
        self.assertIn("导入批次", html)

    def test_import_page_renders_row_errors(self):
        html = views.import_page(self.conn, message="",
                                 errors=[{"row": 1, "field": "status",
                                          "message": "缺少必需列：status"}])
        self.assertIn("第 1 行", html)
        self.assertIn("缺少必需列", html)

    def test_error_page(self):
        self.assertIn("页面不存在", views.error_page("页面不存在"))


class ComponentTest(DemoTests):

    def test_anomaly_rows_one_tr_per_anomaly(self):
        rows = models.list_anomalies(self.conn)
        html = components.anomaly_rows(rows)
        self.assertEqual(html.count("<tr>"), len(rows))
        self.assertIn('href="/trail/', html)

    def test_anomaly_rows_empty(self):
        self.assertEqual(components.anomaly_rows([]), "")

    def test_evidence_drawer_three_sections(self):
        row = models.list_anomalies(self.conn, label="NOT_SETTLED")[0]
        item = views.with_evidence([row])[0]
        html = components.evidence_drawer(item, open_by_default=True)
        for marker in ("原始流水对照", "逐行计算过程", "判定依据", "<details", "open"):
            self.assertIn(marker, html)
        self.assertIn("缺口", html)

    def test_badges_and_money(self):
        self.assertIn("badge-not-settled", components.label_badge("NOT_SETTLED"))
        self.assertIn("sev-high", components.severity_badge("high"))
        self.assertIn("status-pending", components.status_badge("pending"))
        self.assertEqual(components.money(13981), "¥139.81")
        self.assertEqual(components.signed(-176), "−¥1.76")
        self.assertEqual(components.signed(88), "+¥0.88")

    def test_escape_blocks_html_injection(self):
        self.assertNotIn("<b>", components.esc("<b>x</b>"))

    def test_flash_and_error_page_escape_url_input(self):
        """flash / 404 文案可能来自 URL，必须转义后才进页面。"""
        import app as app_module
        payload = '<img src=x onerror=alert(1)>'
        self.assertNotIn("<img", app_module._first({"msg": [payload]}, "msg"))
        html = views.error_page(f"账本中没有订单 {payload}")
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)
        self.assertNotIn("<script>", views.error_page("<script>alert(1)</script>"))


class GroundTruthConsistencyTest(DemoTests):
    """对话页展示的四个数字必须与库内聚合一致（防止界面自己算数）。"""

    def test_stats_match_sql(self):
        stats = reconciler.stats(self.conn)
        total = self.conn.execute("SELECT SUM(amount_fen) AS s FROM orders").fetchone()["s"]
        received = self.conn.execute("SELECT SUM(net_fen) AS s FROM settlements").fetchone()["s"]
        gap = self.conn.execute("SELECT SUM(gap_fen) AS s FROM anomalies").fetchone()["s"]
        self.assertEqual(stats["order_total_fen"], total)
        self.assertEqual(stats["received_fen"], received)
        self.assertEqual(stats["open_gap_fen"], gap)
        self.assertEqual(stats["all_count"], 9)

    def test_health_payload_is_json_serialisable(self):
        payload = {"orders": reconciler.stats(self.conn)["order_count"]}
        self.assertEqual(json.loads(json.dumps(payload))["orders"], 24)


if __name__ == "__main__":
    unittest.main()
