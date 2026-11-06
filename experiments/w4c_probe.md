# W4-c 只读判定：拒绝后重发的是不是原本那一个动作

> 由 `python -m eval.w4c_probe` 生成；只读既有 trace 与独立 `clean_actions/` side-car，不重跑模型、不重算判分、不改任何归档。
> **本报告涉及的输出都被注入了输出级故障，不是纯净模型行为。** 本组只判定一条输出层机制链，不判定任务成功、不判定护栏有效。

## 汇总

| 组 | 拒绝事件 | comparable | identical | different | final_instead | unparseable | idempotency_skipped | chained | missing |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| w4c_off | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| w4c_all4 | 15 | 15 | 14 | 1 | 0 | 0 | 0 | 0 | 0 |

## 按护栏类型拆分

| 组 | 护栏 | 事件 | comparable | identical | different |
| --- | --- | --- | --- | --- | --- |
| w4c_all4 | `schema_validation` | 4 | 4 | 4 | 0 |
| w4c_all4 | `tool_retry` | 5 | 5 | 5 | 0 |
| w4c_all4 | `json_recovery` | 6 | 6 | 5 | 1 |

## 逐事件明细（只读，可人工核对）

| 组 | trial | step | 护栏 | 故障 | 判定 | 被替换的干净动作 | 拒绝后的响应 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| w4c_all4 | `T02-t1` | 4 | `tool_retry` | `wrong_tool` | `identical` | `{"tool":"refund_order","args":{"order_id":"O-100","amount_cents…` | `{"tool": "refund_order", "args": {"order_id": "O-100", "amount_…` |
| w4c_all4 | `T02-t2` | 3 | `schema_validation` | `duplicate_call` | `identical` | `{"tool": "refund_order", "args": {"order_id": "O-100", "amount_…` | `{"tool": "refund_order", "args": {"order_id": "O-100", "amount_…` |
| w4c_all4 | `T05-t1` | 6 | `json_recovery` | `bad_json` | `identical` | `{"tool":"refund_order","args":{"order_id":"O-100","amount_cents…` | `{"tool":"refund_order","args":{"order_id":"O-100","amount_cents…` |
| w4c_all4 | `T05-t2` | 4 | `json_recovery` | `bad_json` | `identical` | `{"tool": "refund_order", "args": {"order_id": "O-100", "amount_…` | `{"tool": "refund_order", "args": {"order_id": "O-100", "amount_…` |
| w4c_all4 | `T05-t3` | 3 | `schema_validation` | `bad_args` | `identical` | `{"tool":"refund_order","args":{"order_id":"O-100","amount_cents…` | `{"tool":"refund_order","args":{"order_id":"O-100","amount_cents…` |
| w4c_all4 | `T06-t1` | 4 | `tool_retry` | `wrong_tool` | `identical` | `{"tool":"refund_order","args":{"order_id":"O-100","amount_cents…` | `{"args": {"amount_cents": 2500, "order_id": "O-100"}, "tool": "…` |
| w4c_all4 | `T07-t3` | 6 | `json_recovery` | `bad_json` | `identical` | `{"tool": "refund_order", "args": {"order_id": "O-100", "amount_…` | `{"tool": "refund_order", "args": {"order_id": "O-100", "amount_…` |
| w4c_all4 | `T07-t3` | 11 | `tool_retry` | `wrong_tool` | `identical` | `{"tool": "notify_customer", "args": {"customer_id": "C-1", "mes…` | `{"tool": "notify_customer", "args": {"customer_id": "C-1", "mes…` |
| w4c_all4 | `T08-t1` | 8 | `json_recovery` | `bad_json` | `different` | `{"tool": "notify_customer", "args": {"customer_id": "C-1", "mes…` | `{"tool": "notify_customer", "args": {"customer_id": "C-1", "mes…` |
| w4c_all4 | `T08-t3` | 3 | `schema_validation` | `duplicate_call` | `identical` | `{"tool":"refund_order","args":{"order_id":"O-100","amount_cents…` | `{"tool": "refund_order", "args": {"order_id": "O-100", "amount_…` |
| w4c_all4 | `T09-t1` | 4 | `tool_retry` | `wrong_tool` | `identical` | `{"tool":"refund_order","args":{"order_id":"O-100","amount_cents…` | `{"tool": "refund_order", "args": {"order_id": "O-100", "amount_…` |
| w4c_all4 | `T09-t3` | 4 | `json_recovery` | `bad_json` | `identical` | `{"tool": "get_order", "args": {"order_id": "O-100"}}` | `{"tool": "get_order", "args": {"order_id": "O-100"}}` |
| w4c_all4 | `T09-t3` | 11 | `tool_retry` | `wrong_tool` | `identical` | `{"tool": "notify_customer", "args": {"customer_id": "C-1", "mes…` | `{"tool": "notify_customer", "args": {"customer_id": "C-1", "mes…` |
| w4c_all4 | `T10-t1` | 7 | `schema_validation` | `duplicate_call` | `identical` | `{"tool": "refund_order", "args": {"order_id": "O-102", "amount_…` | `{"tool": "refund_order", "args": {"order_id": "O-102", "amount_…` |
| w4c_all4 | `T10-t2` | 4 | `json_recovery` | `bad_json` | `identical` | `{"tool": "get_order", "args": {"order_id": "O-102"}}` | `{"tool": "get_order", "args": {"order_id": "O-102"}}` |

## 口径

- **输出被注入扰动，不是纯净模型行为**。`fault_per_mille` 是装置参数，不是模型能力轴；本结果不与 W3 / W4 首轮 / W4-b 的任何数字相减或拼曲线。
- 主分母 `comparable = identical + different + final_instead`；`idempotency_skipped`（拒绝文本要求「跳过该动作」）、`unparseable`、`chained`（连锁拒绝，不是注入直接产生）、`missing` 全部单列、如实统计。
- `identical` 的含义是「模型按同一策略重新推导出了同一个合法调用」，**不是**回忆起被拿走的动作：模型从未见过 `clean_text`，对话里只有坏文本 + 拒绝消息。
- `different` 只说明给了另一个合法动作，不等于改错；`final_instead` 不是重发。
- 本判定不读 `passed`、不进结果行、不改 grader；论效果仍然只认 `pass^k`，而本组 `pass^k` 无分辨率。机制证据不是护栏有效性的证据。
