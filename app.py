"""MoneyTrail 服务入口。

    python app.py            # http://127.0.0.1:8000
    python app.py --port 9000

只用标准库：http.server + sqlite3 + json。零第三方依赖，一条命令启动。
每个请求开一个只读/读写连接，用完即关，因此并发请求之间不会互相污染事务。
"""

import argparse
import json
import mimetypes
import re
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import config
from core import assistant, importer, models, reconciler
from web import exporter, multipart, views

ROUTES = [
    ("GET", re.compile(r"^/$"), "page_overview"),
    ("GET", re.compile(r"^/anomalies$"), "page_anomalies"),
    ("POST", re.compile(r"^/anomalies/(?P<anomaly_id>[^/]+)/status$"), "post_status"),
    ("GET", re.compile(r"^/trail/(?P<order_id>[^/]+)$"), "page_trail"),
    ("GET", re.compile(r"^/import$"), "page_import"),
    ("POST", re.compile(r"^/import$"), "post_import"),
    ("GET", re.compile(r"^/assistant$"), "page_assistant"),
    ("POST", re.compile(r"^/ai/toggle$"), "post_ai_toggle"),
    ("POST", re.compile(r"^/reconcile$"), "post_reconcile"),
    ("GET", re.compile(r"^/export/order/(?P<order_id>[^/]+)\.(?P<ext>md|csv)$"), "export_order"),
    ("GET", re.compile(r"^/export/batch\.(?P<ext>md|csv)$"), "export_batch"),
    ("GET", re.compile(r"^/static/(?P<name>[\w.\-]+)$"), "static_file"),
    ("GET", re.compile(r"^/health$"), "health"),
]

FILTER_KEYS = ("label", "severity", "status", "date_from", "date_to")


