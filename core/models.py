"""SQLite 表定义与读写。

单一职责：连接、建表、以及所有跨模块复用的原子读写。
这里不含任何业务判断，四类规则见 detectors.py。
"""

import sqlite3
from datetime import datetime
from pathlib import Path

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id    TEXT PRIMARY KEY,
    channel     TEXT    NOT NULL,
    paid_at     TEXT    NOT NULL,
    amount_fen  INTEGER NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'PAID',
    batch_id    INTEGER
);

CREATE TABLE IF NOT EXISTS refunds (
    refund_id   TEXT PRIMARY KEY,
    order_id    TEXT    NOT NULL,
    refunded_at TEXT    NOT NULL,
    amount_fen  INTEGER NOT NULL,
    reason      TEXT    NOT NULL DEFAULT '',
    batch_id    INTEGER
);

CREATE TABLE IF NOT EXISTS settlements (
    settlement_id TEXT PRIMARY KEY,
    order_id      TEXT    NOT NULL,
    channel       TEXT    NOT NULL,
    settled_at    TEXT    NOT NULL,
    gross_fen     INTEGER NOT NULL,
    fee_fen       INTEGER NOT NULL,
    refund_fen    INTEGER NOT NULL DEFAULT 0,
    net_fen       INTEGER NOT NULL,
    batch_id      INTEGER
);

