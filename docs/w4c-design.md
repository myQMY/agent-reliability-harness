# W4-c 设计：给注入器补一份「被替换的干净动作」只读证据

> 状态：**用户于 2026-11-06 明确确认「继续补只读证据」**（`experiments/w4b_report.md` §9
> 里列出的唯一值得做的后续）。本文在**跑批前**写死范围、配对口径、判定规则与停止条件，
> 防止事后按结果挑口径；实测结论见
> [experiments/w4c_report.md](../experiments/w4c_report.md)（跑完后生成）。
> W3 / W4 首轮 / W4-b 全部归档与结论**不回溯改写**。

## 1. 剩下的是哪一个开放项

W4-b 关闭了「真实模型在输出层能否响应拒绝消息」（17 次拒绝 / 17 次 responsive），
但留下一个更窄、也更硬的问题：

- 注入器只把 `ModelReply.text` 换成坏文本，**真实模型当次原本生成的动作不进 trace**；
- 因此 W4-b 能验证的只是「下一次输出合法且不同于被拒文本」，
  **无法验证「重发的是不是原本那一个动作」**；
- W3 的 `SeededBackend._pending_correct` 是 harness 硬编码重发，有明确参照；
  W4-b 拒绝代答，于是连参照一起没有了。

W4-c 只做一件事：**把被替换掉的干净动作留档成一份新的只读证据**，再用它和
拒绝后的下一次输出做对比。这不是重跑一遍 W4-b 求一个更漂亮的数字，而是把
「装置还答不了的问题」补成可判定的。

> **成功判据不是 `pass^k` 提升，也不是「identical 率要高」。**
> 判分天花板（`audit_log.notify.message` 自由文本精确匹配）未改、注入扰动仍在、
> 自变量与前几组不同——三条都在。本组主读数是**可比事件里
> `identical` / `different` 的分布**，以及这条机制链到底能不能被判定。

## 2. 装置：留什么、留在哪、绝不留在哪

### 2.1 留档字段

`OutputFaultBackend` 每次注入时追加一条 `clean_actions` 记录：

| 字段 | 含义 |
| --- | --- |
| `task_id` / `trial` / `trial_id` | 与 trace、结果行同一套标识 |
| `cursor` | 后端 `generate` 的序号，决定注入判定 |
| `fault_kind` | 四类之一（`bad_json` / `bad_args` / `wrong_tool` / `duplicate_call`） |
| `clean_text` | **注入前** `reply.text`，即底层真实模型当次原本的输出 |
| `injected_text` | 替换后的坏文本（与 trace 里那条 assistant span 逐字节一致） |

`clean_text` 是模型自己生成的动作，**不是任务脚本、不是目标话术、不是 harness
编出来的答案**。它只被读一次、留作事后对照。

### 2.2 落盘位置

新目录 `clean_actions/<group>/<trial_id>.jsonl`，由 `eval.run --clean-actions-dir`
显式指定；只在确有注入时写文件。

- **不进** `experiments/runs/` 归档三件（`config.json` / `result_rows.jsonl` / `report.md`）；
- **不改** trace 既有 8 字段、结果行、判分器、任务集、护栏实现；
- 与 `traces/<group>/` 物理隔离，`guard_probe.events_from_dir` 的 `*.jsonl` glob
  不会误读 side-car；
- 输出路径**不进 `run_id`**（与 `--traces-dir` / `--results-dir` 同惯例）：
  它是观测产物，不是实验自变量；既有全部 `run_id` 由守门测试钉死为逐字节不变。

### 2.3 防代答红线（本组成立的前提）

`clean_text` **绝不**出现在发往模型的任何 payload：不进 `messages`、不进
system prompt、不进拒绝文本、不进工具观察。

- `generate()` 只在内存里把 `reply.text` 记下来，返回给 loop 的仍是坏文本；
- 拒绝之后照常调用底层真实模型、**只跳过本次注入**（沿用 W4-b §2.1），
  绝不读 `clean_actions` 来构造响应；
