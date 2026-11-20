"""app.py：在进程内起真实 HTTP 服务，逐条验证路由、表单提交与下载。

不起子进程、不占固定端口（bind 0 让系统分配），因此可以随时跑；
每个用例都打真实 TCP，能抓到「视图能渲染但路由接错」这类问题。
"""

import http.client
import json
import tempfile
import threading
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path

import gen_data
from app import Handler
from core import assistant, models
from tests import support

BOUNDARY = "----moneyTrailTestBoundary"


class QuietHandler(Handler):
    """测试期关掉访问日志，避免把 unittest 输出刷屏。"""

    def log_message(self, fmt, *args):
        pass


class HttpTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls._tmp.name) / "http.db"
        gen_data.generate(Path(cls._tmp.name) / "data", cls.db_path, csv_only=False)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        cls.server.daemon_threads = True
        cls.server.db_path = str(cls.db_path)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls._ai_env = support.ai_offline()

    @classmethod
    def tearDownClass(cls):
        support.ai_env_restore(cls._ai_env)
        cls.server.shutdown()
        cls.server.server_close()
        cls._tmp.cleanup()

    # ------------------------------------------------------------ helpers

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        payload = response.read().decode("utf-8", "replace")
        result = {"status": response.status, "body": payload,
                  "headers": dict(response.getheaders())}
        conn.close()
        return result

    def get(self, path):
        return self.request("GET", path)

    def post_form(self, path, data):
        body = urllib.parse.urlencode(data)
        return self.request("POST", path, body,
                            {"Content-Type": "application/x-www-form-urlencoded"})

    def upload(self, kind, filename, content: bytes):
        body = b"".join([
            f"--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"kind\"\r\n\r\n".encode(),
            kind.encode(), b"\r\n",
            (f"--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"file\"; "
             f"filename=\"{filename}\"\r\nContent-Type: text/csv\r\n\r\n").encode(),
            content, b"\r\n", f"--{BOUNDARY}--\r\n".encode(),
        ])
        return self.request("POST", "/import", body,
                            {"Content-Type": f"multipart/form-data; boundary={BOUNDARY}",
                             "Content-Length": str(len(body))})

    def db_count(self, table):
        conn = models.connect_ready(self.db_path)
        try:
            return models.count(conn, table)
        finally:
            conn.close()

    # ------------------------------------------------------------ 页面

    def test_health_returns_json(self):
        response = self.get("/health")
        payload = json.loads(response["body"])
        self.assertEqual(response["status"], 200)
        self.assertEqual(payload["app"], "MoneyTrail")
        self.assertEqual(payload["orders"], 24)
        self.assertEqual(payload["anomalies"], 9)
        self.assertFalse(payload["ai"])

    def test_overview(self):
        response = self.get("/")
        self.assertEqual(response["status"], 200)
        for label in ("订单总额", "实际到账", "待核差异", "异常笔数"):
            self.assertIn(label, response["body"])

    def test_anomaly_center_and_filters(self):
        self.assertEqual(self.get("/anomalies")["status"], 200)
        filtered = self.get("/anomalies?label=FEE_ANOMALY&severity=low")
        self.assertEqual(filtered["status"], 200)
        self.assertIn("badge-fee-anomaly", filtered["body"])
        self.assertNotIn("badge-dup-refund", filtered["body"])

    def test_anomaly_pagination_param_is_tolerated(self):
        # 分页参数非法或越界都必须照常出页，不 500
        for path in ("/anomalies?page=abc", "/anomalies?page=0", "/anomalies?page=99"):
            response = self.get(path)
            self.assertEqual(response["status"], 200, path)
            self.assertIn("异常中心", response["body"])

    def test_wrong_method_returns_405_with_allow(self):
        # GET 打开只接受 POST 的动作路由：要 405 + Allow，而不是 404
        for path in ("/reconcile", "/ai/toggle", "/anomalies/AN-NS-001/status"):
            response = self.get(path)
            self.assertEqual(response["status"], 405, path)
            self.assertIn("POST", response["headers"].get("Allow", ""), path)

    def test_post_to_read_only_route_returns_405(self):
        response = self.request("POST", "/anomalies", b"",
                                {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(response["status"], 405)
        self.assertIn("GET", response["headers"].get("Allow", ""))

    def test_unknown_path_still_returns_404(self):
        response = self.get("/no-such-page")
        self.assertEqual(response["status"], 404)

    def test_trail_page_and_404(self):
        ok = self.get("/trail/MT20260921-001")
        self.assertEqual(ok["status"], 200)
        self.assertIn("<svg", ok["body"])
        self.assertIn("断链 · 缺 ¥139.81", ok["body"])
        missing = self.get("/trail/MT20990101-999")
        self.assertEqual(missing["status"], 404)
        self.assertIn("账本中没有订单", missing["body"])

    def test_assistant_pages(self):
        question = urllib.parse.quote("今天哪几笔没到账")
        response = self.get(f"/assistant?q={question}")
        self.assertEqual(response["status"], 200)
        self.assertIn("MT20260920-002", response["body"])
        self.assertIn("程序生成的 SQL", response["body"])
        refused = self.get("/assistant?q=" + urllib.parse.quote("今天天气怎么样"))
        self.assertIn("超出账本事实", refused["body"])

    def test_import_page_and_static(self):
        self.assertIn("multipart/form-data", self.get("/import")["body"])
        css = self.get("/static/style.css")
        self.assertEqual(css["status"], 200)
        self.assertIn("text/css", css["headers"].get("Content-Type", ""))
        self.assertEqual(self.get("/static/nope.css")["status"], 404)

    def test_unknown_path_is_404(self):
        self.assertEqual(self.get("/nope")["status"], 404)

    # ------------------------------------------------------------ 下载

    def test_export_order_markdown_and_csv(self):
        markdown = self.get("/export/order/MT20260921-001.md")
        self.assertEqual(markdown["status"], 200)
        self.assertIn("资金链路核查单", markdown["body"])
        self.assertIn('attachment; filename="moneytrail-MT20260921-001.md"',
                      markdown["headers"].get("Content-Disposition", ""))
        csv = self.get("/export/order/MT20260921-001.csv")
        self.assertEqual(csv["status"], 200)
        self.assertTrue(csv["body"].lstrip("\ufeff").startswith("order_id,label_code"))

    def test_export_batch_with_filter(self):
        response = self.get("/export/batch.csv?severity=high")
        self.assertEqual(response["status"], 200)
        rows = [line for line in response["body"].splitlines() if line.strip()]
        self.assertEqual(len(rows) - 1, 5)  # 3 笔未到账 + 2 笔重复退款
        self.assertEqual(self.get("/export/batch.md")["status"], 200)

    def test_export_unknown_order_is_404(self):
        self.assertEqual(self.get("/export/order/MT20990101-999.md")["status"], 404)

    # ------------------------------------------------------------ 动作

    def test_reconcile_is_idempotent(self):
        before = self.db_count("anomalies")
        first = self.post_form("/reconcile", {})
        second = self.post_form("/reconcile", {})
        self.assertEqual(first["status"], 303)
        self.assertEqual(second["status"], 303)
        self.assertEqual(self.db_count("anomalies"), before)

    def test_status_transition_persists(self):
        anomaly_id = "AN-FA-MT20260920-001"
        response = self.post_form(f"/anomalies/{anomaly_id}/status",
                                  {"status": "resolved", "return_to": "/anomalies"})
        self.assertEqual(response["status"], 303)
        self.assertIn("已解决", urllib.parse.unquote(
            response["headers"].get("Location", "")) or "")
        conn = models.connect_ready(self.db_path)
        try:
            self.assertEqual(models.get_anomaly(conn, anomaly_id)["status"], "resolved")
        finally:
            conn.close()
        self.post_form(f"/anomalies/{anomaly_id}/status",
                       {"status": "pending", "return_to": "/anomalies"})

    def test_status_rejects_illegal_value_and_foreign_target(self):
        response = self.post_form("/anomalies/AN-FA-MT20260920-001/status",
                                  {"status": "deleted", "return_to": "http://evil.example"})
        self.assertEqual(response["status"], 303)
        location = response["headers"].get("Location", "")
        self.assertTrue(location.startswith("/anomalies"))
        self.assertNotIn("evil.example", location)

    def test_ai_toggle_persists_kill_switch(self):
        before = self.db_count("settings")
        self.post_form("/ai/toggle", {})
        conn = models.connect_ready(self.db_path)
        try:
            self.assertTrue(assistant.kill_switch(conn))
        finally:
            conn.close()
        self.post_form("/ai/toggle", {})
        conn = models.connect_ready(self.db_path)
        try:
            self.assertFalse(assistant.kill_switch(conn))
        finally:
            conn.close()
        self.assertGreaterEqual(self.db_count("settings"), before)

    # ------------------------------------------------------------ 上传

    def test_import_missing_column_reports_row(self):
        bad = ("order_id,channel,paid_at,amount_fen\n"
               "X1,wechat,2026-01-01 10:00:00,100\n").encode("utf-8")
        before = self.db_count("orders")
        response = self.upload("orders", "bad.csv", bad)
        self.assertEqual(response["status"], 400)
        self.assertIn("第 1 行", response["body"])
        self.assertEqual(self.db_count("orders"), before)  # 整体回滚

    def test_import_success_then_hash_dedup(self):
        good = ("order_id,channel,paid_at,amount_fen,status\n"
                "MT20261101-001,wechat,2026-11-01 10:00:00,1000,PAID\n").encode("utf-8")
        before = self.db_count("orders")
        first = self.upload("orders", "one.csv", good)
        self.assertEqual(first["status"], 303)
        self.assertIn("导入成功 1 行", urllib.parse.unquote(
            first["headers"].get("Location", "")))
        self.assertEqual(self.db_count("orders"), before + 1)
        again = self.upload("orders", "one.csv", good)
        self.assertIn("已导入过", urllib.parse.unquote(again["headers"].get("Location", "")))
        self.assertEqual(self.db_count("orders"), before + 1)

    def test_import_rejects_unknown_kind(self):
        response = self.upload("payroll", "x.csv", b"a,b\n1,2\n")
        self.assertEqual(response["status"], 400)
        self.assertIn("请选择要导入的 CSV 文件", response["body"])


if __name__ == "__main__":
    unittest.main()
