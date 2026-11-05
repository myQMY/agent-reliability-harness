# agent-reliability-harness

面向多轮工具调用 Agent 的可靠性评测与运行时平台。

> 状态：W1 全部、W2 全量（判分 / trace / 真实模型观测 / 条件话术 / trace 归因）、P0 实验记录体系、W2.5 测量链路校准，以及 W3 第一条护栏的单变量 A/B 已落地。本文件不写任何未经验证的结论；所有指标以 `experiments/` 下可复现的结果为准。

## 为什么做这个

Agent demo 和能稳定运行的 Agent 之间，差的通常不是模型能力，而是包围模型的那一层工程：工具调用跑飞、JSON 缺字段、重试造成重复扣款、上下文越滚越长，以及最关键的——**无法证明一次改动是否真的让它变好**。

这个项目把「证明变好」做成一套可复现的实验装置：

- 用一批有状态、有副作用、有策略规则的业务任务当标尺；
- 用数据库终态比对自动判分，不依赖主观打分，结果确定且可复现；
- 用 `pass^k` 衡量同一任务重复执行时的稳定性，而不是只报一次成功率；
- 用全链路 trace 做失败归因，再用单变量 A/B 实验逐项验证 harness 改动。

任务集只是标尺；被测量和改进的对象是 Agent 运行时与评测链路本身。

## 目录结构

| 路径 | 内容 |
| --- | --- |
| `tasks/` | 任务集：MVP 自建 10 条最小任务；公开集 W4 / 中文扩展集 W6 接入 |
| `env/` | 隔离业务库、带副作用的工具 API、领域策略规则 |
| `runtime/` | 自研薄运行时：循环、护栏、上下文与预算管理 |
| `eval/` | 用户模拟器、批量执行器、终态判分、归因分析、CLI |
| `web/` | trace 回放与实验对照面板（W5） |
| `docs/` | 产品设计与技术实现方案 |
| `experiments/` | 版本化实验配置（`config.toml` / `calibration.toml` / `guard_ab_*.toml`）、冻结基线、按 `run_id` 留档的报告、配置快照与结果行 |
| `tests/` | 单元测试与回归用例 |
| `traces/`, `results/` | 运行产物，自动创建、不入库 |

MVP 的功能范围、设计原则与非目标见 [docs/mvp-design.md](docs/mvp-design.md)；技术实现口径、依赖策略与确定性约束见 [docs/tech-stack.md](docs/tech-stack.md)。

## 评测口径

- **判分**：对话结束后，将最终数据库状态与目标状态逐字段比对，并检查策略遵循与必要信息告知；全部满足才计 1，否则 0。
- **pass¹**：每个任务首次执行的平均通过率。
- **pass^k**：同一任务连续 k 次独立执行全部通过的概率，用来暴露「偶尔成功」。
- **失败归因**：沿 trace 按 step 顺序取**首个可判定偏离点**作主归因（意图误解 / 工具选错 / 参数错误 / 策略违规 / 循环或超时 / 输出格式失败；判不出如实记 `unknown`）；并列原因与偏离点位置写进结果行的 `reasons` / `first_divergence`。
- **同时观测**：单任务 token 成本、平均步数、p95 延迟、无效工具调用率。`ScriptedBackend` 与 `SeededBackend` 下 token / 延迟恒为 0（不是成本数据）；`OpenAIBackend` 从响应的 `usage` 与墙钟耗时取真实值，**不参与判分**。

## 快速开始

零第三方依赖，Python 3.11+（本机 3.13.12）即可，不需要安装任何包。

```bash
python -m unittest discover -s tests -v
python -m eval.run --tasks tasks/mvp_10.jsonl --k 3
```

一条命令跑完 10 条任务 × 3 次 = 30 个 trial，产出：

- `results/result_rows.jsonl`：结果行，含 `reasons` / `first_divergence` 归因证据（不入库）
- `traces/<trial_id>.jsonl`：每步一个 span 的全链路 trace，assistant span 携带模型观测（不入库）
- `experiments/runs/<run_id>/`：`report.md` 报告、`config.json` 生效配置快照、`result_rows.jsonl` 结果行（均入库；结果行可供下一组实验作对照组）

**实验配置以 `experiments/config.toml` 为唯一事实来源**：后端、k、任务集、两条护栏参数、W3 起的可开关护栏列表、对照基线都在这里；CLI 显式参数只覆盖对应字段。最终生效配置经确定性摘要生成 `run_id`，同配置必得同一 `run_id`，改任一字段就落到新的实验目录，因此做单变量对照实验时每组的输入都可追溯。

`experiments/mvp_baseline.md` 与 `experiments/baseline/result_rows.jsonl` 是 **W1 冻结基线**，只读参照，任何运行都不会覆盖；每次报告自动带「与冻结基线对照」表。显式 `--report` 视为临时报告，且指向冻结基线时会被直接拒绝。

默认使用 `ScriptedBackend`（确定性脚本、零成本、零密钥），token / 延迟恒为 0。切真实模型时复制 `.env.example` 为 `.env` 填入 `LLM_*`，再执行 `python -m eval.run --backend openai --k 3`；该路径走标准库 `urllib`，同样无需安装 SDK，并从响应的 `usage` 读取 prompt / completion token、用墙钟耗时记录 `latency_ms`。真实观测只进 trace 与结果行，不参与判分；报告会如实标注该路径不保证逐字节可复现。

多轮任务可带可选的 `utterance_rules` 条件话术：Agent 上一轮反问时按规则换一句更合适的话，仍是确定性字符串匹配、不调模型。默认任务集的规则在脚本后端下不命中，因此冻结基线保持不变；字段说明见 [tasks/README.md](tasks/README.md)。

