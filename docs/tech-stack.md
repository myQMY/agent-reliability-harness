# 技术实现方案：Agent 可靠性评测与运行时平台

> 状态：选型已确认，W1 全部、W2 全量（判分 / trace / 真实模型观测 / 条件话术 / trace 归因）、P0 实验记录体系、W2.5 测量链路校准，以及 W3 两条护栏（`schema_validation` / `idempotency`）的 2×2 全因子实验与多种子稳健性检验已落地。本文只定技术实现口径，不修改 [MVP 产品设计](mvp-design.md) 已确认的功能范围与验收标准；不写任何未经验证的结果数字。
> 适用阶段：W1–W3（已落地），并给出 W4–W6 的依赖升级点。整体定位见 [README.md](../README.md)，协作规则见 [AGENTS.md](../AGENTS.md)。

## 1. 总体结论

**按「测量仪器」选栈，而不是按「Agent 应用」选栈。**

Python 3.11+ 标准库为主，`sqlite3` 作领域库，JSONL 作任务 / trace / 结果行的载体，`unittest` + `argparse` 作测试与 CLI，MVP 全程**零第三方依赖**。实现时 `OpenAIBackend` 用标准库 `urllib` 直接调用 OpenAI 兼容的 HTTP 接口，连可选后端都不需要 SDK，因此依赖清单实际为零；该开关仅在 `--backend openai` 时启用。

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
- 依赖声明：仓库无 `requirements.txt`，也不需要。真实模型路径只用标准库 `urllib.request`，默认路径 `python -m eval.run` 在裸环境下可直接运行。
- `.env` 只承载密钥与后端地址，不承载实验参数；实验参数必须版本化入库（见 §5）。
- 实验配置已落地为 `experiments/config.toml`，由 `eval/config.py` 用 `tomllib` 读取；未知字段直接报错，配置里不允许出现「写了但没接上」的占位项（见 §5.1）。

## 4. 分层实现约定

### 4.1 领域环境 `env/`

- `sqlite3` 直连 + 手写 `schema.sql`，**不用 ORM**。判分需要一次性把全库 dump 成可比对结构（`table → rows → 字段`），直接 SQL 比 ORM 更短、更可控，也不会把对象生命周期带进评测链路。
- 每个 trial 从**同一份不可变模板快照**重置：内存库配合 `Connection.backup()`，或复制模板库文件，实现时二选一，要求是 trial 之间零残留、模板本身不被写入。
- 工具 API 是普通 Python 函数 + 一张注册表（工具名 → 参数 schema → 函数）。schema 手写，不用反射生成框架，也不用第三方 schema 库。
- 策略规则以 `policy.md` + 可执行的 `policy_checks` 数据表达，判分阶段只做规则比对，不调模型。工具层故意不拦截策略，否则「策略违规」这类失败根本不会出现。

### 4.2 Agent 运行时 `runtime/`

