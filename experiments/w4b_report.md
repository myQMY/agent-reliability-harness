# W4-b 实测报告：真实模型对 `[GUARD-REJECT]` 的响应（2026-11-06）

> **本报告的全部输出都被注入了输出级故障，不是纯净模型行为。**
> 数据来源：两份 `experiments/runs/w4b_*/result_rows.jsonl`、`report.md`、`config.json`，
> 只读探针 `experiments/w4b_probe.md`，以及离线装置校准
> `experiments/w4b_probe_dryrun.md`。`results/` 与 `traces/` 被 `.gitignore` 排除。
> **不改判分器、不改任务集、不回溯改写 W3 / W4 首轮任何归档；
> W3、W4 首轮、W4-b 三套自变量不同，任何数字不可相减或拼成曲线。**

## 1. 结论先行

1. **H1 在字面定义上得到支持**：ALL4 组产生了 **17 次 `[GUARD-REJECT]`**，
   其中 **17 次 responsive（17/17）**，四类护栏全部覆盖。W4 首轮「分母为 0、
   H1 未被检验」的状态由此解除。按 `docs/w4b-design.md` §6 的判定表，
   结论是「真实模型在**输出层**能读懂拒绝协议并作出有效响应」。
2. **但这不是「W3 机制已被验证」，更不是「护栏有效」**。两格 `pass^k`
   仍都是 **0.00**、A/B 差值 `+0.00`，**没有分辨率**；判分器的自由文本精确
   匹配天花板没有改动；被注入替换掉的干净动作没有留档，因此无法验证
   「模型重发的是不是原本那一个动作」。详见 §6 的装置局限。
3. **W3 的 0.1433 → 0.3000 继续不得外推到真实模型**。W3 用的是
   `SeededBackend` + `_pending_correct` 硬编码重发 + `fpm=60`；W4-b 用的是
   真实模型 + `fpm=300` 的输出层扰动。两套装置测的不是同一件事。

**一句话**：W4-b 把首轮的「装置提不出这个问题」修好了，并在 17 次事件上
看到真实模型确实能响应拒绝消息；但响应机制成立 ≠ 任务成功 ≠ 护栏有效，
后两者在本装置下**仍不可测**。

## 2. 执行与实际用量

| 项 | OFF（对照） | ALL4（实验） |
| --- | --- | --- |
| 配置 | `experiments/w4b_off_flash.toml` | `experiments/w4b_all4_flash.toml` |
| `run_id` | `w4b_off_flash-f6e556cd7201` | `w4b_all4_flash-6d7222dc87af` |
| 后端 / 模型 | `openai` / `deepseek-flash` | 同左 |
| 注入参数 | `seed=11`, `fault_per_mille=300` | 同左 |
| `guards.enabled` | 空 | 四条全开 |
| trial 数 | 30（10 任务 × k=3） | 30 |
| 真实模型调用 | 115 次 | 100 次 |
| `tokens`（prompt+completion） | 68,356 | 59,980 |
| `latency_ms` 合计 | 144,155 | 133,645 |
| `guard` span | **0**（无护栏，符合预期） | **17** |
| `invalid_calls` | 11 | 4 |
| trace / results 目录 | `traces/w4b_off`、`results/w4b_off` | `traces/w4b_all4`、`results/w4b_all4` |

- 合计 **215 次真实调用、128,336 tokens**。因 trace 未拆分 prompt / completion，
  这里只给**上界**：全部按最贵的 peak 输出价 $1.2 / 百万 tokens 计，约
  **$0.154 ≈ ¥1.1**；实际跑批落在 off-peak，账单以控制台为准。单格闸门
  `--max-calls 720 --max-total-tokens 3000000 --max-cost 2.0 USD` 远未触发。
- **跑批前的三道零成本闸门全部实际执行**：
  1. `python -m eval.w4b_probe` 装置校准：OFF 14 次注入 / **0 guard span**，
     ALL4 14 次注入 / **14 次拒绝**，另有 `fpm=0` 基准组被强制断言为 0；
  2. 预检（跑 OFF 前）`11 PASS / 1 BLOCKED / 0 FAIL`，OFF 留档生成后
     `--strict` 为 **12 PASS / READY**；
  3. **OFF 跑完后的保险闸门**：`invalid_calls = 11`（首轮为 0）、trace 里
     21 处注入特征（`bad_json` 10 / `wrong_tool` 6 / `bad_args` 5）。若这一项
     仍为 0 就会停止、不跑第二格。注入确实接上了，才继续 ALL4。

## 3. A/B 结果（无分辨率，中间指标不得作因果解读）

