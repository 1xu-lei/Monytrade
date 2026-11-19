import json
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8766/v1"
MODEL = "Baize-Omni-Sense"
OUT = Path(r"C:\Users\11\Desktop\Wallet\reports\model_probe.txt")

CASES = [
    ("trivial", "只回复一个字：好", 8),
    ("intent", "你是资金核查系统的意图解析器。只输出一行 JSON，不要解释。\n"
               'JSON 结构：{"intent":"unsettled_orders|explain_order|fee_overcharge|out_of_scope",'
               '"order_id":"","month":"","confidence":0.0}\n'
               "用户问题：今天哪几笔没到账", 96),
    ("polish", "你是对账结论的措辞整理器。只能使用下面这份事实清单里的数字，"
               "不得引入任何新数字。中文，不超过 6 行。\n"
               '事实清单：[{"order_id":"MT20260919-004","amount_fen":230300,'
               '"gap_fen":230300}]\n'
               "参考措辞：以数据最新流水时间为基准，结算表中查不到 1 笔订单的到账记录。",
     220),
]


def call(prompt, max_tokens):
    payload = json.dumps({"model": MODEL, "temperature": 0, "max_tokens": max_tokens,
                          "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
    request = urllib.request.Request(BASE + "/chat/completions", data=payload,
                                     headers={"Content-Type": "application/json"})
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=540) as response:
            body = json.loads(response.read().decode("utf-8"))
        elapsed = time.perf_counter() - started
        content = body["choices"][0]["message"].get("content") or ""
        return elapsed, content.strip().replace("\n", " ")[:200], body.get("usage")
    except Exception as exc:  # noqa: BLE001
        return time.perf_counter() - started, f"ERROR {type(exc).__name__}: {exc}", None


with OUT.open("a", encoding="utf-8") as fh:
    fh.write(f"\n=== 探测开始 {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    fh.flush()
    for name, prompt, max_tokens in CASES:
        elapsed, content, usage = call(prompt, max_tokens)
        line = f"[{name}] {elapsed:.1f}s · {content} · usage={usage}"
        print(line, flush=True)
        fh.write(line + "\n")
        fh.flush()
    fh.write("=== 探测结束 ===\n")
