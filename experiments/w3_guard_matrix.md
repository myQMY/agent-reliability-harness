# W3 护栏 2×2 全因子结果

> 由 `python -m eval.guard_matrix` 读取四组留档如实生成；本模块不重跑评测、不修改任何留档，也不含生成时间。
> 四组共用同一后端、同一任务集、同一 seed，唯一自变量是 `[guards].enabled`。`run_id` 由配置摘要确定性生成，可逐格回溯生效配置。

## 因子设计

| 格子 | schema_validation | idempotency | run_id | pass¹ | pass^3 | invalid_calls | 平均步数 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| off | OFF | OFF | `w3_ab_off-346ab5e98382` | 0.20 | 0.10 | 7 | 4.50 |
| schema | ON | OFF | `w3_ab_on-fa17143eb761` | 0.20 | 0.20 | 3 | 4.53 |
| idem | OFF | ON | `w3_idem_on-a4e1925e1619` | 0.30 | 0.20 | 7 | 4.53 |
| both | ON | ON | `w3_both_on-3c15f3bd94fd` | 0.30 | 0.30 | 3 | 4.57 |

## 主效应与交互效应

> 主效应 = 该因子 ON 的两格均值 − OFF 的两格均值；交互 = `(both − idem) − (schema − off)`。以下是**描述性差值**，不是显著性检验，也没有置信区间。

| 指标 | schema 主效应 | idempotency 主效应 | 交互（双重差分） |
| --- | --- | --- | --- |
| pass¹ | +0.00 | +0.10 | +0.00 |
| pass^k | +0.10 | +0.10 | +0.00 |
| invalid_calls | -4.00 | +0.00 | +0.00 |
| 平均步数 | +0.03 | +0.03 | +0.00 |

## 怎么读这张表

- `idempotency` 单开（OFF → idem）：pass^3 0.10 → 0.20，`invalid_calls` 7 → 7，`policy_violation` 失败 trial 10 → 7。它拦下的是「工具层**不报错**、但终态被重复写坏」的调用，这类修复本来就不会反映在无效调用数上；**论效果只看 pass^k**，中间指标（上升、下降或不变）都不能当代理。
- 两条单变量边是 `off → schema` 与 `off → idem`：`both` 格一次改了两项，**不能**拿它相对 OFF 的 A/B 表当单变量因果证据，它只用于算交互。
- 交互接近 0 只说明在**本 seed、本任务集、本故障注入率**下两条护栏的效果可加；换任务集或换模型后可能不成立，不推广为一般结论。

## 口径与限制（读表前先看）

- 四组后端都是 `SeededBackend`：确定性伪随机模拟器，**没有语言理解能力**，注入的是人为噪声（坏 JSON / 错参数 / 错工具 / 重复写）。这些数字**不构成任何真实模型能力、成本或延迟结论**，`tokens` / `latency_ms` 恒为 0。
- 每格 10 个任务 × k 次（共 30 个 trial），样本很小；pass^k 是「同一任务 k 次全通过」的任务占比，差一格就是 0.10。
- 判分是终态逐字段 diff + 策略检查 + 必要告知；护栏产生的 `role=guard` span 不进 grader，**开启护栏不改变判分口径**。
- `invalid_calls` 只统计工具层报错。`invalid_calls` 下降不等于成功率提升（坏参数被拦下后任务仍可能失败），反过来，成功率提升也不要求它下降。
- 幂等键由运行时从 `(tool, args)` 经 SHA256 确定性派生，只在单个 trial 的内存里去重；**没有持久化、不承诺跨进程幂等**，也不覆盖模型自带幂等键的场景。

## 数据来源与复现

| 格子 | 生效配置 | 结果行 |
| --- | --- | --- |
| off | `experiments/guard_ab_off.toml` | `experiments/runs/w3_ab_off-346ab5e98382/config.json` · `experiments/runs/w3_ab_off-346ab5e98382/result_rows.jsonl` |
| schema | `experiments/guard_ab_on.toml` | `experiments/runs/w3_ab_on-fa17143eb761/config.json` · `experiments/runs/w3_ab_on-fa17143eb761/result_rows.jsonl` |
| idem | `experiments/guard_ab_idem.toml` | `experiments/runs/w3_idem_on-a4e1925e1619/config.json` · `experiments/runs/w3_idem_on-a4e1925e1619/result_rows.jsonl` |
| both | `experiments/guard_ab_both.toml` | `experiments/runs/w3_both_on-3c15f3bd94fd/config.json` · `experiments/runs/w3_both_on-3c15f3bd94fd/result_rows.jsonl` |

```
python -m eval.guard_matrix
python -m unittest discover -s tests -v
```

逐格复现命令与完整的单变量 A/B 表见各留档目录下的 `report.md`。
