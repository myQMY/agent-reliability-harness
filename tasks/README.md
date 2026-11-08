# MVP 任务集

`mvp_10.jsonl` 是 10 条自建最小任务，`mvp_30.jsonl` 是 W3 收口后新增的
**v2 扩展测量标尺**（前 10 行逐字节沿用 `mvp_10.jsonl`，另加 20 条独立任务）。
每行一个 JSON 对象。任务集是**测量标尺**，不是被测对象；被测量和改进的是
`runtime/` 与 `eval/` 这条链路。两个文件都是版本化 fixture，一经提交不得修改。

## 字段

| 字段 | 含义 |
| --- | --- |
| `id` | 任务编号，`trial_id = <id>-t<trial>` |
| `user_utterances` | 规则用户模拟器逐轮发话的话术；多句即多轮 |
| `initial_db` | 本 trial 的 SQLite 初始快照 |
| `target_db` | 期望终态。只需写**发生变化的行**，未提及的表 / 行从 `initial_db` 继承；`audit_log` 若出现则整体覆盖 |
| `policy_checks` | 本任务适用的策略检查器，枚举见 [`env/policy.md`](../env/policy.md) |
| `required_disclosures` | 最终回复文本必须包含的全部子串 |
| `script` | **基线 Agent 的行为脚本**：`ScriptedBackend` 逐字照念，`SeededBackend` 以其为「意图动作」再注入可复现故障，见下 |
| `utterance_rules` | 可选的条件话术规则，见下 |

## 关于 utterance_rules：可选的条件话术

`utterance_rules` 用于「Agent 上一轮反问时换一句更合适的话」。每项形如：

```
{"turn": 2, "text": "订单 O-102，退 3000 分，请继续。", "when": {"final_contains_any": ["哪", "请问"]}}
```

- `turn` 是 1-based 轮次，必须 ≥ 2（第 1 轮没有上文）；命中首条规则就用 `text` 替换该轮预置话术，否则沿用 `user_utterances`。
- `when` 可含 `final_contains_any`（上一轮 final 文本命中任一子串）、`tool_called`（上一轮调用过某工具）、`termination`（上一轮终止原因）；多个条件需同时满足，至少要有一个条件。
- 规则是**确定性字符串匹配**，不调模型。未知字段、空条件、非法轮次会在解析时直接报错，避免「写了但没生效」。
- MVP 的 T10 带一条规则，但 ScriptedBackend 第一轮回复的是订单状态、不含反问词，因此默认跑批仍走原话术「那帮我退 3000 分吧。」，**W1 冻结基线不受影响**。接真实模型反问或 W4 扩任务集时，这条能力才产生区分度。

## 关于 script：它不是答案

`script` 描述的是「基线 Agent 在这个任务上依次输出什么」，属于评测 fixture，
不是任务的正确答案。正确答案是 `target_db` + `policy_checks` + `required_disclosures`。

- 元素是对象时，会被序列化成模型输出文本；写成**字符串**时原样输出，
  用来制造非法 JSON / 缺字段这类失败。
- 脚本用尽后会重复最后一项：以 `final` 结尾的脚本正常结束，
  以工具调用结尾的脚本会一直循环到 `MAX_STEPS`。
- 脚本**故意包含缺陷**：超额退款、重复退款、取消后退款、选错工具、缺参数、
  连续输出非法 JSON、死循环、漏掉必要告知。这是设计文档「故意留坑」的要求：
  基线难看是资产，这些失败就是 W3 逐项加护栏时的对照样本。
- `SeededBackend`（W2.5 校准）同样读这份 `script`，但把它当作**意图动作**：
  按固定 seed 在部分步骤上替换成坏 JSON / 错参数 / 错工具 / 重复写，让同一任务
  k 次结果可能不同。扰动只发生在后端层，**任务定义、`target_db` 与判分口径都不变**；
  脚本里固有的缺陷原样保留，`final` 不参与注入。校准数字不构成能力结论。

10 条任务按设计覆盖 6 类失败归因（意图误解 / 工具选错 / 参数错误 / 策略违规 /
循环或超时 / 输出格式失败）以及正常通过、重试后修正、多轮对话三种情形。
具体通过率不在此预写，以 `experiments/` 下实跑的结果表为准。

## 领域

retail 最小域：2 位客户、3 笔订单，3 张表（`customers` / `orders` / `audit_log`），
6 个工具（3 读 3 写）。金额单位统一为分，避免浮点。