- `Backend` 用 `typing.Protocol` 定义，三个后端各自实现，切换只改配置不改 `loop.py`：`ScriptedBackend`（默认，按任务预置脚本返回、无网络无随机）、`SeededBackend`（W2.5 校准器，见下）、`OpenAIBackend`（可选真实模型）。`generate()` 返回 `ModelReply(text, prompt_tokens, completion_tokens, latency_ms)`：观测是「一次模型调用」的属性，只能在模型层产生；两个本地后端四个数值恒为 0，OpenAI 后端取响应 `usage` 与墙钟耗时。
- **`SeededBackend` 是确定性伪随机模拟器，零成本、零密钥、不接真实模型**。RNG 由 `(seed, task_id, trial, cursor)` 经 `hashlib.sha256` 显式派生后喂给 `random.Random`，不用内置 `hash()` / 时间 / UUID / 全局随机源，因此跨进程可复现。它把任务 `script` 当作「意图动作」，按 `fault_per_mille` 注入四类可复现故障（坏 JSON / 错参数 / 错工具 / 重复写）；注入只替换本次返回文本、cursor 恒前进 1 步、每个 cursor 至多一次，故不会吃光 `MAX_STEPS`。脚本里固有的缺陷（坏 JSON、错工具等）原样保留，final 不参与注入。
- **W3 修复协议**：注入时被替换的干净动作存为 `_pending_correct`，仅当**紧邻的下一次** `generate` 且 `messages` 末条以 `GUARD_REJECT_PREFIX`（定义于 `runtime/guard.py`）开头时才重发，形成「护栏拦下 → 后端改对」的可测量链路。纠正机会只活一次，避免一次 `wrong_tool` 故障残留的干净动作被后面某次无关拦截误触发；运行时的 `RETRY_HINT` / 工具报错不触发纠正。
- **`schema_validation` 与 `idempotency` 是 W3 已落地的两条护栏**，都由 `[guards].enabled` 开关驱动、默认关闭，共用一个拦截预算与拒绝协议；`loop.py` 的判定顺序固定为 `schema_validation` → `idempotency`，不依赖集合遍历顺序。前者规则与工具层共用 `env/tools.validate_args`（必需字段与类型），只管参数；后者从 `(tool, args)` 经 `hashlib.sha256` 确定性派生幂等键，只拦**已成功执行**的重复写。两条的价值都不在「多查了什么」而在**位置**：工具层报错会让该动作直接丢失，运行时护栏在 `tools.call` 之前拦下，产生 `role=guard` 的 span 并把拒绝前缀写进 messages，让后端有机会重发正确调用。grader 不消费 guard span，**判分口径不因开关改变**；拦截最多 3 次后放行，脚本后端不响应拒绝消息时也能收敛。
- **幂等护栏的状态与边界**：`GuardState` 由 `runner.py` 在一次 trial 创建、跨多个 turn 复用，只存字符串键、不进任何 span 或结果行；不开该护栏时对象根本不创建，因此 OFF 组与既有 `schema_validation` 组产物逐字节不变。写工具清单以 `env/tools.WRITE_TOOLS` 为唯一事实来源，`guard.py` / `grader.py` / `model.py` 共用；读工具重复查询合法、工具层报错的调用不记账、未知工具不归属。**键只在单 trial 内存去重，无持久化、不承诺跨进程幂等**——这是运行时去重，不是幂等存储层。
- `loop.py` 手写 `while` 循环 + 结构化工具调用。W1–W2 严格按 F4 只保留 `MAX_STEPS` 与 JSON 解析失败重试 2 次（留出难看的基线是 W3 的自变量）；W3 起新增的护栏全部走配置开关，OFF 组与 W1 行为逐字等价。`make_span` 把 `ModelReply` 的观测挂到 assistant span，其余 span 保持 0，span 字段仍是设计文档 §6 的 8 个。
- **IO 收敛原则**：模型调用、计时与领域库写入只出现在 `Backend` 与 `env` 两层；`loop.py`、`grader.py` 保持同步纯逻辑、不读时钟，trace span 以内存对象返回、由 `eval/trace.py` 统一落盘。这样 W5 做并发时只需改 `runner.py` 与后端实现，循环与判分逻辑不动。
- MVP 不上 `asyncio`，不上任何 Agent 框架。

### 4.3 评测 Harness `eval/`

- 任务集读 JSONL、实验配置读 TOML；记录类型用 `dataclass` / `TypedDict`，**不用 pydantic**（理由见 §6）。
- `grader.py` 是纯函数：输入终态快照、策略检查项、必须告知项与 trace span 流，输出 0/1、`failure_type`、`reasons`、`first_divergence`；不写文件、不调模型、不读时钟。归因沿 trace 按 step 取**首个可判定偏离点**，判不出记 `unknown`；`failure_type` 保持单标签主归因，并列原因放 `reasons`，避免复合标签把分布表撑爆。
- `runner.py` 串行跑 N × k，每个 trial 独立环境；`user_sim.py` 规则版逐轮发话，可选的 `utterance_rules` 按上一轮 final / 工具 / 终止原因选话术，仍是确定性字符串匹配、不调模型。默认任务集的规则在脚本后端下不命中，标尺不变。
- `run.py` 用 `argparse`，命令形态与设计文档 §8 的验收命令保持一致。

