# 实验结果：calibration_seeded / `calibration_seeded-ea22719ba7c2`

> 本表由 `python -m eval.run` 依据结果行如实生成，数字未修饰。
> 生效配置来自实验配置文件与显式 CLI 覆盖；留档模式下同目录另有 `config.json` 存证。`run_id` 由配置摘要确定性生成，同配置必得同一 `run_id`。

## 运行配置

| 项 | 值 |
| --- | --- |
| run_id | `calibration_seeded-ea22719ba7c2` |
| 后端 | SeededBackend（确定性伪随机模拟器，仅用于校准测量链路） |
| seed | 11 |
| 故障注入率 | 60/1000 |
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
- **它没有语言理解能力**：故障是按固定规则人为注入的噪声（坏 JSON / 错参数 / 错工具 / 重复写），任务脚本里固有的缺陷原样保留。本表数字只校准测量链路，**不构成任何模型能力、成本或延迟结论**，也不是 W3 护栏的对照结果。
- `tokens` / `latency_ms` 恒为 0：模拟器不产生真实模型调用，这是保持产物确定性的零值，**不是成本数据**。
- 无效工具调用率 = 工具层报错次数 / 总步数，来自 trace 的事件计数。
- 失败归因取 trace 中**首个**可判定偏离点作主归因；并列原因与偏离点证据写入结果行的 `reasons` / `first_divergence`，判不出时如实记为 `unknown`。

## 总体指标

| 指标 | 值 |
| --- | --- |
| pass¹ | 2/10 = 0.20 |
| pass^3 | 1/10 = 0.10 |
| 平均步数 | 4.50 |
| 无效工具调用率 | 7/135 = 0.05 |
| token 合计 | 0 |
| p95 延迟 | 0 ms |

## 失败类型分布

| 失败类型 | 失败 trial 数 |
| --- | --- |
| intent_misread | 3 |
| wrong_tool | 3 |
| bad_args | 3 |
| policy_violation | 10 |
| loop_or_timeout | 3 |
| output_format | 3 |
| unknown | 0 |

## 逐任务结果

| task_id | 首句话术 | k 次结果 | 全部通过 | 主要失败类型 | 平均步数 |
| --- | --- | --- | --- | --- | --- |
| T01 | 我要退 O-100 这笔订单，退 2500 分。 | `101` | 否 | policy_violation | 4.00 |
| T02 | 我要退 O-100，退 2500 分。 | `000` | 否 | policy_violation | 4.00 |
| T03 | O-100 帮我退 2500 分，谢谢。 | `000` | 否 | policy_violation | 4.00 |
| T04 | 帮我取消 O-101，另外再退 1000 分。 | `000` | 否 | policy_violation | 4.00 |
| T05 | 我要退 O-100，退 2500 分。 | `111` | 是 | none | 5.00 |
| T06 | 我要退 O-100，退 2500 分。 | `000` | 否 | output_format | 3.00 |
| T07 | 我要退 O-100，退 2500 分。 | `000` | 否 | wrong_tool | 2.00 |
| T08 | 我要退 O-100，退 2500 分。 | `000` | 否 | bad_args | 2.00 |
| T09 | 我要退 O-100，退 2500 分。 | `000` | 否 | loop_or_timeout | 12.00 |
| T10 | 帮我查一下 O-102 的状态。 | `000` | 否 | intent_misread | 5.00 |

## 归因证据（首个偏离点）

> 主归因取 trace 中按 step 顺序的第一个可判定偏离点；这里每个任务只列首个失败 trial，全部 trial 的证据见结果行的 `reasons` / `first_divergence`。

| task_id | trial_id | 主归因 | 偏离点 | 并列原因 |
| --- | --- | --- | --- | --- |
| T01 | `T01-t2` | policy_violation | step 5 / state_write / refund_order | attribution:policy_violation, evidence:trace, policy:notify_after_refund, terminal_state_mismatch |
| T02 | `T02-t1` | policy_violation | step 5 / state_write / refund_order | attribution:policy_violation, evidence:trace, missing_disclosures:2500, policy:no_over_refund, terminal_state_mismatch |
| T03 | `T03-t1` | policy_violation | step 3 / state_write / refund_order | attribution:policy_violation, evidence:trace, policy:no_over_refund,single_refund_per_order, terminal_state_mismatch |
| T04 | `T04-t1` | policy_violation | step 3 / state_write / cancel_order | attribution:policy_violation, evidence:trace, missing_disclosures:无法退款, policy:no_refund_after_cancel, terminal_state_mismatch |
| T06 | `T06-t1` | output_format | step 7 / output_format / json_retry | attribution:output_format, evidence:trace, missing_disclosures:O-100,2500, terminal_state_mismatch, termination:json_failure |
| T07 | `T07-t1` | wrong_tool | step 3 / wrong_tool / refund_money | attribution:wrong_tool, evidence:trace, missing_disclosures:O-100,2500, terminal_state_mismatch |
| T08 | `T08-t1` | bad_args | step 3 / bad_args / refund_order | attribution:bad_args, evidence:trace, missing_disclosures:O-100,2500, terminal_state_mismatch |
| T09 | `T09-t1` | loop_or_timeout | step 24 / assistant / - | attribution:loop_or_timeout, evidence:trace, missing_disclosures:O-100,2500, terminal_state_mismatch, termination:max_steps |
| T10 | `T10-t1` | intent_misread | — | attribution:intent_misread, evidence:none, missing_disclosures:3000 |

## 与冻结基线对照

> 基线来自 `experiments/baseline/result_rows.jsonl`（W1 冻结，k=3）；本报告不修改该文件。

> ⚠️ **本次后端与冻结基线不同，本表只是数值罗列，不是消融实验。** 差异来自后端替换（含人为注入的故障），不能作为 harness 或护栏有效性的因果证据；单变量对照必须在同一后端下、一次只改一个变量进行。

| 指标 | 冻结基线 | 本次 | 变化 |
| --- | --- | --- | --- |
| pass¹ | 0.20 | 0.20 | +0.00 |
| pass^3 | 0.20 | 0.10 | -0.10 |
| 平均步数 | 4.50 | 4.50 | +0.00 |
| 无效工具调用率 | 0.04 | 0.05 | +0.01 |
| intent_misread | 3 | 3 | +0 |
| wrong_tool | 3 | 3 | +0 |
| bad_args | 3 | 3 | +0 |
| policy_violation | 9 | 10 | +1 |
| loop_or_timeout | 3 | 3 | +0 |
| output_format | 3 | 3 | +0 |
| unknown | 0 | 0 | +0 |

## 复现

```
python -m eval.run --config experiments/calibration.toml --tasks tasks/mvp_10.jsonl --k 3 --backend seeded --seed 11 --fault-per-mille 60
python -m unittest discover -s tests -v
```

逐 trial 证据在 `traces/<trial_id>.jsonl`，结果行在 `results/result_rows.jsonl`；两者与本报告用 `trial_id` 串联。

## 下一步

做单变量对照实验时，只改 `experiments/config.toml` 中的一个字段，重跑后会生成新的 `run_id` 与独立报告；用「与冻结基线对照」表判断这次改动是否真的有效。不要为了好看而修饰基线。
