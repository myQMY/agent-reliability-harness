# W4-b 设计：给真实模型路径接上输出级故障注入，检验 H1

> 状态：**用户已于 2026-11-06 确认 `experiments/w4_report.md` §9 的选项 A**。
> 本文是 W4-b 的范围共识、注入口径与执行清单；装置校准已离线完成，
> 两格真实跑批与结论见 `experiments/w4b_report.md`（跑完后生成）。
> W4 首轮的设计与结论见 [w4-design.md](w4-design.md) 与
> [experiments/w4_report.md](../experiments/w4_report.md)，**不回溯改写**。

## 1. 为什么必须另开一组

W4 首轮两格 `pass^k` 均 0.00，纠正探针两组均 **0 次拒绝**。这不是
「responsive 率 0%」，而是**分母为 0**：`openai` 后端没有 `fault_per_mille`
这个自变量，而真实模型在正常路径下 `invalid_calls = 0`、工具调用全部
成功，四条护栏一次都没被触发。W3 离线校准里的 7 次拒绝全部来自注入了
故障的 `SeededBackend`。

因此首轮判定为 **H1 未被检验**（既未证实也未证伪），W3 的
「模型读懂拒绝消息后重发」机制继续不得外推。要让拒绝消息**出现**，
只能给真实模型路径接上同口径的输出级故障注入。

**W4-b 的成功判据不是 `pass^k` 提升**。终态判分含 `audit_log.notify.message`
自由文本精确匹配这一系统性天花板首轮已确认存在，本组不改判分器、不泄露
目标话术，两格 `pass^k` 大概率仍落在地板上。本组要回答的是机制问题：

> **真实模型收到 `[GUARD-REJECT]` 后，下一次输出有没有作出有效响应？**

只要探针分母 > 0 且四态分布可读，W4-b 就完成了它的任务；`pass^k` 仍按
惯例如实报告，但不作为本组成败的判据。

## 2. 注入口径：与 W3 同的是什么、不同的是什么

「与 W3 同口径」不是一句文档承诺，**同一份代码**由
`tests/test_w4b_injection.py` 钉死：

| 维度 | W3 `SeededBackend` | W4-b `OutputFaultBackend` | 是否同源 |
| --- | --- | --- | --- |
| 四类故障 | `bad_json` / `bad_args` / `wrong_tool` / `duplicate_call` | 同左 | **同**：共用 `SEEDED_FAULTS` |
| 判定 RNG | `SHA256(seed\|task_id\|trial\|cursor)` | 同左 | **同**：共用 `fault_rng` / `pick_fault` |
| 坏文本构造 | 模块级 `fault_text` | 同左 | **同**：共用一份实现 |
| 每个 cursor 至多一次、final / 本来就不合法的输出不注入 | 是 | 是 | **同** |
| 替换层次 | 模型输出文本 | 模型输出文本 | **同**：不动工具层 / 判分 / 任务集 |
| 干净动作来源 | 任务脚本 | 底层真实模型当次输出 | 异 |
| `duplicate_call` 重放来源 | 后端自己记下的写动作 | 对话里**确实成功执行过**的写动作 | 异（见 §2.2） |
| 拒绝之后 | `_pending_correct` **硬编码重发**干净动作 | **照常调底层真实模型，只跳过注入** | **本质差异，见 §2.1** |
| 注入率 | W3 归因实验 60 | 300（装置参数） | 异，见 §2.3 |

### 2.1 拒绝后绝不代答（W4-b 成立的前提）

W3 的 `SeededBackend._pending_correct` 在看到 `[GUARD-REJECT]` 时直接重发
被替换掉的干净动作——模拟器没有语言理解能力，这是它唯一能「改对」的方式。
**W4-b 绝不能照搬**：一旦由 harness 代答，测到的又只是脚本行为，真实模型
是否读懂拒绝消息依然未知，整组会退化成一次昂贵的 `SeededBackend` 复跑。

`OutputFaultBackend.generate` 的两条硬规则：