任务格式与脚本说明见 [tasks/README.md](tasks/README.md)，实现口径见 [docs/tech-stack.md](docs/tech-stack.md)。

## 三个后端与 W2.5 校准

| 后端 | 用途 | 成本 / 密钥 | 逐字节可复现 |
| --- | --- | --- | --- |
| `scripted`（默认） | 确定性脚本，W1 冻结基线的等价配置 | 零 / 无 | 是 |
| `seeded` | **确定性伪随机模拟器**：校准 `pass^k` 测量链路 | 零 / 无 | 是 |
| `openai` | 接 OpenAI 兼容的真实模型 | 按量 / 需 `LLM_*` | 否 |

`ScriptedBackend` 下同一任务 k 次结果完全相同，`pass^k` 恒等于 `pass¹`——测量链路虽然可信，却还暴露不了「偶尔成功」。`SeededBackend` 以任务脚本为**意图动作**，按 `(seed, task_id, trial, cursor)` 经 SHA256 派生的固定 RNG 注入可复现故障（坏 JSON / 错参数 / 错工具 / 重复写），让不同 trial 结果可能不同，`pass^k` 首次具备区分度；注入率为 0 时逐字等价于脚本后端。

```bash
python -m eval.run --config experiments/calibration.toml
```

校准配置把 `seed` / `fault_per_mille` 版本化在 `[seeded]` 段；这两个字段只在 `seeded` 后端下生效，因此不会改变默认 `run_id` 或 W1 冻结留档。报告与 `config.json` 落在 `experiments/runs/<run_id>/` 入库作为证据。

**必须说清楚的边界**：`SeededBackend` 没有语言理解能力，故障是人为注入的噪声。校准报告里的数字**只证明尺子能分辨偶发失败，不构成任何模型能力、成本或延迟结论**；它与冻结基线是不同后端，报告的「与冻结基线对照」表只是数值罗列，不能当作 harness 或护栏有效性的因果证据。W3 的单变量对照必须在同一后端下、一次只改一个变量进行。

## W3：第一条护栏与单变量 A/B

W3 的纪律是**一次只改一个变量、一组结果表、一个 commit**。第一条护栏是 `schema_validation`：

- 规则与工具层共用 `env/tools.validate_args`（必需字段、类型），用标准库手写，**不引入 pydantic**。pydantic 原本是技术方案里这条护栏的对照锚点，但「零第三方依赖」是更硬的约束，手写 schema 也让「被测对象是自己的代码」这点更清楚。
- 护栏的价值不在多查了什么，而在**位置**：工具层报错会让这次动作直接丢失；运行时护栏在工具执行前拦下，产生 `role=guard` 的 span 并把 `[GUARD-REJECT]` 拒绝消息写进 messages，让后端有机会重发正确调用——这才是「拦下 → 改对」的可测量链路。
- 它**只管参数**。工具名合法性、业务策略、坏 JSON、重复写都不归它管，避免一个实验里混入多个自变量。护栏最多拦截 3 次后放行，脚本后端不响应拒绝消息时也能靠 `MAX_STEPS` 收敛。

```bash
# 先跑对照组（护栏全关），再跑实验组（只开 schema_validation）
python -m eval.run --config experiments/guard_ab_off.toml
python -m eval.run --config experiments/guard_ab_on.toml
```

两份配置的测量输入（`tasks` / `k` / `backend` / `seed` / `fault_per_mille` / 两条护栏参数）逐字段相同，由 `tests/test_schema_guard.py` 断言；唯一自变量是 `[guards].enabled`。实验组把 `compare` 指向对照组留档的 `result_rows.jsonl`，报告因此切换成「与对照组对照（A/B）」表——**这才是可以作因果解读的对照**；`name` 与 `compare` 只决定落盘位置和对照来源，不进入测量链路。关闭护栏时 `guards` 不进配置摘要，所以 W1 / W2.5 的两个 `run_id` 与快照逐字节不变。

**读表时最容易犯的错**：`invalid_calls` 下降不等于成功率提升。任务脚本里固有的缺陷被拦下后依然失败，只是归因可能从 `bad_args` 变成 `unknown`；论效果只能看 `pass^k`。同理，模拟器没有语言理解能力，这组数字只说明 harness 机制可测量，**不构成真实模型能力、成本或延迟结论**。

## 开发路线

> W1 全部、W2 全量、P0 实验记录体系、W2.5 校准与 W3 第一条护栏 A/B 已落地；冻结基线见 [experiments/mvp_baseline.md](experiments/mvp_baseline.md)。

| 阶段 | 交付 |
| --- | --- |
| W1 | 领域库 + 最小 Agent + 10 条任务，拿到基线（已交付） |
| W2 | trace + 终态判分 + 批量执行，出失败归因分布（已交付） |
| P0 | 实验配置版本化、`run_id` 留档、冻结基线与报告 diff（已交付） |
| W2 余项 | `ModelReply` 真实观测、条件话术、trace 首个偏离点归因与证据入库（已交付） |
| W2.5 | `SeededBackend` 校准 `pass^k` 区分度，零成本、不接真实模型（已交付） |
| W3 | 逐项修运行时，每组只改一个变量：参数 `schema_validation`（已交付）、预算、压缩、幂等、退避 |
| W4 | 4–6 组单变量对照实验，出成功率 / 成本帕累托图 |
| W5 | 服务化、并发、容器化、trace 回放、测试 |
| W6 | 中文扩展任务集、结果表、复现文档 |

## 任务集来源

MVP 使用自建 10 条最小任务，格式对齐 τ-bench 的终态判分口径；公开的 τ-bench 系列（retail / airline）按路线 W4 接入，中文扩展集在 W6。任务格式与引用说明见 [tasks/README.md](tasks/README.md)。
