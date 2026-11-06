# W4-c 实测报告：拒绝后重发的是不是原本那一个动作（2026-11-06）

> **本报告的全部输出都被注入了输出级故障，不是纯净模型行为。**
> 数据来源：两份 `experiments/runs/w4c_*/result_rows.jsonl`、`report.md`、`config.json`，
> 只读判定 `experiments/w4c_probe.md`，离线装置校准 `experiments/w4c_probe_dryrun.md`，
> 以及独立的 `clean_actions/w4c_*/` 只读 side-car（`traces/` / `results/` /
> `clean_actions/` 被 `.gitignore` 排除，不入库）。
> **不改判分器、不改任务集、不回溯改写 W3 / W4 首轮 / W4-b 任何归档；
> 四套自变量不同，任何数字不可相减或拼成曲线。**

## 1. 结论先行

1. **W4-b 剩下的开放项，在输出层得到支持**。ALL4 组产生 **15 次 `[GUARD-REJECT]`**，
   全部配回一份「被替换掉的干净动作」留档（`missing = 0`）；15 个可比事件里
   **14 次 `identical`**（拒绝后重发的动作与被替换掉的那一个逐字段相同）、
   **1 次 `different`**。W4-b 无法回答的「重发的是不是原本那一个动作」，
   现在可判定，且落在「是」的一边。
2. **但 14/15 不能读成「模型理解了被拿走的那一个动作」**。模型从未见过
   `clean_text`；两次模型调用的上下文几乎相同（只多了坏文本 + 拒绝消息），
   且 **15/15 的工具名与结构化参数本来就由任务上下文唯一决定**。更准确的
   说法是「在同一上下文下按同一策略复算出了同一个合法调用」，含明确的
   机械成分。详见 §6。
3. **仍不等于 W3 机制被完整验证，更不等于护栏有效**。两格 `pass^k` 仍
   **0.00**、A/B 差 `+0.00`，**没有分辨率**；W3 靠 `_pending_correct` 由
   harness 代答且任务因此成功，本组既没有代答也没有任务成功。
   **W3 的 0.1433 → 0.3000 继续不得外推到真实模型。**
4. **本组主读数是判定分布，不是通过率**。`pass^k = 0.00` 是
   `docs/w4c-design.md` §1 预先写死的预期，不是失败。

**一句话**：W4-c 补上了那份只读证据，把开放项变成可判定，并在 15 个事件上
看到模型确实重回了同一个调用；但「同一个调用」来自同策略复算而非理解，
响应机制成立 ≠ 任务成功 ≠ 护栏有效，后两者在本装置下仍不可测。

## 2. 装置、执行与实际用量

与 W4-b 的装置差异**只有一处**：`OutputFaultBackend` 在每次注入时把替换前的
`reply.text` 留进 `clean_actions`，由 `--clean-actions-dir` 写到独立目录。
留档是**事后观测**——绝不进 `messages` / system prompt / 拒绝文本，运行时也
绝不读它来构造响应（防代答红线由 `tests/test_w4c_clean_actions.py` 逐条钉死）。
既有 trace 8 字段、结果行、判分、全部既有 `run_id` 逐字节不变。

| 项 | OFF（对照） | ALL4（实验） |
| --- | --- | --- |
| 配置 | `experiments/w4c_off_flash.toml` | `experiments/w4c_all4_flash.toml` |
| `run_id` | `w4c_off_flash-b891da858ccb` | `w4c_all4_flash-b38a0c5106d6` |
| 后端 / 模型 | `openai` / `deepseek-flash` | 同左 |
| 注入参数 | `seed=11`, `fault_per_mille=300` | 同左 |
| `guards.enabled` | 空 | 四条全开 |
| trial 数 | 30（10 任务 × k=3） | 30 |
| 真实模型调用 | 115 次 | 101 次 |
| `tokens`（prompt+completion） | 68,786 | 59,725 |
| `latency_ms` 合计 | 137,007 | 120,785 |
| `guard` span | **0**（无护栏，符合预期） | **15** |
| `invalid_calls` | 15 | 5 |
| side-car | 16 文件 / 21 条留档 | 13 文件 / 15 条留档 |
| 产物目录 | `traces/w4c_off`、`results/w4c_off`、`clean_actions/w4c_off` | `.../w4c_all4` 三处 |