任务格式对齐 τ-bench 的「数据库终态判分」口径，但 MVP 使用自建 10 条；
公开任务集按路线 W4 才接入。

> 现状更正（2026-11-08）：W3 收口时另开版本化扩展集 `mvp_30.jsonl`（前 10 行逐字节沿用 + 20 条独立任务）；τ-bench 公开集与中文扩展集**至今未接入**。上句「按路线 W4 才接入」是当时的规划，不是已交付能力；原文按纪律保留、不回溯改字。

## 复现

在仓库根目录执行：

```
python -m eval.run --tasks tasks/mvp_10.jsonl --k 3
```

产物：`results/result_rows.jsonl`（结果行）、`traces/<trial_id>.jsonl`（全链路 span）、
`experiments/runs/<run_id>/report.md` 与同目录 `config.json`（报告与生效配置快照）。
前两者不入库，留档报告入库。`experiments/mvp_baseline.md` 是 W1 冻结基线，只读，
任何运行都不会覆盖；详见 [README.md](../README.md)。

## v2 扩展任务集 `mvp_30.jsonl`

[分辨率审计](../experiments/w3_resolution.md) 证明 `passk = passk_hits / task_count`，
分辨率恒为 1/N、与 k 无关；而 10 条任务里 7 条是脚本固有缺陷任务，在任何 seed、
任何护栏组合下都恒失败、**不携带信息**，这把尺子实际只有 0.00~0.30 共 4 档。
真正变细只能新增独立任务，不能靠提高 k，也不能把固有缺陷从分母里剔除
（那只会让步进变粗，且等于人为挑选样本）。

### 构造规则

- **前 10 行逐字节取自 `mvp_10.jsonl`**，7 条固有缺陷任务一个都没剔除；
  它们既是归因分层的重要样本，也保证与既有留档可比。
- 后 20 行是 `T11`~`T30`，**脚本全部正确**（没有坏 JSON / 错工具 / 缺参数 /
  超退 / 死循环）。理由不是「让数字好看」：恒失败任务再复制一批只会让分母
  变大、名义步进变细，可达动态范围却不变。正确脚本让成败只由「注入故障
  是否被护栏恢复」决定，因此每条都是真正携带信息的独立测量位点。
- 20 条覆盖 3 笔订单、2 位客户、不同退款金额、单轮与多轮、读 / 退款 / 取消
  三类动作，脚本长度 2~5 步不等（= 不同数量的故障注入 cursor）。
- 每条任务复用同一份 `initial_db` 快照，但每个 trial 独立 `reset`；`task_id`
  进入 `SeededBackend` 的 SHA256 RNG 派生，随机流彼此独立。
- `target_db` 只写发生变化的行，只读任务写 `{}`；`policy_checks` 与
  `required_disclosures` 沿用同类既有任务的口径，**判分逻辑一个字都没改**。

### 生成器

`build_mvp_30.py` 是**设计期工具**，不是运行时依赖：它只读 `mvp_10.jsonl` 与
硬编码的规格表，`--check` 只做只读一致性校验，不写文件。

```
python tasks/build_mvp_30.py --check
```

`mvp_30.jsonl` 是**版本化 fixture，一经提交不得再生成或修改**。守门测试钉死：
前 10 行逐字节等于 `mvp_10.jsonl`、30 行且 ID 唯一、20 条新任务在
`ScriptedBackend` 下 k 次全部通过（证明脚本 / `target_db` / 判分三者一致），
以及生成规则与产物逐字节一致。

### 纪律与边界

- **既有全部 W3 留档、实验配置与分析模块仍指向 `mvp_10.jsonl`**，一个都没改；
  `robustness.py` / `ceiling.py` / `resolution.py` / `attribution.py` 的版本化
  常量原样保留，两组数字不可混用、不可拼接成一条曲线。
- **任务集是第三个自变量**。两个任务集的 `pass^k` 分母与任务构成都不同，
  跨任务集的数值对比只描述分辨率属性，**不是「护栏效应变大」的因果结论**；
  单变量因果证据仍以 `experiments/w3_robustness.md` 与各 `*_sweep.md` 为准。
- 新任务脚本正确 ≠ 任务更容易；`pass^k` 仍是二值化指标，步进变细不等于
  效应量变连续，k 次全过的门槛仍在。
- 全程确定性模拟器，不外推真实模型、不构成成本 / 延迟结论。

完整对照证据与口径边界见 [experiments/w3_taskset_scale.md](../experiments/w3_taskset_scale.md)，
跑法：`python -m eval.taskset_scale`。
