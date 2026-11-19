"""测试共用夹具：临时数据库、CSV 文本构造、直插台账行。

每个用例都在 tempfile.TemporaryDirectory() 里建独立库，绝不读写仓库根目录的
moneytrail.db，也不写 data/ 下的任何文件（只读读取）。
"""

import csv
import io
import os
import tempfile
import unittest
from pathlib import Path

import config
from core import importer, models

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

# 固定的判定基准时点，避免用例结果随「今天」漂移。
REFERENCE_TIME = "2026-09-22 10:00:00"


def ai_offline() -> dict:
    """把助手强制拉回模板路径：关掉免鉴权、不留 Key。

    单元测试不许依赖 127.0.0.1:8766 这类真实端点，否则 CI 上会因网络而红。
    """
    keys = (config.BAIZE_API_KEY_ENV, config.BAIZE_NO_AUTH_ENV)
    saved = {key: os.environ.get(key) for key in keys}
    os.environ.pop(config.BAIZE_API_KEY_ENV, None)
    os.environ[config.BAIZE_NO_AUTH_ENV] = "0"
    return saved


def ai_env_restore(saved: dict) -> None:
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


class TempDBTest(unittest.TestCase):
    """每个测试方法一个独立的临时 SQLite 库，并预置渠道费率。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)  # addCleanup 后进先出：连接先关，目录后删
        self.tmpdir = Path(self._tmp.name)
        self.db_path = self.tmpdir / "moneytrail_test.db"
        self.conn = models.connect_ready(self.db_path)
        self.addCleanup(self.conn.close)
        models.seed_rates(self.conn)


def csv_text(kind, rows, *, header=None) -> str:
    """按 importer.COLUMNS 的列序拼一段 CSV 文本。"""
    fields = list(header if header is not None else importer.COLUMNS[kind])
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(fields)
    for row in rows:
        writer.writerow([row.get(field, "") for field in fields])
    return buffer.getvalue()


# ---------------------------------------------------------------- 直插台账


def add_order(conn, order_id, *, channel="wechat", paid_at="2026-09-19 10:00:00",
              amount_fen=10000, status="PAID"):
    models.insert_rows(conn, "orders", [{
        "order_id": order_id,
        "channel": channel,
        "paid_at": paid_at,
        "amount_fen": amount_fen,
        "status": status,
    }])
    conn.commit()


def add_refund(conn, refund_id, order_id, *, refunded_at="2026-09-19 18:00:00",
               amount_fen=1000, reason=""):
    models.insert_rows(conn, "refunds", [{
        "refund_id": refund_id,
        "order_id": order_id,
        "refunded_at": refunded_at,
        "amount_fen": amount_fen,
        "reason": reason,
    }])
    conn.commit()


def add_settlement(conn, settlement_id, order_id, *, channel="wechat",
                   settled_at="2026-09-20 10:20:00", gross_fen=10000,
                   fee_fen=60, refund_fen=0, net_fen=None):
    if net_fen is None:
        net_fen = gross_fen - fee_fen - refund_fen
    models.insert_rows(conn, "settlements", [{
        "settlement_id": settlement_id,
        "order_id": order_id,
        "channel": channel,
        "settled_at": settled_at,
        "gross_fen": gross_fen,
        "fee_fen": fee_fen,
        "refund_fen": refund_fen,
        "net_fen": net_fen,
    }])
    conn.commit()
