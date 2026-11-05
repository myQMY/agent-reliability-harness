# agent-reliability-harness

面向多轮工具调用 Agent 的可靠性评测与运行时平台。

> 状态：W1 全部、W2 全量（判分 / trace / 真实模型观测 / 条件话术 / trace 归因）、P0 实验记录体系、W2.5 测量链路校准，以及 W3 四条护栏（`schema_validation` / `idempotency` / `tool_retry` / `json_recovery`）的单变量 A/B、2×2 全因子、多种子稳健性检验、可达上限分析，以及 W3 收口后的 `pass^k` 分辨率与效应掩蔽审计已落地。本文件不写任何未经验证的结论；所有指标以 `experiments/` 下可复现的结果为准。

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
| `experiments/` | 版本化实验配置（`config.toml` / `calibration.toml` / `guard_ab_*.toml`）、冻结基线、按 `run_id` 留档的报告与结果行、`w3_guard_matrix.md` 因子汇总、`w3_robustness.md` 多种子稳健性检验、`w3_tool_retry_sweep.md` / `w3_json_recovery_sweep.md` 跨 seed A/B、`w3_ceiling.md` 可达上限分析、`w3_resolution.md` 分辨率与效应掩蔽审计 |
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

## W3：两条护栏与 2×2 全因子实验

W3 的纪律是**一次只改一个变量、一组结果表、一个 commit**。两条护栏都默认关闭、由 `[guards].enabled` 驱动，共用同一个拦截预算与 `[GUARD-REJECT]` 拒绝协议；判定顺序固定为 `schema_validation` → `idempotency`。

| 护栏 | 管什么 | 不管什么 |
| --- | --- | --- |
| `schema_validation` | 参数必需字段与类型，规则与工具层共用 `env/tools.validate_args`（标准库手写，**不引入 pydantic**） | 工具名合法性、业务策略、坏 JSON、重复写 |
| `idempotency` | 从 `(tool, args)` 经 SHA256 确定性派生幂等键，拦下本 trial 内**已成功执行**的重复写 | 读工具（重复查询合法）、失败调用、未知工具、参数 schema、跨进程幂等 |

两条护栏的价值都不在「多查了什么」，而在**位置**：工具层报错会让这次动作直接丢失；运行时护栏在工具执行前拦下，产生 `role=guard` 的 span 并把拒绝消息写进 messages，让后端有机会重发正确调用。`idempotency` 的状态由 `GuardState` 在整个 trial（可能跨多个 turn）共享，键只在内存里、不持久化；写工具清单以 `env/tools.WRITE_TOOLS` 为唯一事实来源。

四份配置构成 **2×2 全因子**，同后端、同 seed、同任务集，唯一自变量是 `[guards].enabled`：

|  | `idempotency` OFF | `idempotency` ON |
| --- | --- | --- |
| `schema_validation` OFF | `guard_ab_off.toml` | `guard_ab_idem.toml` |
| `schema_validation` ON | `guard_ab_on.toml` | `guard_ab_both.toml` |

```bash
python -m eval.run --config experiments/guard_ab_off.toml    # 对照组
python -m eval.run --config experiments/guard_ab_on.toml     # 只开 schema_validation
python -m eval.run --config experiments/guard_ab_idem.toml   # 只开 idempotency
python -m eval.run --config experiments/guard_ab_both.toml   # 两条都开
python -m eval.guard_matrix                                  # 汇总 2×2 主效应 / 交互
```

四格的测量输入由 `tests/test_idempotency_guard.py` 逐字段断言相同；`name` / `compare` 只决定落盘位置与对照来源，不进入测量链路。三个实验组把 `compare` 指向对照组留档的 `result_rows.jsonl`，报告因此切换成「与对照组对照（A/B）」表——**这才是可以作因果解读的对照**；指向 W1 冻结基线则只是跨后端的数值罗列。关闭护栏时 `guards` 不进配置摘要，W1 / W2.5 的 `run_id` 与快照、以及既有 A/B 报告全部逐字节不变。`eval/guard_matrix.py` 只读取四组留档、不重跑评测，产出 [experiments/w3_guard_matrix.md](experiments/w3_guard_matrix.md)：2×2 表、主效应（开 / 关两组均值之差）与交互（双重差分）。

