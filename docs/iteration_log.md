# 迭代日志

> **状态（2026-10-27 更新）：基线已跑完，第 1 轮因 DeepSeek 余额不足中断（180 条完成 55 条），待续跑。** 下面开头那段是改用 DeepSeek 之前写的，保留作记录。
>
> 原状态：还没有任何一轮真实迭代。 本轮无人值守运行时环境里没有 `ANTHROPIC_API_KEY`，所有 runner 只用脚本模拟模型（`--fake`）跑通了管线。模拟模型的输出不反映任何架构的能力，所以下面没有"第 1 轮"。
> 这份文件现在提供三样东西：迭代规则、错误类别表（读 trace 时直接用）、每轮的记录模板。key 到位后从「第 1 轮」开始填。

## 迭代规则（任务书阶段 2，原样执行）

1. 全量跑 180 条：`python run.py --arch V0 --variant b --run-id v0b-r1`
2. 出无标签报告和失败清单：`python scoring/label_free.py runs/V0-b/v0b-r1 --failures --out reports/v0b-r1.md`
3. 打开 `runs/V0-b/v0b-r1/label_free_failures.json`，**逐条读 trace**（`runs/V0-b/v0b-r1/traces/<msg_id>.json`，或者 `python demo.py <msg_id> --arch V0 --replay runs/V0-b/v0b-r1`），每条给一个下表里的类别。
4. 选占比最大的一类，按成本从低到高修：**工具描述 / 错误信息 → prompt → 代码约束 → 架构**。
5. 换一个 run_id 全量重跑，确认目标类别下降、其他指标没变差。
6. 修复只对少数几条有效时，写明"可能过拟合"。

两条红线：不在 prompt 或代码里写只为某一条消息存在的规则；修复前后都**不打开** `runs/*/outputs/` 去看具体判了什么意图、风险、路由（盲标完成之前）。读 trace 只为了归类失败机制。

> 第 2 条红线和"逐条读 trace"有张力：trace 里有模型交的卷。我的做法是读 trace 时只看工具调用、错误信息、claims 和停止原因，跳过 `intents` / `risk_flags` / `route`。如果你觉得这仍然有锚定风险，就先盲标、再迭代——见 `ITERATION_REPORT.md` 第 4 节的顺序建议。

## 错误类别表

前 8 类可以由 `label_free.py --failures` 自动归入；后 5 类要读 trace 判断。

| 代号 | 类别 | 自动识别 | 优先尝试的修复层 |
|---|---|---|---|
| E1 | 订单号未清洗就调用（`#`、`NW-`、字母 O） | `invalid_argument_call` | 工具描述 / 错误信息 |
| E2 | claim 字段路径写错（多了 `result.`、下标越界） | `ghost_citation` | prompt（字段路径示例） |
| E3 | claim 引用了失败的调用 | `ghost_citation` | 代码约束（交卷时检查） |
| E4 | 回复里有日期 / 金额 / 订单号却没有 claim | `unsourced_fact` | prompt |
| E5 | claim 里的日期 / 金额和字段值不一致 | `tool_contradiction` | prompt（时区、日期格式） |
| E6 | 撞到步数 / 调用数上限 | `handoff:max_steps` / `max_tool_calls` | prompt → 代码约束 |
| E7 | 两次交卷都不合契约 | `handoff:contract_invalid` | prompt / schema 描述 |
| E8 | 变体 a：路由和自报风险标签不一致 | `route_self_contradiction` | 架构（这就是变体 b） |
| E9 | 该查没查（问物流却没调 `get_tracking`） | 读 trace | prompt |
| E10 | 查了不该查的（别人的订单、无关 SKU） | 读 trace | prompt / 代码约束 |
| E11 | 回复超出知识库授权（承诺日期、承诺退款） | 读 trace（判官做好后可自动） | prompt |
| E12 | 线程早期轮次的信息被忽略 | 读 trace | prompt / 上下文渲染 |
| E13 | 被消息里的指令带偏 | 读 trace | prompt / 架构 |

