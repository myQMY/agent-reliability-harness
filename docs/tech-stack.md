# 技术实现方案：Agent 可靠性评测与运行时平台

> 状态：选型已确认，待实现。本文只定技术实现口径，不修改 [MVP 产品设计](mvp-design.md) 已确认的功能范围与验收标准；不写任何未经验证的结果数字。
> 适用阶段：W1–W2（MVP），并给出 W3–W5 的依赖升级点。整体定位见 [README.md](../README.md)，协作规则见 [AGENTS.md](../AGENTS.md)。

## 1. 总体结论

**按「测量仪器」选栈，而不是按「Agent 应用」选栈。**

Python 3.11+ 标准库为主，`sqlite3` 作领域库，JSONL 作任务 / trace / 结果行的载体，`unittest` + `argparse` 作测试与 CLI，MVP 全程**零第三方依赖**。唯一预留的可选第三方是 OpenAI 兼容 SDK，仅在切换真实模型时启用，不进入默认执行路径。

明确不引入 Agent 框架。运行时（循环、护栏、上下文、预算）正是本项目要测量和改进的对象；套用 LangChain / LangGraph / LlamaIndex 等于把被测对象换成别人的代码，「证明提升来自 harness 而非换模型」这一核心结论将不再成立。

## 2. 约束来源

技术栈不是自由选型，下面几条已把大部分选择钉死：

| 约束 | 出处 | 技术后果 |
| --- | --- | --- |
| 只依赖 Python 标准库 | `tests/test_project_contract.py` | 依赖清单闭合，MVP 不建默认依赖声明 |
| 确定性判分、`ScriptedBackend` 默认 | 设计文档 §2 | 模型层必须可切换；每次 trial 从同一快照重置 |
| 同配置连跑两次逐字节一致 | 设计文档 §8 | 产物中不得出现时间戳 / 随机 ID / 浮点耗时 / 无序序列化 |
| 唯一使用者，本地跑批 | 设计文档 §3 | 不需要鉴权、服务化、并发、队列 |
| 验收命令形式 | 设计文档 §8 | `argparse` + `unittest`，不引入 CLI / 测试框架 |
| 任务格式对齐 τ-bench 终态判分口径 | 设计文档 §9 | Python 生态主场，`sqlite3` + JSONL 足够 |

结论：换语言（TypeScript / Node）会为了一个本地 CLI 引入双语言，终态 diff、trace 序列化、SQLite 访问全部重写，零收益。

## 3. 语言与基础模块

- **Python 3.11+**（本机 3.13.12）。选 3.11+ 是为了标准库自带 `tomllib`，实验配置可以零依赖读取。
- 按用途使用：`sqlite3`（领域库）、`json`（数据契约与 trace）、`dataclasses` / `typing`（记录类型与 `Protocol`）、`argparse`（CLI）、`unittest`（测试）、`tomllib`（实验配置）、`pathlib`（路径）。
- 依赖声明：MVP 无 `requirements.txt`。切换真实模型时以**独立的可选文件**（如 `requirements-llm.txt`）声明 SDK，保证默认路径 `python -m eval.run` 在裸环境下可直接运行。
- `.env` 只承载密钥与后端地址，不承载实验参数；实验参数必须版本化入库（见 §5）。

## 4. 分层实现约定

### 4.1 领域环境 `env/`

- `sqlite3` 直连 + 手写 `schema.sql`，**不用 ORM**。判分需要一次性把全库 dump 成可比对结构（`table → rows → 字段`），直接 SQL 比 ORM 更短、更可控，也不会把对象生命周期带进评测链路。
- 每个 trial 从**同一份不可变模板快照**重置：内存库配合 `Connection.backup()`，或复制模板库文件，实现时二选一，要求是 trial 之间零残留、模板本身不被写入。
- 工具 API 是普通 Python 函数 + 一张注册表（工具名 → 参数 schema → 函数）。schema 手写，不用反射生成框架，也不用第三方 schema 库。
- 策略规则以 `policy.md` + 可执行的 `policy_checks` 数据表达，判分阶段只做规则比对，不调模型。

### 4.2 Agent 运行时 `runtime/`

- `Backend` 用 `typing.Protocol` 定义，`ScriptedBackend`（默认，按任务预置脚本返回、无网络无随机）与 `OpenAIBackend`（可选）各自实现，切换只改配置不改 `loop.py`。
- `loop.py` 手写 `while` 循环 + 结构化工具调用；护栏严格按 F4 只保留 `MAX_STEPS` 与 JSON 解析失败重试 2 次，**不提前补齐护栏**（留出难看的基线是 W3 的自变量）。
- **IO 收敛原则**：网络与磁盘 IO 只出现在 `Backend` 与 `env` 两层；`loop.py`、`grader.py` 保持同步纯逻辑。这样 W5 做并发时只需改 `runner.py` 与后端实现，循环与判分逻辑不动。
- MVP 不上 `asyncio`，不上任何 Agent 框架。

### 4.3 评测 Harness `eval/`

- 任务集读 JSONL、实验配置读 TOML；记录类型用 `dataclass` / `TypedDict`，**不用 pydantic**（理由见 §6）。
- `grader.py` 是纯函数：输入终态快照、策略检查项、必须告知项，输出 0/1 与 `failure_type`；不写文件、不调模型、不读时钟。
- `runner.py` 串行跑 N × k，每个 trial 独立环境；`user_sim.py` 规则版逐轮发话。
- `run.py` 用 `argparse`，命令形态与设计文档 §8 的验收命令保持一致。

### 4.4 可观测与报告

