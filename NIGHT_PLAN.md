# NIGHT_PLAN · 夜间自主开发任务清单

> 交现场检查版。核心变量不是「Agent 写了多少」，而是**「无人值守期间有没有把能跑的东西改坏」**。
> 所有任务都写成「可被 `check.py` 断言」的形式；任一任务令 PASS 数下降即整分支丢弃。

## 护栏（先立规矩，再谈产出）

1. **只允许在 `feature/*` 分支改，`main` 不动** —— 主分支永远处于可运行、可演示状态。
2. **每个任务结束必须跑 `check.py`，PASS 数不得下降**，否则 `git reset` 回退。
3. **禁止夜间改依赖、改数据库 schema、改 `check.py` 本身**。
4. **未完成功能默认关闭**（开关置 OFF），保证任何时刻被评测都是可运行状态。

## 任务分解（按「失败也不损伤基线」排序）

| # | 任务 | 风险 | 验收断言 | 分支 |
|---|---|---|---|---|
| T1 | 补单元测试（金额 / 导入 / 对账 / 四类规则 / 渲染 / HTTP） | 极低 | `python -m unittest discover -s tests -t .` 全绿，且不改任何业务文件 | `feature/tests` |
| T2 | 补 README / DEMO（功能边界 / 技术路线 / 三步演示） | 极低 | 文档存在，命令可复制执行 | `feature/docs` |
| T3 | 页面四态（空 / 错 / 无结果 / 无异常） | 低 | 四种状态均可渲染且不抛异常 | `feature/ui-states` |
| T4 | 核查单导出增强（批量 + CSV） | 中 | 导出 CSV 行数 = 异常笔数；Markdown 含三段证据 | `feature/export` |
| T5 | AI 助手（模板兜底 + 模型增强 + 数字校验） | 中高 | 无 Key 时 3 类问法仍返回带订单号的模板答案 | `feature/assistant` |
| T6 | 趋势图 / 附加视图（选一） | 高 | 失败即整分支丢弃，不合并 | `feature/chart` |

## 完成判据（收口时逐条核对）

- [x] `python check.py` → `10 / 10 PASS`（06:41 复核，退出码 0）
- [x] `python -m unittest discover -s tests -t .` → `Ran 161 tests · OK`（06:41 复核）
- [x] `python reports/acceptance.py` → `14 / 14 PASS`（真实 HTTP + 介绍书 §11 验收清单）
- [x] `python gen_data.py` 结果可复现（固定种子 20261119）
- [x] `main` 分支可 `python app.py` 直接演示（服务在 8000 端口实测可访问）

> T6（趋势图 / 附加视图）不执行：介绍书 §5.2 已明确「30 天趋势图」不在交付范围，
> 演示集只有 4 天跨度，画趋势图得不出可解释结论。异常中心改为补分页（见 NIGHT_LOG 表六）。