### 4.4 可观测与报告

- `trace.py` 每步一个 span 追加写 JSONL；`analyze.py` 汇总 pass¹ / pass^k、平均步数、失败类型分布、token 与延迟观测。
- 结果表用字符串生成 Markdown 写入 `experiments/`，保持结果可 `git diff`、可追溯；不引入 pandas / Jupyter / matplotlib。
- `guard_matrix.py` 是 W3 的**只读汇总器**：`build_matrix()` 读四组留档的 `result_rows.jsonl` 与生效配置，`render_matrix()` 生成 `experiments/w3_guard_matrix.md` 的 2×2 因子表、主效应（该因子开 / 关两组均值之差）与交互（双重差分）。四条边里 `off → schema` 与 `off → idem` 才是单变量对照，`both` 格一次改两项，只用于算交互。它不重跑评测、不修改留档、不读时钟，同批留档重跑逐字节一致；缺任一格直接报错而不静默补零。浮点残差只做 `|v| < 1e-9 → 0` 的噪声归零，不做任何四舍五入或结果修饰。
- `eval/robustness.py` 是 W3 的**多种子稳健性检验**：矩阵报告只在 `seed=11`、10 条任务、k=3 上跑过一次，`pass^k` 分辨率 0.10，单 seed 的「两条主效应各 +0.10」极易被读成普遍规律。它把完全相同的四格配置在一组硬编码版本化的整数 seed 上重跑，报告效应的正 / 零 / 负符号分布，生成 `experiments/w3_robustness.jsonl`（逐 seed 逐格证据）与 `experiments/w3_robustness.md`。效应公式直接 `from eval.guard_matrix import effects_of`，口径与矩阵报告逐字一致；不读时钟、不读环境变量、不做随机抽样、不引第三方依赖，跑批只写临时目录（各格共用，trace 文件名由 `trial_id` 决定，最多 31 个文件自动清理）。`--from-jsonl` 支持只读重渲染，同命令两次产物逐字节一致。它是描述性统计，**不是显著性检验**，不给置信区间 / p 值；seed 是人为选定的整数而非随机样本，符号分布不能当概率估计。
- `traces/`、`results/` 为运行产物，自动创建、不入库。

## 5. 确定性与可复现（先于编码定下来的硬约束）

「逐字节一致」是本项目最影响代码结构的验收项，比选什么库更优先：

- **禁止进入产物对比范围的非确定值**：wall-clock 时间戳、随机 UUID、真实浮点耗时、无序容器的序列化结果。
- `trial_id` 确定性生成，形如 `task_id + trial index`（可选附配置摘要），不得用随机数或时间。
- **统一序列化出口**：所有 JSONL / 结果文件走同一个函数，`json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"))`；结果行按 `(task_id, trial)` 稳定排序后写出。
- `latency_ms` / `tokens` 由 `ModelReply` 提供：`ScriptedBackend` 与 `SeededBackend` 恒为 0（逐字节一致的前提）；`OpenAIBackend` 是 `usage` + 墙钟耗时取整的真实观测，**明确排除在一致性断言之外，且不参与判分**。计时只在模型层，`loop.py` / `grader.py` 不读时钟。
- **伪随机但可复现**（W2.5）：`SeededBackend` 的随机性只来自固定 seed 的 SHA256 派生流，不同 trial 彼此隔离、同配置连跑两次仍逐字节一致；`tests/test_reproducibility.py` 对两个本地后端都断言这一点。它让 k 次结果可能不同，从而使 `pass^k` 首次具备区分度，但这**不是能力波动**：注入的是人为噪声，数字只用于校准测量链路，**不构成**任何模型能力、成本或延迟结论。
- `failure_type` 用固定枚举字符串（含 `unknown` 兜底），不做自由文本、不做复合标签，保证归因分布可复算；并列原因与偏离点证据另放 `reasons` / `first_divergence`。
- 实验配置与结果表同目录版本化入库，保证每个数字都能在仓库里翻到出处。
- 一致性测试断言同配置连跑两次，`traces/` 与 `results/` 产物逐字节相同。