- 合计 **216 次真实调用、128,511 tokens**。因 trace 未拆分 prompt / completion，
  这里只给**上界**：全部按最贵的 peak 输出价 $1.2 / 百万 tokens 计，约
  **$0.154 ≈ ¥1.1**（与 `w4b_report.md` 同一口径，账单以控制台为准）。
  单格闸门 `--max-calls 720 --max-total-tokens 3000000 --max-cost 2.0 USD` 远未触发。
- **跑批前的零成本闸门全部实际执行**：
  1. `python -m eval.w4c_probe --dry-run`：`fpm=0` 基准 0 注入 / 0 拒绝；
     A 路「给不同动作」13 个 `different` / 0 个 `identical`；
     B 路「重发同一动作」13 个 `identical` / 0 个 `missing`。两路一起证明尺子
     既不误报也不漏报，且配对不漏事件；
  2. 预检（跑 OFF 前）`11 PASS / 1 BLOCKED / 0 FAIL`，BLOCKED 是「OFF 组留档
     尚未生成」的正常阶段语义；OFF 留档生成后 `--strict` 为 **12 PASS / READY**；
  3. **OFF 跑完的保险闸**：`invalid_calls = 15`（W4 首轮为 0）、trace 里
     13 处注入特征、side-car 21 条留档且四类故障全覆盖、`trial_id` 与文件名
     一致、`clean_text != injected_text`、OFF 组拒绝事件为 0。若这些仍为 0
     就会停止、不跑第二格。注入与留档确实接上，才继续 ALL4。

## 3. A/B 结果（无分辨率，中间指标不得作因果解读）

> 完整表见 `experiments/runs/w4c_all4_flash-b38a0c5106d6/report.md` §「与对照组对照（A/B）」。

| 指标 | OFF | ALL4 | 变化 |
| --- | --- | --- | --- |
| `pass¹` | 0.00 | 0.00 | +0.00 |
| `pass^3` | 0.00 | 0.00 | +0.00 |
| 平均步数 | 3.83 | 3.37 | −0.47 |
| 无效工具调用率 | 0.13 | 0.05 | −0.08 |
| `bad_args` | 2 | 0 | −2 |
| `policy_violation` | 20 | 18 | −2 |
| `unknown` | 5 | 9 | +4 |
| `wrong_tool` | 3 | 3 | +0 |

逐任务 10 行全是 `000`。**两格都落在地板上，A/B 没有分辨率**，因此以上任何
中间指标的变化都**不能**解读成「护栏有效 / 有害」：`invalid_calls` 不是成功率
代理（W3 里降 / 持平 / 升都出现过而 `pass^k` 仍提升），`policy_violation`、
平均步数同理。两格的注入轨迹本身也不同（护栏拦下后会多出响应拒绝的
generate、消耗 cursor 且按协议跳过注入），加上 `temperature=0` 下真实模型
服务侧仍有波动，这些差异只能作聚合描述。

## 4. 只读判定（本组主读数）

> 证据：`experiments/w4c_probe.md`。判定器复用 `eval.guard_probe.probe_spans`
> 取拒绝事件，按 `(trial_id, injected_text == rejected_text)` 配回 side-car，
> 再比较 `clean_text` 与 `reply_text` 解析出的动作；判定口径在
> `docs/w4c-design.md` §3.1 / §6 **跑批前写死**。

| 组 | 拒绝事件 | comparable | identical | different | final_instead | unparseable | idempotency_skipped | chained | missing |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `w4c_off` | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `w4c_all4` | **15** | **15** | **14** | **1** | 0 | 0 | 0 | 0 | **0** |

按护栏类型拆分：`schema_validation` 4（全 identical）、`tool_retry` 5（全
identical）、`json_recovery` 6（5 identical / 1 different）。OFF 组 0 拒绝，
`missing = 0`、`chained = 0`、`unparseable = 0` —— 装置接上、配对完整。