**本 seed 下的实测结果**：四格 pass³ 依次为 0.10 / 0.20 / 0.20 / 0.30，两条护栏主效应各 +0.10，交互为 0——只说明在**这个 seed、这个任务集、这个故障注入率**下效果可加，不推广为一般结论；每格只有 30 个 trial，也不是显著性检验。`both` 格一次改了两项，只用于算交互，**不能**拿它相对对照组的 A/B 表当单变量因果证据；单变量结论看 `off → schema` 与 `off → idem` 两条边。

**读表时最容易犯的错**：`invalid_calls` 不是成功率的代理指标。参数护栏拦下坏参数会让它下降，但任务仍可能失败（归因从 `bad_args` 变成 `unknown`）；幂等护栏拦下的是工具层**不报错**、只是把终态写坏的重复写，实测 `invalid_calls` 从 7 到 7 一点没动，pass³ 却翻倍。两个方向都成立，所以论效果只能看 `pass^k`。模拟器没有语言理解能力，这组数字只说明 harness 机制可测量，**不构成真实模型能力、成本或延迟结论**；幂等键也只在单 trial 内存去重，不承诺跨进程幂等。

## W3 稳健性：单 seed 结论不可外推

上面的 2×2 全因子报告只跑在 `seed=11` 上：10 条任务、k=3，`pass^k` 的分辨率是 0.10，一个任务翻转就是 ±0.10。「两条护栏主效应各 +0.10」很容易被读成普遍规律。所以在继续加第三条护栏之前，先把同一套四格配置在一组版本化的整数 seed 上重跑，看效应符号能不能重复。

```bash
python -m eval.robustness                 # 30 个 seed × 四格，写证据 + 报告
python -m eval.robustness --from-jsonl    # 只读证据重渲染，不重跑评测
```

`eval/robustness.py` 复用 `guard_matrix.effects_of` 保证口径一致；seed 集硬编码版本化、不读时钟、不做随机抽样，跑批只写临时目录，仓库只多出 `w3_robustness.jsonl`（逐 seed 逐格的完整指标）与 [experiments/w3_robustness.md](experiments/w3_robustness.md)。既有四份配置的 `run_id`、留档报告、`w3_guard_matrix.md` 与冻结基线**一字节未动**——这一轮没有新增护栏，也没有改判分口径。

**多种子实测结论**：`seed=11` 是**偏乐观**的样本。30 个 seed 里 `schema_validation` 只有 5 个正效应、25 个零效应（均值 +0.015），`idempotency` 有 27 个正效应、3 个零效应（均值 +0.095），交互只有 1 个非零（seed=9001）；**没有任何 seed 出现负效应**，两条护栏在本任务集、本模拟器下都没让 `pass^k` 变差。机制上，`schema_validation` 把坏参数拦在执行前，`invalid_calls` 下降但归因变成 `unknown`、任务仍失败；`pass^k` 又要求 k 次全过，条件苛刻，因此多数 seed 下不翻转任务。`idempotency` 直接减少 `policy_violation`，修的是终态，所以稳定得多。

**口径**：这是描述性统计，**不是显著性检验**，没有置信区间与 p 值；seed 是人为选定的整数、不是随机抽样，「30 个里 X 个为正」不能当概率估计，更不能外推到真实模型；全部数字来自确定性模拟器，不构成能力 / 成本 / 延迟结论。因此既有报告里的 +0.10 / +0.10 只描述那一次随机流，**不能外推**为普遍效应。

后续每条护栏仍按「一组一条、单变量 A/B」推进，但每组都应给出同一 seed 集上的符号分布；只报一个 seed 的 A/B 不算可复现结论。

## W3 第三条护栏：工具失败后的确定性重试

