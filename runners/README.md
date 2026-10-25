# runners/ —— 输出契约与沙箱用法

这里只放说明，**不放任何 runner**。V0 由你手写 agent loop；V1 / V2 以后再加。runner 的唯一职责：读消息、调用沙箱、产出符合契约的输出。评分只看输出、工具日志和金标，不关心架构内部。

## 1. 输入

- 消息：`eval/messages.jsonl`，一行一条：`msg_id, channel, lang, from, subject（仅邮件）, received_at, thread[{role, text, ts}]`。`thread` 按时间排序，最后一条是客户，`role=agent` 的是之前的人工 / AI 回复。
- 当前日期：`eval/env/meta.json` 的 `as_of`（2026-10-20），也可以用 `Sandbox().as_of`。所有"第 N 天"的计算都以它为准，不要用系统时间。
- 知识库：`sandbox.kb`。`render_all()` 一次给出所有文档（带行号），适合 V0 单次长 prompt。

```python
from sandbox import kb
kb.load_kb()                                   # {doc_id: [line1, line2, ...]}
kb.get_lines("policy_returns", 5, 6)           # 第 5–6 行（从 1 开始，含两端）
print(kb.render_with_line_numbers("faq"))      # " 1 | # Frequently Asked Questions" ...
print(kb.render_all())                         # 全部文档，带 === doc_id: xxx === 分隔
```

## 2. 沙箱（每条消息一个实例）

```python
from sandbox.tools import Sandbox, FaultConfig, load_tool_schemas

tools = load_tool_schemas()                    # Anthropic 格式：name / description / input_schema
sb = Sandbox(log_path=f"runs/V0/{run_id}/tool_logs/{msg_id}.jsonl")   # 第 1 周故障注入保持关闭
# sb = Sandbox(log_path=..., fault=FaultConfig(seed=1, timeout_rate=0.1, not_found_rate=0.05))   # 第 2 周起测健壮性

# 模型发来 tool_use(name, input) 时：
r = sb.call(name, input)
# r = {"call_id": "c1", "tool": "get_order", "status": "ok" | "not_found" | "invalid_argument" | "timeout",
#      "result": {...} | None, "error": None | "..."}
# 把 r 原样（或去掉 tool 字段）作为 tool_result 回给模型，call_id 要让模型看到，claims 里要引用它。
```

要点：

- 每条消息新建一个 `Sandbox`，`call_id` 从 `c1` 开始编号。日志文件按追加写，**同一个 run_id 不要重跑同一条消息**（换一个 run_id，`runs/` 永不覆盖）。
- 工具不替 agent 规范化参数：`"#10231"`、`"NW-10231"`、`"1O231"`、整数 `10231` 都会返回 `invalid_argument`。订单号只能是纯数字字符串，SKU 只能是 `AAA-000`。
- 结果是深拷贝，同一输入两次调用结果完全一致（故障注入关闭时）。
- 故障注入由 `sha256(seed, tool, 规范化 args, 第几次相同调用)` 决定：同一配置下整次运行可复现，同一调用重试可能成功。
- 开启故障注入时，把 `FaultConfig.to_dict()` 写进 `run_meta.json` 的 `fault` 字段，报告会单独标出。

## 3. 输出契约

每条消息写一个 `runs/<arch>/<run_id>/outputs/<msg_id>.json`，必须通过 `contract/output.schema.json`：

```json
{
  "msg_id": "m017",
  "intents": ["WISMO", "RETURN_REFUND"],
  "priority": "P1",
  "risk_flags": ["REFUND_REQUEST"],
  "route": "DRAFT_FOR_REVIEW",
  "tool_calls": [
    {"call_id": "c1", "name": "get_order", "args": {"order_id": "10231"}, "status": "ok"}
  ],
  "reply": {
    "text": "Hi Jane, ...",
    "claims": [
      {"text": "Your order shipped on 26 September.", "source": {"type": "tool", "call_id": "c1", "field": "shipped_at"}},
      {"text": "Returns can be started within 30 days of delivery.", "source": {"type": "kb", "doc_id": "policy_returns", "lines": [5, 6]}}
    ]
  },
  "handoff_note": "Customer asks for a refund; needs human approval.",
  "meta": {"arch": "V0", "model": "claude-...", "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "latency_ms": 0, "run_id": "2026-10-21-a"}
}
```

| 字段 | 规则 |
|---|---|
| `intents` | 1–2 个，取自 `eval/policy/intents.yaml` |
| `priority` | P0 / P1 / P2 |
| `risk_flags` | 0 个或多个，取自 `eval/policy/risk_policy.yaml` |
| `route` | AUTO_REPLY / DRAFT_FOR_REVIEW / ESCALATE。AUTO 和 DRAFT 必须有 `reply`；ESCALATE 的 `reply` 可以为 `null`，但 `handoff_note` 必须非空 |
| `tool_calls` | 和工具日志一一对应：同样的 `call_id`、`name`、`args`、`status`（`contract/validate.py --log` 会核对） |
| `reply.claims` | 回复里每条事实拆成一个 claim。`source` 只有两种：`{"type":"kb","doc_id","lines":[起,止]}` 或 `{"type":"tool","call_id","field"}` |
| `claims[].source.field` | 指向那次调用 `result` 里的路径：点号取键，`[i]` 取下标，支持负数。例：`shipped_at`、`items[0].sku`、`shipments[0].events[-1].description`。不要加 `result.` 前缀 |
| `meta` | 必填 `arch, model, tokens_in, tokens_out, cost_usd, latency_ms, run_id`，可以加别的字段 |

评分怎么用这些字段（`scoring/score.py`）：

- 引用不存在的 doc_id、越界行号、日志里没有的 call_id、返回非 ok 的调用、不存在的字段 → **幽灵引用**。
- claim 引用工具字段时，claim 文本里出现的日期 / 金额必须和该字段的值一致（时间戳字段允许相邻一天，照顾时区换算）→ 否则记为**工具数据矛盾**。
- 工具调用准确率看**日志**，不看输出里的 `tool_calls`。参数不规范的调用（如 `"#10231"`）算多余调用。

## 4. 运行目录

```
runs/<arch>/<run_id>/
  run_meta.json            # {"arch", "run_id", "model", "fault": {...} 或省略, 其他随意}
  outputs/<msg_id>.json    # 契约输出
  tool_logs/<msg_id>.jsonl # Sandbox 自动写
  errors/<msg_id>.json     # runner 自己出错时写：{"msg_id", "error_type": "exception" | "timeout", "message", "latency_ms"?, "cost_usd"?}
  traces/                  # 可选：原始请求/响应，评分不读
```

- 某条消息 runner 崩了或整体超时：写 `errors/<msg_id>.json`，不要写残缺的 output。
- 输出不合 schema、缺失、出错都算**失败**。默认 `--failure-policy human`：失败视为转人工（计入捕获率和误拦率，并在失败率里单独报告）；`exclude` 则把失败消息排除在路由指标外。

## 5. 评分

```bash
.venv/bin/python contract/validate.py runs/V0/r1/outputs
.venv/bin/python scoring/score.py runs/V0/r1 runs/V0/r2 runs/V0/r3 --split dev --out reports/v0-dev.md --details reports/v0-dev.jsonl
```

每个配置跑 3 次，报告会给出均值和最小 / 最大值。LLM 判官部分（编造第 3、4 项、must_address 覆盖）还没实现，接口在 `scoring/score.py` 的 `LLMJudge`。
