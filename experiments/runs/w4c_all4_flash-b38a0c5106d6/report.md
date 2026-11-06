# 实验结果：w4c_all4_flash / `w4c_all4_flash-b38a0c5106d6`

> 本表由 `python -m eval.run` 依据结果行如实生成，数字未修饰。
> 生效配置来自实验配置文件与显式 CLI 覆盖；留档模式下同目录另有 `config.json` 存证。`run_id` 由配置摘要确定性生成，同配置必得同一 `run_id`。

## 运行配置

| 项 | 值 |
| --- | --- |
| run_id | `w4c_all4_flash-b38a0c5106d6` |
| 后端 | OpenAIBackend |
| 已开启护栏 | `schema_validation`, `idempotency`, `tool_retry`, `json_recovery` |
| 任务集 | `tasks/mvp_10.jsonl` |
| 任务数 | 10 |
| k | 3 |
| trial 总数 | 30 |
| MAX_STEPS（配置） | 12 |
| JSON 失败重试（配置） | 2 次 |
| 领域库 | 内存 SQLite，每 trial 从同一模板 backup 重置 |
| 判分 | 终态逐字段 diff + 策略检查 + 必要告知，全部满足才计 1 |

## 口径与限制（读表前先看）

- **pass¹**：每个任务首次执行的平均通过率。
- **pass^k**：同一任务 k 次独立执行全部通过的任务占比。
- OpenAIBackend 调用真实模型，同一任务 k 次结果可能不同；pass^k 此时才真正用于暴露「偶尔成功」。
- 结果受模型服务影响，不保证逐字节可复现；「同配置连跑两次逐字节一致」这一验收项仅在 ScriptedBackend 下成立。
- `tokens` 来自响应的 `usage`（prompt + completion），`latency_ms` 是单次调用的墙钟耗时取整；两者是真实观测，但**不参与判分**，且随服务波动。
- 无效工具调用率 = 工具层报错次数 / 总步数，来自 trace 的事件计数。
- 失败归因取 trace 中**首个**可判定偏离点作主归因；并列原因与偏离点证据写入结果行的 `reasons` / `first_divergence`，判不出时如实记为 `unknown`。

## 总体指标

| 指标 | 值 |
| --- | --- |
| pass¹ | 0/10 = 0.00 |
| pass^3 | 0/10 = 0.00 |
| 平均步数 | 3.37 |
| 无效工具调用率 | 5/101 = 0.05 |
| token 合计 | 59725 |
| p95 延迟 | 6705 ms |

## 失败类型分布

| 失败类型 | 失败 trial 数 |
| --- | --- |
| intent_misread | 0 |
| wrong_tool | 3 |
| bad_args | 0 |
| policy_violation | 18 |
| loop_or_timeout | 0 |
| output_format | 0 |
| unknown | 9 |

## 逐任务结果

| task_id | 首句话术 | k 次结果 | 全部通过 | 主要失败类型 | 平均步数 |
| --- | --- | --- | --- | --- | --- |
| T01 | 我要退 O-100 这笔订单，退 2500 分。 | `000` | 否 | policy_violation | 2.00 |
| T02 | 我要退 O-100，退 2500 分。 | `000` | 否 | wrong_tool | 3.33 |
| T03 | O-100 帮我退 2500 分，谢谢。 | `000` | 否 | policy_violation | 3.00 |
| T04 | 帮我取消 O-101，另外再退 1000 分。 | `000` | 否 | unknown | 2.00 |
| T05 | 我要退 O-100，退 2500 分。 | `000` | 否 | unknown | 3.67 |
| T06 | 我要退 O-100，退 2500 分。 | `000` | 否 | policy_violation | 2.33 |
| T07 | 我要退 O-100，退 2500 分。 | `000` | 否 | policy_violation | 4.00 |
| T08 | 我要退 O-100，退 2500 分。 | `000` | 否 | unknown | 4.00 |
| T09 | 我要退 O-100，退 2500 分。 | `000` | 否 | policy_violation | 4.33 |
| T10 | 帮我查一下 O-102 的状态。 | `000` | 否 | policy_violation | 5.00 |