> 完整表见 `experiments/runs/w4b_all4_flash-6d7222dc87af/report.md` §「与对照组对照（A/B）」。

| 指标 | OFF | ALL4 | 变化 |
| --- | --- | --- | --- |
| `pass¹` | 0.00 | 0.00 | +0.00 |
| `pass^3` | 0.00 | 0.00 | +0.00 |
| 平均步数 | 3.83 | 3.33 | −0.50 |
| 无效工具调用率 | 0.10 | 0.04 | −0.06 |
| `wrong_tool` | 3 | 1 | −2 |
| `bad_args` | 3 | 0 | −3 |
| `policy_violation` | 16 | 24 | +8 |
| `unknown` | 8 | 5 | −3 |

逐任务 10 行全是 `000`。**两格都落在地板上，A/B 没有分辨率**，
因此以上任何中间指标的变化都**不能**解读成「护栏有效 / 有害」：
`invalid_calls` 不是成功率代理（W3 里降 / 持平 / 升都出现过而 `pass^k`
仍提升），`policy_violation`、平均步数同理。两格的注入轨迹本身也不同
（护栏拦下后会多出响应拒绝的 generate），加上 `temperature=0` 下真实模型
服务侧仍有波动，这些差异只能作聚合描述。

**`pass^k = 0.00` 是 `docs/w4b-design.md` §1 预先写死的预期，不是本组失败**：
本组的成功判据是探针分母，不是通过率。

## 4. `[GUARD-REJECT]` 纠正探针（本组主读数）

> 证据：`experiments/w4b_probe.md`（`python -m eval.guard_probe` 只读两格 trace，
> 与 W4 首轮、装置校准共用同一把尺子）。

| 组 | 拒绝数 | 有下一次输出 | 输出合法 | 已改变 | responsive | 后续 tool 成功 |
| --- | --- | --- | --- | --- | --- | --- |
| `w4b_off` | 0 | 0 | 0 | 0 | 0 | 0 |
| `w4b_all4` | **17** | 17 | 17 | 17 | **17** | 15 |

按护栏类型拆分：

| 护栏 | 拒绝数 | responsive | 后续 tool 成功 |
| --- | --- | --- | --- |
| `schema_validation` | 5 | 5 | 5 |
| `idempotency` | 1 | 1 | 1 |
| `tool_retry` | 4 | 4 | 4 |
| `json_recovery` | 7 | 7 | 5 |

四类护栏全部被触发，`0 次 no_reply / 0 次 unparseable / 0 次 repeated`。
**分母 > 0，§6 的可证伪条件这次真正可判**：responsive 率 100%，
与「原样重复被拒动作」的比例（0）明显不同，因此按判定表落在
「机制在输出层成立」一行，**不触发**「W3 机制不成立、改协议」的分支。

### 4.1 响应的实际形态（比 100% 更有信息量）

只读拆解 17 次拒绝后的 `reply_text`：

| 响应形态 | 次数 | 说明 |
| --- | --- | --- |
| 合法工具调用且随后成功执行 | **15** | `refund_order` 9、`get_order` 4、`notify_customer` 2 |
| 直接输出 `final` | **2** | 均来自 `json_recovery`，`followup_tool_ok = False` |

- 15 次工具调用**全部**在拒绝后成功执行了工具，说明模型不只是「换了一句
  话」，而是重新给出了语法与参数都合法、能跑通的动作。
- `idempotency` 那 1 次的拒绝文本要求「跳过该动作，直接输出下一个工具
  调用或 final」，模型输出的是 `notify_customer`（下一个动作）——
  语义上确实照做了，而不是重复被拒的写动作。按探针口径，
  `idempotency` 的「后续 tool 成功」不与另外三类横向比较。
- 2 次 `final` 响应**不是「重发被跳过的那一个工具调用」**。拒绝文本字面上
  允许「工具调用或 final」，所以它们仍计入 responsive，但不能算作
  「纠正成功」的强证据。
- 3 个 trial 出现连锁拒绝（同一 trial 内 2 次），拦截预算
  `MAX_GUARD_REJECTIONS = 3` 未被吃光。

## 5. H1 判定

`docs/w4-design.md` §2 写死的 H1 是「真实模型收到 `[GUARD-REJECT]` 后，
下一次能输出**合法且不同于被拒动作**的响应」。本次：

- 前提成立：ALL4 组 17 次拒绝（不是首轮的分母 0）；
- 观测：17/17 输出合法、与被拒文本不同；15/17 进一步是一个成功执行的
  工具调用；0 次「原样重复」；
- **判定：H1 在字面定义上得到支持**（输出层机制成立）。

即便如此，`docs/w4b-design.md` §6 明确要求两个结论分别报：

