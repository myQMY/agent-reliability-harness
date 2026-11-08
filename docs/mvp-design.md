# MVP 产品设计：Agent 可靠性评测与运行时平台

> 状态：范围已确认，待实现。本文只定方向与功能边界，不写任何未经验证的结果数字。
> 项目整体定位见 [README.md](../README.md)，协作规则见 [AGENTS.md](../AGENTS.md)。

## 1. 定位与目标

做一个「测量 Agent、并证明它变好了」的实验装置：用一批有状态、有副作用的业务任务当标尺，用数据库终态比对自动判分，用重复执行与全链路 trace 暴露不稳定，再用单变量对照实验逐项验证改动。

**MVP 的目标不是让 Agent 表现好，而是让这条测量链路可信、可复现、可测试。** 第一版跑出来的基线越难看，后续对照实验越有价值。

方向依据：2026 年互联网厂 Agent 实习普遍要求运行时 + 评测 + 可观测 + 稳定性；会做 Agent 的人多，能证明 Agent 变好的人少。一次成功演示说明不了问题，能复现的结果表和消融实验才扛得住追问。全程单机、自建数据、无需合作方，MVP 阶段零模型 API 成本。

## 2. 设计原则

| 原则 | 含义 | 带来的约束 |
| --- | --- | --- |
| 测量优先 | 评测链路是一等公民，Agent 只是被测对象 | 先写 grader 与 trace，再写 Agent |
| 确定性判分 | 数据库终态逐字段比对，不用 LLM 打分 | 结果可复现，提升可归因 |
| 故意留坑 | MVP 只保留最低限度护栏 | 难看的基线 = 后续对照实验的自变量 |
| 可复现优先 | 默认 ScriptedBackend，零成本、结果确定 | 模型层必须可切换；每次 trial 重置同一快照 |
| 证据入库 | 配置、trace、结果表都可追溯 | 每个数字都能在仓库里翻到出处 |

## 3. 使用者与场景

唯一使用者是开发者本人，在本地跑批。不存在终端用户、登录、并发请求或线上服务。

典型动作：改一处 harness → 一条命令重跑 30 个 trial → 对比前后结果表 → 用 trace 定位失败 → 决定保留还是回滚这次改动。

## 4. 分层与数据流

四层：领域环境 / Agent 运行时 / 评测 Harness / 可观测与报告。

```
tasks/*.jsonl → runner (N × k)
                  ↓ 每个 trial 从同一快照重置
              env: SQLite + 工具 API + 策略
                  ↓ 规则用户模拟器逐轮发话
              runtime: 循环 → 工具调用 → 观察
                  ↓ 每步写 trace span
              grader: 终态 diff + 策略检查
                  ↓
              analyze: pass¹ / pass³ / 失败分布 → 结果表
```

一次 eval run 六步：读取任务 → 起独立环境 → 模拟器发话 → Agent 循环调用工具（每步记 trace）→ 对话结束后比对终态与策略 → 汇总指标与失败归因。同一任务重复 k 次，全部通过才计入 pass^k。

## 5. MVP 主要功能

| # | 模块 | 功能 | 交付文件 |
| --- | --- | --- | --- |
| F1 | 任务集 | 10 条最小任务：用户话术、目标 DB 终态、策略约束、必须告知项 | `tasks/mvp_10.jsonl` |
| F2 | 领域环境 | 3 张表的 SQLite 快照、6 个带副作用的工具、一份策略文档；每 trial 独立重置 | `env/schema.sql` `db.py` `tools.py` `policy.md` |
| F3 | 模型层 | `Backend` 抽象；`ScriptedBackend` 默认（确定性）、`OpenAIBackend` 可选 | `runtime/model.py` |
| F4 | 最小运行时 | while 循环 + 结构化工具调用；仅 `MAX_STEPS` 与 JSON 失败重试 2 次两条护栏 | `runtime/loop.py` `guard.py` |
| F5 | 规则用户模拟器 | 按任务预置话术逐轮发话，不调模型 | `eval/user_sim.py` |
| F6 | 批量执行 | 串行跑 10 条 × k 次，每条独立环境 | `eval/runner.py` |
| F7 | 终态判分 | DB 逐字段 diff + 策略遵循 + 必要告知检查，输出 0/1 | `eval/grader.py` |
| F8 | 全链路 trace | 每步一个 span 写 jsonl | `eval/trace.py` |
| F9 | 指标与结果表 | pass¹、pass³、平均步数、失败类型分布、成本与延迟 | `eval/analyze.py` `experiments/mvp_baseline.md` |
| F10 | CLI 入口 | 一条命令跑完全流程并产出证据 | `eval/run.py` |

## 6. 数据契约（最小字段）

- **任务**：`id` / `user_utterances` / `initial_db` / `target_db` / `policy_checks` / `required_disclosures`
- **trace span**：`trial_id` / `step` / `role` / `tool` / `args` / `observation` / `tokens` / `latency_ms`
- **结果行**：`task_id` / `trial` / `passed` / `failure_type` / `steps` / `tokens` / `latency_ms`

字段名实现时可微调，但三者必须能用 `trial_id` 串起来，否则失败归因做不了。

## 7. 明确不做（非目标）

- 上下文压缩、幂等键、人工审批、熔断与退避等完整护栏 —— **留给 W3 逐项加，作为对照实验自变量**
- Web trace 回放页 —— 先看 jsonl
- FastAPI / 队列 / 并发 / Docker Compose —— W5
- LLM-as-Judge —— 判分必须确定
- 165 条全量任务集、telecom 中文扩展集、LangGraph 等框架对照 —— W4 / W6
- 模型训练、RL、论文式算法改进 —— 与岗位目标错配

## 8. 验收标准

- `python -m eval.run --tasks tasks/mvp_10.jsonl --k 3` 跑完 30 个 trial，产出结果表与 traces
- ScriptedBackend 下同一配置连跑两次，结果逐字节一致（写成测试断言）
- 单测覆盖：环境重置隔离、grader 正例/反例、`MAX_STEPS` 触发、trace 完整性、runner 重复次数
- 结果表数字真实、不修饰；基线难看是资产
- 每个改动按 `AGENTS.md`：补测试 → 全部验证通过 → 一个对应 commit

## 9. 路线衔接

MVP ≈ W1 全部 + W2 的判分与 trace。拿到基线后：W3 按失败归因逐项加护栏（一次只改一个变量）、W4 做 4–6 组对照实验出成功率/成本帕累托图、W5 服务化与回放页、W6 扩展任务集与结果文档。

> 任务格式对齐 τ-bench 的终态判分口径，但 MVP 使用自建 10 条最小任务；公开任务集在 W4 接入。

> 现状更正（2026-11-08）：本节规划里的「W4 做 4–6 组对照实验出成功率 / 成本帕累托图」、W5 服务化与回放页、W6 扩展任务集、以及上句「公开任务集在 W4 接入」**均未实施**，且「4–6 组对照 / 成本帕累托图」已按收口停止线明确取消；当前任务集只有 `mvp_10` / `mvp_30`。实际交付以 README「开发路线」与 `experiments/` 下归档为准。原文按纪律保留、不回溯改历史设计文档的字。