CREATE TABLE IF NOT EXISTS channel_rates (
    channel TEXT PRIMARY KEY,
    rate    REAL    NOT NULL,
    rate_bp INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS anomalies (
    anomaly_id   TEXT PRIMARY KEY,
    order_id     TEXT    NOT NULL,
    label_code   TEXT    NOT NULL,
    severity     TEXT    NOT NULL,
    expected_fen INTEGER NOT NULL DEFAULT 0,
    actual_fen   INTEGER NOT NULL DEFAULT 0,
    diff_fen     INTEGER NOT NULL DEFAULT 0,
    gap_fen      INTEGER NOT NULL DEFAULT 0,
    evidence_json TEXT   NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'pending',
    note         TEXT    NOT NULL DEFAULT '',
    first_seen_at TEXT   NOT NULL,
    updated_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS import_batches (
    batch_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    file_hash   TEXT    NOT NULL,
    kind        TEXT    NOT NULL,
    filename    TEXT    NOT NULL DEFAULT '',
    rows        INTEGER NOT NULL,
    imported_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_paid_at    ON orders(paid_at);
CREATE INDEX IF NOT EXISTS idx_refunds_order     ON refunds(order_id);
CREATE INDEX IF NOT EXISTS idx_settlements_order ON settlements(order_id);
CREATE INDEX IF NOT EXISTS idx_anomalies_status  ON anomalies(status);
CREATE INDEX IF NOT EXISTS idx_anomalies_label   ON anomalies(label_code);
CREATE INDEX IF NOT EXISTS idx_anomalies_order   ON anomalies(order_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_batches_hash ON import_batches(file_hash, kind);
"""


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect(db_path=None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else config.DB_PATH
    if path != Path(":memory:"):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection, *, reset: bool = False) -> None:
    if reset:
        for table in ("anomalies", "settlements", "refunds", "orders",
                      "import_batches", "settings", "channel_rates"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.executescript(SCHEMA)
    conn.commit()
    if not get_setting(conn, "seeded"):
        set_setting(conn, "seeded", "1")


def connect_ready(db_path=None, *, reset: bool = False) -> sqlite3.Connection:
    conn = connect(db_path)
    init_db(conn, reset=reset)
    return conn


# ---------------------------------------------------------------- settings

def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    conn.commit()


# ---------------------------------------------------------------- 基础读写

def insert_rows(conn: sqlite3.Connection, table: str, rows: list) -> int:
    if not rows:
        return 0
    columns = list(rows[0].keys())
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
    conn.executemany(sql, [tuple(row[c] for c in columns) for row in rows])
    return len(rows)


def count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


def load_rates(conn: sqlite3.Connection) -> dict:
    """返回 {channel: {'rate': float, 'rate_bp': int}}。"""
    rows = conn.execute("SELECT channel, rate, rate_bp FROM channel_rates").fetchall()
    return {row["channel"]: {"rate": row["rate"], "rate_bp": row["rate_bp"]} for row in rows}


def seed_rates(conn: sqlite3.Connection, rates: dict = None) -> None:
    rates = rates or config.DEFAULT_RATE_BP
    conn.executemany(
        "INSERT INTO channel_rates(channel, rate, rate_bp) VALUES(?, ?, ?) "
        "ON CONFLICT(channel) DO UPDATE SET rate = excluded.rate, rate_bp = excluded.rate_bp",
        [(channel, bp / 10000, bp) for channel, bp in rates.items()],
    )
    conn.commit()


def get_order(conn: sqlite3.Connection, order_id: str):
    return conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()


def get_refunds(conn: sqlite3.Connection, order_id: str) -> list:
    return conn.execute(
        "SELECT * FROM refunds WHERE order_id = ? ORDER BY refunded_at, refund_id",
        (order_id,),
    ).fetchall()


def get_settlement(conn: sqlite3.Connection, order_id: str):
    return conn.execute(
        "SELECT * FROM settlements WHERE order_id = ? ORDER BY settled_at, settlement_id LIMIT 1",
        (order_id,),
    ).fetchone()


def get_settlement_by_id(conn: sqlite3.Connection, settlement_id: str):
    return conn.execute(
        "SELECT * FROM settlements WHERE settlement_id = ?", (settlement_id,)
    ).fetchone()


def count_settlements(conn: sqlite3.Connection, order_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM settlements WHERE order_id = ?", (order_id,)
    ).fetchone()
    return int(row["n"])


def reference_time(conn: sqlite3.Connection) -> str:
    """判定基准时点 T = 数据集中最新流水时间。

    统一用 T 而不是「此刻」，避免把正常在途订单误判为未到账。
    """
    row = conn.execute(
        "SELECT MAX(t) AS t FROM ("
        "  SELECT MAX(paid_at) AS t FROM orders"
        "  UNION ALL SELECT MAX(refunded_at) FROM refunds"
        "  UNION ALL SELECT MAX(settled_at) FROM settlements)"
    ).fetchone()
    return (row["t"] if row and row["t"] else "")


def get_anomaly(conn: sqlite3.Connection, anomaly_id: str):
    return conn.execute("SELECT * FROM anomalies WHERE anomaly_id = ?", (anomaly_id,)).fetchone()


def list_anomalies(conn: sqlite3.Connection, *, label: str = "", severity: str = "",
                   status: str = "", date_from: str = "", date_to: str = "",
                   limit: int = 0, offset: int = 0) -> list:
    where, params = _anomaly_filter(label, severity, status, date_from, date_to)
    sql = (
        "SELECT a.*, o.channel, o.paid_at, o.amount_fen FROM anomalies a "
        "LEFT JOIN orders o ON o.order_id = a.order_id"
        f" WHERE {where}"
        " ORDER BY CASE a.severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,"
        "          ABS(a.gap_fen) DESC, a.order_id"
    )
    if limit:
        sql += " LIMIT ? OFFSET ?"
        params += [limit, offset]
    return conn.execute(sql, params).fetchall()


def count_anomalies(conn: sqlite3.Connection, *, label: str = "", severity: str = "",
                    status: str = "", date_from: str = "", date_to: str = "") -> int:
    where, params = _anomaly_filter(label, severity, status, date_from, date_to)
    sql = (
        "SELECT COUNT(*) AS n FROM anomalies a "
        "LEFT JOIN orders o ON o.order_id = a.order_id "
        f"WHERE {where}"
    )
    return conn.execute(sql, params).fetchone()["n"]


def _anomaly_filter(label, severity, status, date_from, date_to):
    """按订单支付日期（o.paid_at 的前 10 位）做时间范围过滤。"""
    clauses, params = [], []
    if label:
        clauses.append("a.label_code = ?")
        params.append(label)
    if severity:
        clauses.append("a.severity = ?")
        params.append(severity)
    if status:
        clauses.append("a.status = ?")
        params.append(status)
    if date_from:
        clauses.append("substr(o.paid_at, 1, 10) >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("substr(o.paid_at, 1, 10) <= ?")
        params.append(date_to)
    return (" AND ".join(clauses) if clauses else "1=1"), params