- 这不等于 `pass^k` 提升（两格都 0.00，无分辨率）；
- 这不等于护栏有效（论效果只认 `pass^k`）；
- 这不等于 W3 的「模型读懂拒绝 → 重发同一个干净动作 → 任务因此成功」
  这条完整机制被验证（见 §6）。

按可证伪条件的反面：本次**不触发**「W3 机制不得外推、改协议」的动作。
但「不得外推 W3 数字」这条约束本身**没有解除**——它由自变量不同决定，
与 responsive 高低无关。

## 6. 装置局限（必须连结论一起讲）

1. **被替换掉的干净动作没有留档**。注入器只把 `ModelReply.text` 换成坏文本，
   真实模型当次原本生成的动作不进 trace。因此能验证的是「下一次输出合法、
   不同于被拒文本」，**无法验证「重发的是不是原本那一个动作」**。
   W3 靠 `_pending_correct` 有明确参照，W4-b 没有——这正是两者不等价的原因。
2. **responsive 的定义偏宽**。「合法且与被拒文本不同」的门槛不高：被拒的
   是 `{not-json`、`__missing_tool__`、`args:{}` 这类明显异常的文本，
   真实模型几乎不可能原样复现。17/17 里含这部分**机械成分**，
   不能读成「模型理解了 100% 的拒绝语义」。更强的证据是 §4.1 的
   15 次成功工具调用，而不是那个百分比。
3. **模型看到的是一个它自己没写过的坏输出**。注入发生在 harness 侧，
   对话里出现的是被替换后的 assistant 消息 + 拒绝文本，模型被要求
   「重新输出这一个完整的工具调用」，但它并没有生成过那一个动作。
   所以本组检验的是「能否在被要求时继续给出合法动作」，比
   「能否修正自己的真实错误」弱一层。
4. **输出被注入扰动，不是纯净模型行为**。`fault_per_mille=300` 是装置参数，
   不是模型能力轴；本组不与 W3（`fpm=60`）、W4 首轮（无注入）相减或拼曲线。
5. **小样本**：一个模型、10 条任务、每格 30 trial、17 次拒绝事件，
   是描述性统计，**不是显著性检验**；`temperature=0` 也不等于逐字节可复现。

## 7. 为什么 `pass^k` 仍全 0（辅助观测，不是实验结论）

与 W4 首轮同一套只读重放（从 trace 重放工具调用重建终态，纯只读、
不重跑模型）：

| 判据（每格 30 trial） | OFF | ALL4 |
| --- | --- | --- |
| `orders` 表与 target 一致 | 27 | 27 |
| `required_disclosures` 全部出现 | 26 | 26 |
| 30/30 带 `terminal_state_mismatch` | 是 | 是 |
| 所有 `policy_checks` 通过（重放态） | 14 | 6 |
| 放宽 `notify` 话术后四项全满足（**假设性通过**） | **12** | **4** |

- 30/30 的 `terminal_state_mismatch` 仍是 `state != target` 完整 dict 比对里
  `audit_log.notify.message` 的自由文本精确匹配造成的系统性天花板，
  W4-b **没有修改判分器、没有把目标话术喂给模型**。
- ALL4 的假设性通过数（4）低于 OFF（12）：护栏拦下动作改变了工具轨迹
  （17 次拦截、其中 2 次模型直接给 `final` 跳过后续动作），重放出的
  `audit_log` 行数与 target 不同；同时两格的注入轨迹与模型服务侧输出也不同。
- > **这是事后假设性重算，不进 `pass^k`、不与 W3 比较，更不构成
  > 「护栏让分数下降」的证据。** 它只说明判分天花板仍在，且模型仍有真实
  > 策略违规（通知文本不含订单号、部分 trial 回复「无法退款」）。

## 8. 口径边界（面试与对外表述必须连边界一起讲）

- **可以说**：W4 首轮分母为 0 后，通过给真实模型路径接上与 W3 同源的
  输出级故障注入，在 17 次拒绝事件上看到真实模型能输出合法且不同的响应，
  其中 15 次是成功执行的工具调用；装置校准用 `fpm=0` 基准证明分母只来自注入。
- **不可以说**：
  - 「护栏在真实模型上有效 / 复现了 W3 的提升」——`pass^k` 两格都 0.00，
    没有分辨率，中间指标不作因果解读；
  - 「W3 机制已被验证」——§6 的三层局限，尤其干净动作没留档；
  - 「responsive 率 100%」当作模型理解率——定义宽、含机械成分、样本 17；
  - 「W4-b 的 12 → 4 说明护栏有害」——假设性重放，不进 `pass^k`；
  - 「真实模型能力差」——`pass^k = 0` 混合了判分口径与模型行为；
  - 「`pass^k` 分辨率为 0」——分辨率恒为 `1/N`（此处 `1/10`），
    与 k 无关，这里是两格落在同一个取值上；
  - 把 W3（0.1433 / 0.3000）、W4 首轮（0.00）、W4-b（0.00）
    拼成一条曲线或相减。
