# W3 可达上限分析：护栏阶段为什么到此收口

> 由 `python -m eval.ceiling` 依据 `w3_ceiling.jsonl` 如实生成；不含生成时间，同命令两次产物逐字节一致。
> 本模块**不新增护栏、不改判分口径、不加配置字段、不修改任何既有留档**：全部既有 `run_id` / 归档报告 / 矩阵 / 稳健性 / A-B 扫描产物与冻结基线一字节未动。

## 结论先行

- **四条护栏全开 == 关掉注入故障 + 四条护栏全开**：`30/30` 个 seed 上 `pass^k` 逐 seed 相等。四类注入故障已被运行时完全恢复，当前任务集 / 判分口径下的护栏收益已到顶。
- **单变量 A/B 会低估这类护栏**：`json_recovery` 单开的 Δ`pass^k` 分布是 正 5 / 零 25 / 负 0（均值 +0.0167），但它把「前三条全开」的 8 个 seed 补到上限（1, 3, 7, 23, 88, 99, 3001, 8192），变差的 seed 0 个。原因不是护栏弱，而是 `pass^k` 要求 k 次全过：其他类别的故障还在时，恢复一个 `bad_json` 不产生任何分数变化。
- **`fpm=0` 不是天花板**：关掉注入故障且不开启护栏时 `pass^k` 平均只有 0.2000，反而低于四条护栏全开 0.3000。因为 `idempotency` 还修了任务脚本里**固有的重复写**（与注入故障无关）。拿 `fpm=0` 当分母算「达成率」会得到 >100% 的荒谬数字。
- **剩余 gap 不该由护栏修**：10 个任务里有 7 个在任何护栏组合、任何 seed 下都 k 次全不过（见下表），全是脚本固有缺陷。让护栏去修它们，等于让评测替模型把任务做对 —— 是作弊，不是可靠性提升。

## 方法与九格输入

| 格子 | 护栏 | 故障注入率 | 用途 |
| --- | --- | --- | --- |
| `off` | 全部关闭 | 60/1000 | W3 对照组：护栏全关 |
| `schema` | `schema_validation` | 60/1000 | 单开 `schema_validation` |
| `idempotency` | `idempotency` | 60/1000 | 单开 `idempotency` |
| `tool_retry` | `tool_retry` | 60/1000 | 单开 `tool_retry` |
| `json_recovery` | `json_recovery` | 60/1000 | 单开 `json_recovery` |
| `all3` | `schema_validation`, `idempotency`, `tool_retry` | 60/1000 | 前三条护栏全开 |
| `all4` | `schema_validation`, `idempotency`, `tool_retry`, `json_recovery` | 60/1000 | 四条护栏全开 |
| `no_fault` | 全部关闭 | 0/1000 | 诊断参照：关掉注入故障，护栏也全关 |
| `no_fault_all4` | `schema_validation`, `idempotency`, `tool_retry`, `json_recovery` | 0/1000 | 诊断参照：关掉注入故障 + 四条护栏全开 |

测量输入固定：任务集 `tasks/mvp_10.jsonl`、k=3、`SeededBackend`、30 个 seed（与 `w3_robustness.md` **完全相同**的一组整数）。seed 列表：`1, 2, 3, 5, 7, 11, 13, 17, 23, 31, 42, 55, 77, 88, 99, 101, 123, 256, 512, 777, 1024, 2026, 2048, 3001, 4096, 6007, 8192, 9001, 12345, 65535`。

> **`fpm=0` 的两格是诊断参照，不是对照组**：它们改了故障注入率这个测量输入，因此**不与 OFF 组做因果对比**，只用来标注「哪些失败来自注入故障、哪些来自任务脚本」。因果结论只取前七格之间「只差 `[guards].enabled`」的对照。RNG 在这两格里根本没被使用，所以它们与 seed 无关 —— 这个性质本身可复现。

> **逐字节可复现**：seed 集、任务集、k、故障率、九格定义全部硬编码并版本化；RNG 由 `(seed, task_id, trial, cursor)` 经 SHA256 派生。模块不读时钟、不读环境变量、不用全局随机源；跑批只写共用临时目录，仓库里只留下证据 JSONL 与本报告。`--from-jsonl` 可从证据只读重渲染，不重跑评测。