### 5.1 实验配置与留档（P0 已落地）

W3 的单变量对照实验要求「每组的输入都能翻到出处」，因此先把记录体系做完，再加护栏：

- `experiments/config.toml` 是一次实验的唯一事实来源；`--tasks` / `--k` / `--backend` 等 CLI 参数只覆盖对应字段，**最终生效配置**决定 `run_id`。
- `run_id = <name>-<sha256(配置摘要)[:12]>`，摘要走 `sort_keys` 稳定序列化；不含时间戳、随机值、机器路径与输出目录。改任一字段必得新 `run_id`，同配置必得同一 `run_id`。
- **摘要字段白名单（W2.5 / W3 兼容约束）**：`ExperimentConfig` 新增 `seed` / `fault_per_mille` / `guards` 后，若直接把 `asdict(config)` 全字段喂给摘要，默认配置会多出键，W1 冻结留档的 `run_id`（`baseline_scripted-a346e8acef15`）与其目录、`config.json` 全部失效。因此 `fingerprint()` 与 `config_snapshot()` 统一走 `_core()`：原有 7 个字段恒在，`seed` / `fault_per_mille` 只在 `backend=seeded` 时并入，`guards` 只在**非空**时并入。于是「全部关闭」的配置摘要逐字节等于 W1 / W2.5，开启任一护栏才分叉出新 `run_id`——这正是 W3 单变量 A/B 需要的性质。默认 `experiments/config.toml` 故意既不写 `[seeded]` 值也不开护栏；校准与 A/B 参数另放版本化的 `experiments/calibration.toml`、`experiments/guard_ab_*.toml`。守门测试把默认 `run_id`、默认快照字面量与「测量输入逐字段相同」全部钉死。
- 报告口径随 `backend` 三分支：`seeded` 明确写出它是确定性伪随机模拟器、无语言理解能力、数字不构成能力结论、token / 延迟零值不是成本数据。跨后端的「与冻结基线对照」表前插醒目警告，说明差异来自后端替换，**不能**作为 harness 或护栏有效性的因果证据。
- **冻结基线**：`experiments/mvp_baseline.md` 与 `experiments/baseline/result_rows.jsonl` 只读；`--report` 不再以基线文件为默认值，且指向冻结基线时直接拒绝，防止重跑一次就丢掉对照组。
- 留档模式写 `experiments/runs/<run_id>/` 下的 `report.md`、`config.json` 与 `result_rows.jsonl`；报告内容只依赖结果行、配置与基线行，不含输出路径与时钟，因此同 `run_id` 的报告逐字节一致。结果行入库后可供下一组实验把 `compare` 指过来：此时报告自动切成「与对照组对照（A/B）」版式，并提醒读者以两份 `config.json` 核对是否真的是**单变量**对照；指向 W1 冻结基线时仍用「与冻结基线对照」并保留跨后端警告。
- **2×2 全因子留档（W3）**：`guard_ab_off.toml` / `guard_ab_on.toml` / `guard_ab_idem.toml` / `guard_ab_both.toml` 四份配置的测量输入逐字段相同，唯一自变量是 `[guards].enabled`，由 `tests/test_idempotency_guard.py` 断言；每格的 `run_id`、快照、结果行与报告各自留档。新增护栏名只进 `runtime/guard.py` 的 `GUARD_NAMES` 白名单，未接通的名字在配置校验时直接报错；`_core()` 里 `guards` 仍只在非空时并入，所以既有的 `run_id` 与归档报告逐字节不变。报告口径按已开启护栏**分支**：只开 `schema_validation` 时保留「`invalid_calls` 下降不等于提升」，开启 `idempotency` 时补上「中间指标不变而 `pass^k` 提升」，避免用一份措辞覆盖两类机制不同的护栏。
- 报告口径随 `backend` 分支：脚本后端明确写出 token / 延迟是零值而非成本数据；真实模型后端写明观测来源是 `usage` 与墙钟耗时、不参与判分且不保证可复现。两边都不拿占位数字充当成本结论。报告另按任务列出「归因证据（首个偏离点）」，完整证据在结果行里。
- 「逐字节一致」的验收范围明确为 **ScriptedBackend**；真实模型路径不承诺结果可复现，只保证配置可追溯。

