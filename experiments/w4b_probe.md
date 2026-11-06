# W4 `[GUARD-REJECT]` 纠正探针

> 由 `python -m eval.guard_probe` 生成；只读取 trace span，不重跑判分。
> **responsive 只是输出层面的响应，不是任务成功、不是护栏有效**；论效果只认 `pass^k`。

## 汇总

| 组 | 拒绝数 | 有下一次输出 | 输出合法 | 已改变 | responsive | 后续 tool 成功 |
| --- | --- | --- | --- | --- | --- | --- |
| w4b_off | 0 | 0 | 0 | 0 | 0 | 0 |
| w4b_all4 | 17 | 17 | 17 | 17 | 17 | 15 |

## 按护栏类型拆分

| 组 | 护栏 | 拒绝数 | responsive | 后续 tool 成功 |
| --- | --- | --- | --- | --- |
| w4b_all4 | `schema_validation` | 5 | 5 | 5 |
| w4b_all4 | `idempotency` | 1 | 1 | 1 |
| w4b_all4 | `tool_retry` | 4 | 4 | 4 |
| w4b_all4 | `json_recovery` | 7 | 7 | 5 |

## 口径

- `no_reply` / `unparseable` / `repeated` 三类都不计入 responsive；只要下一次输出合法且与被拒那次不同，才算作出了有效响应。
- `idempotency` 的正确响应是**跳过**重复写动作，「后续 tool 成功」对它不适用，不能拿这一列横向比较四条护栏。
- 探针不读 `passed` 字段，不进结果行，不改 grader；它只回答「拒绝消息有没有被读懂」这一层机制问题。