这一轮把上面那条纪律做成了可复用能力，同时交付路线表里的「退避」——在本同步确定性 harness 里落地为 **`tool_retry`**：工具层报错后追加 `[GUARD-REJECT]` 要求重发，固定次数重试预算，**不做真实 sleep、不引入指数延迟**（那会破坏逐字节复现，也不是本实验的自变量）。

| 护栏 | 管什么 | 不管什么 |
| --- | --- | --- |
| `tool_retry` | `tools.call` 返回 `wrong_tool` / `bad_args` 后请求重发；**唯一能覆盖 `wrong_tool` 的护栏** | 坏 JSON（未进工具层）、重复写（工具层不报错）、业务策略、脚本固有缺陷 |

与前两条护栏的关键差别在**位置**：`schema_validation` / `idempotency` 在 `tools.call` 之前拦下，不写库、不记 `errors`；`tool_retry` 在报错之后工作，**如实保留报错的 tool span 与 `errors`**，只额外追加一个 `role=guard` 的 span 与一条拒绝消息，trace 因此能还原「报错 → 要求重发 → 重发成功」的完整链路。三条护栏共用 `MAX_GUARD_REJECTIONS` 预算。

四类人为缺陷与机制一一对应：`bad_json` → W1 的 JSON 重试（只提示、不恢复被跳过的动作）；`bad_args` → `schema_validation`（执行前）/ `tool_retry`（失败后）；`wrong_tool` → 仅 `tool_retry`；`duplicate_call` → `idempotency`。**只有带 `_pending_correct` 的可恢复注入故障能被修好**：T07（`refund_money` 错工具）、T08（`refund_order` 缺参数）这类脚本固有缺陷照样产生 guard span，但重发只是让后端推进到下一个动作，任务仍失败——护栏没有越界。

```bash
python -m eval.run --config experiments/guard_ab_retry.toml   # 单变量 A/B（seed=11）
python -m eval.ab_sweep                                        # 同一 30-seed 集的符号分布
python -m eval.ab_sweep --from-jsonl                            # 只读证据重渲染
python -m eval.ab_sweep --treatment idempotency                 # 换一条护栏复用同一能力
```

`eval/ab_sweep.py` 把「OFF / ON 两组 × 30 个 seed」做成通用纯函数 + CLI：seed 集与测量输入全部复用 `eval/robustness.py` 的常量，CLI 不允许覆盖，避免一次检查混入第二个自变量；不读时钟、零第三方依赖、跑批只写临时目录。证据在 `w3_tool_retry_sweep.jsonl`，报告在 [experiments/w3_tool_retry_sweep.md](experiments/w3_tool_retry_sweep.md)。

**实测结果**：单变量 A/B（seed=11）pass³ 0.10 → 0.20、`pass¹` 0.20 → 0.20、`invalid_calls` 7 → 7，唯一翻转的 trial 是 `T01-t2`。放到 30 个 seed 上，Δpass^k 为**正 9 / 零 21 / 负 0**（均值 +0.0300），没有负效应，但远不如 `idempotency`（27/30 为正）；两组合计 `invalid_calls` 241 → 247——`tool_retry` 保留原错误，重发又可能让后续缺陷动作再报一次错，所以这个指标**只会持平或上升**。这是「中间指标不是成功率代理」的第三个方向：下降、持平、上升都不能替代 `pass^k`。

**一个反直觉但更有价值的发现**：`tool_retry` 的正效应 seed 集合与 `schema_validation` **不是包含关系**——前者独有 `{5, 13, 2048, 4096, 12345}`，后者独有 `{9001}`，由 `tests/test_tool_retry_guard.py` 钉死。作用位置不同（执行前 vs 失败后），在不同随机流下各有胜负；**不能因为「多覆盖一类错误」就推断它必然更优**。

口径同上一节：描述性统计、**不是显著性检验**，seed 人为选定、不是随机抽样；全部来自确定性模拟器，不构成真实模型能力 / 成本 / 延迟结论；`[GUARD-REJECT]` 在真实模型下能否被读懂并改对仍是开放项。既有 6 个 `run_id`、5 份归档报告、`w3_guard_matrix.md`、`w3_robustness.*` 与冻结基线全部一字节未动。