class Handler(BaseHTTPRequestHandler):
    server_version = "MoneyTrail/" + config.VERSION
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------ 基础

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _connect(self):
        return models.connect_ready(self.server.db_path)

    def _query(self):
        parsed = urlparse(self.path)
        return parsed, parse_qs(parsed.query, keep_blank_values=False)

    def _filters(self, params: dict) -> dict:
        return {key: (params.get(key) or [""])[0] for key in FILTER_KEYS}

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str):
        parsed, params = self._query()
        path = parsed.path.rstrip("/") or "/"
        if parsed.path != "/" and path == "/":
            path = "/"
        for route_method, pattern, handler_name in ROUTES:
            if route_method != method:
                continue
            match = pattern.match(path)
            if not match:
                continue
            conn = None
            try:
                conn = self._connect()
                getattr(self, handler_name)(conn, params, **match.groupdict())
            except BrokenPipeError:
                return
            except views.NotFound as exc:
                self._not_found(conn, str(exc))
            except Exception:  # 兜底：任何异常都给出可读页面，不白屏
                traceback.print_exc()
                try:
                    self._html(500, views.error_page(
                        "服务端处理这句请求时出错了",
                        hint="已记录完整堆栈到终端。回到总览继续，不要紧 —— 数据不会被改坏。"))
                except Exception:
                    pass
            finally:
                if conn is not None:
                    conn.close()
            return
        self._not_found(None)

    # ------------------------------------------------------------ 响应

    def _html(self, status: int, body: str):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _download(self, payload: bytes, content_type: str, filename: str):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(payload)

    def _redirect(self, location: str, status: int = 303):
        body = f'<a href="{location}">继续</a>'.encode("utf-8")
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _flash(self, message: str, target: str = "/") -> str:
        return f"{target}{'&' if '?' in target else '?'}msg={quote(message)}"

    # ------------------------------------------------------------ 页面

    def page_overview(self, conn, params):
        self._html(200, views.overview(conn, flash=_first(params, "msg")))

    def page_anomalies(self, conn, params):
        self._html(200, views.anomalies_page(conn, self._filters(params),
                                            flash=_first(params, "msg")))

    def page_trail(self, conn, params, order_id):
        self._html(200, views.trail_page(conn, unquote(order_id), flash=_first(params, "msg")))

    def page_import(self, conn, params):
        self._html(200, views.import_page(conn, message=_first(params, "msg")))

    def page_assistant(self, conn, params):
        self._html(200, views.assistant_page(conn, question=_first(params, "q"),
                                            flash=_first(params, "msg")))

    def static_file(self, conn, params, name):
        path = Path(config.STATIC_DIR) / name
        if not path.is_file():
            self._not_found()
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        payload = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "max-age=60")
        self.end_headers()
        self.wfile.write(payload)

    def health(self, conn, params):
        stats = reconciler.stats(conn)
        payload = json.dumps({
            "app": config.APP_NAME,
            "version": config.VERSION,
            "orders": stats["order_count"],
            "settlements": stats["settlement_count"],
            "anomalies": stats["all_count"],
            "reference_time": stats["reference_time"],
            "ai": assistant.ai_status(conn)["available"],
        }, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # ------------------------------------------------------------ 动作

    def post_status(self, conn, params, anomaly_id):
        form = self._read_form()
        status = (form.get("status") or [""])[0]
        target = (form.get("return_to") or ["/anomalies"])[0] or "/anomalies"
        # 只允许站内绝对路径：'//evil.example' 会被 startswith('/') 放行，必须额外挡掉。
        if not target.startswith("/") or target.startswith("//"):
            target = "/anomalies"
        target = target.replace("\\", "/")
        ok = reconciler.set_status(conn, unquote(anomaly_id), status)
        message = (f"工单 {unquote(anomaly_id)} 已更新为 "
                   f"{config.STATUS_TEXT.get(status, status)}" if ok else "工单不存在或状态非法")
        self._redirect(self._flash(message, target))

    def post_reconcile(self, conn, params):
        result = reconciler.reconcile(conn)
        message = (f"对账完成：工单 {result['total']} 笔，缺口合计 "
                   f"{result['gap_fen']} 分，耗时 {result['duration_ms']} ms "
                   f"（继承状态 {result['carried']} 笔）")
        self._redirect(self._flash(message, "/anomalies"))

    def post_ai_toggle(self, conn, params):
        current = assistant.kill_switch(conn)
        assistant.set_kill_switch(conn, not current)
        message = ("已开启「模拟 AI 不可用」熔断开关，助手回落到模板答案"
                   if not current else "已关闭熔断开关")
        self._redirect(self._flash(message, "/assistant"))

    def post_import(self, conn, params):
        content_type = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length") or 0)
        if length > multipart.MAX_BODY_BYTES:
            self._html(413, views.import_page(conn, message="", errors=[
                {"row": 1, "field": "file", "message": "文件超过 8 MB，请拆分后再上传"}]))
            return
        try:
            fields = multipart.parse(self.rfile.read(length), content_type)
        except multipart.MultipartError as exc:
            self._html(400, views.import_page(conn, message="", errors=[
                {"row": 1, "field": "form", "message": str(exc)}]))
            return

        kind = (fields.get("kind") or {}).get("data", b"").decode("utf-8", "replace")
        blob = fields.get("file") or {}
        filename = blob.get("filename") or ""
        if kind not in importer.COLUMNS or not filename:
            self._html(400, views.import_page(conn, message="", errors=[
                {"row": 1, "field": "file", "message": "请选择要导入的 CSV 文件"}]))
            return

        try:
            result = importer.import_csv(conn, kind, blob["data"], filename)
        except importer.ImportValidationError as exc:
            self._html(400, views.import_page(conn, message="", errors=exc.errors))
            return

        if result["skipped"]:
            message = (f"{importer.KIND_TEXT[kind]} 文件已导入过（哈希一致），本次跳过，"
                       f"库内数据未变")
        else:
            message = f"{importer.KIND_TEXT[kind]} 导入成功 {result['rows']} 行"
        rec = reconciler.reconcile(conn)
        message += f"；已重新对账，共 {rec['total']} 笔工单"
        self._redirect(self._flash(message, "/import"))

    # ------------------------------------------------------------ 导出

    def export_order(self, conn, params, order_id, ext):
        order_id = unquote(order_id)
        trail = reconciler.build_trail(conn, order_id)
        if not trail:
            self._not_found(conn, f"账本中没有订单 {order_id}")
            return
        items = views.with_evidence(trail["anomalies"])
        if ext == "md":
            text = exporter.order_markdown(conn, order_id, trail, items)
            self._download(text.encode("utf-8"), "text/markdown; charset=utf-8",
                           f"moneytrail-{_safe_name(order_id)}.md")
            return
        text = exporter.batch_csv([dict(trail, anomalies=items)], trail["reference"])
        self._download(text.encode("utf-8"), "text/csv; charset=utf-8",
                       f"moneytrail-{_safe_name(order_id)}.csv")

    def export_batch(self, conn, params, ext):
        filters = self._filters(params)
        trails = []
        for row in models.list_anomalies(conn, **filters):
            trail = reconciler.build_trail(conn, row["order_id"])
            if trail:
                trails.append(dict(trail, anomalies=views.with_evidence(trail["anomalies"])))
        reference = models.reference_time(conn)
        if ext == "csv":
            text = exporter.batch_csv(trails, reference)
            self._download(text.encode("utf-8"), "text/csv; charset=utf-8",
                           "moneytrail-batch.csv")
            return
        description = "；".join(f"{key}={value}" for key, value in filters.items() if value)
        text = exporter.batch_markdown(conn, trails, models.now_text(), description)
        self._download(text.encode("utf-8"), "text/markdown; charset=utf-8",
                       "moneytrail-batch.md")

    # ------------------------------------------------------------ 工具

    def _read_form(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        body = self.rfile.read(length).decode("utf-8", "replace")
        return parse_qs(body, keep_blank_values=True)

    def _not_found(self, conn=None, message: str = ""):
        try:
            body = views.error_page(message or "页面不存在")
        except Exception:
            body = "<h1>页面不存在</h1>"
        self._html(404, body)


def _first(params: dict, key: str) -> str:
    """取单值查询参数。msg 等参数会原样进页面，因此在这里统一转义。"""
    return escape((params.get(key) or [""])[0])


def _safe_name(order_id: str) -> str:
    """订单号要进 URL / 响应头，只保留安全字符，避免头注入与路径穿越。"""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", order_id)[:64] or "order"


def bootstrap(db_path) -> dict:
    """首次启动时如果库是空的、且 data/ 下已有演示 CSV，就自动灌一遍。

    这样 `python app.py` 单独一条命令也能直接看到有数据的界面。
    """
    conn = models.connect_ready(db_path)
    try:
        models.seed_rates(conn)
        if models.count(conn, "orders") > 0:
            return {"seeded": False}
        csv_dir = Path(config.DATA_DIR)
        if not csv_dir.is_dir():
            return {"seeded": False}
        imported = 0
        for kind in importer.KINDS:
            path = csv_dir / f"{kind}.csv"
            if not path.is_file():
                continue
            try:
                result = importer.import_csv(conn, kind, path.read_bytes(), str(path))
            except importer.ImportValidationError as exc:
                print(f"  跳过 {path.name}：{exc.summary()}")
                continue
            imported += 0 if result["skipped"] else result["rows"]
        if imported:
            reconciler.reconcile(conn)
        return {"seeded": bool(imported), "rows": imported}
    finally:
        conn.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="MoneyTrail 资金链路核查台")
    parser.add_argument("--host", default=config.HOST)
    parser.add_argument("--port", type=int, default=config.PORT)
    parser.add_argument("--db", default=str(config.DB_PATH))
    args = parser.parse_args(argv)

    boot = bootstrap(args.db)
    if boot.get("seeded"):
        print(f"  已从 data/*.csv 自动导入 {boot['rows']} 行并完成首次对账")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    server.db_path = args.db
    conn = models.connect_ready(args.db)
    stats = reconciler.stats(conn)
    conn.close()
    print(f"{config.APP_NAME} v{config.VERSION} · {config.APP_SUBTITLE}")
    print(f"  数据库 : {args.db}")
    print(f"  台账   : orders {stats['order_count']} / settlements "
          f"{stats['settlement_count']} / refunds {stats['refund_count']}")
    print(f"  工单   : {stats['all_count']} 笔，未结缺口 {stats['open_gap_fen']} 分")
    print(f"  基准时点 T = {stats['reference_time'] or '（无数据）'}")
    print(f"  服务地址: http://{args.host}:{args.port}   (Ctrl+C 停止)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main())
