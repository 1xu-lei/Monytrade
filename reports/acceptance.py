"""MoneyTrail 验收脚本：起真实 HTTP 服务，按介绍书 §11 验收清单逐条实测。

    python reports/acceptance.py

在临时数据库副本 + 独立端口 8124 上跑，不碰 moneytrail.db 与 data/。
"""

import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
REPORT_DIR = Path(__file__).resolve().parent
LOG_PATH = REPORT_DIR / "acceptance.log"
PORT = 8124
HOST = "127.0.0.1"
ROOT = f"http://{HOST}:{PORT}"

results = []


def record(name, ok, evidence):
    results.append((name, bool(ok), str(evidence)))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


opener = urllib.request.build_opener(NoRedirect)


def fetch(path, data=None, headers=None, method=None):
    url = path if path.startswith("http") else ROOT + path
    # 只对幂等 GET 重试一次：POST 里 /ai/toggle 是开关、状态流转是写入，重放会污染断言
    attempts = 2 if data is None else 1
    for attempt in range(attempts):
        req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        try:
            with opener.open(req, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()
        except OSError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.2)


def multipart(fields, files):
    boundary = "----MoneyTrailBoundary7d91"
    parts = []
    for key, value in fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode()
        )
    for key, (filename, blob) in files.items():
        parts.append(
            (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"; "
             f"filename=\"{filename}\"\r\nContent-Type: text/csv\r\n\r\n").encode() + blob + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def port_listening():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((HOST, PORT)) == 0


def start_server(db_path, env=None):
    # 上一轮残留监听会让新服务抢不到端口：Windows 下 HTTPServer 默认
    # allow_reuse_address，两个监听可同时绑上，请求被随机分给旧进程，
    # 表现为某条断言莫名超时。先等端口真空出来再起。
    for _ in range(50):
        if not port_listening():
            break
        time.sleep(0.1)
    else:
        raise RuntimeError(f"端口 {PORT} 在 5 秒内未释放，无法启动验收服务")
    proc = subprocess.Popen(
        [sys.executable, "app.py", "--port", str(PORT), "--db", str(db_path)],
        cwd=str(BASE_DIR),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(100):
        try:
            status, _, _ = fetch("/health")
            if status == 200:
                return proc
        except OSError:
            pass
        time.sleep(0.1)
    proc.terminate()
    raise RuntimeError("服务未能在 10 秒内就绪")


def stop_server(proc):
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def main():
    workdir = tempfile.mkdtemp(prefix="mt_accept_")
    db_copy = Path(workdir) / "accept.db"
    shutil.copy2(BASE_DIR / "moneytrail.db", db_copy)
    # 结果要可复现：验收进程强制走「模型不可用」的降级路径，不依赖 8766 端点在线。
    offline = dict(os.environ)
    offline.pop("BAIZE_API_KEY", None)
    offline["BAIZE_NO_AUTH"] = "0"
    proc = start_server(db_copy, offline)
    try:
        # 01 总览
        status, headers, body = fetch("/")
        text = body.decode("utf-8")
        record("01 GET / 总览四个数字",
               status == 200 and "订单总额" in text and "待核差异" in text and "异常" in text,
               f"{status} · {len(body)} 字节")

        # 02 异常中心 + 三条件筛选
        status, _, body = fetch("/anomalies")
        base_text = body.decode("utf-8")
        status2, _, body2 = fetch("/anomalies?label=NOT_SETTLED&severity=high&status=pending")
        filtered = body2.decode("utf-8")
        ids_all = set(re.findall(r"AN-[A-Z]{2}-MT[\d\-]+", base_text))
        ids_filtered = set(re.findall(r"AN-[A-Z]{2}-MT[\d\-]+", filtered))
        record("02 GET /anomalies 三条件筛选",
               status == 200 and status2 == 200 and ids_filtered and ids_filtered <= ids_all
               and ids_filtered != ids_all,
               f"全量 {len(ids_all)} 笔 → 筛选后 {len(ids_filtered)} 笔")

        # 03 资金链路（真实订单 + 404）
        order_id = "MT20260921-001"
        status, _, body = fetch(f"/trail/{order_id}")
        trail_text = body.decode("utf-8")
        bad_status, _, _ = fetch("/trail/MT-NOT-EXIST")
        record("03 GET /trail 五节点链路与 404",
               status == 200 and "结算到账" in trail_text and bad_status == 404,
               f"正常 {status} / 不存在 {bad_status}")

        # 04 导入页 / 助手页 / 静态资源 / health
        s_import, _, _ = fetch("/import")
        s_assist, _, body = fetch("/assistant?q=" + urllib.parse.quote("今天哪几笔没到账"))
        s_css, h_css, _ = fetch("/static/style.css")
        s_health, _, body_h = fetch("/health")
        health = json.loads(body_h.decode("utf-8"))
        record("04 页面与健康检查 200",
               all(s == 200 for s in (s_import, s_assist, s_css, s_health))
               and health["orders"] == 24 and health["anomalies"] == 9,
               f"import={s_import} assistant={s_assist} css={s_css} health={s_health} "
               f"orders={health['orders']} anomalies={health['anomalies']}")

        # 05 助手无 API Key 走确定性兜底
        record("05 无 API Key 时助手不 500",
               s_assist == 200 and "未到账" in body.decode("utf-8"),
               f"{s_assist} · 含确定性答案")

        # 06 对账幂等（连续两次工单数不变）
        conn = sqlite3.connect(db_copy)
        before = conn.execute("SELECT COUNT(*) FROM anomalies").fetchone()[0]
        fetch("/reconcile", data=b"", method="POST")
        after1 = conn.execute("SELECT COUNT(*) FROM anomalies").fetchone()[0]
        fetch("/reconcile", data=b"", method="POST")
        after2 = conn.execute("SELECT COUNT(*) FROM anomalies").fetchone()[0]
        record("06 POST /reconcile 幂等",
               before == after1 == after2 == 9, f"{before} → {after1} → {after2}")

        # 07 状态流转 + 重启后持久化
        target = conn.execute(
            "SELECT anomaly_id FROM anomalies WHERE status='pending' ORDER BY anomaly_id LIMIT 1").fetchone()[0]
        form = b"status=in_progress&return_to=/anomalies"
        status, headers_loc, body = fetch(
            f"/anomalies/{target}/status", data=form, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        db_status = conn.execute("SELECT status FROM anomalies WHERE anomaly_id=?", (target,)).fetchone()[0]
        loc = headers_loc.get("Location", "")
        record("07 POST 状态流转落库",
               status == 303 and db_status == "in_progress"
               and "处理中" in urllib.parse.unquote(loc),
               f"{status} · loc={urllib.parse.unquote(loc)[:60]} · db={db_status}")

        stop_server(proc)
        proc = start_server(db_copy, offline)
        conn.close()
        conn = sqlite3.connect(db_copy)
        db_status_after = conn.execute(
            "SELECT status FROM anomalies WHERE anomaly_id=?", (target,)).fetchone()[0]
        _, _, page = fetch("/anomalies?status=in_progress")
        record("08 重启后状态仍在",
               db_status_after == "in_progress" and "处理中" in page.decode("utf-8"),
               f"重启后 db={db_status_after}")

        # 09 导出四件
        s_md, h_md, b_md = fetch(f"/export/order/{order_id}.md")
        s_csv, h_csv, b_csv = fetch(f"/export/order/{order_id}.csv")
        s_bmd, h_bmd, b_bmd = fetch("/export/batch.md")
        s_bcsv, h_bcsv, b_bcsv = fetch("/export/batch.csv")
        disps = [h.get("Content-Disposition", "") for h in (h_md, h_csv, h_bmd, h_bcsv)]
        record("09 导出 Markdown / CSV 四件",
               all(s == 200 for s in (s_md, s_csv, s_bmd, s_bcsv))
               and all("attachment" in d for d in disps)
               and "流水号" in b_md.decode("utf-8")
               and b_csv.decode("utf-8").count("\n") > 1,
               f"md={s_md} csv={s_csv} batch.md={s_bmd} batch.csv={s_bcsv}")

        # 10 同文件二次导入被哈希识别
        blob = (BASE_DIR / "data" / "orders.csv").read_bytes()
        body_mp, ctype = multipart({"kind": "orders"}, {"file": ("orders.csv", blob)})
        s1, h1, _ = fetch("/import", data=body_mp, method="POST",
                          headers={"Content-Type": ctype})
        body_mp2, ctype2 = multipart({"kind": "orders"}, {"file": ("orders.csv", blob)})
        s2, h2, _ = fetch("/import", data=body_mp2, method="POST",
                          headers={"Content-Type": ctype2})
        loc2 = urllib.parse.unquote(h2.get("Location", ""))
        record("10 重复导入被哈希识别跳过",
               s1 == 303 and s2 == 303 and "已导入过" in loc2,
               f"{s1}/{s2} · {loc2[:70]}")

        # 11 缺列文件被拒并按行号报错
        bad_csv = b"order_id,channel,amount\nMT20260919-001,wechat,1000\n"
        body_mp3, ctype3 = multipart({"kind": "orders"}, {"file": ("bad.csv", bad_csv)})
        s3, _, b3 = fetch("/import", data=body_mp3, method="POST",
                          headers={"Content-Type": ctype3})
        record("11 缺列文件被拒并报行号",
               s3 == 400 and "行" in b3.decode("utf-8"),
               f"{s3} · 含行号提示")

        # 12 熔断开关（无 API Key 时 /health 的 ai 恒为 false，改看设置项与页面提示）
        def kill_switch():
            row = conn.execute(
                "SELECT value FROM settings WHERE key='ai_kill_switch'").fetchone()
            return row[0] if row else "0"

        ks_before = kill_switch()
        r_status, r_head, _ = fetch("/ai/toggle", data=b"", method="POST")
        ks_after = kill_switch()
        _, _, a_page = fetch("/assistant")
        record("12 POST /ai/toggle 熔断生效",
               r_status == 303 and ks_before != ks_after
               and "熔断" in urllib.parse.unquote(r_head.get("Location", ""))
               and "熔断" in a_page.decode("utf-8"),
               f"ai_kill_switch {ks_before} → {ks_after}")

        # 13 检出率 / 误报率对照 ground_truth
        truth = json.loads((BASE_DIR / "data" / "ground_truth.json").read_text(encoding="utf-8"))
        expected = {(a["label_code"], a["order_id"]) for a in truth["expected_anomalies"]}
        detected = {(row[0], row[1]) for row in conn.execute(
            "SELECT label_code, order_id FROM anomalies").fetchall()}
        hit, miss, extra = expected & detected, expected - detected, detected - expected
        record("13 与 ground_truth 对照",
               not miss and not extra,
               f"应报 {len(expected)} / 实报 {len(detected)} / 命中 {len(hit)} / "
               f"漏报 {len(miss)} / 误报 {len(extra)} → 检出率 "
               f"{len(hit) / len(expected) * 100:.1f}%")

        # 14 库内无浮点金额
        float_cols = []
        for table, cols in (("orders", ("amount_fen",)),
                            ("refunds", ("amount_fen",)),
                            ("settlements", ("gross_fen", "fee_fen", "refund_fen", "net_fen")),
                            ("anomalies", ("gap_fen", "diff_fen"))):
            for col in cols:
                bad = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE typeof({col}) != 'integer'").fetchone()[0]
                if bad:
                    float_cols.append(f"{table}.{col}={bad}")
        record("14 库内金额字段全为整数", not float_cols, float_cols or "全部 INTEGER")
        conn.close()
    finally:
        stop_server(proc)
        shutil.rmtree(workdir, ignore_errors=True)

    lines = ["MoneyTrail 验收实测", "=" * 78]
    for name, ok, evidence in results:
        lines.append(f"[{'PASS' if ok else 'FAIL'}] {name} · {evidence}")
    passed = sum(1 for _, ok, _ in results if ok)
    lines.append("=" * 78)
    lines.append(f"{passed} / {len(results)} PASS")
    out = "\n".join(lines)
    print(out)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(f"\n## {time.strftime('%Y-%m-%d %H:%M:%S')}\n{out}\n")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    raise SystemExit(main())
