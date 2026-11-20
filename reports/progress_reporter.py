"""MoneyTrail 进度报备器：每 30 分钟落一份项目快照。

用法：
    python reports/progress_reporter.py            # 报一次
    python reports/progress_reporter.py --loop 1800 # 每 1800 秒（30 分钟）报一次（常驻）
"""

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
REPORT_DIR = Path(__file__).resolve().parent
LOG_PATH = REPORT_DIR / "进度报备.log"
LATEST_PATH = REPORT_DIR / "latest.md"
SNAP_PATH = REPORT_DIR / ".snapshot.json"
DB_PATH = BASE_DIR / "moneytrail.db"
DEFAULT_PORT = "8000"


def service_port() -> str:
    """探活端口：环境变量 MONEYTRAIL_PORT > 服务启动时写进库的 listen_port > 8000。

    app.py 在默认端口被占时会自动顺延，并把实际端口写进 settings；
    报备器读这里，才不会对着旧端口报「服务不可达」。
    """
    env = os.environ.get("MONEYTRAIL_PORT")
    if env:
        return env
    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=3)
            try:
                row = conn.execute(
                    "SELECT value FROM settings WHERE key = 'listen_port'").fetchone()
            finally:
                conn.close()
            if row and row[0]:
                return str(row[0])
        except sqlite3.Error:
            pass
    return DEFAULT_PORT


def http_health():
    url = f"http://127.0.0.1:{service_port()}/health"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            return f"OK {resp.status}", resp.read(160).decode("utf-8", "replace").replace("\n", " ")
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code}", ""
    except Exception as exc:  # noqa: BLE001 - 报备器不因服务未起而中断
        return f"不可达（{type(exc).__name__}）", ""

# 交付物清单：路径 -> (权重, 说明)。权重相加为 100。
DELIVERABLES = [
    ("app.py", 14, "服务入口"),
    ("config.py", 3, "阈值与常量"),
    ("gen_data.py", 5, "演示数据生成器"),
    ("core/models.py", 5, "表结构与读写"),
    ("core/importer.py", 6, "CSV 导入与校验"),
    ("core/reconciler.py", 6, "对账匹配"),
    ("core/detectors.py", 7, "四类异常检测"),
    ("core/assistant.py", 5, "自然语言助手"),
    ("web/views.py", 6, "页面控制器"),
    ("web/templates/", 5, "服务端模板"),
    ("web/static/style.css", 4, "样式"),
    ("web/svg.py", 3, "手写 SVG 组件"),
    ("web/exporter.py", 4, "核查单导出"),
    ("check.py", 10, "自检脚本 10 条断言"),
    ("run.bat", 3, "一键启动"),
    ("tests/", 6, "单元测试"),
    ("README.md", 4, "启动说明"),
    ("DEMO.md", 2, "三步演示路径"),
    ("NIGHT_LOG.md", 2, "夜间执行记录"),
]

DOC_PATH = BASE_DIR / "MoneyTrail-项目介绍书.md"


def db_stats():
    if not DB_PATH.exists():
        return {"状态": "数据库不存在"}
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=3)
    except sqlite3.Error as exc:
        return {"状态": f"打不开（{exc}）"}
    out = {}
    try:
        cur = conn.cursor()
        for table, label in (
            ("orders", "订单"),
            ("refunds", "退款"),
            ("settlements", "结算"),
            ("anomalies", "异常工单"),
        ):
            try:
                out[label] = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error:
                out[label] = "—"
        try:
            rows = cur.execute(
                "SELECT status, COUNT(*) FROM anomalies GROUP BY status ORDER BY status"
            ).fetchall()
            out["工单状态"] = " / ".join(f"{k}={v}" for k, v in rows) or "—"
        except sqlite3.Error:
            out["工单状态"] = "—"
        try:
            gap = cur.execute(
                "SELECT COALESCE(SUM(gap_fen), 0) FROM anomalies "
                "WHERE status IN ('pending', 'in_progress')"
            ).fetchone()[0]
            out["未结缺口"] = f"{gap} 分（¥{gap / 100:.2f}）"
        except sqlite3.Error:
            out["未结缺口"] = "—"
    finally:
        conn.close()
    return out