## 30 seed 汇总（描述性统计）

| 格子 | 平均 `pass^k` | min | max | 平均 `pass¹` | `invalid_calls` 合计 | `total_steps` 合计 |
| --- | --- | --- | --- | --- | --- | --- |
| `off` | 0.1433 | 0.10 | 0.20 | 0.1833 | 241 | 4050 |
| `schema` | 0.1567 | 0.10 | 0.20 | 0.1833 | 112 | 4094 |
| `idempotency` | 0.2367 | 0.10 | 0.30 | 0.2733 | 241 | 4063 |
| `tool_retry` | 0.1733 | 0.10 | 0.20 | 0.1933 | 247 | 4115 |
| `json_recovery` | 0.1600 | 0.10 | 0.20 | 0.1900 | 244 | 4079 |
| `all3` | 0.2700 | 0.10 | 0.30 | 0.2900 | 112 | 4128 |
| `all4` | 0.3000 | 0.30 | 0.30 | 0.3000 | 113 | 4157 |
| `no_fault` | 0.2000 | 0.20 | 0.20 | 0.2000 | 180 | 4050 |
| `no_fault_all4` | 0.3000 | 0.30 | 0.30 | 0.3000 | 90 | 4050 |

## 核心对照

| 对照 | 结果 | 含义 |
| --- | --- | --- |
| `all4` vs `no_fault_all4` | 30/30 个 seed 相等 | 四类注入故障已被完全恢复；护栏组合达到当前任务集的可达上限 |
| `all3` vs `no_fault_all4` | 22/30 个 seed 相等 | 前三条护栏还留着 `bad_json` 这个缺口 |
| `off` → `all4` | 0.1433 → 0.3000 | W3 四条护栏在本任务集上拿到的全部收益 |
| `no_fault` → `no_fault_all4` | 0.2000 → 0.3000 | 关掉注入故障后护栏仍在涨：涨的是**固有重复写**被幂等去重修好 |
| `all3` → `all4` | 8 个 seed 提升、0 个变差 | `json_recovery` 是补齐残余 gap 的最后一块拼图 |

四类注入故障与运行时机制的归属（`bad_args` 被两条护栏覆盖，其余一对一）：

| 注入故障 | 恢复机制 | 对应护栏 |
| --- | --- | --- |
| `bad_args` | 执行前参数校验 / 工具报错后重发 | `schema_validation` / `tool_retry` |
| `wrong_tool` | 工具报错后重发 | `tool_retry` |
| `duplicate_call` | 幂等键拦截重复写 | `idempotency` |
| `bad_json` | JSON 重试改走 `[GUARD-REJECT]` 协议、重发被跳过的动作 | `json_recovery` |

## 不可恢复的固有缺陷（从证据自动推导，未硬编码任务 ID）

下表是 `no_fault_all4` 里**所有 seed 都 k 次全不过**的 7 个任务；改任务集后本表会自动更新。

| 任务 | `fpm=0` 无护栏归因 | 四条全开后归因 |
| --- | --- | --- |
| `T02` | `policy_violation` | `policy_violation` |
| `T04` | `policy_violation` | `policy_violation` |
| `T06` | `output_format` | `output_format` |
| `T07` | `wrong_tool` | `wrong_tool` |
| `T08` | `bad_args` | `unknown` |
| `T09` | `loop_or_timeout` | `loop_or_timeout` |
| `T10` | `intent_misread` | `intent_misread` |

这些失败分别来自脚本自带的坏 JSON、缺参数、错工具、没有 final、超退 / 取消后退款、以及 final 漏说必要告知。它们与 seed 无关、与故障注入率无关（每 seed 计数恒定），是 W1 时代刻意留下的「难看的基线」。护栏按设计不碰它们：`SeededBackend` 只在注入故障时存 `_pending_correct`，固有缺陷没有可重发的干净动作，拒绝消息只会自然收敛到 `TERMINATION_JSON_FAILURE` / `MAX_STEPS`。

