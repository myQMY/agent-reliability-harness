# W4 设计：真实模型验证 `[GUARD-REJECT]`

> 状态：**前置工程已就绪，尚未发起任何真实模型 API 请求，未花一分钱。**
> 本文件是 W4 的范围共识与执行清单；所有数字要么是离线上界，要么是引用
> `experiments/` 下已入库的证据，没有「跑出来」的实验结果。

## 1. 判断：卡点不是预算，是「现在直接开跑测不到想测的东西」

W3 收口后路线表只剩 W4。此前一直把「先定 API 预算」当门槛，但把真实模型
路径从头读一遍会发现三个更靠前的工程缺口——它们**零成本、可离线单测**，
不补上，给了预算和密钥也只会得到一批与 `[GUARD-REJECT]` 无关的全败：

| # | 缺口 | 后果 | 本轮修复 |
| --- | --- | --- | --- |
| 1 | `SYSTEM_PROMPT` 只讲输出格式，**没有工具清单、参数名、领域库状态** | 用户话术只有「我要退 O-100，退 2500 分」，模型无从得知 `refund_order` / `amount_cents` / `customer_id=C-1`，必然瞎猜 | `runtime/model.build_tool_context()` 把 `env/tools.TOOL_SCHEMAS` 与 `db.dump_state()` 序列化成确定性片段，只由 `OpenAIBackend` 消费 |
| 2 | loop 用 `role=tool` 记工具观察，而 chat completions 的 tool role 必须带 `tool_call_id` | 本项目走纯文本 JSON 动作、不用原生 `tool_calls`，请求会被服务端判 400，第一次工具调用后就断 | `OpenAIBackend._wire_messages()` 在发请求前把 tool 消息转成带 `[TOOL-RESULT]` 前缀的 user 文本；语义不变，trace / 结果行不变 |
| 3 | 没有成本上限，模型陷入循环时 `MAX_STEPS=12` × N 任务 × k 会静默烧钱 | 无法把「预算」变成可执行约束 | `eval/budget.py` 提供离线上界估算 + `BudgetedBackend` 跨 run 共享账本的硬闸门；`eval.run` 在 openai 路径**强制要求**显式 `--max-calls` |

第三条同时改写了「定预算」的含义：预算不再是一句口头约定，而是命令行里
一个会在超限调用**之前**停下来的数。

两处修复都严格只在 `openai` 后端生效：`ScriptedBackend` / `SeededBackend`
既不构造也不消费工具上下文，不包裹预算装饰器，既有 `run_id`、归档报告与
冻结基线逐字节不变（由 `tests/test_w4_readiness.py` 钉死）。

## 2. 假设与可证伪条件

W4 只验证一条机制假设，不追求「又涨了多少分」：

- **H1**：真实模型收到 `[GUARD-REJECT]` 后，下一次能输出**合法且不同于被拒动作**的响应。
- **效果指标仍是 `pass^k`**：两格（OFF / 四条全开）在同后端、同模型、同任务集下的 A/B 差值。
- **`[GUARD-REJECT]` 纠正率是机制证据，不是效果指标**：`eval/guard_probe.py` 的
  `responsive` 只说明输出层面作出了响应，不代表任务成功，也不代表护栏有效。

**可证伪条件（必须提前写死）**：若实验组的 responsive 率接近 0、或与「原样
重复被拒动作」的比例无异，则 W3「护栏靠模型读懂拒绝消息后重发」的机制在
真实模型上不成立——W3 的全部数字都来自 `_pending_correct` 硬编码重发的
确定性模拟器，**不得外推到真实模型**。此时正确动作是改协议（结构化
`tool_calls` / 函数级重试），而不是继续调 prompt 文案或挑好看的 seed。

反过来，即使 responsive 很高，也不自动等于 `pass^k` 提升；两个结论必须
分别报，不能互相替代。

## 3. 实验矩阵

两格、单变量、同一 `mvp_10.jsonl`（**不修改任何已提交任务集**）：

| 格 | 配置 | `guards.enabled` | `compare` |
| --- | --- | --- | --- |
| OFF（对照） | `experiments/w4_off_flash.toml` | 空 | 空 |
| ALL4（实验） | `experiments/w4_all4_flash.toml` | 四条全开 | `experiments/runs/w4_off_flash-61491decf48c/result_rows.jsonl` |

- k=3、`max_steps=12`、`max_json_retries=2` 两格一致；`name` / `compare` 只决定
  落盘位置与对照来源，不进测量链路（与 W3 四份 A/B 配置同一口径）。
