# W4 首轮真实模型实验报告（2026-11-06）

> 本报告是 W4 首轮两格 A/B 的**实测结论**，不是设计文档。
> 数据来源：两份 `experiments/runs/*/result_rows.jsonl`、`report.md`、`config.json`，
> 以及只读探针 `experiments/w4_probe.md`。`results/` 与 `traces/` 被 `.gitignore`
> 排除，复现以归档结果行与本报告为准。
> **本报告不修改、不重算、不回溯改写任何 W3 归档，也不把 W3 数字外推到真实模型。**

## 1. 结论先行

1. **两格 `pass^k` 都是 0.00**（30 trial 全败），A/B 差值 `+0.00`。首轮 A/B **没有分辨率**，
   不能据此判断护栏在真实模型上有效或无效。
2. **纠正探针两组都是 0 次 `[GUARD-REJECT]`、0 次 responsive**。这是**分母为 0**，
   不是「responsive 率接近 0」——护栏全程没有被触发的机会。因此 H1
   **未被检验**，既未证实也未证伪，W3 的「模型读懂拒绝消息后重发」机制
   **继续不得外推到真实模型**。
3. 全 0 是**测量设计与模型行为共同造成**的，不能单归因于任一方：终态判分对
   自由文本精确匹配构成系统性天花板，同时模型也确实存在真实策略违规。
   详见 §5 的可复现重放分析。

**一句话**：W4 首轮没有回答「真实模型能否读懂 `[GUARD-REJECT]`」，
它回答的是「当前实验装置无法提出这个问题」。这本身是可复现的结论，
但**不能包装成护栏有效的证据**。

## 2. 执行与实际用量

| 项 | OFF（对照） | ALL4（实验） |
| --- | --- | --- |
| 配置 | `experiments/w4_off_flash.toml` | `experiments/w4_all4_flash.toml` |
| `run_id` | `w4_off_flash-61491decf48c` | `w4_all4_flash-f6a4ea1674dc` |
| 后端 / 模型 | `openai` / `deepseek-flash` | 同左 |
| trial 数 | 30（10 任务 × k=3） | 30 |
| 真实模型调用 | 89 次 | 92 次 |
| `tokens`（prompt+completion） | 50,817 | 52,478 |
| `latency_ms` 合计 | 106,941 | 113,590 |
| `invalid_calls` | 0 | 0 |
| `guard` span | 0 | 0 |
| trace / results 目录 | `traces/w4_off`、`results/w4_off` | `traces/w4_all4`、`results/w4_all4` |

- 单格预算闸门：`--max-calls 720 --max-total-tokens 3000000 --max-cost 2.0 USD`；
  实际单格 89 / 92 次、约 5.1 万 / 5.2 万 tokens，**远未触发**。另有 1 次
  最小连通性冒烟调用（235 tokens），合计 182 次真实调用、103,530 tokens。
- 跑批时间落在 DeepSeek 的 **peak 时段**（周五 09:47–09:57 UTC，官方定义
  06:00–10:00 UTC 为 peak），单价是 off-peak 的 2 倍。因 trace 未拆分
  prompt / completion，这里只给**上界**：全部按最贵的 peak 输出价
  $1.2 / 百万 tokens 计，约 **$0.124 ≈ ¥0.9**；实际账单以控制台为准。
  预算闸门按 off-peak 单价换算会低估约一半，但离 $2.0 上限极远，不影响判定。
- 跑 OFF 前预检为 `11 PASS / 1 BLOCKED / 0 FAIL`；OFF 留档生成后（ALL4 开跑前）
  预检 `--strict` 为 **12 PASS / READY**，与 `docs/w4-design.md` §5 的阶段顺序一致。

## 3. A/B 结果

> 完整表见 `experiments/runs/w4_all4_flash-f6a4ea1674dc/report.md` §「与对照组对照（A/B）」。

| 指标 | OFF | ALL4 | 变化 |
| --- | --- | --- | --- |
| `pass¹` | 0.00 | 0.00 | +0.00 |
| `pass^3` | 0.00 | 0.00 | +0.00 |
| 平均步数 | 2.97 | 3.07 | +0.10 |
| 无效工具调用率 | 0.00 | 0.00 | +0.00 |
| `policy_violation` | 18 | 21 | +3 |
| `unknown` | 12 | 9 | −3 |

逐任务 10 行全是 `000`。两格 step 分布、失败类型计数的差异**不是护栏造成的**：
ALL4 的 `guard` span 为 0，护栏从未介入。同一 `temperature: 0` 下真实模型
输出仍非逐 trial 确定，这是模型服务侧的波动，A/B 只能作聚合比较。

## 4. `[GUARD-REJECT]` 纠正探针

