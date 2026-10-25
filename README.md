# 跨境女装询盘 / 售后 Agent · Eval 基础设施

虚构品牌 **Northwren**（深圳 / 东莞仓发货，卖往 US / UK / EU / CA / AU）的询盘与售后处理 Agent。V0 / V1 / V2 三种编排架构共用这一套"尺子"：沙箱、知识库、输出契约、评分脚本、标注工具。本仓库**只有尺子，没有 agent**。

| 目录 | 内容 |
|---|---|
| `eval/kb/` | 12 份知识库文档，每行一个事实，`doc_id` = 文件名 |
| `eval/env/` | 30 个 SKU、60 个订单、物流事件、`meta.json`（`as_of = 2026-10-20`） |
| `eval/policy/` | 风险策略表、意图表 |
| `eval/messages.jsonl` | 180 条候选客户消息（不含任何标签） |
| `eval/labels/` | 金标（由人标注，`tools/label.py` 写入） |
| `eval/_sealed/` | 盲标 ≥50 条之前不要打开 |
| `sandbox/` | `kb.py` 行号加载器，`tools.py` 4 个模拟接口 + 日志 + 故障注入，`tool_schemas.json` |
| `contract/` | 输出契约与金标 schema，`validate.py` |
| `scoring/` | `route.py` 路由推导，`score.py` 确定性指标 |
| `tools/` | `check_data.py`、`label.py`、`agreement.py`、`split.py` |
| `runners/README.md` | 写 runner 前先读：输出契约与沙箱用法 |

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python pyyaml jsonschema pytest
.venv/bin/python -m pytest -q
.venv/bin/python tools/check_data.py --stats
.venv/bin/python tools/label.py
```

只依赖标准库、pyyaml、jsonschema、pytest；不调用任何外部 LLM API。所有人名、邮箱、地址均为虚构，邮箱只用 example.com / example.net / example.org 和 `.example` 保留域名。
