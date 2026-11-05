# 实验结果：w3_ab_on / `w3_ab_on-fa17143eb761`

> 本表由 `python -m eval.run` 依据结果行如实生成，数字未修饰。
> 生效配置来自实验配置文件与显式 CLI 覆盖；留档模式下同目录另有 `config.json` 存证。`run_id` 由配置摘要确定性生成，同配置必得同一 `run_id`。

## 运行配置

| 项 | 值 |
| --- | --- |
| run_id | `w3_ab_on-fa17143eb761` |
| 后端 | SeededBackend（确定性伪随机模拟器，W3 单变量对照实验） |
| seed | 11 |
| 故障注入率 | 60/1000 |
| 已开启护栏 | `schema_validation` |
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
- SeededBackend 是**确定性伪随机模拟器**：RNG 由 `(seed, task_id, trial, cursor)` 经 SHA256 派生，不使用时间、UUID 或全局随机源，因此同配置连跑两次仍逐字节一致。
- 不同 trial 的 RNG 流互相独立，所以同一任务 k 次结果可能不同；**pass^k 在本后端下首次具备区分度**，可用于校准「偶尔成功」的测量链路。
- **本次已开启运行时护栏**（名称见「运行配置」）：本表是 W3 单变量对照的实验组结果，不再是纯校准。模拟器仍**没有语言理解能力**，注入的仍是人为噪声，因此这些数字**不构成真实模型能力、成本或延迟结论**；护栏是否有效请以「与对照组对照（A/B）」表与对照组 `config.json` 为准。
- 护栏在工具执行前拦下调用，产生 `role=guard` 的 span 与一条拒绝消息：不写库、不计入 `invalid_calls`，grader 的判分口径不因开关而改变。**`invalid_calls` 下降不等于成功率提升**——任务脚本固有的缺陷被拦下后依然失败，只是归因可能从 `bad_args` 变成 `unknown`；论效果应看 `pass^k`。
- `tokens` / `latency_ms` 恒为 0：模拟器不产生真实模型调用，这是保持产物确定性的零值，**不是成本数据**。
- 无效工具调用率 = 工具层报错次数 / 总步数，来自 trace 的事件计数。
- 失败归因取 trace 中**首个**可判定偏离点作主归因；并列原因与偏离点证据写入结果行的 `reasons` / `first_divergence`，判不出时如实记为 `unknown`。

## 总体指标

| 指标 | 值 |
| --- | --- |
| pass¹ | 2/10 = 0.20 |
| pass^3 | 2/10 = 0.20 |
| 平均步数 | 4.53 |
| 无效工具调用率 | 3/136 = 0.02 |
| token 合计 | 0 |
| p95 延迟 | 0 ms |

## 失败类型分布

| 失败类型 | 失败 trial 数 |
| --- | --- |
| intent_misread | 3 |
| wrong_tool | 3 |
| bad_args | 0 |
| policy_violation | 9 |
| loop_or_timeout | 3 |
| output_format | 3 |
| unknown | 3 |

## 逐任务结果

| task_id | 首句话术 | k 次结果 | 全部通过 | 主要失败类型 | 平均步数 |
| --- | --- | --- | --- | --- | --- |
| T01 | 我要退 O-100 这笔订单，退 2500 分。 | `111` | 是 | none | 4.33 |
| T02 | 我要退 O-100，退 2500 分。 | `000` | 否 | policy_violation | 4.00 |
| T03 | O-100 帮我退 2500 分，谢谢。 | `000` | 否 | policy_violation | 4.00 |
| T04 | 帮我取消 O-101，另外再退 1000 分。 | `000` | 否 | policy_violation | 4.00 |
| T05 | 我要退 O-100，退 2500 分。 | `111` | 是 | none | 5.00 |
| T06 | 我要退 O-100，退 2500 分。 | `000` | 否 | output_format | 3.00 |
| T07 | 我要退 O-100，退 2500 分。 | `000` | 否 | wrong_tool | 2.00 |
| T08 | 我要退 O-100，退 2500 分。 | `000` | 否 | unknown | 2.00 |
| T09 | 我要退 O-100，退 2500 分。 | `000` | 否 | loop_or_timeout | 12.00 |
| T10 | 帮我查一下 O-102 的状态。 | `000` | 否 | intent_misread | 5.00 |