## 6. 依赖策略：按阶段引入，而非按需引入

这是本项目和普通应用最关键的区别——**依赖引入时机与对照实验设计绑定**：

| 档位 | 组件 | 引入时机 | 理由 |
| --- | --- | --- | --- |
| 现在用 | 标准库（§3） | W1 | 守门测试要求零依赖，默认路径可在裸环境跑通 |
| 可选开关 | OpenAI 兼容 HTTP 接口（标准库 `urllib`，无需 SDK） | W2 切真实模型 | 走 `LLM_BASE_URL` 等配置；不开启时零成本、零依赖 |
| 已落地（标准库） | 参数 schema 校验 `validate_args` | W3 第一条护栏 `schema_validation` | 规则与工具层同源，护栏价值在「执行前拦下并触发重发」而非换校验库；**pydantic 全程不引入**——零第三方依赖是更硬的约束，且被测对象必须是自己的代码。真要做「第三方校验库 vs 手写」消融，应作为独立自变量另开一组实验，不与本条护栏混在一起 |
| 已落地（标准库） | SHA256 确定性幂等键 + 单 trial 内存去重 | W3 第二条护栏 `idempotency` | 不引入 Redis / 数据库唯一索引：运行时去重只需要一次 trial 的状态，外部存储会破坏零依赖与逐字节可复现；键由 `(tool, args)` 派生、不含时间或随机值，可写进 guard span 作审计证据。**不承诺持久化或跨进程幂等**，需要那一层能力时应作为独立自变量另开实验 |
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
- 任务的 `target_db` 只需写发生变化的行 / 字段，其余从 `initial_db` 继承；`audit_log` 出现时整体覆盖。

## 8. 测试与验证

- 统一 `unittest`（仅标准库），命令：`python -m unittest discover -s tests -v`。
- 已有守门测试继续守住协作约定、仓库骨架、`.gitignore`、设计文档锚点；本次新增技术方案锚点守门，防止关键选型被悄悄改掉。
- 实现阶段需补的用例：环境重置隔离、grader 正例 / 反例、`MAX_STEPS` 触发、trace 完整性、runner 重复次数、**逐字节一致性**、序列化排序稳定、Backend 可切换。以上用例均已落地于 `tests/test_env.py`、`test_grader.py`、`test_loop.py`、`test_runner.py`、`test_reproducibility.py`。
- W2 余项另补 `tests/test_observability.py`、`test_user_sim.py`、`test_attribution.py`：usage 传播与缺失降级、观测不进判分、条件话术命中与规则校验、首个偏离点、`unknown` 兜底、以及「口径升级后默认分布与冻结基线逐项一致」。
- W2.5 另补 `tests/test_seeded_backend.py`、`test_seeded_calibration.py`：RNG 确定性、四类故障可构造、修复协议、零故障率逐字等价脚本后端、耗尽收敛、默认 `run_id` 字面量与快照字节、seeded 连跑两次逐字节一致、**k=3 跑批 `pass^k < pass¹`**、跨后端对照警告。注意默认 scripted 结果行不能断言「与冻结基线逐字节一致」——冻结基线是 W1 的 9 字段 schema，W2 起结果行多了 `reasons` / `first_divergence`；可守门的是所有测量指标逐项一致。
- W3 另补 `tests/test_schema_guard.py`：护栏规则正反例与单变量边界（未知工具放行）、工具层契约不变、拦截发生在 `tools.call` 之前、guard span 不进 grader、拦截次数上限收敛、`_pending_correct` 只活一次、**两份 A/B 配置的测量输入逐字段相同**、两个既有 `run_id` 与默认快照不变、OFF / ON 两组连跑两次逐字节一致、**ON 组 `pass^k` 严格提升且 `bad_args` 归零**、脚本固有缺陷不被静默修好、A/B 报告版式与「`invalid_calls` 下降不等于提升」的口径提醒。
- W3 第二条护栏另补 `tests/test_idempotency_guard.py`：幂等键确定性与参数顺序无关、写工具清单单一事实来源、读工具 / 失败调用 / 未知工具不拦、`GuardState` 跨 turn 共享、拦截预算收敛、真实跑批里 guard span 只针对写工具、**四份配置构成 2×2 且测量输入逐字段相同**、既有 4 个 `run_id` 与 3 份归档报告逐字节不变、**`idempotency` 让 `pass^k` 提升而 `invalid_calls` 一点不变**、T03 重复写被修好而 T08 缺参数没被静默修好、矩阵主效应 / 交互的纯计算与留档报告逐字节一致、缺任一格报错。守门类 `TestW3FactorialContract` 另钉死源码锚点：幂等键必须走 `hashlib.sha256` 且不含时间 / UUID / 全局随机，矩阵模块不得读时钟。
- W3 稳健性检验另补 `tests/test_robustness.py`：效应公式与 `effects_of` 同源、缺格不静默补零、符号分布纯计算、证据 / 报告与代码逐字节一致、**seed 集硬编码且顺序固定**（防止换 seed 挑结论）、**30 个 seed 的实测分布与 `seed=11` 的偏乐观取值被钉死**、无任何负效应且仅 seed=9001 交互非零、既有 4 个 `run_id` 与矩阵报告不变、CLI 连跑两次与 `--from-jsonl` 逐字节一致、模块不读时钟 / 不用全局随机 / 只导入标准库与本地模块、文档口径守门。
- 每次改动按 `AGENTS.md`：改动 → 补 / 更新测试 → 全部验证通过 → 一个对应 commit → 交付。