### 4.1 比百分比更有信息量的三个观察

1. **15/15 的工具名与结构化参数完全一致**。把 `clean_text` 与 `reply_text`
   解析后比较，15 个事件里工具名全部相同，除自由文本 `message` 外的 args
   也全部相同。唯一那个 `different`（`T08-t1` / `json_recovery` / `bad_json`）
   是**同一个 `notify_customer` 调用换了通知话术**：
   - 被替换的干净动作：`"您的订单 O-100 已退款 2500 分（25.00 元），款项将退回原支付方式。"`
   - 拒绝后的响应：`"您的订单 O-100 已退款 2500 分（即 25.00 元），退款已处理完成。"`

   也就是说，**没有一次响应换成了另一个动作**；所谓「不同」只发生在自由文本
   字段。这既是对结论的补充，也是 §6 里「动作空间窄」的直接证据。
2. **`identical` 是规范化判定，不是逐字节比对**。比较 `tool` + `args`
   （`sort_keys` + 紧凑分隔符），忽略键序与空格；14 个 identical 里有 **4 个**
   原始文本存在空白 / 键序差异（例如紧凑 JSON 对 `{"args": {...}, "tool": ...}`）。
   若按原始字符串比较会假判成 `different`。自由文本 `message` 也算进 args，
   所以换个说法就是 different —— 口径偏严，不是偏松。
3. **`idempotency_skipped` 分层本轮 0 样本**。15 个事件来自 schema 4 / retry 5 /
   json 6，**没有一次 `idempotency` 拒绝**（3 条 `duplicate_call` 故障在缺少
   可重放写动作时退化成 `bad_args`，被 schema 拦下）。W4-b 那 1 次 idempotency
   拒绝属另一组、另一随机流。**不能**据此推断「模型会按拒绝文本跳过重复写」，
   那条在本组未被检验。

## 5. 与跑批前判定表逐条对照

| `docs/w4c-design.md` §6 的观测 | 本次 | 判定 |
| --- | --- | --- |
| `comparable = 0` | `comparable = 15` | 不触发「开放项仍未检验」 |
| `identical` 占多数 | 14 / 15 | 「拒绝后能重新推导出同一个合法动作」在**输出层**成立 |
| 任意结果 | `pass^k` 两格 0.00 | 不作成败判据；中间指标不作因果解读 |

按判定表的三个「不等于」逐条声明：这**不等于** W3 机制被完整验证（W3 靠
harness 代答且任务因此成功）；**不等于**护栏有效（论效果只认 `pass^k`，本次
无分辨率）；**不等于** W3 的 0.1433 → 0.3000 被复现。按可证伪条件的反面，
本次**不触发**「改协议」的动作建议；但「不得外推 W3 数字」这条约束本身
**没有解除**——它由自变量不同决定，与 identical 高低无关。

## 6. 装置局限（必须连结论一起讲）

1. **同策略复算，不是回忆 / 理解**。模型**从未见过** `clean_text`：对话里
   出现的是被替换后的坏文本 + 拒绝消息。两次 model call 的 `messages` 只差
   这两条，模型被要求「重新输出这一个完整的工具调用」，于是按几乎相同的
   上下文重新算了一次。14/15 说明它算回了同一个调用，**不说明它知道有一个
   动作被拿走了**，更不说明它「理解了拒绝语义」。
2. **动作空间窄，比例含机械成分**。15/15 工具名与结构化参数一致；任务脚本
   与工具观察已经把下一步限定得很死，拒绝文本又明确要求重发「这一个」。
   在这样的动作空间里，重回同一个调用是高度可预期的。**14/15 不是理解率**，
   不衡量语义理解深度，也不能与 W4-b 的 17/17 responsive 相减或拼接。
3. **规范化比较的口径边界**。`identical` 比较 tool + args（含自由文本字段），
   忽略键序 / 空格；4 个事件靠规范化才判 identical。这是跑批前写死的口径，
   没有事后放宽，但它定义的是「同一个合法调用」，不是「同一个字节串」。