- 留档是**事后观测**，不是运行时输入。守门测试用 fake opener 捕获 HTTP payload，
  逐字节断言其中不含 `clean_text`。

一旦留档被喂回模型，「重发同一个动作」就又是 harness 代答，整组立刻退化成
一次昂贵的 `SeededBackend` 复跑——这是本组最需要防的失败模式。

### 2.4 与 W4-b 的装置差异只有这一处

`seed=11` / `fpm=300` / 四类故障 / `fault_rng` / `pick_fault` / `fault_text` /
拒绝后不代答 / `_skip_next` / `fpm=0` 不包装，**全部不动**。因此在相同
`(seed, task_id, trial, cursor)` 上，两组的注入判定由同一份代码给出。
两格仍用新 `name` / 新 `run_id` 另开，不覆盖任何既有归档。
真实模型服务侧波动会让实际动作序列不同，所以**不承诺** W4-b 与 W4-c 的
trace 逐字节相同；可比的是装置参数与判定函数，不是逐条输出。

## 3. 只读判定：怎么把两份证据对上

`eval/w4c_probe.py` 纯只读，不重跑模型、不重算判分：

1. 复用 `eval.guard_probe.probe_spans` 从既有 trace 取拒绝事件
   （`trial_id` / `step` / `kind` / `reply_text`）——与 W4 首轮、W4-b 同一把尺子；
2. 从同一份 trace 取该 `guard` span 之前最近的 `assistant` span 文本 =
   `rejected_text`，它就是被注入替换后落盘的那一条；
3. 在 side-car 里按 `(trial_id, injected_text == rejected_text)` 找对应留档；
   同一 trial 内出现多条相同坏文本（如两次 `{not-json`）时按 `cursor` 顺序消费；
4. 比较 `clean_text` 与 `reply_text` 解析出的动作。

配不到留档时先判是不是**连锁拒绝**：`rejected_text` 等于该 trial 里之前
某次拒绝的 `reply_text`，说明这次拦下的是「模型自己刚才的响应动作」，不是
注入直接产生的坏输出。这类事件单列 `chained`，不算装置故障、不进主分母。
其余配不到的一律记 `missing`——它表示「看起来由注入造成的拒绝却找不到
原始动作」，是装置没接上的唯一硬信号。**`missing` 必须为 0** 才允许下结论，
否则按 §7 停止。W4-b 实测里已有 3 个 trial 出现连锁拒绝，所以这个分支
必须在跑批前定义好，不能事后按结果补。

### 3.1 判定分层

| 类别 | 条件 |
| --- | --- |
| `identical` | 两者都是 tool 动作，规范化 JSON 逐字段相同（`tool` + `args` 全等） |
| `different` | 两者都是 tool 动作，但不相同 |
| `final_instead` | 干净动作是 tool，响应是 `final` |
| `unparseable` | 任一侧解析不出动作（如实单列，不猜） |
| `idempotency_skipped` | `idempotency` 类拒绝**单列**：拒绝文本要求「跳过该动作，直接输出下一个工具调用或 final」，本来就不该期待重发同一个动作，**不进主分母** |
| `chained` | 连锁拒绝：被拒文本是该 trial 之前某次拒绝的响应动作，不是注入直接产生，**不进主分母**、不算装置故障 |
| `missing` | 既配不到留档、也不是连锁拒绝——装置没接上的硬信号 |

主分母 `comparable = identical + different + final_instead`。
`idempotency_skipped` / `unparseable` / `chained` / `missing` 单独统计、如实列出。

### 3.2 认识论边界（必须连结论一起讲）

模型**从未见过** `clean_text`：对话里出现的是坏文本 + 拒绝消息。
所以 `identical` 的含义是「模型按同一策略重新推导出了同一个合法调用」，
**不是**「回忆起 / 复述了被拿走的动作」，更不是 harness 把答案还给了它。
`different` 也只说明给了另一个合法动作，不等于「改错了」。
这条边界决定了本组结论的措辞上限，与 identical 率高低无关。

