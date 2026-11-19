"""自然语言助手：只理解语言，不负责算数。

严格三段式（项目介绍书 4.3）：

    用户提问 → [模型] 只输出意图 JSON → [程序] 按意图生成 SQL 并执行
             → [程序] 结果已确定 → [模型] 只做措辞组织

四条铁律的落地方式：
1. 金额、笔数全部来自 SQL 查询结果，模型只拿到「事实清单」；
2. 模型写出的每句话都要过数字校验 —— 出现事实清单以外的数字即作废，
   自动回落到模板答案（因此回答里不可能出现模型瞎编的金额）；
3. 只支持账本内的事实，其余一律拒答；
4. 模型不可用（未配置 / 被熔断 / 超时 / 返回非法）时，导入、对账、追踪、
   导出全部照常，助手本身回落到模板答案，不阻塞任何核心路径。
"""

import json
import os
import re
import urllib.error
import urllib.request

import config
from core import models, reconciler
from core.money import fen_to_yuan, yuan_mark

ORDER_ID_RE = re.compile(r"\b([A-Z]{2}\d{8}-\d{3})\b")

INTENT_SCHEMA = ('{"intent":"unsettled_orders|explain_order|fee_overcharge|out_of_scope",'
                 '"order_id":"订单号或空字符串","month":"YYYY-MM 或空字符串",'
                 '"confidence":0.0}')

SQL = {
    "unsettled_orders": """SELECT o.order_id, o.channel, o.paid_at, o.amount_fen
FROM orders o
LEFT JOIN settlements s ON s.order_id = o.order_id
WHERE s.order_id IS NULL
ORDER BY o.paid_at""",
    "explain_order": """SELECT a.anomaly_id, a.label_code, a.severity, a.gap_fen
FROM anomalies a WHERE a.order_id = ?""",
    "fee_overcharge": """SELECT a.order_id, o.channel, o.paid_at, a.expected_fen, a.actual_fen, a.gap_fen
FROM anomalies a JOIN orders o ON o.order_id = a.order_id
WHERE a.label_code = 'FEE_ANOMALY' AND substr(o.paid_at, 1, 7) = ?
ORDER BY a.gap_fen DESC""",
}

SUPPORTED = ("unsettled_orders", "explain_order", "fee_overcharge")


# ---------------------------------------------------------------- 模型接入

def api_key() -> str:
    return os.environ.get(config.BAIZE_API_KEY_ENV, "").strip()


def base_url() -> str:
    return os.environ.get(config.BAIZE_BASE_URL_ENV, config.DEFAULT_BAIZE_BASE_URL).rstrip("/")


def model_name() -> str:
    return os.environ.get(config.BAIZE_MODEL_ENV, config.DEFAULT_BAIZE_MODEL)


def no_auth() -> bool:
    """本机端点免鉴权（默认）；换云端端点时需同时给出 API Key。"""
    return os.environ.get(config.BAIZE_NO_AUTH_ENV, config.DEFAULT_BAIZE_NO_AUTH) == "1"


def kill_switch(conn) -> bool:
    return models.get_setting(conn, config.AI_KILL_SWITCH_KEY, "0") == "1"


def set_kill_switch(conn, enabled: bool) -> None:
    models.set_setting(conn, config.AI_KILL_SWITCH_KEY, "1" if enabled else "0")


def ai_status(conn) -> dict:
    key = api_key()
    killed = kill_switch(conn)
    if killed:
        return {"available": False, "reason": "已开启「模拟 AI 不可用」熔断开关"}
    if not no_auth() and not key:
        return {"available": False, "reason": f"未配置环境变量 {config.BAIZE_API_KEY_ENV}"}
    return {"available": True, "reason": "模型服务已接入", "model": model_name(),
            "endpoint": base_url(), "no_auth": no_auth()}


