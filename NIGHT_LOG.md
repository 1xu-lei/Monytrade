# NIGHT_LOG · 执行记录

> 记录格式沿用项目介绍书 §9.4：用「带断言的任务 + 可复核的绿数」代替「Agent 干了多久」这种无法验证的叙述。
> 本项目未使用 git 分支（单机单人交付），因此「分支」列改为「隔离方式」——每步都不许污染演示数据。

## 表一 · 交付时间线

| 时间 | 任务 | 执行者 | 结果 | 断言证据 | 隔离方式 |
|---|---|---|---|---|---|
| 12:45 | core 基础层（money / models） | 人 + KCoder | 完成 | 金额全整数「分」 | — |
| 12:50 | importer / reconciler / detectors | 人 + KCoder | 完成 | 四类规则命中 9 笔 | — |
| 13:18 | assistant 助手三段式 + 熔断 | 人 + KCoder | 完成 | 无 Key 时走确定性兜底 | — |
| 13:38 | web 层（模板 / SVG 管道 / 导出 / multipart） | 人 + KCoder | 完成 | 13 条路由可访问 | — |
| 13:50 | `check.py` 10 条断言 | 子智能体产出，主控复核 | 完成 | `10 / 10 PASS`，退出码 0 | `tempfile` 临时库 |
| 14:02 | `reports/progress_reporter.py` 每 30 分钟报备 | KCoder | 完成 | 连续 18 轮快照无异常 | 只读打开数据库 |
| 14:56 | `run.bat` 一键启动 | 子智能体产出，主控实测 | 完成 | 自动识别 Python 3.14.5 | 不改数据 |
| 15:05 | `tests/` 单元测试（含 assistant / web / http 三层） | 子智能体产出，主控复核 | 完成 | `Ran 144 tests … OK` | 每例独立临时库 |
| 15:12 | `reports/acceptance.py` 验收实测 14 条 | KCoder | 完成 | `14 / 14 PASS` | 库副本 + 独立端口 8124 |
| 15:30 | README / DEMO / 介绍书状态回填 | KCoder | 完成 | 介绍书里「计划」标记归零 | — |

## 表二 · 断言快照（可复核）

| 检查项 | 命令 | 结果 |
|---|---|---|
| 自检 10 条 | `python check.py` | `10 / 10 PASS`，退出码 0；连跑两次一致 |
| 单元测试 | `python -m unittest discover -s tests -t . -v` | `Ran 144 tests in 9.667s · OK` |
| 验收实测 | `python reports/acceptance.py` | `14 / 14 PASS`，退出码 0 |
| 检出率 | 与 `data/ground_truth.json` 对照 | 应报 9 / 实报 9 / 漏报 0 / 误报 0 → **100.0%** |
| 对账耗时 | `POST /reconcile` | 首次 6.5 ms，复跑 3.3 ms，工单恒为 9 笔（幂等） |
| 数据完整性 | 运行前后 SHA256 对比 5 个文件 | `data/*.csv`、`ground_truth.json`、`moneytrail.db` 全部未被自检污染 |

## 表三 · 失败与回滚记录（不隐瞒）

