# 实验结果：baseline_scripted / `baseline_scripted-a346e8acef15`

> 本表由 `python -m eval.run` 依据结果行如实生成，数字未修饰。
> 生效配置来自 `experiments/config.toml` 与显式 CLI 覆盖；留档模式下同目录另有 `config.json` 存证。`run_id` 由配置摘要确定性生成，同配置必得同一 `run_id`。

## 运行配置

| 项 | 值 |
| --- | --- |
| run_id | `baseline_scripted-a346e8acef15` |
| 后端 | ScriptedBackend（确定性脚本） |
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
- ScriptedBackend 是确定性脚本，同一任务 k 次结果完全一致，因此本次 **pass^k 与 pass¹ 必然相等**；pass^k 暴露「偶尔成功」的能力要等 W2 接入真实模型后才显现。这一相等不是缺陷，是测量链路确定性的证据。
- 无效工具调用率 = 工具层报错次数 / 总步数，来自 trace 的事件计数。
- `tokens` / `latency_ms` 当前在所有后端下都是 0：ScriptedBackend 使用占位常量，OpenAIBackend 尚未接入 usage 观测（W2 待办），因此成本与延迟结论暂不成立。

## 总体指标

| 指标 | 值 |
| --- | --- |
| pass¹ | 2/10 = 0.20 |
| pass^3 | 2/10 = 0.20 |
| 平均步数 | 4.50 |
| 无效工具调用率 | 6/135 = 0.04 |
| token 合计 | 0 |
| p95 延迟 | 0 ms |

## 失败类型分布

| 失败类型 | 失败 trial 数 |
| --- | --- |
| intent_misread | 3 |
| wrong_tool | 3 |
| bad_args | 3 |
| policy_violation | 9 |
| loop_or_timeout | 3 |
| output_format | 3 |

## 逐任务结果

| task_id | 首句话术 | k 次结果 | 全部通过 | 主要失败类型 | 平均步数 |
| --- | --- | --- | --- | --- | --- |
| T01 | 我要退 O-100 这笔订单，退 2500 分。 | `111` | 是 | none | 4.00 |
| T02 | 我要退 O-100，退 2500 分。 | `000` | 否 | policy_violation | 4.00 |
| T03 | O-100 帮我退 2500 分，谢谢。 | `000` | 否 | policy_violation | 4.00 |
| T04 | 帮我取消 O-101，另外再退 1000 分。 | `000` | 否 | policy_violation | 4.00 |
| T05 | 我要退 O-100，退 2500 分。 | `111` | 是 | none | 5.00 |
| T06 | 我要退 O-100，退 2500 分。 | `000` | 否 | output_format | 3.00 |
| T07 | 我要退 O-100，退 2500 分。 | `000` | 否 | wrong_tool | 2.00 |
| T08 | 我要退 O-100，退 2500 分。 | `000` | 否 | bad_args | 2.00 |
| T09 | 我要退 O-100，退 2500 分。 | `000` | 否 | loop_or_timeout | 12.00 |
| T10 | 帮我查一下 O-102 的状态。 | `000` | 否 | intent_misread | 5.00 |

## 与冻结基线对照

> 基线来自 `experiments/baseline/result_rows.jsonl`（W1 冻结，k=3）；本报告不修改该文件。

| 指标 | 冻结基线 | 本次 | 变化 |
| --- | --- | --- | --- |
| pass¹ | 0.20 | 0.20 | +0.00 |
| pass^3 | 0.20 | 0.20 | +0.00 |
| 平均步数 | 4.50 | 4.50 | +0.00 |
| 无效工具调用率 | 0.04 | 0.04 | +0.00 |
| intent_misread | 3 | 3 | +0 |
| wrong_tool | 3 | 3 | +0 |
| bad_args | 3 | 3 | +0 |
| policy_violation | 9 | 9 | +0 |
| loop_or_timeout | 3 | 3 | +0 |
| output_format | 3 | 3 | +0 |

## 复现

```
python -m eval.run --tasks tasks/mvp_10.jsonl --k 3
python -m unittest discover -s tests -v
```

逐 trial 证据在 `traces/<trial_id>.jsonl`，结果行在 `results/result_rows.jsonl`；两者与本报告用 `trial_id` 串联。

## 下一步

做单变量对照实验时，只改 `experiments/config.toml` 中的一个字段，重跑后会生成新的 `run_id` 与独立报告；用「与冻结基线对照」表判断这次改动是否真的有效。不要为了好看而修饰基线。