- `invalid_calls`、`policy_violation`、平均步数都是中间指标，
  **论效果只认 `pass^k`**，且本次 `pass^k` 无信息量。

## 9. 下一步：建议收口

W4-b 已完成选项 A 的全部目标：**把「装置提不出 H1」修好，并给出可判定的
机制结论**。继续加实验组的边际收益很低：

- 想让 `pass^k` 有分辨率，只能改判分器或泄露目标话术，两者都触及
  「评测替模型做对」的红线；W4-b 本就不追求分数。
- 想验证「重发的是不是原本那一个动作」，需要让注入器把被替换的干净动作
  写进一份新的只读证据（不改 trace 的既有 8 字段、不改判分）。这可以做，
  但会把装置复杂度再抬一层，而结论仍受制于 `pass^k` 天花板。
- **推荐就此收口，转向面试叙事**：W3 的归因对照仍是最值钱的证据；
  W4 首轮 + W4-b 合起来是一条很扎实的「发现装置缺陷 → 离线校准 →
  修复重跑 → 如实分层下结论」的工程故事，比硬凑一个正向通过率更扛追问。

用户若决定继续，需**明确确认新实验组**；本报告之后不擅自扩
「4–6 组单变量对照」、不做成本帕累托图、不改两个已提交任务集。

## 10. 复现与产物索引

```bash
# 0) 装置校准（零 API 请求）
python -m eval.w4b_probe --seed 11 --fault-per-mille 300 --k 3 \
  --out experiments/w4b_probe_dryrun.md

# 1) 预检（两格配置与目录均为 W4-b 专属）
PRE="--off-config experiments/w4b_off_flash.toml \
     --on-config experiments/w4b_all4_flash.toml \
     --off-results results/w4b_off --off-traces traces/w4b_off \
     --on-results results/w4b_all4 --on-traces traces/w4b_all4 \
     --out experiments/w4b_preflight.md \
     --max-calls 720 --max-total-tokens 3000000 \
     --price-in 0.15 --price-out 0.6 --currency USD"
python -m eval.w4_preflight $PRE

# 2) OFF，然后核对 invalid_calls / 注入特征非零（保险闸门）
python -m eval.run --config experiments/w4b_off_flash.toml \
  --max-calls 720 --max-total-tokens 3000000 \
  --price-in 0.15 --price-out 0.6 --currency USD --max-cost 2.0 \
  --traces-dir traces/w4b_off --results-dir results/w4b_off

python -m eval.w4_preflight --strict $PRE    # 应为 12 PASS / READY

# 3) ALL4
python -m eval.run --config experiments/w4b_all4_flash.toml \
  --max-calls 720 --max-total-tokens 3000000 \
  --price-in 0.15 --price-out 0.6 --currency USD --max-cost 2.0 \
  --traces-dir traces/w4b_all4 --results-dir results/w4b_all4

# 4) 只读探针
python -m eval.guard_probe --traces-dir traces/w4b_off --label w4b_off \
  --traces-dir traces/w4b_all4 --label w4b_all4 \
  --out experiments/w4b_probe.md

python -m unittest discover -s tests -v
```

> 真实模型结果**不保证逐字节可复现**（服务侧波动）；归档的
> `result_rows.jsonl` 是本次实测证据，重跑会写入新的 `traces/` / `results/`，
> 但同配置 `run_id` 不变。装置校准的确定性数字可逐字节复现。

| 产物 | 路径 |
| --- | --- |
| 本报告 | `experiments/w4b_report.md` |
| OFF 归档 | `experiments/runs/w4b_off_flash-f6e556cd7201/` |
| ALL4 归档（含 A/B 表） | `experiments/runs/w4b_all4_flash-6d7222dc87af/` |
| 纠正探针（实测） | `experiments/w4b_probe.md` |
| 装置离线校准 | `experiments/w4b_probe_dryrun.md` |
| 跑批前预检（READY 快照） | `experiments/w4b_preflight.md` |
| 注入口径与判定规则 | `docs/w4b-design.md` |
| 注入器与守门测试 | `runtime/model.py`、`eval/w4b_probe.py`、`tests/test_w4b_injection.py` |
| W4 首轮（对照阅读） | `experiments/w4_report.md`、`docs/w4-design.md` |