| 时间 | 事件 | 处理 | 结论 |
|---|---|---|---|
| 14:40 | 三个并行子智能体（check.py / tests / 验收）均在运行中报 `NATIVE_TASK_AUTHORITY_UNAVAILABLE` 失败 | 保留已落盘的 `check.py`、`tests/`，其余改为主控直接执行 | 委派链路不可用，交付不受影响；子智能体产物一律由主控重跑断言后采信 |
| 14:56 | `run.bat` 首次后台拉起时 8000 端口无监听（`cmd → python` 启动态与端口绑定不同步） | 结束残留进程后改用 `app.py` 直启，并单独用探针副本验证 `run.bat` 的解释器识别与分支逻辑 | `run.bat` 逻辑正确；双重启属操作并发症，非脚本缺陷 |
| 15:05 | `reports/acceptance.py` 首轮 12/14：第 07 条断言误判 303 跳转文案、第 12 条误判 `/health` 的 `ai` 字段 | 断言改为校验 `Location` 里的中文状态文案与 `settings.ai_kill_switch` 落库值 | 两处均为**断言写错**，应用无缺陷；改后 14/14 |
| 17:10 | 并发写入期间 `core/detectors.py`、`core/models.py`、`core/reconciler.py`、`web/views.py` 被另一个会话/智能体同目录改动，`check.py` 一度跌到 0/10（`models.get_settlement_by_id` 尚未落地） | 不抢改业务文件，等写入停止后重跑；仅把 `tests/test_detectors.py` 中失效的 `detectors.fen_display` 改指 `core.money.fen_to_yuan`（对方已把金额格式化统一到 money 模块） | 重跑后 `10 / 10`、`144 tests OK`、`14 / 14`；**同一目录只应保留一个写入会话**，否则绿数会因并发改动瞬间翻红 |
| 17:10 | 复跑验收连续 2 次在 `04 /import` 卡到 10 秒超时（`TimeoutError`），同一时刻 `GET /import` 在 8000 手工探针却 200 | 定位为上一轮残留监听与 `HTTPServer.allow_reuse_address` 在 Windows 下可双绑同端口，请求被分给旧进程；`start_server` 改为先等 8124 真空出来（最多 5 秒），`fetch` 仅对幂等 GET 重试一次 | 应用无缺陷，验收脚本竞态已修；连跑 2 次 + 主控复跑共 3 次均 `14 / 14 PASS` |
| 19:24 | 8000 服务随后台任务 2 小时生命周期到期被回收（任务超时，非应用崩溃；应用最后一次改动 17:37，17:21–19:21 全程正常） | 尝试重启：**任何新的 Python 进程都无法启动** —— `python.exe` / `pythonw.exe` / `py.exe` 均在同一位置挂死，换工作目录（`C:\Windows\Temp`）与 `-E -S -s` 隔离启动同样挂死；只有 `python -V` 立即返回。已排除磁盘（剩 1.9 TB）、目录锁、并发写入（代码 mtime 17:37）与残留进程（仅报备器存活） | 判定为**环境级问题**（安全软件/DLL 加载），应用侧无回归；期间报备器仍每 30 分钟落盘并把服务标成「不可达」。恢复方式：安全软件放行 python.exe 或重启会话后执行 `run.bat` |


## 表四 · 演示数据基线（勿随手改）

| 项 | 值 |
|---|---|
| 随机种子 | `20261119` |
| 订单 / 退款 / 结算 | 24 / 9 / 21 |
| 订单总额 | ¥3,428.45（342845 分） |
| 实际到账 | ¥2,519.45（251945 分） |
| 待核缺口 | ¥696.80（69680 分，9 笔） |
| 基准时点 T | `2026-09-22 10:20:55` |

> 若演示前发现工单状态不是「9 笔待处理」，执行 `python gen_data.py` 重建，或把非 pending 工单改回 `pending`；稳态口径以上表为准。

## 表五 · 接入本机模型这一轮（16:30–17:10）

| 项 | 改动 | 证据 |
|---|---|---|
| 端点接入 | `config.py` 默认端点改为 `http://127.0.0.1:8766/v1`、模型 `Baize-Omni-Sense`，新增免鉴权开关 `BAIZE_NO_AUTH`（默认 1） | `GET /v1/models` 返回该模型，`max_input_tokens=1000000` |
| 鉴权放宽 | `core/assistant.py` 新增 `base_url()` / `model_name()` / `no_auth()`；无 Key 但免鉴权时也算可用；有 Key 才带 `Authorization` | 助手页显示「Baize-Omni-Sense · http://127.0.0.1:8766/v1」 |
| 超时收紧 | `AI_TIMEOUT_SECONDS` 45 → 25 秒，超时即回落模板 | 端到端问句实测 3–15 秒 |
| 数字校验修正 | `verify_numbers` 白名单加入「模板答案里的数字」（如容差 10%），否则费用类问句总被误判回落 | 费用问句由「模板兜底」变为「模型措辞（已通过数字校验）」 |
| 测试离线化 | `tests/support.ai_offline()` 强制关掉免鉴权；`test_web` / `test_http` / `test_assistant` 共用 | 144 条用例仍全绿，且不依赖 8766 在线 |
| 报备节奏 | 5 分钟 → 30 分钟（`--loop 1800`） | `reports/进度报备.log` 改为 30 分钟一条 |

模型实测（`reports/model_probe.txt`，同一端点）：