> 证据：`experiments/w4_probe.md`（由 `python -m eval.guard_probe` 只读两格 trace 生成）。

| 组 | 拒绝数 | 有下一次输出 | 输出合法 | 已改变 | responsive | 后续 tool 成功 |
| --- | --- | --- | --- | --- | --- | --- |
| `w4_off` | 0 | 0 | 0 | 0 | 0 | 0 |
| `w4_all4` | 0 | 0 | 0 | 0 | 0 | 0 |

按护栏类型拆分的四行也都是 0。**分母为 0 时任何比率都无定义**，
不能读成「responsive 率 = 0%」，更不能据此触发 `docs/w4-design.md` §2
写死的「responsive 接近 0 ⇒ 机制不成立」判据——那条判据的前提是
**已经发生了拒绝**。本次正确的判定是：**H1 未被检验**。

## 5. 为什么全 0：三层原因（只读重放，不是新实验）

### 5.1 测量口径：终态判分含自由文本精确匹配

`eval/grader.py` 用 `state != target` 做完整 dict 比对，其中
`audit_log` 的 `notify.message` 要求逐字等于任务里的模板，例如 T01 的
`"订单 O-100 已退款 2500 分"`。真实模型收到的是用户话术与工具 schema，
**没有任何地方告诉它这句通知必须逐字一致**。T01-t1 实测输出：

```
get_order(O-100) → refund_order(O-100, 2500) → notify_customer(C-1, "您的订单 O-100 已退款 25.00 元…") → final
```

三次工具调用全部 `ok: true`、`invalid_calls = 0`，但终态仍判 mismatch。
**30/30 个 trial 都带 `terminal_state_mismatch`**，这是判分口径的
系统性天花板，不是模型「完全不会做」。

### 5.2 模型行为：也有真实错误

不能把全 0 全部推给判分器。`policy:notify_after_refund` OFF 18 / ALL4 20
（策略检查要求通知文本里包含被退款订单号，模型常写成不含订单号的话术），
3 个 trial 的最终回复出现「无法退款」导致 `missing_disclosures`，
ALL4 另有 1 个 `no_refund_after_cancel` 违规。这些是真实行为偏离。

### 5.3 假设性重放：放宽话术后会怎样

从 trace 重放工具调用重建终态（`env.db` + `env.tools.call`，纯只读、不重跑模型），
把 `audit_log` 里 `notify.message` 从精确比对中剔除后统计：

| 判据（每格 30 trial） | OFF | ALL4 |
| --- | --- | --- |
| `orders` 表与 target 一致 | 27 | 27 |
| `required_disclosures` 全部出现在 final | 27 | 27 |
| 所有 `policy_checks` 通过 | 12 | 9 |
| 放宽话术后终态相等 | 9 | 8 |
| **放宽话术后四项全满足（假设性通过）** | **9** | **8** |

> **这是事后假设性重算，不是实验结果，不进 `pass^k`，不与 W3 任何数字比较，
> 也不构成「护栏让分数下降」的证据。** 它只说明：判分口径是天花板之一，
> 但即使放宽，仍有约 2/3 的 trial 因真实策略违规而失败。

## 6. 护栏为什么一次都没触发

- 四条护栏的触发条件分别是：输出不合 schema、重复写、工具返回失败、
  JSON 解析失败。真实模型在两格里 `invalid_calls = 0`、
  `output_format = 0`、工具调用全部 `ok: true`，**没有产生任何可拒绝的动作**。
- W3 里那些 `[GUARD-REJECT]` 来自 `fault_per_mille` 对 `SeededBackend` 的
  **输出级故障注入**；`openai` 后端没有这个自变量。离线校准时全开组能数到
  7 次拒绝，正是因为它用的是注入了故障的确定性模拟器。
- 所以 `0 guard span` 是**实验设计的结果，不是护栏失效**。想检验模型能否
  读懂拒绝消息，必须先让拒绝消息出现。

## 7. 口径边界（面试与对外表述必须连边界一起讲）

- **不说**「护栏在真实模型上无效 / 有害」：护栏根本没触发，A/B 差值为 0
  且两格都在地板上，没有测量压力。
- **不说**「真实模型能力差」：`pass^k = 0` 混合了判分口径与模型行为，
  不是能力的干净测量；27/30 的 `orders` 状态其实是对的。
- **不说**「W3 的 0.1433 → 0.3000 在真实模型上复现/被推翻」：W3 用
  `SeededBackend` + 故障注入，W4 用真实模型 + 工具上下文，自变量不同，
  两组数字**不可混用、不可拼成一条曲线**。
