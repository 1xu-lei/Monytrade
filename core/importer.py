"""CSV 导入与字段校验。

职责：把三类台账（订单 / 退款 / 结算）读进 SQLite，过程中做到三件事：
1. 字段校验并对「行号」报错（缺列、类型错、金额为负、主键重复）；
2. 单事务写入，任一行失败整体回滚，不留半份数据；
3. 同文件重复导入按 SHA-256 识别并跳过（幂等）。
"""

import csv
import hashlib
import io
import re
import sqlite3
from datetime import datetime

import config
from core import models

KINDS = ("orders", "refunds", "settlements")

KIND_TEXT = {"orders": "订单", "refunds": "退款", "settlements": "结算"}

# 每类文件的必需列，顺序即 CSV 列序。
COLUMNS = {
    "orders": ("order_id", "channel", "paid_at", "amount_fen", "status"),
    "refunds": ("refund_id", "order_id", "refunded_at", "amount_fen", "reason"),
    "settlements": ("settlement_id", "order_id", "channel", "settled_at",
                    "gross_fen", "fee_fen", "refund_fen", "net_fen"),
}

PRIMARY_KEY = {"orders": "order_id", "refunds": "refund_id", "settlements": "settlement_id"}

TIME_FIELD = {"orders": "paid_at", "refunds": "refunded_at", "settlements": "settled_at"}

MONEY_FIELDS = {
    "orders": ("amount_fen",),
    "refunds": ("amount_fen",),
    "settlements": ("gross_fen", "fee_fen", "refund_fen", "net_fen"),
}

INT_RE = re.compile(r"^-?\d+$")
TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S")

# 行内私有 key：保存该行在 CSV 里的真实行号。空行会被跳过，因此不能靠枚举序号推算。
LINE_KEY = "__line__"

# 单份文件最多逐条列出多少处错误，其余合并成一条提示，避免页面被刷爆。
MAX_REPORTED_ERRORS = 20


class ImportValidationError(Exception):
    """字段校验失败：携带逐行错误，调用方负责展示。"""

    def __init__(self, kind: str, errors: list, filename: str = ""):
        self.kind = kind
        self.errors = errors
        self.filename = filename
        super().__init__(self.summary())

    def summary(self) -> str:
        if not self.errors:
            return f"{KIND_TEXT.get(self.kind, self.kind)} 文件校验失败"
        first = self.errors[0]
        return f"{KIND_TEXT.get(self.kind, self.kind)} 文件第 {first['row']} 行：{first['message']}"


def _error(row: int, field: str, message: str) -> dict:
    return {"row": row, "field": field, "message": message}


def normalize_time(text: str):
    """把常见时间写法统一成 'YYYY-MM-DD HH:MM:SS'；失败返回 None。"""
    text = (text or "").strip()
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None


def _decode(content) -> str:
    if isinstance(content, str):
        return content
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ImportValidationError("", [_error(1, "", "文件编码无法识别，请使用 UTF-8 或 GBK")])


def parse_and_validate(kind: str, text: str, filename: str = "") -> list:
    """解析并校验，返回规范化后的行列表；有错直接抛 ImportValidationError。

    行号约定：表头是第 1 行，第一条数据是第 2 行，与用 Excel 看到的行号一致。
    """
    if kind not in COLUMNS:
        raise ImportValidationError(kind, [_error(1, "kind", f"未知的台账类型：{kind}")], filename)

    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise ImportValidationError(kind, [_error(1, "", "文件为空，缺少表头")], filename)

    header = [cell.strip().lstrip("\ufeff") for cell in header]
    required = COLUMNS[kind]
    missing = [col for col in required if col not in header]
    if missing:
        raise ImportValidationError(
            kind,
            [_error(1, missing[0], f"缺少必需列：{', '.join(missing)}")],
            filename,
        )

    index = {col: header.index(col) for col in required}

    rows, errors = [], []
    seen_keys, seen_pairs = set(), set()
    pk = PRIMARY_KEY[kind]
    time_field = TIME_FIELD[kind]

    for line_no, raw in enumerate(reader, start=2):
        if not any(cell.strip() for cell in raw):
            continue  # 跳过空行，不报错
        if len(raw) < len(header):
            errors.append(_error(line_no, "", f"列数不足：期望 {len(header)} 列，实际 {len(raw)} 列"))
            continue

        record = {col: raw[index[col]].strip() for col in required}

        for col in required:
            if record[col] == "" and col not in ("reason", "status"):
                errors.append(_error(line_no, col, f"字段 {col} 不能为空"))

        moment = normalize_time(record[time_field])
        if moment is None:
            errors.append(_error(line_no, time_field,
                                 f"字段 {time_field} 时间格式无法识别：{record[time_field]!r}"))
        else:
            record[time_field] = moment

        for money_field in MONEY_FIELDS[kind]:
            value = record.get(money_field, "")
            if not INT_RE.match(value):
                errors.append(_error(line_no, money_field,
                                     f"字段 {money_field} 必须是整数分，实际 {value!r}"))
                continue
            number = int(value)
            if number < 0:
                errors.append(_error(line_no, money_field,
                                     f"字段 {money_field} 不能为负数：{number} 分"))
                continue
            record[money_field] = number

        if kind == "orders":
            if record["channel"] not in config.CHANNELS:
                errors.append(_error(line_no, "channel",
                                     f"未知渠道 {record['channel']!r}，应为 "
                                     f"{'/'.join(config.CHANNELS)}"))
            record["status"] = (record["status"] or "PAID").upper()

        if kind == "settlements" and record["channel"] not in config.CHANNELS:
            errors.append(_error(line_no, "channel",
                                 f"未知渠道 {record['channel']!r}，应为 "
                                 f"{'/'.join(config.CHANNELS)}"))

        if kind == "refunds":
            record["reason"] = record.get("reason") or ""

        key = record[pk]
        if key in seen_keys:
            errors.append(_error(line_no, pk, f"文件内主键重复：{key}"))
            continue
        seen_keys.add(key)

        # 同一订单在同一秒出现两条退款，是明显的重复粘贴
        if kind == "refunds":
            pair = (record["order_id"], record["refunded_at"], record["amount_fen"])
            if pair in seen_pairs:
                errors.append(_error(line_no, "refund_id", "同订单、同时间、同金额的退款记录重复"))
                continue
            seen_pairs.add(pair)

        rows.append(dict(record, **{LINE_KEY: line_no}))

    if errors:
        errors.sort(key=lambda item: (item["row"], item["field"]))
        raise ImportValidationError(kind, errors, filename)
    if not rows:
        raise ImportValidationError(kind, [_error(2, "", "文件只有表头，没有数据行")], filename)
    return rows