| 用例 | 耗时 | 输出合规 |
|---|---|---|
| 冷启动问候（8 tokens） | 16.8 s | — |
| 意图 JSON（23 tokens） | 1.0 s | `{"intent":"unsettled_orders",...,"confidence":0.95}` |
| 措辞整理（39 tokens） | 2.5 s | 数字全部来自事实清单 |

## 表六 · 收口复查这一轮（06:05–06:50）

| 项 | 改动 | 证据 |
|---|---|---|
| 异常中心分页 | models.list_anomalies 的 limit/offset 早已支持但页面从未使用；补 iews.PAGE_SIZE = 20 与分页条，翻页只改 page、筛选条件原样带回，导出仍按筛选取全量 | Ran 161 tests · OK（新增 4 条分页 / 编码用例）；GET /anomalies?page=abc|0|99 均 200，越界回落到最后一页 |
| 示例问句链接 | 助手页 `/assistant?q=` 里带中文与空格，改走 `urlencode`；补断言 | `test_assistant_example_links_are_url_encoded` |
| 报备器误报 | 旧 `Get-PythonState` 用 `Start-Process -WindowStyle Hidden` + 重定向探测，本机该组合会挂住；`Wait-Process -Timeout` 又不刷新 `HasExited`，于是「Python 正常」被写成「挂死 8 秒」 | 复现：同一条命令 `-Wait` 0.1 s 退出，`WaitForExit(8000)` 返回 `False`；改为「真正跑一次 `progress_reporter.py`」判定可用性，并用 `.NET WaitForExit(45000)` |
| 解释器定位 | PATH 里的 `python` 是应用商店占位符（退出码 9009），旧逻辑「文件存在即采用」会选错；改为逐个实测 `--version`，与 `run.bat` 同规矩 | 修复后 `progress_reporter.ps1 -Once` 输出「Python 报备成功」，`latest.md` 为真实台账读数 |
| 兜底不再编数字 | 删掉硬编码「基线 9 笔 / ¥696.80 / 进度 100-100」，改为「未取数 + 上次成功读数（仅作参照）」；两版报备器共用 `seq` | `reports/latest.md` 第 38 次快照：订单 24 / 退款 9 / 结算 21 / 工单 9，未结缺口 69680 分 |
| 文档口径 | 介绍书 §5.1 异常中心补「每页 20 笔分页」；`NIGHT_PLAN.md` 完成判据逐条打勾并说明 T6 不做；DEMO 第 0 步数字对齐基线表 | 与 `NIGHT_LOG` 表四基线一致 |

## 表七 · 环境级故障复现与绕行（06:25 起）

| 现象 | 判定依据 | 绕行 |
|---|---|---|
| 新起的 Python 进程卡在初始化：`python -V` 125 ms 返回，`python -c print(1)` / 脚本 / `-m unittest` 全部超过 120 s 无输出 | `-B` / `-S` / `-I` 隔离启动同样挂住；同一时刻其他会话仍能正常起 `app.py`，说明不是代码回归 | `cmd /c ""<python>" -u <脚本> > out.txt 2>&1` 可正常执行；本机 Python 在 stdout 被父进程用管道接管时会卡住，改为文件重定向即可 |
| 影响面 | 应用侧无回归：`check.py` 10 / 10、`161 tests OK`、`reports/acceptance.py` 14 / 14 均在该绕行方式下复跑通过 | 安全软件放行 `python.exe` 或重启会话后可直接 `run.bat` |

## 表六 · 交付前复验（11-20 06:20–07:10，封板前最后一轮）

| 项 | 命令 / 做法 | 结果 |
|---|---|---|
| 端到端自检 | `python check.py` | `10 / 10 PASS`，退出码 0 |
| 单元测试 | `python -m unittest discover -s tests -t .` | `Ran 161 tests in 9.864s · OK` |
| 真实 HTTP 验收 | `python reports/acceptance.py` | `14 / 14 PASS`（独立端口 8124、库副本） |
| 代码包独立复验 | 把 `01 项目代码` 整目录复制到交付路径后重跑 `gen_data.py` / `check.py` / 单元测试 | 全绿：9 笔工单 / 缺口 69680 分 / 10-10 PASS / 161 OK（确认不依赖开发目录） |
| 缺陷修复 | `web/svg.py` 的 `_wrap`：中文硬折行会把 `¥241.61` 拆成两行 | 改为在数字 / `¥` / 小数点前回退；回归后 161 tests OK、10/10 PASS，链路图归因文字恢复整行 |
| 文档口径校正 | `DEMO.md` 开场数字（旧稿 ¥3,208.15）与实测不符 | 改为 `¥3,428.45 / ¥2,519.45 / 9 笔 / ¥696.80`，与总览页一致 |
| 演示材料 | 11 章分镜 + 中文旁白 + ffmpeg 合成 | `MoneyTrail-演示视频.mp4`：1920×1080 / 30 fps / 3 分 29 秒 / 响度归一 -16 LUFS |