- `compare` 指向 OFF 组留档，报告生成「与对照组对照（A/B）」表；**绝不指向 W1
  冻结基线**——那是 scripted 后端，跨后端只能是数值罗列，不能作因果解读。
- 两格必须写**不同的 trace 目录**（`traces/w4_off` / `traces/w4_all4`）：默认
  `traces/` 下文件名是 `<trial_id>.jsonl`，同跑两格会互相覆盖，纠正探针就会
  读到错的那一组。两个目录都在 `.gitignore` 的 `/traces/` 之下，不污染仓库。
- `run_id` 摘要不含模型名 / 单价 / 预算（它们不是实验自变量）。**换模型必须
  同时改 `name`**（例如 `w4_off_qwen`），否则新结果会覆盖同一 `run_id` 的报告；
  `compare` 路径也要跟着改。这条硬编码字符串由 `eval/w4_preflight.py` 按 OFF 组
  `run_id` 机器校验：改了 `name` 却忘了同步 `compare`，预检直接报 FAIL。
- `eval.run` 在 `openai` 后端下若找不到 `compare` 指向的对照留档，会**在读取密钥、
  发起任何模型调用之前**拒绝启动。过去这里是静默跳过，会产出一份没有
  「与对照组对照（A/B）」表却返回 0 的报告——W4 的结论恰恰来自那张表，
  这比跑失败更危险。仅离线调试协议时可用 `--allow-missing-compare` 显式跳过。

主指标 `pass^k`（分辨率仍是 1/10 = 0.10，与 W3 一致）；辅助观测 token、
latency、`invalid_calls`、纠正探针的四态分布。`pass^k` 仍是二值化指标，
两格各 30 个 trial 是描述性统计，**不是显著性检验**。

## 4. 预算：上界、单价来源与闸门

调用次数上界 = 任务数 × k × `MAX_STEPS` × 格数 = 10 × 3 × 12 × 2 = **720**。
这是「每个 trial 都跑满 12 步」的最坏情况；实际多数 trial 在 4~8 步给出
`final` 或触发终止，实际调用数会明显更低。

单价只采用服务商**官方定价页**，2026-11-06 查阅：

- DeepSeek 官方 `Models & Pricing`：`deepseek-flash`（DeepSeek-V4.1-Flash-1017）
  off-peak input cache-miss **$0.15 / 百万 tokens**、output **$0.6 / 百万**；
  peak 时段翻倍（$0.30 / $1.20）；cache hit $0.003 / $0.006。
- 阿里云百炼官方「模型调用价格」页：`qwen-plus` ≤128K 档输入 **¥0.8 / 百万**、
  输出按版本约 **¥2~4.8 / 百万**；新用户有 90 天免费额度（各 100 万 tokens）。
- 第三方聚合站给出的 DeepSeek 价格**互相矛盾**（同一模型从 $0.09 到 $1.32
  都有），本表一概不采信；价格会变，下单前以服务商账单页为准。
- 人民币换算按 1 USD ≈ 7.2 CNY 粗算，只用于量级判断，不是汇率结论。

`python -m eval.budget` 用上述 off-peak 单价 + 单次调用 1500 prompt / 200
completion tokens 的假设，产出 [experiments/w4_budget_estimate.md](../experiments/w4_budget_estimate.md)：

| 情景（A 档：mvp_10 × k=3 × 2 格） | 调用上界 | tokens 上界 | 费用上界 |
| --- | --- | --- | --- |
| off-peak，200 output/次 | 720 | 1,224,000 | $0.2484 ≈ **¥1.79** |
| off-peak，thinking 模式 1000 output/次 | 720 | 1,800,000 | $0.5940 ≈ ¥4.28 |
| peak 时段翻倍 | 720 | 1,224,000 | $0.4968 ≈ ¥3.58 |
| **peak + thinking（最保守）** | 720 | 1,800,000 | $1.1880 ≈ **¥8.55** |
| 若再加 `mvp_30`（40 任务 × 2 格，off-peak） | 2,880 | 4,896,000 | $0.9936 ≈ ¥7.15 |

**结论：W4 首轮实验的量级是几元到十几元人民币，不是需要立项审批的开支。**
真正需要用户决定的只有两件事：用哪家 / 哪个模型，以及是否愿意提供 API key。

