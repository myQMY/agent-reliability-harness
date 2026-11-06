# W4 跑批前预检（就绪度快照）

> 由 `python -m eval.w4_preflight` 生成。**本命令不发起任何 API 请求、不花一分钱、不跑模型、不判分。**
> 本文件是环境 / 配置状态快照，每次运行覆盖更新；**不属于冻结归档，也不是 W4 的实验结果**，不含任何模型分数、实测 token 或 latency。

整体状态：**READY**（0 FAIL / 0 BLOCKED / 12 PASS）

| # | 检查项 | 状态 | 说明 |
| --- | --- | --- | --- |
| 1 | 两份 W4 配置可读且字段合法 | PASS | validate() 通过；`w4_off_flash.toml` / `w4_all4_flash.toml` |
| 2 | 两格只差 [guards].enabled（单变量） | PASS | tasks / k / backend / max_steps / max_json_retries / seed / fault_per_mille 全部相同 |
| 3 | 两格后端均为 openai | PASS | 确定性后端不产生真实调用，不能用来验证 [GUARD-REJECT] |
| 4 | 护栏为唯一自变量（OFF 空 / ALL4 全开） | PASS | ALL4 = ['schema_validation', 'idempotency', 'tool_retry', 'json_recovery'] |
| 5 | 两格 run_id 不同（留档不会互相覆盖） | PASS | w4b_off_flash-f6e556cd7201 / w4b_all4_flash-6d7222dc87af |
| 6 | ALL4.compare 精确指向同后端 OFF 留档 | PASS | experiments/runs/w4b_off_flash-f6e556cd7201/result_rows.jsonl |
| 7 | OFF 组留档已生成 | PASS | `experiments/runs/w4b_off_flash-f6e556cd7201/result_rows.jsonl` 存在 |
| 8 | 两格 results / traces 目录互不冲突 | PASS | off_results=results/w4b_off / off_traces=traces/w4b_off / on_results=results/w4b_all4 / on_traces=traces/w4b_all4 |
| 9 | 任务集 mvp_10.jsonl sha256 未变 | PASS | 与守门测试钉死的常量一致 |
| 10 | 任务集 mvp_30.jsonl sha256 未变 | PASS | 与守门测试钉死的常量一致 |
| 11 | .env 已配置三个必需键 | PASS | 三个键均已设置（值不读取、不展示） |
| 12 | 预算闸门覆盖单格上界 | PASS | 单格上界 360 次 / 612000 tokens；两格计划合计 720 次 / 1224000 tokens。 `--max-calls 720` 是**单次 eval.run** 的闸门，两格各自独立，合计硬上限 1440 次（不是总预算 720）。 |

## 预算作用域（最容易被读错的一条）

- 实验计划总量（两格）= `720` 次调用、`1224000` tokens 上界。
- 单次 `eval.run` 的上界 = `360` 次调用、`612000` tokens。
- `--max-calls` 作用于**单次 `eval.run`**，两格是两次独立进程、各有一份账本；传 `720` 时合计硬上限是 `1440`，**不是总预算 720 次**。闸门保证的是「单格不失控」，不是「整轮不超某个总额」。
- 估算用上界不是预测；`avg_prompt` / `avg_completion` 是假设不是实测；单价由命令行注入，代码不硬编码任何服务商价格。

## 三态口径

- `FAIL`：配置或代码写错，必须先修；`--strict` 会以此返回非零退出码。
- `BLOCKED`：外部输入尚未到位（`.env` 未填、OFF 组留档未生成），不是错误。
- `PASS`：该项就绪，**不等于 W4 可以开跑**——只有整体 `READY` 才是。

## 跑批前必须补齐

- 无。全部检查通过；按 `docs/w4-design.md` §5 执行命令。