def file_sig(path: Path):
    if path.is_dir():
        files = sorted(p for p in path.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
        return {"dir": True, "count": len(files), "size": sum(p.stat().st_size for p in files),
                "mtime": max((p.stat().st_mtime for p in files), default=0.0)}
    if path.is_file():
        st = path.stat()
        return {"dir": False, "count": 1, "size": st.st_size, "mtime": st.st_mtime}
    return None


def survey():
    done, missing, signatures = [], [], {}
    for rel, weight, desc in DELIVERABLES:
        path = BASE_DIR / rel
        sig = file_sig(path)
        if sig is None:
            missing.append((rel, weight, desc))
        else:
            done.append((rel, weight, desc, sig))
            signatures[rel] = sig
    earned = sum(w for _, w, _, _ in done)
    return done, missing, signatures, earned


def doc_status():
    if not DOC_PATH.exists():
        return {"交付状态": "介绍书缺失", "验收清单": "—"}
    text = DOC_PATH.read_text(encoding="utf-8", errors="replace")
    planned = text.count("| 计划 |")
    delivered = text.count("| 已交付 |")
    pending_test = text.count("【待实测】")
    verified = text.count("【实测通过】")
    return {
        "交付状态": f"已交付 {delivered} / 仍为「计划」 {planned}",
        "验收清单": f"已实测通过 {verified} / 仍「待实测」 {pending_test}",
    }


def diff_since_last(signatures):
    prev = {}
    if SNAP_PATH.exists():
        try:
            prev = json.loads(SNAP_PATH.read_text(encoding="utf-8")).get("files", {})
        except (OSError, ValueError):
            prev = {}
    added = [k for k in signatures if k not in prev]
    changed = [
        k for k in signatures
        if k in prev and (prev[k].get("mtime") != signatures[k]["mtime"]
                          or prev[k].get("size") != signatures[k]["size"])
    ]
    removed = [k for k in prev if k not in signatures]
    return added, changed, removed


def log_last_seq() -> int:
    """日志里已出现的最大序号。

    序号以**日志本身**为准：Python 版与 PowerShell 兜底版都会往同一个日志追加，
    各自读自己的快照文件计数就会撞号（实测出现过两条「第 38 次」）。
    """
    if not LOG_PATH.exists():
        return 0
    last = 0
    with LOG_PATH.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.startswith("## 第 "):
                continue
            try:
                number = int(line.split("## 第 ", 1)[1].split(" 次", 1)[0])
            except (IndexError, ValueError):
                continue
            last = max(last, number)
    return last


def collect(seq: int) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    health, health_body = http_health()
    stats = db_stats()
    done, missing, signatures, earned = survey()
    added, changed, removed = diff_since_last(signatures)
    doc = doc_status()

    lines = [
        f"## 第 {seq} 次报备 · {now}",
        f"- 进度：**{earned}/100**（按介绍书交付物加权）",
        f"- 服务：127.0.0.1:{service_port()} → {health}",
        f"- 台账：订单 {stats.get('订单')} / 退款 {stats.get('退款')} / 结算 {stats.get('结算')} / 工单 {stats.get('异常工单')}",
        f"- 工单状态：{stats.get('工单状态')}　未结缺口：{stats.get('未结缺口')}",
        f"- 介绍书：{doc['交付状态']}；{doc['验收清单']}",
    ]
    if missing:
        lines.append("- 缺口（" + str(len(missing)) + "项）："
                     + "、".join(f"{rel}(权重{w})" for rel, w, _ in missing))
    else:
        lines.append("- 缺口：无，交付物齐套")
    if added or changed or removed:
        seg = []
        if added:
            seg.append("新增 " + "、".join(added))
        if changed:
            seg.append("变更 " + "、".join(changed))
        if removed:
            seg.append("消失 " + "、".join(removed))
        lines.append("- 距上次：" + "；".join(seg))
    else:
        lines.append("- 距上次：无文件变化")
    lines.append("")

    SNAP_PATH.write_text(
        json.dumps({"at": now, "seq": seq, "earned": earned, "files": signatures},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="MoneyTrail 进度报备器")
    parser.add_argument("--loop", type=int, default=0, help="循环间隔秒数，0 表示只报一次")
    args = parser.parse_args(argv)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    seq = 0
    if SNAP_PATH.exists():
        try:
            seq = int(json.loads(SNAP_PATH.read_text(encoding="utf-8")).get("seq", 0))
        except (OSError, ValueError):
            seq = 0
    seq = max(seq, log_last_seq())

    while True:
        seq += 1
        block = collect(seq)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(block + "\n")
        LATEST_PATH.write_text("# MoneyTrail 最新进度快照\n\n" + block, encoding="utf-8")
        print(block, flush=True)
        if not args.loop:
            return 0
        time.sleep(max(30, args.loop))


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main())