4. **输出被注入扰动，不是纯净模型行为**。`fault_per_mille=300` 是装置参数，
   不是模型能力轴；模型看到的是一个它自己没写过的坏输出。本组检验的是
   「被要求时能否继续给出同一个合法动作」，比「能否修正自己的真实错误」
   弱一层，且不与 W3（fpm=60）、W4 首轮（无注入）相减或拼曲线。
5. **`idempotency` 分层 0 样本**；一个模型、10 条任务、每格 30 trial、
   15 个拒绝事件：这是**描述性统计，不是显著性检验**；`temperature=0`
   也不等于逐字节可复现。
6. **不等于任务成功**。判分天花板未改（见 §7），模型仍有真实策略违规；
   本组的判定不读 `passed`、不进结果行、不改 grader。

## 7. 为什么 `pass^k` 仍全 0（辅助观测，不是实验结论）

- 与 W4 首轮 / W4-b 同源：终态判分 `state != target` 是完整 dict 比对，含
  `audit_log.notify.message` 的**自由文本精确匹配**。两格结果行里
  **30/30 都带 `terminal_state_mismatch`**；归档报告的归因证据里仍有
  `policy:notify_after_refund`、`missing_disclosures:无法退款` 等真实策略违规。
- W4-c **没有修改判分器、没有把目标话术喂给模型**。要让它有分辨率只能改
  判分或泄露话术，两者都触及「评测替模型做对」的红线。
- 本组**不重做** W4-b 那种假设性只读重放：它不进 `pass^k`、不与 W3 比较，
  在判分器未改的前提下不会提供新信息；也不做成本帕累托图。

## 8. 口径边界（面试与对外表述必须连边界一起讲）

- **可以说**：W4-b 留下「被替换的干净动作没留档」这一开放项后，W4-c 给注入器
  补了一份独立只读 side-car（绝不回灌给模型），用离线两路校准证明尺子没装反；
  在 15 次拒绝上看到模型重回了同一个合法调用（14/15），其中 15/15 的工具名与
  结构化参数一致；`missing=0`、OFF 组 0 拒绝，装置与配对都完整。
- **不可以说**：
  - 「护栏在真实模型上有效 / 复现了 W3 的提升」——`pass^k` 两格 0.00，
    没有分辨率，中间指标不作因果解读；
  - 「W3 机制已被验证」——W3 靠 harness 代答且任务因此成功，本组两者都没有；
  - 「14/15 是模型的理解率」——同策略复算、动作空间窄、含机械成分、样本 15；
  - 「模型回忆起了被拿走的动作」——它从未见过 `clean_text`；
  - 「`idempotency` 的跳过行为被验证」——本轮该分层 0 样本；
  - 「真实模型能力差」或「`pass^k` 分辨率为 0」——`pass^k = 0` 混合了判分口径
    与模型行为；分辨率恒为 `1/N`（此处 `1/10`），与 k 无关；
  - 把 W3（0.1433 / 0.3000）、W4 首轮（0.00）、W4-b（17/17 responsive）、
    W4-c（14/15 identical）拼成一条曲线或相减。
- `invalid_calls`、`policy_violation`、平均步数都是中间指标，
  **论效果只认 `pass^k`**，且本次 `pass^k` 无信息量。

## 9. 下一步：建议收口

W4-c 已完成用户确认的「补一份被替换干净动作的只读证据」的全部目标：
开放项从「不可判定」变成「可判定且在输出层成立」，且装置校准、防代答、
既有归档零改动三条都有机器守门。继续加实验组的边际收益很低：

- 想让 `pass^k` 有分辨率，只能改判分器或泄露目标话术（红线）；
- 想检验「模型能否修正自己的真实错误」，需要一套不再替换模型输出的全新装置，
  而不是给现有注入器再加参数；
- 想补齐 `idempotency` 分层，需要刻意制造可重放的成功写动作，那会改变装置参数
  （新的 `fpm` / 新的实验组），按纪律须用户**明确确认**后才可开。

