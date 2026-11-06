# W4 前置：纠正探针离线校准（零成本、零网络）

> 本文件由 `python -m eval.guard_probe` 用 **SeededBackend** 生成，全程没有发起任何模型 API 请求，也没有花一分钱。
> **这些数字只证明探针数得对，不构成 W4 的实验结论。** 模拟器的 `_pending_correct` 是硬编码的「收到拒绝就重发干净动作」，它当然能读懂——真实模型能不能读懂，必须等 W4 接真实模型才知道。

校准输入：`tasks/mvp_10.jsonl`，k=1，seed=11，fault_per_mille=60，两格只差 `[guards].enabled`。

- OFF 组拒绝数 **0**（护栏全关时探针不应凭空数出拒绝）；
- 四条全开组拒绝数 **7**，其中 responsive **4**、后续 tool 成功 **2**；类型分布 idempotency, json_recovery, schema_validation, tool_retry。

这组数字回答的是「尺子有没有装反」：OFF 组必须是 0，全开组必须能把四类拒绝分别归类。它**不回答**真实模型是否理解 `[GUARD-REJECT]`，也不与任何 W3 留档的分数混用。W4 跑完用 `python -m eval.guard_probe --traces-dir <off> --label w4_off --traces-dir <all4> --label w4_all4` 只读复用同一把尺子。

## 汇总

| 组 | 拒绝数 | 有下一次输出 | 输出合法 | 已改变 | responsive | 后续 tool 成功 |
| --- | --- | --- | --- | --- | --- | --- |
| seeded_off | 0 | 0 | 0 | 0 | 0 | 0 |
| seeded_all4 | 7 | 6 | 4 | 4 | 4 | 2 |

## 按护栏类型拆分

| 组 | 护栏 | 拒绝数 | responsive | 后续 tool 成功 |
| --- | --- | --- | --- | --- |
| seeded_all4 | `schema_validation` | 1 | 1 | 0 |
| seeded_all4 | `idempotency` | 1 | 1 | 1 |
| seeded_all4 | `tool_retry` | 1 | 1 | 0 |
| seeded_all4 | `json_recovery` | 4 | 1 | 1 |

## 口径

- `no_reply` / `unparseable` / `repeated` 三类都不计入 responsive；只要下一次输出合法且与被拒那次不同，才算作出了有效响应。
- `idempotency` 的正确响应是**跳过**重复写动作，「后续 tool 成功」对它不适用，不能拿这一列横向比较四条护栏。
- 探针不读 `passed` 字段，不进结果行，不改 grader；它只回答「拒绝消息有没有被读懂」这一层机制问题。