## 4. 离线装置校准（零 API 请求，跑批前必须过）

`python -m eval.w4c_probe --dry-run`，用确定性夹具证明尺子没装反：

| 校准路 | 夹具行为 | 强制断言 |
| --- | --- | --- |
| `fpm=0` 基准 | 不注入 | 0 留档 / 0 拒绝 |
| A：给不同动作 | `ScriptedBackend` 被拒后按 cursor 返回脚本下一项 | `identical = 0`、`different > 0`、`missing = 0` |
| B：重发同一动作 | `_ReplaySameBackend` 被拒后返回上一次被替换的干净动作 | `identical = comparable`、`missing = 0` |

A 与 B 一起证明：尺子既能数出「不同」，也能数出「相同」，且配对不漏事件。
**校准数字不是实验结论**，不与任何实测值相减或比较。

## 5. 实验矩阵与执行清单

两格、单变量、同一 `mvp_10.jsonl`（**不修改任何已提交任务集**）：

| 格 | 配置 | `guards.enabled` | `compare` |
| --- | --- | --- | --- |
| OFF（对照） | `experiments/w4c_off_flash.toml` | 空 | 空 |
| ALL4（实验） | `experiments/w4c_all4_flash.toml` | 四条全开 | `experiments/runs/w4c_off_flash-<hash>/result_rows.jsonl` |

两格的 `tasks` / `k` / `backend` / `max_steps` / `max_json_retries` /
`seed` / `fault_per_mille` 全部相同，唯一自变量仍是 `[guards].enabled`；
`eval/w4_preflight.py` 机器校验。产物目录与前三组完全隔离
（`traces/w4c_*`、`results/w4c_*`、`clean_actions/w4c_*`）。

```bash
# 0) 装置校准（零 API 请求）；0.5) 预检（跑 OFF 前 11 PASS / 1 BLOCKED / 0 FAIL）
python -m eval.w4c_probe --dry-run --seed 11 --fault-per-mille 300 --k 3 \
  --out experiments/w4c_probe_dryrun.md
PRE="--off-config experiments/w4c_off_flash.toml \
     --on-config experiments/w4c_all4_flash.toml \
     --off-results results/w4c_off --off-traces traces/w4c_off \
     --on-results results/w4c_all4 --on-traces traces/w4c_all4 \
     --out experiments/w4c_preflight.md \
     --max-calls 720 --max-total-tokens 3000000 \
     --price-in 0.15 --price-out 0.6 --currency USD"
python -m eval.w4_preflight $PRE

# 1) OFF（带 side-car 输出目录）
python -m eval.run --config experiments/w4c_off_flash.toml \
  --max-calls 720 --max-total-tokens 3000000 \
  --price-in 0.15 --price-out 0.6 --currency USD --max-cost 2.0 \
  --traces-dir traces/w4c_off --results-dir results/w4c_off \
  --clean-actions-dir clean_actions/w4c_off

# 1.5) 保险闸：invalid_calls 合计 > 0 且 clean_actions/w4c_off 非空，
#      否则说明注入没接上，停止、不跑 ALL4。
python -m eval.w4_preflight --strict $PRE    # 应为 12 PASS / READY

# 2) ALL4
python -m eval.run --config experiments/w4c_all4_flash.toml \
  --max-calls 720 --max-total-tokens 3000000 \
  --price-in 0.15 --price-out 0.6 --currency USD --max-cost 2.0 \
  --traces-dir traces/w4c_all4 --results-dir results/w4c_all4 \
  --clean-actions-dir clean_actions/w4c_all4

# 3) 只读判定（两格各跑一遍，OFF 组应为 0 事件）
python -m eval.w4c_probe \
  --traces-dir traces/w4c_off --clean-actions-dir clean_actions/w4c_off --label w4c_off \
  --traces-dir traces/w4c_all4 --clean-actions-dir clean_actions/w4c_all4 --label w4c_all4 \
  --out experiments/w4c_probe.md

python -m unittest discover -s tests -v
```