1. `messages` 末条以 `[GUARD-REJECT]` 开头时，**不注入任何故障**，照常调用
   底层 `OpenAIBackend`（一次真实请求），返回模型自己的下一次输出；
2. 每次注入后紧邻的下一次 generate 一律不注入（`_skip_next`），既给模型
   留出响应空间，也避免连续故障吃光 `MAX_STEPS` / 拦截预算。

因此探针里 `reply_text` **必定是真实模型对拒绝消息的响应**，不是扰动文本，
也不是 harness 塞回去的答案。测试
`test_rejection_response_is_a_real_call_and_is_never_faulted` 断言底层后端被
真的再调一次、`injected` 不再增长。

### 2.2 `duplicate_call` 只重放已成功执行的写动作

`idempotency` 护栏只对 `guard_state.executed_write_keys` 里**已成功执行**的
写动作拒绝。若重放一个从未执行过的动作，只会再写一次库、制造不出拒绝。
W4-b 因此从 `messages` 里找最近一条 `role=tool` 且 `ok: true` 的观察，回溯
它前面最近的 assistant 动作，只重放 `WRITE_TOOLS` 里的那一条；找不到时按
W3 口径退化成 `bad_args`。

### 2.3 注入率 300 是装置参数，不是模型能力轴

W3 归因实验用 `fpm=60`。W4-b 取 `300`，理由是**真实模型的动作序列短**：
W4 首轮实测每 trial 平均约 3 步（脚本后端往往 5–6 步），可注入的 cursor
少得多；离线扫描显示 `fpm=60` 在干净脚本上只能覆盖 3 类护栏，
`fpm≥100` 才能四类全覆盖。

> **W3 的 60 与 W4-b 的 300 是两套装置参数，跨组任何数字不可相减、
> 不可拼成一条曲线。** 这里改变的是「拒绝消息出现的频率」，不是模型能力，
> 也不是 `pass^k` 的效应量。

`[seeded]` 段名是 W2.5 校准器留下的历史命名，这里承载的是通用注入参数；
不改段名是为了不动 W3 全部既有配置与归档。`eval/config.py` 在
`backend=openai` 且 `fault_per_mille>0` 时把 `seed` / `fault_per_mille`
并入 `run_id` 与 `config.json`，避免「同 run_id 不同注入率」悄悄覆盖报告；
`fpm=0` 时完全不并入，首轮 W4 两个 `run_id` 已由守门测试钉死为逐字节不变。

## 3. 装置校准：先证明分母只来自注入，再花钱

`python -m eval.w4b_probe` 零网络、零 API 请求、不判分。证据：
[experiments/w4b_probe_dryrun.md](../experiments/w4b_probe_dryrun.md)。

校准脚本**先过滤掉任务集里的 6 类固有缺陷**（含成对的重复写动作），只留
合法工具调用、末尾补一个不参与判分的占位 `final`。这一步是必需的：不过滤
时 `fpm=60` 会出现「注入 0 次却数到 7 次拒绝」——那 7 次全是 W3 的脚本
固有缺陷，装置根本没生效。

`fpm=300, seed=11, k=3` 的校准结果：

| 组 | 注入次数 | guard span | 探针拒绝数 |
| --- | --- | --- | --- |
| OFF（护栏全关） | 14 | **0** | 0 |
| ALL4（四条全开） | 14 | **14** | 14 |
| ALL4 @ `fpm=0` 基准 | 0 | **0**（CLI 强制断言） | 0 |

四类故障与四类护栏全部覆盖。`fpm=0` 基准保证「干净脚本本身不触发护栏」，
所以 ALL4 组数到的拒绝**只能来自注入**。两格注入次数可能不同：护栏拦下后
会多出几次「响应拒绝」的 generate，每次消耗 cursor 且按 §2.1 跳过注入——
这是护栏的处理效应，不是自变量不同。

> **校准里的 responsive 恒等于拒绝数，是 `ScriptedBackend` 按 cursor 前进的
> 机械性质**（被拒后总返回脚本下一项，必然「合法且不同」）。它**不预示
> 真实模型的 responsive 率**，不与 W4-b 实测值相减或比较。