def _chat(messages: list):
    """调用 OpenAI 兼容接口；任何失败都返回 None，绝不抛异常。"""
    if not no_auth() and not api_key():
        return None
    headers = {"Content-Type": "application/json"}
    if api_key():
        headers["Authorization"] = f"Bearer {api_key()}"
    payload = json.dumps({"model": model_name(), "messages": messages,
                          "temperature": 0}).encode("utf-8")
    request = urllib.request.Request(f"{base_url()}/chat/completions", data=payload,
                                     headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=config.AI_TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode("utf-8"))
        return body["choices"][0]["message"]["content"]
    except (urllib.error.URLError, OSError, KeyError, IndexError, ValueError, TypeError):
        return None


# ---------------------------------------------------------------- 第一段：意图

def keyword_intent(question: str) -> dict:
    """不依赖模型的意图识别（降级路径，也是唯一的兜底）。"""
    text = question or ""
    match = ORDER_ID_RE.search(text.upper())
    if match:
        return {"intent": "explain_order", "order_id": match.group(1), "month": "",
                "confidence": 0.9}
    if any(word in text for word in ("手续费", "费率", "扣点", "抽成")):
        return {"intent": "fee_overcharge", "order_id": "", "month": "", "confidence": 0.8}
    if any(word in text for word in ("没到账", "未到账", "没到", "未结算", "断链", "没到卡")):
        return {"intent": "unsettled_orders", "order_id": "", "month": "", "confidence": 0.8}
    return {"intent": "out_of_scope", "order_id": "", "month": "", "confidence": 0.0}


def parse_intent(conn, question: str) -> dict:
    status = ai_status(conn)
    if not status["available"]:
        intent = keyword_intent(question)
        intent["by"] = "keyword"
        return intent
    prompt = (
        "你是资金核查系统的意图解析器。只输出一行 JSON，不要解释，不要代码块。\n"
        f"JSON 结构：{INTENT_SCHEMA}\n"
        "可用意图：unsettled_orders=问哪些订单已支付但没到账；"
        "explain_order=问某一笔订单为什么对不上（必须给出 order_id）；"
        "fee_overcharge=问手续费/费率多收；其他一律 out_of_scope。\n"
        f"用户问题：{question}"
    )
    reply = _chat([{"role": "user", "content": prompt}])
    intent = _parse_intent_json(reply)
    if intent is None:
        intent = keyword_intent(question)
        intent["by"] = "keyword"
    else:
        intent["by"] = "model"
    return intent


def _parse_intent_json(reply):
    if not reply:
        return None
    text = reply.strip().strip("`")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    intent = str(data.get("intent", "")).strip()
    if intent not in SUPPORTED + ("out_of_scope",):
        return None
    order_id = str(data.get("order_id") or "").strip().upper()
    month = str(data.get("month") or "").strip()
    if intent == "explain_order" and not ORDER_ID_RE.fullmatch(order_id):
        return None
    if month and not re.fullmatch(r"\d{4}-\d{2}", month):
        month = ""
    return {"intent": intent, "order_id": order_id, "month": month,
            "confidence": float(data.get("confidence") or 0.0)}


# ---------------------------------------------------------------- 第二段：SQL