## 归因证据（首个偏离点）

> 主归因取 trace 中按 step 顺序的第一个可判定偏离点；这里每个任务只列首个失败 trial，全部 trial 的证据见结果行的 `reasons` / `first_divergence`。

| task_id | trial_id | 主归因 | 偏离点 | 并列原因 |
| --- | --- | --- | --- | --- |
| T01 | `T01-t1` | policy_violation | step 3 / state_write / refund_order | attribution:policy_violation, evidence:trace, policy:notify_after_refund, terminal_state_mismatch |
| T02 | `T02-t1` | wrong_tool | step 3 / wrong_tool / __missing_tool__ | attribution:wrong_tool, evidence:trace, terminal_state_mismatch |
| T03 | `T03-t1` | policy_violation | step 5 / state_write / refund_order | attribution:policy_violation, evidence:trace, policy:notify_after_refund, terminal_state_mismatch |
| T04 | `T04-t1` | unknown | — | attribution:unknown, evidence:none, missing_disclosures:无法退款, terminal_state_mismatch |
| T05 | `T05-t1` | unknown | — | attribution:unknown, evidence:none, terminal_state_mismatch |
| T06 | `T06-t1` | policy_violation | step 6 / state_write / refund_order | attribution:policy_violation, evidence:trace, policy:notify_after_refund, terminal_state_mismatch |
| T07 | `T07-t1` | policy_violation | step 3 / state_write / refund_order | attribution:policy_violation, evidence:trace, policy:notify_after_refund, terminal_state_mismatch |
| T08 | `T08-t1` | unknown | — | attribution:unknown, evidence:none, terminal_state_mismatch |
| T09 | `T09-t1` | policy_violation | step 6 / state_write / refund_order | attribution:policy_violation, evidence:trace, policy:notify_after_refund, terminal_state_mismatch |
| T10 | `T10-t1` | policy_violation | step 9 / state_write / refund_order | attribution:policy_violation, evidence:trace, missing_disclosures:3000, policy:notify_after_refund, terminal_state_mismatch |

## 与对照组对照（A/B）

> 对照组结果行来自 `experiments/runs/w4c_off_flash-b891da858ccb/result_rows.jsonl`（k=3）；本报告不修改该文件。
> 是否构成**单变量对照**请以两份 `config.json` 为准：只有目标变量不同，本表的差异才能作因果解读。

| 指标 | 对照组 | 本次 | 变化 |
| --- | --- | --- | --- |
| pass¹ | 0.00 | 0.00 | +0.00 |
| pass^3 | 0.00 | 0.00 | +0.00 |
| 平均步数 | 3.83 | 3.37 | -0.47 |
| 无效工具调用率 | 0.13 | 0.05 | -0.08 |
| intent_misread | 0 | 0 | +0 |
| wrong_tool | 3 | 3 | +0 |
| bad_args | 2 | 0 | -2 |
| policy_violation | 20 | 18 | -2 |
| loop_or_timeout | 0 | 0 | +0 |
| output_format | 0 | 0 | +0 |
| unknown | 5 | 9 | +4 |

## 复现

```
python -m eval.run --config experiments/w4c_all4_flash.toml --tasks tasks/mvp_10.jsonl --k 3 --backend openai --guard schema_validation --guard idempotency --guard tool_retry --guard json_recovery
python -m unittest discover -s tests -v
```

逐 trial 证据在 `traces/<trial_id>.jsonl`，结果行在 `results/result_rows.jsonl`；两者与本报告用 `trial_id` 串联。

## 下一步

做 W3 单变量对照时，只改一个配置字段（如 `[guards].enabled`），把 `compare` 指向对照组的 `result_rows.jsonl`；重跑会生成新的 `run_id` 与独立报告，用「与对照组对照（A/B）」表判断改动是否真的有效。不要为了好看而修饰基线。