**闸门作用域（最容易被读错的一条）**：上表的 720 是**两格合计**的计划上界；
而 `--max-calls` 作用于**单次 `eval.run`**，两格是两次独立进程、各持一份
`BudgetLedger`。单格上界 = 10 × 3 × 12 = **360 次 / 612,000 tokens**，所以按
§5 给每格 `--max-calls 720` 时，两格合计硬上限是 **1440 次**，不是「整轮
总预算 720」。闸门保证的是「单格不失控」，不是「整轮不超某个总额」；若要
把整轮卡在 720，应给每格 360（但那样就没有步数波动的余量了）。按 off-peak
单价，两格各 $0.2484、合计 $0.4968，仍远低于 `--max-cost 2.0`，因此首轮
保留 2× 余量是安全的。这段口径由 `eval/w4_preflight.py` 机器校验，不靠人记。

建议 A 档闸门：`--max-calls 720`、`--max-total-tokens 3000000`、
`--max-cost 2.0 USD`（≈¥14.4，约为最保守情景的 1.7 倍；`max_total_tokens`
同样是**单格**上限，为 612,000 的约 4.9 倍）。

口径：估算用上界不是预测；`avg_prompt` / `avg_completion` 是假设不是实测；
thinking mode 的 reasoning tokens 计入 output 计费；缓存命中可能降低 input
成本但不能假设命中率；`usage` 缺失时 token 记为 0，此时 token / 费用闸门
不再可靠而调用数闸门仍生效——所以 `eval.run` 强制要求 `--max-calls`。
闸门在**调用边界**检查，最多多花一次已经发出的调用；超限抛
`BudgetExceeded` 并停止，已发出的调用仍会计费。

## 5. 执行清单（确认预算与密钥后）

```bash
# 0) 复制 .env.example 为 .env，填 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL
#    DeepSeek:  https://api.deepseek.com  +  deepseek-flash
#    百炼:      https://dashscope.aliyuncs.com/compatible-mode/v1  +  qwen-plus

# 0.5) 离线预检：零 API 请求、零花费，把执行清单里靠人肉记住的约束
#    变成机器校验（单变量 / compare 链接 / 目录冲突 / fixture 哈希 / 预算作用域）。
#    此时应报 0 FAIL；BLOCKED 是「还没轮到」的外部输入，不是错误。
python -m eval.w4_preflight --max-calls 720 --max-total-tokens 3000000 \
  --price-in 0.15 --price-out 0.6 --currency USD

# 1) 离线上算，确认上界（不联网、不花钱）
python -m eval.budget --tasks tasks/mvp_10.jsonl --k 3 --cells 2 \
  --price-in 0.15 --price-out 0.6 --currency USD

# 2) 对照组 OFF
python -m eval.run --config experiments/w4_off_flash.toml \
  --max-calls 720 --max-total-tokens 3000000 \
  --price-in 0.15 --price-out 0.6 --currency USD --max-cost 2.0 \
  --traces-dir traces/w4_off --results-dir results/w4_off

# 2.5) ALL4 开跑前再预检一次：OFF 留档与 .env 都已到位，整体应为 READY。
#     --strict 下非 READY 返回非零退出码，可直接串进脚本；任何 FAIL 先修再跑。
python -m eval.w4_preflight --strict --max-calls 720 \
  --max-total-tokens 3000000 --price-in 0.15 --price-out 0.6 --currency USD

# 3) 实验组 ALL4（compare 已指向第 2 步的留档）
python -m eval.run --config experiments/w4_all4_flash.toml \
  --max-calls 720 --max-total-tokens 3000000 \
  --price-in 0.15 --price-out 0.6 --currency USD --max-cost 2.0 \
  --traces-dir traces/w4_all4 --results-dir results/w4_all4

# 4) 纠正探针：只读两格 trace，复用离线校准过的同一把尺子
python -m eval.guard_probe \
  --traces-dir traces/w4_off --label w4_off \
  --traces-dir traces/w4_all4 --label w4_all4 \
  --out experiments/w4_probe.md

python -m unittest discover -s tests -v
```

用百炼时把 `--price-in 0.8 --price-out 2.0 --currency CNY --max-cost 20`
替换对应参数，并把两份配置的 `name` / `compare` 改成带 `qwen` 的版本。
探针的离线校准证据（OFF 组必须数到 0 次拒绝、全开组能识别四类拒绝）
在 [experiments/w4_probe_dryrun.md](../experiments/w4_probe_dryrun.md)。

## 6. 口径边界（比数字更重要）

- **W3 与 W4 的数字不可混用、不可拼成一条曲线**。W3 的「7 条脚本固有缺陷」
  是 `ScriptedBackend` / `SeededBackend` **脚本的属性**，真实模型根本不读脚本，
  这些任务的难度会完全不同；W3 的 0.1433 / 0.3000、可达上限 0.30、`fpm`
  模型能力轴全部失效。W4 还额外引入了工具清单 + 领域库状态这一新
  system prompt，是 W3 没有的自变量。W4 的因果结论只能来自两格内部 A/B。