## W3 收口：第四条护栏与可达上限分析

第四条护栏 `json_recovery` 补的是四类注入故障里最后一个缺口 `bad_json`。W1 的 JSON 重试只给 `RETRY_HINT` 提示、会**跳过被替换掉的那个动作**；这条护栏保留原提示证据（grader 的 `output_format` 归因链路不变），只额外追加一个 `role=guard` 的 span 与一条 `[GUARD-REJECT]` 消息，让后端重发那一个干净动作。T06 那种脚本固有的连续坏 JSON 没有干净动作可重发，仍收敛到 `TERMINATION_JSON_FAILURE`——护栏不越界。

**最重要的结果是「单变量 A/B 会低估它」**：seed=11 单开是 0.10 → 0.10 的**零效应**，30 个 seed 也只有正 5 / 零 25 / 负 0（均值 +0.0167）；但四条护栏全开把 8 个 seed 补到上限，30/30 个 seed 与「关掉注入故障 + 四条全开」逐 seed 相等。原因不是护栏弱，而是 `pass^k` 要求 k 次全过——其他三类故障还在时，恢复一个 `bad_json` 不产生任何分数变化。**只看单变量 A/B 会得出「这条护栏没用」的错误结论。**

`eval/ceiling.py` 把这件事做成 [可达上限分析](experiments/w3_ceiling.md)：九格 × 30 seed，两个口径陷阱被钉死——

- **fpm=0 不是天花板**：关掉注入故障且不开启护栏时 `pass^k` 只有 0.2000，反而低于四条全开 0.3000，因为 `idempotency` 还修了任务脚本里**固有的重复写**。拿它当分母算「达成率」会得到 >100% 的荒谬数字；W3 的对照组始终是 `fpm=60` 的 OFF 组。
- **剩余 gap 不该由护栏修**：10 条任务里 7 条在任何护栏组合、任何 seed 下都 k 次全不过，全是脚本固有缺陷（坏 JSON、缺参数、错工具、无 final、超退、少说必要告知）。让护栏去修它们等于让评测替模型做对，是作弊不是可靠性提升。清单由证据自动推导、不硬编码任务 ID。

```bash
python -m eval.run --config experiments/guard_ab_json_recovery.toml   # 单变量 A/B 留档
python -m eval.ab_sweep --treatment json_recovery                       # 同一 30-seed 集的符号分布
python -m eval.ceiling                                                  # 九格可达上限分析
python -m eval.ceiling --from-jsonl                                      # 只读证据重渲染
```

**W3 护栏阶段收口**：四类注入故障（`bad_json` / `bad_args` / `wrong_tool` / `duplicate_call`）已有四条护栏覆盖，组合达到当前任务集的可达上限，不再追加「又能涨 0.10」的护栏。路线表剩余的上下文压缩 / token 与步数预算在确定性模拟器上**没有可观测的测量输入**（`prompt_tokens` 恒 0、没有真实上下文窗口、`MAX_STEPS` 是 W1 固定常量），硬塞开关等于换测量仪器；它们要等 W4 接真实模型、构造有成本压力的任务集后才可测。下一步该做的是**新增独立任务**以提高 `pass^k` 分辨率，以及验证 `[GUARD-REJECT]` 在真实模型下能否被读懂并改对。注意：提高 k 不改变分辨率（它恒为 1/任务数），把固有缺陷从分母里剔除也只会让步进变粗；口径纠正与证据见下一节。

## W3 测量仪器审计：分辨率与效应掩蔽

护栏阶段收口后，先把尺子本身校准了一次。[w3_resolution.md](experiments/w3_resolution.md) 用三档 k（3 / 5 / 8）× 30 seed × 三格（`off` / 单开 `schema_validation` / 四条全开）回答三个问题。

