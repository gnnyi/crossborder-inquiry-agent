# 迭代日志

> **状态（2026-10-27）：还没有任何一轮真实迭代。** 本轮无人值守运行时环境里没有 `ANTHROPIC_API_KEY`，所有 runner 只用脚本模拟模型（`--fake`）跑通了管线。模拟模型的输出不反映任何架构的能力，所以下面没有"第 1 轮"。
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