def _existing_keys(conn, kind: str, rows: list) -> set:
    pk = PRIMARY_KEY[kind]
    keys = [row[pk] for row in rows]
    found = set()
    for start in range(0, len(keys), 400):
        chunk = keys[start:start + 400]
        marks = ", ".join("?" for _ in chunk)
        sql = f"SELECT {pk} AS k FROM {kind} WHERE {pk} IN ({marks})"
        found.update(existing["k"] for existing in conn.execute(sql, chunk))
    return found


def import_csv(conn, kind: str, content, filename: str = "", *, record_batch: bool = True) -> dict:
    """导入一个 CSV。返回 {'kind','rows','skipped','batch_id','hash'}。

    校验失败抛 ImportValidationError，事务回滚，库里不留半份数据。
    """
    text = _decode(content)
    digest = hashlib.sha256((text.encode("utf-8"))).hexdigest()

    rows = parse_and_validate(kind, text, filename)

    if record_batch:
        dup = conn.execute(
            "SELECT batch_id, imported_at FROM import_batches WHERE file_hash = ? AND kind = ?",
            (digest, kind),
        ).fetchone()
        if dup:
            return {"kind": kind, "rows": 0, "skipped": True, "batch_id": dup["batch_id"],
                    "hash": digest, "filename": filename, "reason": "同一文件已导入过（哈希一致）"}

    pk = PRIMARY_KEY[kind]
    already = _existing_keys(conn, kind, rows)
    if already:
        # 用每行自带的原始行号（空行会被跳过，不能按枚举序号推算）。
        errors = [
            _error(row[LINE_KEY], pk, f"{KIND_TEXT[kind]}主键已存在于库中：{row[pk]}")
            for row in rows if row[pk] in already
        ]
        if len(errors) > MAX_REPORTED_ERRORS:
            hidden = len(errors) - MAX_REPORTED_ERRORS
            errors = errors[:MAX_REPORTED_ERRORS] + [
                _error(errors[MAX_REPORTED_ERRORS - 1]["row"], "",
                       f"另有 {hidden} 行主键冲突未逐条列出")]
        raise ImportValidationError(kind, errors, filename)

    try:
        with conn:  # 单事务：要么全部写入，要么整体回滚
            batch_id = None
            if record_batch:
                cursor = conn.execute(
                    "INSERT INTO import_batches(file_hash, kind, filename, rows, imported_at) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (digest, kind, filename, len(rows), models.now_text()),
                )
                batch_id = cursor.lastrowid
            # 写入前去掉行内私有 key，保证入库列与 COLUMNS 完全一致。
            payload = [{key: value for key, value in row.items() if key != LINE_KEY}
                       | {"batch_id": batch_id} for row in rows]
            models.insert_rows(conn, kind, payload)
    except sqlite3.DatabaseError as exc:
        raise ImportValidationError(kind, [_error(2, "", f"写入失败，已回滚：{exc}")], filename)

    return {"kind": kind, "rows": len(rows), "skipped": False, "batch_id": batch_id,
            "hash": digest, "filename": filename, "reason": ""}


def import_path(conn, kind: str, path, *, record_batch: bool = True) -> dict:
    with open(path, "rb") as handle:
        content = handle.read()
    return import_csv(conn, kind, content, filename=str(path), record_batch=record_batch)


def recent_batches(conn, limit: int = 12) -> list:
    return conn.execute(
        "SELECT * FROM import_batches ORDER BY batch_id DESC LIMIT ?", (limit,)
    ).fetchall()
