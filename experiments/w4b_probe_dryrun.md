# W4-b 前置：注入装置离线校准（零成本、零网络）

> 由 `python -m eval.w4b_probe` 生成。**全程没有发起任何模型 API 请求，
> 没有花一分钱，不跑判分、不改任何归档。**
> **这些数字只证明装置接得上，不是 W4-b 的实验结论。** 校准用的是
> `ScriptedBackend + OutputFaultBackend`：脚本后端按 cursor 前进、不会理解
> 拒绝消息，它的 responsive 不是语言模型的表现。真实模型能否读懂
> `[GUARD-REJECT]`，只能由 W4-b 两格实测的探针回答。

校准输入：`tasks/mvp_10.jsonl`（10 任务 × k=3 = 30 trial、单轮）、`seed=11`、`fault_per_mille=300`，两格只差 `[guards].enabled`。

> **校准脚本先过滤掉了任务集里的 6 类固有缺陷**（含成对的重复写动作），只保留合法工具调用，末尾补一个不参与判分的占位 `final`。因此 OFF 组必须为 0 个 guard span，ALL4 组数到的拒绝只能来自注入。CLI 另跑一组 `fpm=0` 的 ALL4 基准并**强制断言其 guard span 为 0**——这是「干净脚本本身不触发护栏」的机器证明。不过滤时 fpm=60 会「注入 0 次却数到 7 次拒绝」，那是脚本固有缺陷，不是装置生效。

## 装置是否成立

| 组 | 注入次数 | guard span | 拒绝数 | responsive |
| --- | --- | --- | --- | --- |
| OFF（护栏全关） | 14 | 0 | 0 | 0 |
| ALL4（四条全开） | 14 | 14 | 14 | 14 |

- OFF 组必须是 **0 个 guard span / 0 次拒绝**；ALL4 组必须 > 0，否则注入器与护栏没接上，不准发真实调用。
- **本校准里 responsive 恒等于拒绝数，是脚本后端的机械性质**：
  `ScriptedBackend` 被拒后总返回脚本下一项，必然「合法且不同」。
  它**不预示真实模型的 responsive 率**，也不与 W4-b 实测值相减或比较。
- 本次注入覆盖的故障类型：`bad_args, bad_json, duplicate_call, wrong_tool`（四类见 `runtime/model.py` 的 `SEEDED_FAULTS`）。
- ALL4 拒绝护栏类型分布：`idempotency, json_recovery, schema_validation, tool_retry`。
- 两格注入次数可能不同：护栏拦下后会多出几次「响应拒绝」的 generate，
  每次都消耗 cursor 且按协议跳过注入。这是护栏的处理效应，不是自变量不同。

## 按护栏类型拆分（只读探针，同一把尺子）

| 护栏 | 拒绝数 | responsive | 后续 tool 成功 |
| --- | --- | --- | --- |
| `schema_validation` | 5 | 5 | 4 |
| `idempotency` | 2 | 2 | 0 |
| `tool_retry` | 3 | 3 | 1 |
| `json_recovery` | 4 | 4 | 3 |

## 口径与边界

- 注入参数 `fault_per_mille` 是**装置参数**，不是模型能力轴；W3 归因实验
  用 60，这里为在较短的动作序列上取得足够分母而另设，
  **两组的任何数字都不可相减、不可拼曲线**。
- `responsive` 只是输出层面的机制证据，不是任务成功；论效果只认 `pass^k`，
  而 W4-b 的主读数是探针分母，**不是** `pass^k`。
- 注入只替换模型输出层的文本；终态判分、任务集、护栏实现、grader 全部未改。
- 真实跑批的产物分析用 `python -m eval.guard_probe --traces-dir`，
  与本校准共用 `probe_spans` / `summarize`，不存在两把尺子。