## 两个口径陷阱（读表前先看）

1. **不要拿 `fpm=0` 当 oracle / 天花板**。它只关掉注入故障，脚本固有缺陷仍在；而 `idempotency` 的去重机制天然覆盖脚本里固有的重复写，所以护栏全开可以严格超过它。任何「达成率 = 实测提升 / (fpm=0 − OFF)」的算法都会算出 >100%，那是分母选错了，不是护栏超常发挥。W3 的对照组始终是 `fpm=60` 的 OFF 组。
2. **不要把剩余 gap 读成「护栏还不够强」**。上表的固有缺陷是任务集设计的一部分，不是运行时能修的可靠性问题。要继续提高 `pass^k`，该改的是任务集（把固有缺陷分层标注、或补充可被运行时恢复的故障），而不是让护栏越界替模型做对 —— 那样测到的是评测作弊，不是 harness 的价值。

## 为什么剩余两条路线表护栏当前不可测

- **上下文压缩**：`ScriptedBackend` / `SeededBackend` 没有真实上下文窗口，`messages` 长度不受限、`ModelReply.prompt_tokens` 恒为 0。压缩没有可观测的自变量落点；强行裁 messages 只会改变脚本后端的行为契约，等于换测量仪器。
- **token / 步数预算**：`tokens` 恒 0，步数预算是 W1 起固定的 `MAX_STEPS=12`，改它同样是改测量仪器而不是「加护栏」。且 W3 四条护栏共享的 `MAX_GUARD_REJECTIONS` 已经是运行时预算，与本条不是同一个变量。
- 要测它们需要：接 `OpenAIBackend` 拿真实 token / 延迟、构造有长上下文与成本压力的任务集、并新增对应的观测口径。这属于 W4，不是把开关硬塞进当前 harness。

## 口径与限制

- **不是显著性检验**：没有置信区间、没有 p 值、不做多重比较校正；只是把同一套确定性跑批在多个 seed 上重做一遍，报告逐格取值与符号分布。
- **seed 是人为选定的一组整数，不是随机抽样**：「N 个里 X 个相等」不能当概率估计，更不能外推到真实模型、其他任务集或其他故障注入率。
- **分辨率极粗**：10 条任务 × k=3，`pass^k` 只能取 0.10 的整数倍；`+0.10` 只代表「一个任务从不是 k 次全过变成k 次全过」，不是连续的效应量。
- **全部数字来自确定性模拟器**：`SeededBackend` 没有语言理解能力，注入的是人为噪声；`tokens` / `latency_ms` 恒为 0，**不构成任何真实模型能力、成本或延迟结论**。
- **护栏拒绝协议只在模拟器里验证过**：`[GUARD-REJECT]` 靠 `_pending_correct` 触发重发干净动作；真实模型能否读懂拒绝消息并改对，必须接 `OpenAIBackend` 后另开实验，本表不能外推。
- 效应只做 `|v| < 1e-9 → 0` 的浮点噪声归零，不做任何四舍五入或结果修饰。

## 下一步

- W3 护栏阶段到此收口：四条护栏覆盖四类注入故障，组合已到可达上限，不再追加「又能涨 0.10」的护栏（那只会是零效应或越界）。
- W4：接真实模型，验证 `[GUARD-REJECT]` 协议能否被读懂并改对，并让上下文 / 预算类护栏具备可观测的测量输入。
- 提高 `pass^k` 分辨率：扩大任务集或提高 k，并把固有缺陷分层标注，否则 0.10 的步进会继续淹没真实差异。
- 前端 demo 仍后置。

## 数据来源与复现

每行一格的完整指标（seed / cell / guards / fault_per_mille / pass¹ / pass^k / `invalid_calls` / `total_steps` / `failure_counts` / 逐任务序列）在 `w3_ceiling.jsonl`。单变量 A/B 的正式留档在 `experiments/runs/<run_id>/`。

```
python -m eval.ceiling
python -m eval.ceiling --from-jsonl
python -m unittest discover -s tests -v
```