- **不说**「`pass^k` 分辨率为 0」：分辨率恒为 `1/N`（此处 `1/10`），
  与 k 无关；这里是**两格都落在同一个取值上**，是取值范围未被利用。
- **不说**「`responsive = 0`」：分母为 0，比率无定义。
- `invalid_calls`、`policy_violation`、平均步数都是中间指标，
  **论效果只认 `pass^k`**，且本次 `pass^k` 无信息量。

## 8. 可证伪条件判定

`docs/w4-design.md` §2 写死的是：「若实验组的 responsive 率接近 0、
或与『原样重复被拒动作』的比例无异，则 W3 机制在真实模型上不成立」。
本次观测是**没有任何拒绝事件**，前提不成立，因此：

- 判定：**H1 未被检验**（underdetermined），不是「机制被证伪」。
- 结论约束不变：W3 的机制仍**不得外推**到真实模型。
- 按 §2 的精神，正确动作是**改实验装置 / 协议**，
  而不是继续调 prompt 文案、挑好看的 seed，或把 0.00 修饰成别的东西。

## 9. 下一步选项（需确认后执行，本轮未擅自改动）

| 选项 | 做什么 | 代价 / 风险 |
| --- | --- | --- |
| **A（推荐）** | 给 `openai` 后端加与 W3 同口径的**输出级故障注入**，用新配置名 / 新 `run_id` 另开 W4-b 两格，专门给探针制造分母，检验 H1 | 需改代码 + 新守门测试 + 一次约 ¥1 的重跑；必须明确标注「输出被注入扰动，不是纯净模型行为」；不动任何既有归档与任务集 |
| B | 把 `notify` 消息模板写进 system prompt，让真实模型有机会命中精确终态 | 会让 `pass^k` 有分辨率，但等于把目标话术喂给模型，有「评测替模型做对」的嫌疑；W4 本就不追求分数 |
| C | 放宽判分器对自由文本的匹配 | 改变测量标尺，W3 全部数字不可比；必须新增版本化判分器并另开实验组 |
| D | 就此收口，把「真实模型路径暴露了测量仪器的边界」作为 W4 结论，转向面试叙事 | 最省时间；但对「护栏在真实模型上到底有没有用」只能诚实回答「未验证」 |

**建议 A**：它是唯一能在不改判分器、不改任务集、不泄露目标话术的前提下
真正回答 H1 的路径，且延续项目「用可复现实验定位问题」的风格。
若时间优先，选 D 也成立——W3 的归因对照已经是最值钱的证据，
W4 的负面结果如实讲清楚，比硬凑一个好看的正向数字更能扛追问。

## 10. 复现与产物索引

```bash
# 前置：.env 三键（LLM_BASE_URL / LLM_API_KEY / LLM_MODEL），DeepSeek deepseek-flash
python -m eval.w4_preflight --max-calls 720 --max-total-tokens 3000000 \
  --price-in 0.15 --price-out 0.6 --currency USD
python -m eval.run --config experiments/w4_off_flash.toml \
  --max-calls 720 --max-total-tokens 3000000 \
  --price-in 0.15 --price-out 0.6 --currency USD --max-cost 2.0 \
  --traces-dir traces/w4_off --results-dir results/w4_off
python -m eval.w4_preflight --strict --max-calls 720 \
  --max-total-tokens 3000000 --price-in 0.15 --price-out 0.6 --currency USD
python -m eval.run --config experiments/w4_all4_flash.toml \
  --max-calls 720 --max-total-tokens 3000000 \
  --price-in 0.15 --price-out 0.6 --currency USD --max-cost 2.0 \
  --traces-dir traces/w4_all4 --results-dir results/w4_all4
python -m eval.guard_probe --traces-dir traces/w4_off --label w4_off \
  --traces-dir traces/w4_all4 --label w4_all4 --out experiments/w4_probe.md
python -m unittest discover -s tests -v
```

> 真实模型结果**不保证逐字节可复现**（服务侧波动）；归档的 `result_rows.jsonl`
> 是本次实测证据，重跑会写入新的 `traces/` / `results/`，但同配置 `run_id` 不变。

| 产物 | 路径 |
| --- | --- |
| OFF 归档 | `experiments/runs/w4_off_flash-61491decf48c/` |
| ALL4 归档（含 A/B 表） | `experiments/runs/w4_all4_flash-f6a4ea1674dc/report.md` |
| 纠正探针 | `experiments/w4_probe.md` |
| 跑批前预检状态（READY 快照） | `experiments/w4_preflight.md` |
| 探针离线校准（SeededBackend，7 拒绝 / 4 responsive） | `experiments/w4_probe_dryrun.md` |
| 预算估算与实验设计 | `experiments/w4_budget_estimate.md`、`docs/w4-design.md` |