- `trace.py` 每步一个 span 追加写 JSONL；`analyze.py` 汇总 pass¹ / pass^k、平均步数、失败类型分布、token 与延迟观测。
- 结果表用字符串生成 Markdown 写入 `experiments/`，保持结果可 `git diff`、可追溯；不引入 pandas / Jupyter / matplotlib。
- `traces/`、`results/` 为运行产物，自动创建、不入库。

## 5. 确定性与可复现（先于编码定下来的硬约束）

「逐字节一致」是本项目最影响代码结构的验收项，比选什么库更优先：

- **禁止进入产物对比范围的非确定值**：wall-clock 时间戳、随机 UUID、真实浮点耗时、无序容器的序列化结果。
- `trial_id` 确定性生成，形如 `task_id + trial index`（可选附配置摘要），不得用随机数或时间。
- **统一序列化出口**：所有 JSONL / 结果文件走同一个函数，`json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"))`；结果行按 `(task_id, trial)` 稳定排序后写出。
- `latency_ms` / `tokens` 在 `ScriptedBackend` 下必须确定化：统一记为固定常量，**或**明确排除在一致性断言之外。实现时二选一并写进测试说明，不得写入真实计时。
- `failure_type` 用固定枚举字符串，不做自由文本，保证归因分布可复算。
- 实验配置与结果表同目录版本化入库，保证每个数字都能在仓库里翻到出处。
- 一致性测试断言同配置连跑两次，`traces/` 与 `results/` 产物逐字节相同。

## 6. 依赖策略：按阶段引入，而非按需引入

这是本项目和普通应用最关键的区别——**依赖引入时机与对照实验设计绑定**：

| 档位 | 组件 | 引入时机 | 理由 |
| --- | --- | --- | --- |
| 现在用 | 标准库（§3） | W1 | 守门测试要求零依赖，默认路径可在裸环境跑通 |
| 可选开关 | OpenAI 兼容 SDK | W2 切真实模型 | 唯一第三方；走 `LLM_BASE_URL` 等配置，不开启时零成本 |
| 按阶段引入 | pydantic 或等价 schema 校验 | W3「参数校验护栏」对照组 | 它**本身就是实验自变量**，MVP 就用掉等于提前消耗掉这次消融 |
| 按阶段引入 | 静态图生成（优先脚本直出 SVG） | W4 出成功率 / 成本帕累托图 | 只影响报告层；优先不引绘图栈，结果表仍是 Markdown |
| 按阶段引入 | FastAPI / asyncio / Docker / 静态回放页 | W5 服务化 | 单使用者本地跑批不需要 |
| 全程不引入 | LangChain / LangGraph / LlamaIndex | — | 运行时是被测对象（§1） |
| 全程不引入 | SQLAlchemy 等 ORM | — | 终态比对需要裸库 dump（§4.1） |
| 全程不引入 | LLM-as-Judge | — | 设计文档 §7 已否决，判分必须确定 |

W4 若做框架对照，应作为**独立 backend / loop 实现**并列接入，不与主运行时耦合，避免污染被测对象。

## 7. 数据契约落地

- 任务 / trace span / 结果行三类记录沿用设计文档 §6 的最小字段，**必须能用 `trial_id` 串起来**，否则失败归因不成立。
- 三类记录共用 §5 的统一序列化函数与固定字段顺序；字段名实现时可微调，但「`trial_id` 串联」与「确定性序列化」不可让步。
- DB 终态快照规范化为 `{table: [row, ...]}`，行内字段有序；`grader.py` 在规范化之后逐字段 diff，避免把 SQLite 返回顺序当作语义。

## 8. 测试与验证

- 统一 `unittest`（仅标准库），命令：`python -m unittest discover -s tests -v`。
- 已有守门测试继续守住协作约定、仓库骨架、`.gitignore`、设计文档锚点；本次新增技术方案锚点守门，防止关键选型被悄悄改掉。
- 实现阶段需补的用例：环境重置隔离、grader 正例 / 反例、`MAX_STEPS` 触发、trace 完整性、runner 重复次数、**逐字节一致性**、序列化排序稳定、Backend 可切换。
- 每次改动按 `AGENTS.md`：改动 → 补 / 更新测试 → 全部验证通过 → 一个对应 commit → 交付。

## 9. 演进路线（核心层不动）

| 阶段 | 技术变化 | 不变的部分 |
| --- | --- | --- |
| W2 | 落地 `OpenAIBackend`，接真实模型 | `Backend` 接口、grader、trace 格式 |
| W3 | 逐项加护栏：参数校验、幂等键、上下文压缩、预算；一次只改一个变量 | 判分与执行主链路 |
| W4 | 多组单变量对照实验、结果表与帕累托图 | 任务集与判分口径 |
| W5 | runner 并发化（IO 已收敛）、FastAPI + 读 JSONL 的静态回放页、容器化 | 循环与判分逻辑 |
| W6 | 中文扩展任务集与复现文档 | 上述全部 |

Web 层选型到 W5 再定；届时的回放页是只读 JSONL 的展示层，不需要前端框架与构建链。

## 10. 落地前置动作与开放项

- **范围泄漏待清理**：`.env.example` 中的 `EVAL_CONCURRENCY` 与 `MAX_TOKENS_PER_TASK` 与「MVP 串行」「预算护栏属 W3 自变量」冲突，容易诱导提前上 `asyncio` 或提前加护栏。W1 落地前应注释掉或标注生效阶段。本文只记录此项，未在本次改动中执行。
- **模型服务未定**：若目标服务不是 OpenAI 兼容协议，`Backend` 接口需为工具调用格式 / 流式返回差异留一层适配。
- **零第三方依赖**本次按硬约束执行；若后续决定放宽，需同步修改守门测试与设计原则，而不是静默引入。
- 本文不调整产品范围；范围变更走 `docs/mvp-design.md` 并同步守门测试。