def run_intent(conn, intent: dict) -> dict:
    """按意图执行固定的 SQL（参数化），所有数字从这里出来。"""
    name = intent["intent"]
    reference = models.reference_time(conn)
    if name == "unsettled_orders":
        rows = conn.execute(SQL[name]).fetchall()
        facts = []
        for row in rows:
            fee = conn.execute(
                "SELECT COALESCE(SUM(amount_fen), 0) AS s FROM refunds WHERE order_id = ?",
                (row["order_id"],)).fetchone()["s"]
            gaps = conn.execute(
                "SELECT gap_fen FROM anomalies WHERE order_id = ? AND label_code = 'NOT_SETTLED'",
                (row["order_id"],)).fetchone()
            gap = int(gaps["gap_fen"]) if gaps else int(row["amount_fen"])
            facts.append({
                "order_id": row["order_id"],
                "channel": config.CHANNEL_TEXT.get(row["channel"], row["channel"]),
                "paid_at": row["paid_at"],
                "amount_fen": int(row["amount_fen"]),
                "refund_fen": int(fee),
                "gap_fen": gap,
            })
        return {"intent": name, "facts": facts, "sql": SQL[name], "reference": reference,
                "total_gap_fen": sum(item["gap_fen"] for item in facts)}

    if name == "explain_order":
        order_id = intent["order_id"]
        rows = conn.execute(SQL[name], (order_id,)).fetchall()
        trail = reconciler.build_trail(conn, order_id)
        if not trail:
            return {"intent": name, "facts": [], "sql": SQL[name], "reference": reference,
                    "error": f"账本中没有订单 {order_id}"}
        facts = [{
            "order_id": order_id,
            "channel": config.CHANNEL_TEXT.get(trail["order"]["channel"],
                                              trail["order"]["channel"]),
            "paid_at": trail["order"]["paid_at"],
            "amount_fen": int(trail["order"]["amount_fen"]),
            "label_code": row["label_code"],
            "label_text": config.LABEL_TEXT[row["label_code"]],
            "gap_fen": abs(int(row["gap_fen"])),
            "broken_stage": reconciler.STAGE_NAMES[trail["broken_index"]]
            if trail["broken_index"] >= 0 else "",
            "reason": trail["reason"],
        } for row in rows]
        return {"intent": name, "facts": facts, "sql": SQL[name], "reference": reference,
                "trail": trail, "order_id": order_id}

    if name == "fee_overcharge":
        month = intent.get("month") or reference[:7]
        rows = conn.execute(SQL[name], (month,)).fetchall()
        facts = [{
            "order_id": row["order_id"],
            "channel": config.CHANNEL_TEXT.get(row["channel"], row["channel"]),
            "paid_at": row["paid_at"],
            "agreed_fen": int(row["expected_fen"]),
            "actual_fen": int(row["actual_fen"]),
            "gap_fen": int(row["gap_fen"]),
        } for row in rows]
        return {"intent": name, "facts": facts, "sql": SQL[name], "reference": reference,
                "month": month, "total_gap_fen": sum(item["gap_fen"] for item in facts)}

    return {"intent": "out_of_scope", "facts": [], "sql": "", "reference": reference}


# ---------------------------------------------------------------- 第三段：措辞

def template_answer(result: dict) -> str:
    name = result["intent"]
    if name == "unsettled_orders":
        facts = result["facts"]
        if not facts:
            return f"以数据最新流水时间 {result['reference']} 为基准，所有订单都有对应结算记录。"
        lines = [f"以数据最新流水时间 {result['reference']} 为基准，"
                 f"结算表中查不到以下 {len(facts)} 笔订单的到账记录（规则一 · 已支付未到账）："]
        for index, item in enumerate(facts, start=1):
            lines.append(
                f"{index}. {item['order_id']}（{item['channel']}）订单额 "
                f"{yuan_mark(item['amount_fen'])}，{item['paid_at']} 支付，"
                f"应收净额 {yuan_mark(item['gap_fen'])} 未到账"
            )
        lines.append(f"合计缺口 {yuan_mark(result['total_gap_fen'])}。"
                     f"点击订单号可查看该笔的逐行计算过程。")
        return "\n".join(lines)

    if name == "explain_order":
        if result.get("error"):
            return result["error"] + "。本系统只回答账本内的事实，请核对订单号。"
        order_id = result["order_id"]
        facts = result["facts"]
        if not facts:
            return f"{order_id} 在账本中没有任何异常记录：五段链路全部走通，金额与约定费率一致。"
        parts = [f"{order_id} 命中 {len(facts)} 条规则："]
        for item in facts:
            parts.append(
                f"· {item['label_text']}（断点在「{item['broken_stage']}」），"
                f"缺口 {yuan_mark(item['gap_fen'])}。归因：{item['reason']}"
            )
        return "\n".join(parts)

    if name == "fee_overcharge":
        facts = result["facts"]
        if not facts:
            return f"{result['month']} 没有手续费率偏离约定的结算记录。"
        lines = [f"{result['month']} 共有 {len(facts)} 笔结算的手续费率偏离约定"
                 f"（容差 {config.FEE_TOLERANCE_RATIO:.0%}，规则四）："]
        for item in facts:
            lines.append(
                f"· {item['order_id']}（{item['channel']}）约定手续费 "
                f"{yuan_mark(item['agreed_fen'])}，实际扣除 {yuan_mark(item['actual_fen'])}，"
                f"多收 {yuan_mark(item['gap_fen'])}"
            )
        lines.append(f"合计多收 {yuan_mark(result['total_gap_fen'])}。")
        return "\n".join(lines)

    return ("这个问题超出账本事实范围。我只能回答三类问题："
            "① 哪几笔订单没到账；② 某一笔订单为什么对不上；③ 手续费多收了多少。")


