# W4 真实模型预算估算（离线上界）

> 本表由 `python -m eval.budget` 生成，**没有发起任何网络请求**。
> 调用次数是「每个 trial 都跑满 MAX_STEPS」的最坏情况上界，不是期望值；单价由命令行注入，价格会变，以服务商账单为准。

## 输入

| 项 | 值 |
| --- | --- |
| 任务集 | `tasks/mvp_10.jsonl` |
| 任务数（合计） | 10 |
| k | 3 |
| MAX_STEPS | 12 |
| 实验格数 | 2 |
| 单次调用平均 prompt tokens（假设） | 1500 |
| 单次调用平均 completion tokens（假设） | 200 |

## 上界

| 项 | 值 |
| --- | --- |
| 模型调用次数上界 | 720 |
| prompt tokens 上界 | 1080000 |
| completion tokens 上界 | 144000 |
| 合计 tokens 上界 | 1224000 |
| input 费用上界 | 0.1620 USD |
| output 费用上界 | 0.0864 USD |
| **合计费用上界** | **0.2484 USD** |

## 使用方式

- 把 `--max-calls 720` 传给 `python -m eval.run`，闸门会在达到该调用数时停止，不会静默继续烧钱。
- 实际 trial 通常在 4~8 步就给出 final 或触发终止，实际调用数会明显低于上界；跑完请如实记录 `usage` 实测值，不要用本表当实测成本。
- 若模型开启 thinking mode，completion tokens 可能远高于假设值，此时应以 `--max-cost` 配合单价一起兜底。
