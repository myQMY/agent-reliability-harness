# MVP 任务集

`mvp_10.jsonl` 是 10 条自建最小任务，每行一个 JSON 对象。任务集是**测量标尺**，
不是被测对象；被测量和改进的是 `runtime/` 与 `eval/` 这条链路。

## 字段

| 字段 | 含义 |
| --- | --- |
| `id` | 任务编号，`trial_id = <id>-t<trial>` |
| `user_utterances` | 规则用户模拟器逐轮发话的话术；多句即多轮 |
| `initial_db` | 本 trial 的 SQLite 初始快照 |
| `target_db` | 期望终态。只需写**发生变化的行**，未提及的表 / 行从 `initial_db` 继承；`audit_log` 若出现则整体覆盖 |
| `policy_checks` | 本任务适用的策略检查器，枚举见 [`env/policy.md`](../env/policy.md) |
| `required_disclosures` | 最终回复文本必须包含的全部子串 |
| `script` | **ScriptedBackend 的确定性行为脚本**，见下 |

## 关于 script：它不是答案

`script` 描述的是「基线 Agent 在这个任务上依次输出什么」，属于评测 fixture，
不是任务的正确答案。正确答案是 `target_db` + `policy_checks` + `required_disclosures`。

- 元素是对象时，会被序列化成模型输出文本；写成**字符串**时原样输出，
  用来制造非法 JSON / 缺字段这类失败。
- 脚本用尽后会重复最后一项：以 `final` 结尾的脚本正常结束，
  以工具调用结尾的脚本会一直循环到 `MAX_STEPS`。
- 脚本**故意包含缺陷**：超额退款、重复退款、取消后退款、选错工具、缺参数、
  连续输出非法 JSON、死循环、漏掉必要告知。这是设计文档「故意留坑」的要求：
  基线难看是资产，这些失败就是 W3 逐项加护栏时的对照样本。

10 条任务按设计覆盖 6 类失败归因（意图误解 / 工具选错 / 参数错误 / 策略违规 /
循环或超时 / 输出格式失败）以及正常通过、重试后修正、多轮对话三种情形。
具体通过率不在此预写，以 `experiments/` 下实跑的结果表为准。

## 领域

retail 最小域：2 位客户、3 笔订单，3 张表（`customers` / `orders` / `audit_log`），
6 个工具（3 读 3 写）。金额单位统一为分，避免浮点。

任务格式对齐 τ-bench 的「数据库终态判分」口径，但 MVP 使用自建 10 条；
公开任务集按路线 W4 才接入。

## 复现

在仓库根目录执行：

```
python -m eval.run --tasks tasks/mvp_10.jsonl --k 3
```

产物：`results/result_rows.jsonl`（结果行）、`traces/<trial_id>.jsonl`（全链路 span）、
`experiments/mvp_baseline.md`（结果表）。前两者不入库，结果表入库。