## 4. 实验矩阵

两格、单变量、同一 `mvp_10.jsonl`（**不修改任何已提交任务集**）：

| 格 | 配置 | `guards.enabled` | `compare` |
| --- | --- | --- | --- |
| OFF（对照） | `experiments/w4b_off_flash.toml` | 空 | 空 |
| ALL4（实验） | `experiments/w4b_all4_flash.toml` | 四条全开 | `experiments/runs/w4b_off_flash-f6e556cd7201/result_rows.jsonl` |

- 两格的 `tasks` / `k` / `backend` / `max_steps` / `max_json_retries` /
  **`seed` / `fault_per_mille`** 全部相同；唯一自变量仍是 `[guards].enabled`。
  `eval/w4_preflight.py` 已把注入参数并入单变量字段机器校验。
- `run_id`（含注入参数）由代码确定性算出，配置里的 `compare` 由该 `run_id`
  推导；预检会校验这个硬编码字符串，改了 `name` 忘改 `compare` 直接报 FAIL。
- 产物目录与首轮 W4 **完全隔离**（`traces/w4b_*` / `results/w4b_*`）：
  trace 文件名是 `<trial_id>.jsonl`，共用目录会互相覆盖，探针会读到错的组。
- 模型仍是 DeepSeek `deepseek-flash`，`.env` 三键沿用首轮，不新增服务商变量。

## 5. 执行清单

```bash
# 0) 装置校准：零成本，必须先过（OFF 0 guard span / ALL4 > 0 / fpm=0 基准 0）
python -m eval.w4b_probe --seed 11 --fault-per-mille 300 --k 3 \
  --out experiments/w4b_probe_dryrun.md

# 0.5) 离线预检：跑 OFF 前应为 11 PASS / 1 BLOCKED / 0 FAIL
#      （BLOCKED = OFF 组留档尚未生成，是正常的阶段语义）
PRE="--off-config experiments/w4b_off_flash.toml \
     --on-config experiments/w4b_all4_flash.toml \
     --off-results results/w4b_off --off-traces traces/w4b_off \
     --on-results results/w4b_all4 --on-traces traces/w4b_all4 \
     --out experiments/w4b_preflight.md \
     --max-calls 720 --max-total-tokens 3000000 \
     --price-in 0.15 --price-out 0.6 --currency USD"
python -m eval.w4_preflight $PRE

# 1) 对照组 OFF
python -m eval.run --config experiments/w4b_off_flash.toml \
  --max-calls 720 --max-total-tokens 3000000 \
  --price-in 0.15 --price-out 0.6 --currency USD --max-cost 2.0 \
  --traces-dir traces/w4b_off --results-dir results/w4b_off

# 1.5) 保险闸门：读 results/w4b_off/result_rows.jsonl，
#      invalid_calls 合计 + output_format 计数必须由首轮的 0 变正。
#      若仍全为 0，说明注入没接上：**停止，不要跑 ALL4**。

# 2) ALL4 开跑前预检 --strict，此时应为 12 PASS / READY
python -m eval.w4_preflight --strict $PRE

# 3) 实验组 ALL4
python -m eval.run --config experiments/w4b_all4_flash.toml \
  --max-calls 720 --max-total-tokens 3000000 \
  --price-in 0.15 --price-out 0.6 --currency USD --max-cost 2.0 \
  --traces-dir traces/w4b_all4 --results-dir results/w4b_all4

# 4) 纠正探针：只读两格 trace，复用与装置校准 / 首轮完全相同的尺子
python -m eval.guard_probe \
  --traces-dir traces/w4b_off --label w4b_off \
  --traces-dir traces/w4b_all4 --label w4b_all4 \
  --out experiments/w4b_probe.md

python -m unittest discover -s tests -v
```