def _allowed_numbers(result: dict, template: str = "") -> set:
    """把事实清单里的所有数字（分值 / 元值 / 笔数 / 序号）变成白名单。

    序号与笔数与查询结果条数一致，模板答案同样由程序生成，
    因此这两类数字一并放行；其余数字都视为模型自带，会被 verify_numbers 拒绝。
    展示值统一走 core.money.fen_to_yuan，不在本文件里做任何浮点换算。
    """
    allowed = set()

    def add(token: str):
        """同时放行「带千分位」与「不带千分位」两种写法。"""
        allowed.add(token)
        allowed.add(token.replace(",", ""))

    def walk(value):
        if isinstance(value, bool):
            return
        if isinstance(value, int):
            add(str(value))
            add(fen_to_yuan(value, thousands=False))
            add(fen_to_yuan(value))
        elif isinstance(value, str):
            for token in re.findall(r"\d[\d,]*(?:\.\d+)?", value):
                add(token)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(result.get("facts", []))
    walk(result.get("total_gap_fen", 0))
    walk(result.get("month", ""))
    walk(template)
    count = len(result.get("facts") or [])
    add(str(count))
    for index in range(1, count + 1):
        add(str(index))
    return allowed


def verify_numbers(text: str, result: dict, template: str = "") -> bool:
    """模型措辞里不允许出现事实清单与模板以外的数字。

    先剥掉日期与时间（它们来自数据本身，不是模型编的金额），再逐个 token 比对；
    token 里的千分位逗号会被去掉，因此「¥1,398.10」与「¥1398.10」等价。
    """
    if not text:
        return False
    stripped = re.sub(r"\d{4}-\d{2}-\d{2}", " ", text)
    stripped = re.sub(r"\d{1,2}:\d{2}(?::\d{2})?", " ", stripped)
    allowed = _allowed_numbers(result, template)
    for raw in re.findall(r"\d[\d,]*(?:\.\d+)?", stripped):
        if raw.replace(",", "") not in allowed:
            return False
    return True


def polish(conn, intent: dict, result: dict, template: str):
    """第三段：让模型只做措辞。失败 / 校验不过 → 直接用模板。"""
    status = ai_status(conn)
    if not status["available"]:
        return template, "template", status["reason"]
    facts = json.dumps(result.get("facts", []), ensure_ascii=False)
    prompt = (
        "你是对账结论的措辞整理器。只能使用下面这份事实清单里的数字，"
        "不得引入任何新数字、不得推测、不得省略订单号。中文，不超过 6 行。\n"
        f"事实清单：{facts}\n"
        f"参考措辞：{template}"
    )
    reply = _chat([{"role": "user", "content": prompt}])
    if not reply or not verify_numbers(reply, result, template):
        return template, "template", "模型措辞未通过数字校验，已回落模板"
    return reply.strip(), "model", status["reason"]


# ---------------------------------------------------------------- 对外入口

def ask(conn, question: str) -> dict:
    intent = parse_intent(conn, question)
    result = run_intent(conn, intent) if intent["intent"] in SUPPORTED else {
        "intent": "out_of_scope", "facts": [], "sql": "", "reference": models.reference_time(conn)}
    template = template_answer(result)
    if result["intent"] in SUPPORTED:
        answer, mode, note = polish(conn, intent, result, template)
    else:
        answer, mode, note = template, "refuse", "问题超出账本事实，按铁律直接拒答"
    sources = [item.get("order_id") for item in result.get("facts", []) if item.get("order_id")]
    return {
        "question": question,
        "intent": intent,
        "sql": result.get("sql", ""),
        "reference": result.get("reference", ""),
        "facts": result.get("facts", []),
        "answer": answer,
        "mode": mode,
        "note": note,
        "sources": sources,
        "degraded": mode != "model",
    }