## 记录模板

```
## 第 N 轮 · YYYY-MM-DD · run_id
- 错误分布（类别：条数）
- 本轮针对的类别与假设
- 改了什么（文件、diff 摘要），修复属于哪一层（工具/prompt/代码约束/架构）
- 指标变化（前 → 后），有没有副作用
- 没解决的和下一步
```

## 第 0 轮（管线冒烟，不是迭代）· 2026-10-27 · `--fake`

- 目的：确认 run.py → agent → 沙箱 → 契约校验 → 评分 这条链路在三种架构上都能跑通。
- 结果：见 `ITERATION_REPORT.md` 第 1 节。模拟模型只会"有订单号就查一次订单，然后交一份固定格式的卷"，所以它的指标没有任何比较意义。
- V0 至少 3 轮、V1 和 V2 各至少 1 轮的真实迭代：**未做**，等 key。


## 基线 · 2026-10-27 · `v0b-r1`（deepseek-flash）

- 规模：180 条，$0.14，契约合规 100%，agent 自己完成 179 条，被迫转人工 1 条（`contract_invalid`）；幽灵引用 3 处（2 条回复）；非法参数调用 0（模型自己就把 `#`、`NW` 这类写法清洗掉了）。
- **读 trace 后发现指标本身有问题**："无来源事实"的 17 条回复里，9 条是复述客户自己说过的日期或订单号（契约没有"来自对话"这种来源类型）。为此在 `label_free.py` 新增一列"不算对话里已出现的"（对话文本 + 每轮时间戳 + 今天的日期都算来源），**原列保留、口径不变**。新列：基线 4.6%（9 处，8 条回复）。这是修尺子，不是修模型，不算一轮迭代。
- 错误分布（按条，只记机制）：

| 类别 | 条数 | 层 |
|---|---|---|
| E4-B 自己算出来的值（截止日、合计、差价）没有 claim | 3 | prompt |
| E4-C 举例用的订单号（"例如 10231"，其实是另一位客户的真实订单号） | 3 | prompt |
| 指标误报：日期解析器读不出 "the 30th" / "the 1st of November" | 2 | 不修，已知局限 |
| E2 字段路径写错 | 1 | prompt |
| E3 引用了返回 not_found 的调用（想证明"查不到"） | 1 | 代码约束 |
| E7 连续两次提交空的 `submit_response` | 1 | 工具错误信息 |

- **过拟合风险提示**：每一类只有 1–3 条。任何一轮的改善都必须和"同配置重跑"的波动比较，才算数。

## 第 1 轮 · 2026-10-27 · `v0b-it1`（进行中）

- 针对的类别：E4-B（自己算的值没有 claim）。
- 假设：`CLAIMS` 只说"每个事实要有 claim"，模型把推算出来的值当成推理过程，而不是事实。
- 改了什么：`agents/prompts.py` 的 `CLAIMS` 加了三行：截止日、天数、差价、合计也是事实，各自单独一条 claim，引用所用规则的 KB 行；算它用到的输入也要有 claim。属于 **prompt 层**。`CLAIMS` 由 V0 / V1 / V2 共用。
- 结果：**待续跑**。DeepSeek 返回 402（余额不足），180 条只完成 55 条。
- 同时发生的基础设施修正（不属于迭代）：
  - 401 / 402 / 403 现在会让整批运行停下，不再被写成单条消息的失败（`ProviderBlocked`）；被 402 写坏的错误文件已挪到 `traces/aborted_402/`，续跑时这些消息会重跑。
  - `run_meta` 记录 prompt 文件的哈希（`prompt_sha`），prompt 改了就拒绝续跑，防止一次运行里混用两版 prompt。用旧 prompt 做的噪声重跑（已完成 70 条）因此作废，改为续跑后再跑一次 `v0b-it1-repeat` 来测波动。