> 环境备注（与项目无关）：本轮复验开始时，本机 `python.exe` 在受限会话内启动即挂死（`python -V` 之外全部卡在解释器初始化），
> 换成系统「任务计划程序」拉起后一切正常，`python -V` 也正常返回 3.14.5 —— 判定为安全软件 / 沙箱层拦截，**应用侧无回归**。
> 该现象已写入 `02 部署或运行入口/运行说明.md` 常见问题第 2 条（换解释器绝对路径 / 加白名单 / 用计划任务）。
## 表六 · 08:46 恢复记录（Python 可用、端口迁移）

| 项 | 事实 | 证据 |
|---|---|---|
| Python 恢复 | 08:46 复测 \python.exe\ 跑 \print('hello')\ 正常退出（前一夜 02:49 仍挂死） | 探针 exited=True，out=[hello] |
| 端口冲突 | 8000 已被另一个项目占用：\C:\Users\11\Desktop\Competition_game\backend\.venv\python.exe -m uvicorn app.main:app --port 8000\，MoneyTrail 启动报 \PermissionError: [WinError 10013]\ | 进程命令行 + 报错堆栈 |
| 端口迁移 | MoneyTrail 改跑 **8090**；报备器新增 \MONEYTRAIL_PORT\ 环境变量覆盖健康检查地址（并修正快照里的端口标签） | \/health\ 8090 返回 orders=24 settlements=21 anomalies=9 ai=true |
| 报备恢复 | 切回 Python 版报备器（信息更全：能读库、报工单状态与缺口），第 47 期 08:56:27 显示 \127.0.0.1:8090 → OK 200\ | reports/latest.md |
| 三道闸门 | \check.py\ 10/10；\unittest\ 167 tests OK；\cceptance.py\ 14/14（检出率 100%、幂等 9→9→9、重启后状态仍在） | 本轮实跑输出 |
| 助手实测 | \/assistant\ 8090 上问「今天哪几笔没到账」→ 6.5 s，模型措辞（已通过数字校验），状态栏 \Baize-Omni-Sense · http://127.0.0.1:8766/v1\ | HTTP 200 |

## 表八 · 收口后的四项加固（08:45–09:05）

| 项 | 处理 | 证据 |
|---|---|---|
| 常驻报备器还在跑旧代码 | 停掉 02:49 启动的旧进程（PID 39476，内存里是旧脚本），改用修好的脚本重启常驻；新进程首轮即走「真正跑一次 Python 版」的判断 | 后台任务首轮输出「Python 报备成功」；`latest.md` 第 47 次为真实台账（订单 24 / 退款 9 / 结算 21 / 工单 9） |
| 快照序号撞号 | 两版报备器统一为「取日志里最大 N + 1」，快照文件里的 seq 只作兜底 | 序号 43→47 连续无重号（此前「第 38 次」出现过两条） |
| 档位映射漂移 | `reconciler.stats()` 增加 `by_severity`（SQL `GROUP BY severity`）；总览卡片的「高/中/低」改读该字段，视图不再自己推断哪类规则算高危 | 新增断言：演示集 高 5 / 中 2 / 低 2，且 `sum(by_severity) == all_count` |
| 方法不匹配返回 404 | 路径命中但方法不对时返回 405 并带 `Allow` 头 | 新增 3 条 HTTP 用例：`GET /reconcile`、`GET /ai/toggle`、`GET /anomalies/<id>/status` 均 405；`POST /anomalies` 为 405；未知路径仍 404 |
| 可访问性 | 表头补 `scope="col"`；规则分布条形图补 `role="img"` + `aria-label`（如「已支付未到账 3 笔，占全部工单 33%」），视觉元素标 `aria-hidden` | 新增用例断言上述片段 |
| 版本库 | `.gitignore` 补 `reports/.snapshot*.json` 与 `reports/latest.md`；报备运行产物不再入库（`git rm --cached`） | 本轮改动单独提交，工作树干净 |