预算闸门与首轮完全一致：单格 `--max-calls 720` / 3M tokens / $2.0，
两格合计硬上限 1440 次（闸门作用于**单次** `eval.run`）。注入会让部分 trial
多出几次「响应拒绝」的调用，但按 off-peak 单价两格量级仍是约 ¥1，
远低于上限。价格来源与估算见 [w4-design.md](w4-design.md) §4。

## 6. H1 判定规则（首轮 §2 的条件在这里才真正可判）

首轮写死的可证伪条件以「已经发生拒绝」为前提，本次分母 > 0 后按此判定：

| 观测 | 判定 |
| --- | --- |
| ALL4 组 `guard` span 仍为 0 | 装置未生效，**H1 仍未检验**；按 §7 停止，不得写成证伪 |
| 分母 > 0 且 responsive 率接近 0、或与「原样重复被拒动作」的比例无异 | W3「靠模型读懂拒绝消息后重发」机制在真实模型上**不成立**；W3 数字不得外推，正确动作是改协议（结构化 `tool_calls` / 函数级重试），不是调 prompt 或挑 seed |
| 分母 > 0 且 responsive > 0 | 机制在**输出层**成立：真实模型能读懂并作出有效响应。但**不等于** `pass^k` 提升，**不等于**护栏有效；两个结论分别报，不能互相替代 |

`followup_tool_ok` 按护栏类型分别解读：`idempotency` 的正确响应是「跳过
重复写」，不该期待新的写工具执行，四类不能拿同一列横向比较。

## 7. 口径红线与停止条件

- **输出被注入扰动，不是纯净模型行为**。这句话必须出现在 W4-b 报告第一屏，
  以及任何对外表述里。本组检验的是「对拒绝消息的响应机制」，不是模型能力、
  不是业务成功率、不是护栏的效果量。
- **不改** `mvp_10.jsonl` / `mvp_30.jsonl`（sha256 由守门测试钉死）；
  **不改**判分器、**不泄露**目标话术、**不回溯改写**任何 W3 / W4 首轮归档；
  **不扩**路线表里那「4–6 组单变量对照」——W4-b 是首轮装置缺陷的修复性
  重跑，仍只有两格。
- **不可拼曲线**：W3（脚本后端 + fpm=60）、W4 首轮（真实模型 + 无注入）、
  W4-b（真实模型 + fpm=300）是三套自变量不同的实验，任何数字都不能相减
  或连成趋势。
- 一个模型、10 条任务、每格 30 个 trial：`pass^k` 是描述性统计，
  **不是显著性检验**；`temperature=0` 也不等于逐字节可复现。
- **不要因为 `pass^k` 又全是 0 就判定白跑**：本组主读数是探针四态分布与
  分母，判据见 §1 与 §6。

| 情况 | 动作 |
| --- | --- |
| 装置校准未过 / 预检有 FAIL | 先修，**不许**开跑 |
| OFF 组 `invalid_calls` 与 `output_format` 仍全 0 | 注入没接上，停止，不跑 ALL4 |
| 预检非 READY | 不许带 `--allow-missing-compare` 跑正式实验 |
| 预算闸门触发 / 大量 400 / 401 / 超时 | 立即停，核对用量；不擅自提额重跑，不把请求失败算进通过率或纠正率 |
| ALL4 组 `guard` span 为 0 | 如实写「H1 未被检验」，不得写成证伪 |

## 8. 产物索引

| 产物 | 路径 |
| --- | --- |
| 本文（范围与口径） | `docs/w4b-design.md` |
| 装置离线校准 | `experiments/w4b_probe_dryrun.md` |
| 跑批前预检状态快照 | `experiments/w4b_preflight.md` |
| 两格配置 | `experiments/w4b_off_flash.toml`、`experiments/w4b_all4_flash.toml` |
| 注入器实现 | `runtime/model.py` 的 `OutputFaultBackend` 及共享 `fault_rng` / `pick_fault` / `fault_text` |
| 守门测试 | `tests/test_w4b_injection.py` |
| 实测结论（跑批后） | `experiments/w4b_report.md` |
| 纠正探针（跑批后） | `experiments/w4b_probe.md` |