## 9. 演进路线（核心层不动）

| 阶段 | 技术变化 | 不变的部分 |
| --- | --- | --- |
| W2 | `ModelReply` 真实观测、条件话术、trace 首个偏离点归因与证据入库（已落地，离线单测覆盖）；真实模型服务待选型验证 | 判分口径、trace 的 8 字段、冻结基线的 pass 数字与失败分布 |
| P0 | 实验配置版本化、`run_id` 留档、冻结基线与报告 diff（已交付） | `Backend` 接口、判分口径、trace 格式 |
| W2.5 | `SeededBackend` 校准 `pass^k` 区分度：零成本、零依赖、确定性伪随机、预留护栏拒绝协议（已交付） | 默认 `run_id`、默认配置快照、冻结基线三件、默认 scripted 的全部测量指标 |
| W3 | 逐项加护栏、一次只改一个变量：`schema_validation` 与 `idempotency` 及其 2×2 全因子主效应 / 交互报告、30 个 seed 的多种子稳健性检验（已交付）；上下文压缩 / 预算 / 退避待续，每组须附多 seed 符号分布 | 判分口径、trace 的 8 字段、冻结基线三件、既有 4 个 `run_id` / 快照、既有归档报告与矩阵报告 |
| W4 | 多组单变量对照实验、结果表与帕累托图 | 任务集与判分口径 |
| W5 | runner 并发化（IO 已收敛）、FastAPI + 读 JSONL 的静态回放页、容器化 | 循环与判分逻辑 |
| W6 | 中文扩展任务集与复现文档 | 上述全部 |

Web 层选型到 W5 再定；届时的回放页是只读 JSONL 的展示层，不需要前端框架与构建链。

## 10. 落地前置动作与开放项