## 归因证据（首个偏离点）

> 主归因取 trace 中按 step 顺序的第一个可判定偏离点；这里每个任务只列首个失败 trial，全部 trial 的证据见结果行的 `reasons` / `first_divergence`。

| task_id | trial_id | 主归因 | 偏离点 | 并列原因 |
| --- | --- | --- | --- | --- |
| T02 | `T02-t1` | policy_violation | step 5 / state_write / refund_order | attribution:policy_violation, evidence:trace, missing_disclosures:2500, policy:no_over_refund, terminal_state_mismatch |
| T03 | `T03-t1` | policy_violation | step 3 / state_write / refund_order | attribution:policy_violation, evidence:trace, policy:no_over_refund,single_refund_per_order, terminal_state_mismatch |
| T04 | `T04-t1` | policy_violation | step 3 / state_write / cancel_order | attribution:policy_violation, evidence:trace, missing_disclosures:无法退款, policy:no_refund_after_cancel, terminal_state_mismatch |
| T06 | `T06-t1` | output_format | step 7 / output_format / json_retry | attribution:output_format, evidence:trace, missing_disclosures:O-100,2500, terminal_state_mismatch, termination:json_failure |
| T07 | `T07-t1` | wrong_tool | step 3 / wrong_tool / refund_money | attribution:wrong_tool, evidence:trace, missing_disclosures:O-100,2500, terminal_state_mismatch |
| T08 | `T08-t1` | unknown | — | attribution:unknown, evidence:none, missing_disclosures:O-100,2500, terminal_state_mismatch |
| T09 | `T09-t1` | loop_or_timeout | step 24 / assistant / - | attribution:loop_or_timeout, evidence:trace, missing_disclosures:O-100,2500, terminal_state_mismatch, termination:max_steps |
| T10 | `T10-t1` | intent_misread | — | attribution:intent_misread, evidence:none, missing_disclosures:3000 |

## 与对照组对照（A/B）

> 对照组结果行来自 `experiments/runs/w3_ab_off-346ab5e98382/result_rows.jsonl`（k=3）；本报告不修改该文件。
> 是否构成**单变量对照**请以两份 `config.json` 为准：只有目标变量不同，本表的差异才能作因果解读。

| 指标 | 对照组 | 本次 | 变化 |
| --- | --- | --- | --- |
| pass¹ | 0.20 | 0.20 | +0.00 |
| pass^3 | 0.10 | 0.20 | +0.10 |
| 平均步数 | 4.50 | 4.53 | +0.03 |
| 无效工具调用率 | 0.05 | 0.02 | -0.03 |
| intent_misread | 3 | 3 | +0 |
| wrong_tool | 3 | 3 | +0 |
| bad_args | 3 | 0 | -3 |
| policy_violation | 10 | 9 | -1 |
| loop_or_timeout | 3 | 3 | +0 |
| output_format | 3 | 3 | +0 |
| unknown | 0 | 3 | +3 |

## 复现

```
python -m eval.run --config experiments/guard_ab_on.toml --tasks tasks/mvp_10.jsonl --k 3 --backend seeded --seed 11 --fault-per-mille 60 --guard schema_validation
python -m unittest discover -s tests -v
```

逐 trial 证据在 `traces/<trial_id>.jsonl`，结果行在 `results/result_rows.jsonl`；两者与本报告用 `trial_id` 串联。

## 下一步

做 W3 单变量对照时，只改一个配置字段（如 `[guards].enabled`），把 `compare` 指向对照组的 `result_rows.jsonl`；重跑会生成新的 `run_id` 与独立报告，用「与对照组对照（A/B）」表判断改动是否真的有效。不要为了好看而修饰基线。