预算闸门与 W4-b 完全一致：单格 `--max-calls 720` / 3M tokens / $2.0，
作用于**单次** `eval.run`，两格合计硬上限 1440 次，远低于实际用量。

## 6. 判定规则（跑批前写死）

| 观测 | 判定 |
| --- | --- |
| `comparable = 0` | 开放项**仍未检验**（分母为 0），不得写成证伪，也不得写成支持 |
| `identical` 占 `comparable` 多数 | 「拒绝后能重新推导出同一个合法动作」在**输出层**成立；但**不等于** W3 机制被完整验证（W3 靠 harness 代答且任务因此成功），**不等于**护栏有效，**不等于** W3 的 0.1433 → 0.3000 被复现 |
| `different` 占多数 | 该链条在输出层不成立；正确动作是改协议（结构化 `tool_calls` / 函数级重试），不是调 prompt 或挑 seed；**不得**据此说 W3 数字被证伪 |
| 任意结果 | 两格 `pass^k` 仍 0.00 是设计文档写死的预期，不作成败判据；中间指标不作因果解读 |

W4-b 的「17/17 responsive」与本组的 `identical` 率是**两个不同问题**，
不可相减、不可拼成趋势；`responsive` 定义宽、含机械成分，本组不复用那个百分比
当分母。

## 7. 口径红线与停止条件

- **输出被注入扰动，不是纯净模型行为**。这句话必须出现在 W4-c 报告第一屏。
- **不改** `mvp_10.jsonl` / `mvp_30.jsonl`（sha256 由守门测试钉死）；**不改**判分器、
  **不泄露**目标话术、**不回溯改写**任何 W3 / W4 首轮 / W4-b 归档；
  **不扩**路线表那「4–6 组单变量对照」——W4-c 仍只有两格。
- **不可拼曲线**：W3（脚本后端 + fpm=60）、W4 首轮（真实模型 + 无注入）、
  W4-b（真实模型 + fpm=300）、W4-c（同注入 + 多一份只读留档）自变量不同，
  任何数字不可相减或连成趋势。
- 一个模型、10 条任务、每格 30 trial、拒绝事件为十几次量级：
  **描述性统计，不是显著性检验**；`temperature=0` 也不等于逐字节可复现。
- 不因为 `pass^k` 又全是 0 就判定白跑；也不因为 identical 率高就宣布「护栏有效」。

| 情况 | 动作 |
| --- | --- |
| 离线校准未过 / 预检有 FAIL | 先修，**不许**开跑 |
| OFF 组 `invalid_calls` 仍全 0 或 side-car 为空 | 注入没接上，停止，不跑 ALL4 |
| `missing > 0`（拒绝既配不到留档、也不是连锁拒绝） | 装置没接上，停止，不下机制结论；`chained > 0` 本身不触发停止 |
| 预算闸门触发 / 大量 4xx / 超时 | 立即停，核对用量；不擅自提额重跑，不把请求失败算进任何率 |
| ALL4 组 `guard` span 为 0 | 如实写「开放项未被检验」，不得写成证伪 |

## 8. 产物索引

| 产物 | 路径 |
| --- | --- |
| 本文（范围与口径） | `docs/w4c-design.md` |
| 装置离线校准（两路 + fpm=0 基准） | `experiments/w4c_probe_dryrun.md` |
| 跑批前预检快照 | `experiments/w4c_preflight.md` |
| 两格配置 | `experiments/w4c_off_flash.toml`、`experiments/w4c_all4_flash.toml` |
| 留档实现 | `runtime/model.py`、`eval/run.py`、`eval/runner.py` |
| 只读判定模块 | `eval/w4c_probe.py` |
| 守门测试 | `tests/test_w4c_clean_actions.py`、`tests/test_w4c_results.py` |
| 实测结论（跑批后） | `experiments/w4c_report.md` |
| 只读判定结果（跑批后） | `experiments/w4c_probe.md` |
| 两格归档（跑批后） | `experiments/runs/w4c_off_flash-<hash>/`、`w4c_all4_flash-<hash>/` |
