# MVP 基线结果

> 本表由 `python -m eval.run` 依据 `results/result_rows.jsonl` 如实生成，数字未修饰。
> 基线难看是资产：它的用途是校准测量链路，并为 W3 的单变量对照实验提供参照。

## 运行配置

| 项 | 值 |
| --- | --- |
| 后端 | ScriptedBackend（确定性脚本） |
| 任务集 | `tasks/mvp_10.jsonl` |
| 任务数 | 10 |
| k | 3 |
| trial 总数 | 30 |
| MAX_STEPS | 12 |
| JSON 失败重试 | 2 次 |
| 领域库 | 内存 SQLite，每 trial 从同一模板 backup 重置 |
| 判分 | 终态逐字段 diff + 策略检查 + 必要告知，全部满足才计 1 |

## 口径与限制（读表前先看）

- **pass¹**：每个任务首次执行的平均通过率。
- **pass^k**：同一任务 k 次独立执行全部通过的任务占比。
- ScriptedBackend 是确定性脚本，同一任务 k 次结果完全一致，因此本次 **pass^k 与 pass¹ 必然相等**；pass^k 暴露「偶尔成功」的能力要等 W2 接入真实模型后才显现。这一相等不是缺陷，是测量链路确定性的证据。
- `tokens` / `latency_ms` 在该后端下统一为占位常量 0，不代表真实模型开销；成本与延迟观测同样留到 W2。
- 无效工具调用率 = 工具层报错次数 / 总步数，来自 trace 的事件计数。

## 总体指标

| 指标 | 值 |
| --- | --- |
| pass¹ | 2/10 = 0.20 |
| pass^3 | 2/10 = 0.20 |
| 平均步数 | 4.50 |
| 无效工具调用率 | 6/135 = 0.04 |
| token 合计（占位） | 0 |
| p95 延迟（占位） | 0 ms |

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

## 复现

```
python -m eval.run --tasks tasks/mvp_10.jsonl --k 3
python -m unittest discover -s tests -v
```

逐 trial 证据在 `traces/<trial_id>.jsonl`，结果行在 `results/result_rows.jsonl`；两者与本报告用 `trial_id` 串联。

## 下一步

W3 按上表失败归因逐项加护栏（参数校验、幂等键、上下文压缩、预算等），一次只改一个变量，重跑同一条命令后与本表做对照；不要为了好看而修饰基线。