- `temperature=0` **不等于逐字节可复现**，服务商不保证；`pass^k` 在真实模型
  下才第一次真正用来暴露「偶尔成功」，但两次连跑产物不同是预期而非缺陷。
- 纠正率是**输出层面的机制证据**，不是成功率；`idempotency` 的正确响应是
  「跳过重复写」，不该期待有新的写工具执行，四类护栏不能拿同一列横向比较。
- 报告里的 token / latency 是真实观测但**不参与判分**，且随服务波动；不据此
  下成本或延迟的显著性结论，也不与脚本后端的 0 值相减。
- 首轮只有一个模型、一个 seed 集、10 条任务、每格 30 个 trial：描述性统计，
  不是显著性检验，不外推到其他模型 / 服务商 / 真实业务。
- **`mvp_10.jsonl` 与 `mvp_30.jsonl` 一律不改**。若还要更细的 `pass^k` 分辨率，
  只能新增版本化任务集并另开实验组，绝不能动这两个已提交 fixture 或把它们
  的分数拼在一起（sha256 由守门测试钉死）。

## 7. 停止条件与失败处理

| 情况 | 动作 |
| --- | --- |
| 预检报 FAIL，或整体非 READY | 先修配置 / 补齐输入；**不许**带着 `--allow-missing-compare` 跑正式实验 |
| 预算闸门触发 | 立即停，核对累计用量；**不擅自提额重跑**，先确认原因 |
| 大量 400 / 401 / 超时 | 先修接口与凭据，不要把请求失败算进通过率或纠正率 |
| 两格 `pass^k` 都接近 0 | 说明任务 / 上下文不可解，先诊断提示与工具协议，**不要靠加护栏刷分** |
| 两格都接近满分 | 测量天花板，效应无从分辨；另开更难的**新版本化任务集**，不改现有两个 |
| 修正探针 responsive ≈ 0 | 触发 §2 的可证伪条件：W3 机制不得外推，改协议而非调文案 |

## 8. 非目标

不做多模型横评、不做成本 / 延迟显著性分析、不接公开 τ-bench 任务集、不做
上下文压缩或 token 预算对照（这些要等 W4 首轮有了真实成本压力才有测量输入）、
不做前端回放页（W5 之后）。本轮也不新增加分护栏：W3 已收口，四类故障已有
四条护栏覆盖，继续挑「又能涨 0.10」的护栏等于放大选择性报告。

## 9. 首轮实测结果与装置缺陷（2026-11-06）

首轮两格已按 §5 跑完（DeepSeek `deepseek-flash`，182 次真实调用，费用上界约 ¥0.9），
完整数据表、只读重放、口径边界与四个下一步选项见
[experiments/w4_report.md](../experiments/w4_report.md)。结论摘要：

- 两格 `pass^k` 均 **0.00**，A/B 差值 `+0.00`，**没有分辨率**。
- 纠正探针两组均 **0 次拒绝 / 0 次 responsive**。这是**分母为 0**，
  而 §2 的可证伪条件以「已经发生拒绝」为前提，因此判定为
  **H1 未被检验**，既非证实也非证伪；W3 机制继续不得外推到真实模型。
- **装置缺陷（设计阶段未预见）**：`openai` 后端没有 `fault_per_mille`
  这个自变量，而真实模型在正常路径下 `invalid_calls = 0`、工具调用
  全部成功，**不会自然产生四类输出级故障**，护栏因此一次都没触发。
  W3 离线校准里的 7 次拒绝全部来自注入了故障的 `SeededBackend`。
- **另一个口径缺陷**：终态判分 `state != target` 是完整 dict 比对，
  含 `audit_log.notify.message` 的自由文本精确匹配，30/30 trial 都带
  `terminal_state_mismatch`；但模型也确有真实策略违规（通知文本不含订单号、
  3 个 trial 回复「无法退款」），两者不能互相开脱。

**因此 §5 的执行清单虽已全部完成，却不足以检验 H1**。若要继续，必须另开
版本化的 **W4-b 实验组**（新 `name` / 新 `run_id`），给 `openai` 后端接入与 W3
同口径的输出级故障注入，并明确标注「输出被注入扰动，不是纯净模型行为」。
**不修改 `mvp_10.jsonl` / `mvp_30.jsonl`、不改判分器、不回溯改写任何归档**。
在用户确认选项之前，不擅自启动新的实验组。
