# MoneyTrail · 资金链路核查台

把「平台少给我钱」从一句抱怨，变成一份**能点开、能导出、能拿去对账**的证据。

一句话：**记录谁都会做，归因才是刚需。** 本项目只回答三个问题 —— 哪笔钱断了、断在哪里、凭什么这么说。

---

## 环境要求

- Python **3.10+**（开发与验证使用 3.14.5）
- **零第三方依赖**，不需要 `pip install`
- 操作系统不限（Windows / macOS / Linux）

> Windows 提示：如果 `python` 双击没反应或报「应用商店」，说明解释器没进 PATH。
> 直接使用安装路径即可，例如：
> `C:\Users\<你>\AppData\Local\Programs\Python\Python314\python.exe app.py`

---

## 三条命令跑起来

```bash
# 1. 生成演示数据（固定种子 20261119，结果可复现）
python gen_data.py

# 2. 启动服务
python app.py
#    → 浏览器打开 http://127.0.0.1:8000

# 3. 自检（10 条断言，输出 PASS / FAIL，全绿退出码 0）
python check.py
```

Windows 用户也可以直接**双击 `run.bat`**：它会先补数据、再起服务，并在窗口里提示服务地址。

可选参数：

```bash
python app.py --host 127.0.0.1 --port 8000 --db moneytrail.db
```

---

## 目录结构

```
Wallet/
├─ app.py                服务入口（标准库 http.server，13 条路由）
├─ check.py              自检脚本 · 10 条断言（临时目录运行，不碰演示数据）
├─ gen_data.py           演示数据生成器（固定种子）
├─ config.py             阈值与常量（费率容差、时间窗、端口）
├─ run.bat               一键启动（双击可用）
├─ core/
│  ├─ money.py           整数「分」的解析与格式化
│  ├─ models.py          SQLite 表定义与读写
│  ├─ importer.py        CSV 导入与字段校验（按行号报错）
│  ├─ reconciler.py      对账匹配（订单号精确匹配、幂等）
│  ├─ detectors.py       四类异常检测
│  └─ assistant.py       自然语言助手（可选 · 有确定性兜底）
├─ web/
│  ├─ views.py           页面控制器
│  ├─ templates/         服务端渲染模板
│  ├─ svg.py             手写 SVG 组件（五节点管道）
│  ├─ exporter.py        核查单导出（Markdown / CSV）
│  └─ static/style.css   样式
├─ data/                 演示 CSV 与 ground_truth.json
├─ tests/                unittest 单元测试
├─ reports/              每 30 分钟自动落盘的进度快照
├─ moneytrail.db         SQLite 台账（首次启动自动建表并导入）
├─ DEMO.md               三步演示路径（60 秒版）
└─ MoneyTrail-项目介绍书.md  设计文档全文
```

---

## 三条口径（避免答辩时口径不一）

1. **金额一律整数「分」**。库里不存浮点，差额用整数相减，不存在 0.01 的舍入故事。
2. **基准时点 T 固定为数据集最新时间**。T 之后没到账的在途订单会被判为「未到账」，属已知限制（见介绍书 §12）。
3. **AI 只做语言理解，不做算术**。关掉模型服务（`POST /ai/toggle` 熔断，或不配 API Key），总览 / 筛选 / 链路 / 导出**全部照常**，助手退回关键词兜底，答案仍来自同一套 SQL。

---

## 自检与验收

| 命令 | 作用 | 期望 |
|---|---|---|
| `python check.py` | 10 条断言：数据生成、导入校验与幂等、四类规则命中数、状态持久化、无 Key 降级、无浮点金额 | `10 / 10 PASS`，退出码 0 |
| `python -m unittest discover -s tests -t . -v` | 单元测试：金额边界、导入校验、对账幂等、四类规则命中与反例 | 全绿 |

`check.py` 全程在临时目录内构造数据，**不会**修改 `data/` 与 `moneytrail.db`，可以随时反复执行（已用文件哈希验证）。

---

## 进度报备

`reports/` 下是运行期快照，用于「进度可核查」：

```bash
python reports/progress_reporter.py            # 立刻落一份
python reports/progress_reporter.py --loop 1800 # 每 30 分钟落一份（常驻）
```

- `reports/latest.md` —— 最新一次快照（进度分、服务健康、台账数字、缺口清单、与上次的差异）
- `reports/进度报备.log` —— 全量追加日志

Python 解释器起不来时，用 PowerShell 版兜底。它会**先真正跑一次** `progress_reporter.py`：跑得通就照常落真实台账；跑不通才写兜底快照，且台账写「未取数」，只手抄上一次成功读数作参照，不填任何推算数字。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File reports\progress_reporter.ps1 -IntervalSeconds 1800
```

> 本机若出现「`python -V` 秒回、跑脚本却卡住」的环境级挂起（安全软件拦截解释器初始化），可用文件重定向绕开：
> `cmd /c ""<python.exe>" -u reports\progress_reporter.py > out.txt 2>&1`

---

## 模型接入（自然语言助手）

助手只做「语言 → 意图 JSON → SQL → 措辞」三段式，算数全在确定性代码里。模型端点默认指向本机：

| 项 | 默认值 | 覆盖方式 |
|---|---|---|
| 端点 | `http://127.0.0.1:8766/v1` | `BAIZE_BASE_URL` |
| 模型 | `Baize-Omni-Sense` | `BAIZE_MODEL` |
| 鉴权 | 免鉴权（本机） | `BAIZE_NO_AUTH=0` + `BAIZE_API_KEY` |
| 超时 | 25 秒 | `config.AI_TIMEOUT_SECONDS` |

- 助手页顶部会显示当前模型与端点；回答方式标注「模型措辞（已通过数字校验）」或「模板兜底」。
- 模型不可达 / 超时 / 措辞里出现事实清单以外的数字 → 一律回落模板答案，不阻塞任何核心路径。
- 想复现「AI 全挂也能用」：`POST /ai/toggle` 打开熔断开关即可。

## 已知限制

模糊匹配、合伙人分账、多商户、账号体系均**明确不做**，理由与取舍见 `MoneyTrail-项目介绍书.md` §5.2 / §12。

---

## 声明

演示数据由 `gen_data.py` 以固定随机种子模拟生成，不含任何真实商户信息、支付流水或个人隐私；模型凭据仅通过环境变量注入，不写入源码、日志与截图。