验证：`check.py` 10 / 10、`unittest` 167 tests OK、`reports/acceptance.py` 14 / 14。

## 表九 · 启动期体验：端口被占要说人话（09:20）

| 项 | 说明 |
|---|---|
| 现象 | 用户执行 `python app.py` 直接抛 `PermissionError: [WinError 10013]`，看起来像权限/防火墙问题 |
| 真因 | 8000 被**另一个项目**占用：`Competition_game\backend\.venv\python.exe -m uvicorn app.main:app --port 8000`（09:00:34 启动）。uvicorn 走 asyncio，在 Windows 上给套接字设了 `SO_EXCLUSIVEADDRUSE`，后到的 `HTTPServer`（`SO_REUSEADDR`）抢同一端口，系统回的是 10013 而不是 10048 |
| 处理 | `app.py` 新增 `_make_server()` / `_free_port()`：绑定失败时退出码 2，打印端口号、错误码、可照做的 `--port` 建议与查占用者的命令，不再甩堆栈；README 增加「端口被占怎么办」 |
| 验证 | 新增 `tests/test_startup.py`（2 条，Windows 下用 `SO_EXCLUSIVEADDRUSE` 复现 10013）：断言退出码 2、含 `--port` 与 `Get-NetTCPConnection`、且无 Traceback；实机 `python app.py` 输出建议 8001，`--port 8001` 起来后 `/health` 200 |

## 表十 · 默认端口被占：自动顺延而不是罢工（09:40）

| 项 | 处理 |
|---|---|
| 触发 | 本机 8000 长期被另一个项目的 uvicorn 占用，`python app.py` 每次都起不来 |
| 行为 | 未显式指定 `--port` 时，默认端口被占会自动顺延到下一个可用端口，并打印「注意：8000 已被占用，自动改用 8001」；显式 `--port` 失败仍按错误处理（调用方指定了端口，不能偷偷换） |
| 顺带 | 启动时把实际端口写入 `settings.listen_port`；报备器探活顺序改为 环境变量 `MONEYTRAIL_PORT` > `listen_port` > 8000，避免顺延后一直报「服务不可达」 |
| 验证 | `tests/test_startup.py` 增 `test_default_port_falls_back_instead_of_dying`；实机 `python app.py` 自动用 8001，`/health` 200 |

## 表七 · 交付后优化同步（11-20 09:55 复验）

选手侧二次优化（健壮性与可访问性）已合入交付包，并对提交材料做同步替换：

| 改动 | 内容 | 对提交材料的影响 |
|---|---|---|
| 端口冲突处理 | `app.py` 新增 `_make_server` / `_free_port`：不带 `--port` 时默认端口被占（含 uvicorn 造成的 `WinError 10013`）**自动顺延**并打印实际地址；显式 `--port` 失败时给出可照做的提示；实际端口写入 `settings.listen_port` 供守护脚本探活 | 新增「启动健壮性」条目（项目说明 §2.1 / 运行说明 §6），视频无需改 |
| 405 语义 | 路径存在但方法不对时回 **405** + `Allow` 头 + 中文提示页，不再笼统 404 | 无影响 |
| 可访问性 | 表头补 `scope="col"`；规则分布条 `role="img"` + `aria-label`；严重度计数改由 SQL 聚合（视图不再自己猜档位） | 页面像素无变化：B/C/D/F 截图逐字节一致，A 总览重拍后视觉一致 |
| 测试 | 新增 `tests/test_startup.py` 等，用例数 **161 → 170** | 视频第 10 章与四份文档的绿数同步为 170（本章画面与旁白已重录重剪） |

**优化后复验（本机实测）**：`check.py` 10 / 10 PASS（exit 0）· `Ran 170 tests in 10.029s · OK` · `acceptance.py` 14 / 14 PASS ·
总览四数字与 9 笔工单/缺口 69680 分保持不变；演示视频时长 3 分 29 秒（8.5 MB）。