**推荐就此收口，转向面试叙事**：W3 的归因对照仍是最值钱的证据；
W4 首轮 → W4-b → W4-c 合起来是一条完整的「发现装置缺陷 → 离线校准 →
修复重跑 → 如实分层下结论」的工程故事，比硬凑一个正向通过率更扛追问。
用户若决定继续，需明确确认新实验组；本报告之后不擅自扩「4–6 组单变量对照」、
不改判分器与两个已提交任务集。

## 10. 复现与产物索引

```bash
# 0) 装置校准（零 API 请求）
python -m eval.w4c_probe --dry-run --seed 11 --fault-per-mille 300 --k 3 \
  --out experiments/w4c_probe_dryrun.md

# 1) 预检（跑 OFF 前 11 PASS / 1 BLOCKED；OFF 后 --strict 为 12 PASS / READY）
PRE="--off-config experiments/w4c_off_flash.toml \
     --on-config experiments/w4c_all4_flash.toml \
     --off-results results/w4c_off --off-traces traces/w4c_off \
     --on-results results/w4c_all4 --on-traces traces/w4c_all4 \
     --out experiments/w4c_preflight.md \
     --max-calls 720 --max-total-tokens 3000000 \
     --price-in 0.15 --price-out 0.6 --currency USD"
python -m eval.w4_preflight $PRE

# 2) OFF（--clean-actions-dir 必须显式给出，否则不产生 side-car）
python -m eval.run --config experiments/w4c_off_flash.toml \
  --max-calls 720 --max-total-tokens 3000000 \
  --price-in 0.15 --price-out 0.6 --currency USD --max-cost 2.0 \
  --traces-dir traces/w4c_off --results-dir results/w4c_off \
  --clean-actions-dir clean_actions/w4c_off

# 2.5) 保险闸：invalid_calls > 0 且 clean_actions/w4c_off 非空，才继续
python -m eval.w4_preflight --strict $PRE

# 3) ALL4
python -m eval.run --config experiments/w4c_all4_flash.toml \
  --max-calls 720 --max-total-tokens 3000000 \
  --price-in 0.15 --price-out 0.6 --currency USD --max-cost 2.0 \
  --traces-dir traces/w4c_all4 --results-dir results/w4c_all4 \
  --clean-actions-dir clean_actions/w4c_all4

# 4) 只读判定
python -m eval.w4c_probe \
  --traces-dir traces/w4c_off --clean-actions-dir clean_actions/w4c_off --label w4c_off \
  --traces-dir traces/w4c_all4 --clean-actions-dir clean_actions/w4c_all4 --label w4c_all4 \
  --out experiments/w4c_probe.md

python -m unittest discover -s tests -v
```

> **归档 `runs/*/report.md` 里自动生成的「复现」命令不含
> `--clean-actions-dir`**（`analyze.render_report` 不知道这个输出路径，
> 且不能为它改动所有既有归档报告）。照抄那段命令不会生成 side-car，
> `eval.w4c_probe` 会如实报目录不存在；完整复现请用上面的命令块。
> 真实模型结果**不保证逐字节可复现**（服务侧波动）；装置校准的确定性
> 数字可逐字节复现；同配置 `run_id` 不变。

| 产物 | 路径 |
| --- | --- |
| 本报告 | `experiments/w4c_report.md` |
| OFF 归档 | `experiments/runs/w4c_off_flash-b891da858ccb/` |
| ALL4 归档（含 A/B 表） | `experiments/runs/w4c_all4_flash-b38a0c5106d6/` |
| 只读判定（实测） | `experiments/w4c_probe.md` |
| 装置离线校准 | `experiments/w4c_probe_dryrun.md` |
| 跑批前预检（READY 快照） | `experiments/w4c_preflight.md` |
| 注入口径与判定规则 | `docs/w4c-design.md` |
| 留档实现与守门测试 | `runtime/model.py`、`eval/run.py`、`eval/runner.py`、`eval/w4c_probe.py`、`tests/test_w4c_clean_actions.py`、`tests/test_w4c_results.py` |
| 前序结论（对照阅读） | `experiments/w4_report.md`、`experiments/w4b_report.md` |