**分辨率恒为 1/N，与 k 无关**。`analyze.summarize` 里 `passk = passk_hits / task_count`，分子只能是 0..10 的整数，所以 `pass^k` 只能落在 11 个格点上、最小步进恒为 0.10。实测三档 k 的步进都是 0.10，`pass¹` 也完全相同（只取 trial=1）；k 是难度旋钮，不是分辨率旋钮。**既有多份报告里「扩大任务集或提高 k 能提高分辨率」的后半句是错的**：归档报告按实验记录纪律保留原文、不回溯改字（它们是当时跑批的证据），本文件与 [docs/tech-stack.md](docs/tech-stack.md) 是活文档，直接改并由守门测试钉死。

**实际可用的只有 4 档**。10 条任务里 7 条在任何 seed、任何护栏组合下都 k 次全不过（脚本固有缺陷），真正可翻转的只有 3 条，动态范围 0.00~0.30。清单由证据自动推导、不硬编码任务 ID。

**零效应要拆成两类，不能一刀切解释**。k=3 时 `off → schema` 有 26 个零效应 seed，其中 24 个是护栏在这个 seed 上根本没修好任何 trial，只有 2 个是「修好了 trial 却被 k 次全过门槛吞掉」；k 提到 8 时掩蔽升到 7 个。所以既不能把零效应一律读成「护栏没用」，也不能一律解释成「只是尺子太钝」。相对地，`off → all4` 修好 100 个 trial、翻转 47 个任务、零掩蔽、零回归，三档 k 的平均 `pass^k` 都是 0.3000——组合效应够大时，门槛不再吞掉任何东西。另外单开护栏有 17 次 trial 级 1→0 变差，但逐 seed Δ`pass^k` 没有负值，这是「中间指标不是结果代理」的又一个方向。

```bash
python -m eval.resolution                 # 三档 k × 30 seed × 三格
python -m eval.resolution --from-jsonl     # 只读证据重渲染
```

本审计不引入新指标、不改判分口径、不修改任何既有留档；k=5 / k=8 只是诊断，**主指标仍是 k=3 的 `pass^k`**。它是描述性统计、不是显著性检验，全部来自确定性模拟器，不构成真实模型能力 / 成本 / 延迟结论。真正提高分辨率只能新增独立任务（新的版本化任务集、另开实验组），并把固有缺陷作为归因分层单独报告——不能悄悄改既有任务集，否则既有留档失去可比性。

## 开发路线

> W1 全部、W2 全量、P0 实验记录体系、W2.5 校准，以及 W3 四条护栏、2×2 全因子、多种子稳健性检验、可达上限分析与分辨率审计已落地（护栏阶段收口）；冻结基线见 [experiments/mvp_baseline.md](experiments/mvp_baseline.md)。

| 阶段 | 交付 |
| --- | --- |
| W1 | 领域库 + 最小 Agent + 10 条任务，拿到基线（已交付） |
| W2 | trace + 终态判分 + 批量执行，出失败归因分布（已交付） |
| P0 | 实验配置版本化、`run_id` 留档、冻结基线与报告 diff（已交付） |
| W2 余项 | `ModelReply` 真实观测、条件话术、trace 首个偏离点归因与证据入库（已交付） |
| W2.5 | `SeededBackend` 校准 `pass^k` 区分度，零成本、不接真实模型（已交付） |
| W3 | 逐项修运行时，每组只改一个变量：`schema_validation` / `idempotency` 的 2×2 主效应 / 交互、30 个 seed 的稳健性检验、`tool_retry` 与 `json_recovery` 的跨 seed A/B，以及九格可达上限分析并收口、`pass^k` 分辨率与效应掩蔽审计（已交付）；上下文压缩 / 预算转到 W4 真实模型阶段 |
| W4 | 4–6 组单变量对照实验，出成功率 / 成本帕累托图 |
| W5 | 服务化、并发、容器化、trace 回放、测试 |
| W6 | 中文扩展任务集、结果表、复现文档 |

## 任务集来源

MVP 使用自建 10 条最小任务，格式对齐 τ-bench 的终态判分口径；公开的 τ-bench 系列（retail / airline）按路线 W4 接入，中文扩展集在 W6。任务格式与引用说明见 [tasks/README.md](tasks/README.md)。