- **范围泄漏已清理**：`.env.example` 中 `EVAL_CONCURRENCY` / `MAX_TOKENS_PER_TASK` / `EVAL_DB_TEMPLATE` 已改为注释并标注 W3 / W5 生效；守门测试增加用例，防止这三个参数以生效配置形式回到 MVP。
- **实验记录体系已落地（P0）**：配置版本化、`run_id` 留档、冻结基线保护、报告 diff 与随后端切换的口径说明均已实现，并由 `tests/test_experiment_config.py` 与守门测试覆盖。W3 起每组实验改一个配置字段即可，不需要再靠命令行人肉记忆。
- **模型服务未定**：`OpenAIBackend` 目前按 OpenAI 兼容 `chat/completions` 实现，已用可注入 opener 做离线单测；若目标服务协议不同，需在 `runtime/model.py` 内加一层适配，`loop.py` 与 grader 不受影响。W2.5 的 `SeededBackend` 只是零成本校准器，不能替代真实模型；`pass^k` 的「偶尔成功」在真实模型下的表现仍待 W2 接服务后验证。
- **W3 两条护栏与 2×2 全因子已落地**：`schema_validation` 与 `idempotency` 都由 `[guards].enabled` 驱动，`loop.py` 在工具执行前拦截、`GUARD_REJECT_PREFIX` 触发后端重发干净动作；四份同后端同 seed 的配置构成 2×2，`guard_matrix.py` 只读留档算主效应与交互。单变量结论只能取 `off → schema` 与 `off → idem` 两条边；`both` 格一次改两项，仅用于算交互。跨后端数字、`invalid_calls` 的升降或持平都不构成因果证据，**论效果只看 `pass^k`**。交互为 0 也只描述本 seed / 本任务集下的可加性，不做显著性推断。后续每组仍须在同后端下只改一个变量；剩余护栏（上下文压缩、token / 步数预算、退避）各自独立成组，不要合并提交。
- **多种子稳健性检验已落地，且证伪了「两条护栏各 +0.10」的普遍化解读**：`eval/robustness.py` 在 30 个版本化 seed 上重跑同一套四格，`seed=11` 是**偏乐观**样本——`schema_validation` 只有 5 个 seed 正效应、25 个零效应（均值 +0.015），`idempotency` 27 个正效应、3 个零效应（均值 +0.095），交互仅 seed=9001 非零；**没有任何 seed 出现负效应**。机制上参数护栏只是把 `bad_args` 变成 `unknown`、任务仍失败，`pass^k` 要求 k 次全过，条件苛刻；幂等护栏直接减少 `policy_violation`，修的是终态。这是描述性统计、**不是显著性检验**，seed 非随机抽样，符号分布不能当概率估计，更不能外推到真实模型。既有矩阵报告与全部留档一字节未动，结论修正只写进新报告与 README。**这一步必须排在第三条护栏之前**：单 seed 结论尚未站稳时继续挑「又能涨 0.10」的护栏，等于放大选择性报告；后续每条护栏（上下文压缩、token / 步数预算、退避）仍各自独立成组，但须附同一 seed 集上的符号分布，只报单 seed 的 A/B 不算可复现结论。
- **开放项：护栏拒绝协议只在模拟器里验证过**。`SeededBackend` 靠 `_pending_correct` 在看到 `[GUARD-REJECT]` 后重发干净动作，它没有语言理解能力；真实模型能否读懂拒绝消息并改对，必须等接 `OpenAIBackend` 后另开实验验证，不能拿 W3 的数字外推。
- **真实观测已接入（W2 余项）**：`Backend.generate` 返回 `ModelReply`，`OpenAIBackend` 读 `usage` 并计时；观测只挂 assistant span、只汇总进结果行，不参与判分。脚本后端恒为零值，逐字节一致仍成立；真实模型路径明确排除出该验收项。
- **归因与话术已接入（W2 余项）**：`grader` 从 trace 首个偏离点判定，把 `reasons` / `first_divergence` 写进结果行；`user_sim` 支持可选 `utterance_rules`。两项升级都保证默认跑批的 pass 数字与 6 类失败分布和冻结基线逐项一致，由 `tests/test_attribution.py` 守门——**测量能力升级不得偷偷改变测量结果**。
- **零第三方依赖**本次按硬约束执行；若后续决定放宽，需同步修改守门测试与设计原则，而不是静默引入。
- 本文不调整产品范围；范围变更走 `docs/mvp-design.md` 并同步守门测试。
