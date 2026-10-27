**未检测到 API key，以下指标都没有真实运行。**

# 迭代任务报告 · 2026-10-27

**结论**：阶段 0–6 的代码、测试和文档都完成了，每个阶段一个 commit，在分支 `iterate/agents` 上，没有 push。137 个测试通过（另有 5 个是留给你的练习，默认跳过），`check_data.py` 0 错误。**但任务的核心（真实运行、V0 三轮迭代、V1 / V2 各一轮迭代、三种架构的对比结论）一项都没做**：环境里没有 `ANTHROPIC_API_KEY`。所以这份报告里没有任何真实指标，也没有"三个初步结论"。

`~/.zshrc` 里有几组 `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` 配置。我没有使用：它们指向第三方转发地址，任务书只允许调用 Anthropic 自己的 API，而且这些不是 `ANTHROPIC_API_KEY`。

## key 到位后要执行的命令

```bash
cd /Users/zhang/Documents/Agents/crossborder-inquiry-agent && git checkout iterate/agents
cp .env.example .env        # 填 ANTHROPIC_API_KEY；按下面的预算估算决定 ITER_BUDGET_USD
scripts/run_matrix.sh calibrate      # 10 条，先看真实的 $/条、缓存命中率、平均步数
scripts/run_matrix.sh baseline       # V0-b 全量 180 条 + 无标签报告 + 失败清单
# 然后按 docs/iteration_log.md 的流程迭代（至少 3 轮），再：
scripts/run_matrix.sh final v1       # 对照矩阵，按预算优先级跑，到 85% 自动停
```

**预算估算（估算，不是实测，calibrate 之后以实测为准）**：V0 每条约 3 次模型调用，按 Sonnet 5.5 的价格和缓存命中估算，约 $0.03–0.06 / 条，全量一次约 $5–11。$30 上限（85% 熔断即 $25.5）大约只够 **3–4 次全量运行**。任务书要求的完整矩阵（基线 + 3 轮迭代重跑 + V0-b / V1 各 3 次 + V0-a + V2 + 3 次故障注入 + Haiku）约 15 次全量，按估算需要 $80–150。两种处理方式，由你决定：

- 把 `ITER_BUDGET_USD` 提到 100–150；
- 或者迭代时只跑固定子集：`python run.py --arch V0 --run-id v0b-r2 --limit 60`（消息编号是按种子打乱分配的，前 60 条大致有代表性），只在最终对照时跑全量。

## 1. 做完了什么

| 阶段 | commit | 产物 |
|---|---|---|
| 0 公共底座 | `fc4f2ef` | `agents/llm.py`（四类 token 分别计价、按消息计量、持久化预算账本 + 85% 熔断、429 / 5xx 有上限退避、KB 放在可缓存前缀、`FakeLLM`）；`agents/common.py`（线程渲染、由契约自动生成的 `submit_response`、契约组装与校验、永不覆盖 + 可续跑的 run 目录）；`run.py`；`config/models.yaml`、`config/pricing.yaml` |
| 1 V0 | `c2bd4f7` | `agents/v0.py`（146 行，无框架，变体 a / b）；`docs/V0_walkthrough.md`（逐段导读 + 5 个练习）；`tests/test_v0_exercises.py`（练习的验收测试，默认跳过） |
| 2 无标签指标 | `6050447` | `scoring/label_free.py`；`docs/iteration_log.md`（流程、13 类错误表、模板；**0 轮真实迭代**） |
| 3 V1 | `d35f18f` | `agents/v1.py`（LangGraph，classify → fetch → draft → risk_gate，边固定；fetch 里由代码清洗订单号） |
| 4 V2 | `d2db767` | `agents/v2.py`（主 Agent + 订单 / 政策 / 审核子 Agent，复用 V0 的循环；brief / findings / review 格式写在代码里；"上下文割裂"指标） |
| 5 健壮性 | `617c5a4` | `scripts/run_matrix.sh`（按预算优先级的运行矩阵）、`demo.py`；**真实故障注入和模型消融未跑** |
| 6 收尾 | 最后一个 commit | `README.md` 重写、`docs/design_decisions.md`（10 条 ADR + V1 适用性假设表）、`docs/interview_prep.md`（30 + 5 题）、本报告 |

### 测试

```
.venv/bin/python -m pytest -q     → 137 passed, 5 skipped
.venv/bin/python tools/check_data.py → 0 error(s), 0 warning(s)
```

新增 45 个测试，覆盖任务书点名的四项（FakeLLM 回放、预算熔断、重试、契约校验失败写 `errors/` 而不是残缺输出），以及：V0 的每种停止条件（超步数、超调用数、两次交卷不合格、不行动、未知工具）；timeout 重试有上限；`invalid_argument` 不经修改回传给模型；变体 a / b 的路由来源；续跑时半截日志被挪开；同名 run 配置不同会被拒绝；V1 的订单号清洗（7 种写法）和"邮箱名下有唯一订单才继续查"；V2 子 Agent 看不到对话、审核拿到由代码解析的证据、来源不合法的 finding 会被打回；无标签指标在完全没有金标时能算。

### 管线冒烟（脚本模型，`--fake`）

4 种配置 × 180 条，故障注入开 / 关各一次，**8 次全量运行全部 180/180 通过契约校验**，`label_free.py` 能出完整报告。模拟模型只会"看到订单号就查一次，然后交固定格式的卷"，所以它的指标**没有任何比较意义**，这里不列。冒烟的 run 放在 scratchpad 里，没有写进仓库的 `runs/`。

### 总花费

**$0**。没有发出过任何 API 请求，预算账本 `runs/_budget_ledger.jsonl` 不存在。

## 2. 我替你做的决定（每条附一句理由，方便推翻）

### 环境与依赖
1. **没有 key 时不去找别的凭据**：`.zshrc` 里的第三方转发配置一律不用。理由：任务书铁律 6。
2. **新增 `.env.example`，`run.py` / `demo.py` 用 python-dotenv 读 `.env`**，`.env` 加进 gitignore。理由：任务书允许了 dotenv，这是它唯一合理的用途。
3. **价格来自 claude-api 工具内置的价格表（缓存日期 2026-10-06），没有联网核实**，`pricing.yaml` 标了 `verified: false`。缓存写入价按输入价的 1.25 倍推算；Haiku 5.5 的缓存读写价是我按比例推算的。
4. **思考强度用 `medium`**（`config/models.yaml`）。理由：Sonnet 5.5 默认 `high`，多步工具调用从 `medium` 起步更省钱；校准时值得扫一遍。
5. **不发 `temperature`**。理由：新模型已经不接受采样参数，发了会 400。

### V0
6. **默认变体是 b（路由由代码推导）**，`--variant a` 才让模型给路由。理由：b 是项目要论证的做法；a 是对照组。
7. **上限**：10 步、12 次工具调用、timeout 重试 2 次、契约修正 1 次、不行动提醒 1 次。理由见导读第 1 节；都是先验，等实测步数分布再调。
8. **timeout 由代码重试，`invalid_argument` / `not_found` 交给模型**。理由：前者是基础设施问题，后者需要理解客户写了什么。
9. **`msg_id`、`tool_calls`、`meta`（以及变体 b 的 `route`）由代码填**，`submit_response` 的 schema 里没有这些字段。理由：能从事实得出的，就不让模型抄。
10. **agent 没做完时输出合法的 ESCALATE（`meta.fallback=true` + `handoff_reason`），只有崩溃或最终输出仍不合契约才写 `errors/`**。占位意图 `["OTHER"]` 会拉低意图准确率，这是有意的。
11. **契约校验不检查"claim 引用了非 ok 调用"**，交给评分时的幽灵引用。理由：校验函数和评分脚本保持一致；要不要提前拦，我写成了导读里的思考题，留给你判断。
12. **工具结果给模型看的是 `{call_id, status, result, error}`**（去掉工具名）。理由：runners/README 的建议。

### V1 / V2
13. **V1 的 classify 节点也带完整 KB**。理由：要靠 catalog_index 把商品名映射成 SKU。
14. **V1 只有在按邮箱查到唯一订单时才继续查订单**，多个订单时让草稿去追问。理由：对应 `cs_style_guide` 第 20 行"不清楚是哪个订单就问"。
15. **V1 遇到 ESCALATE 也保留草稿**（契约允许），给人工作参考。
16. **V2 的审核是一个可派单的子 Agent，而不是代码在交卷时强制调用**。理由：主 Agent 跳过审核本身就是要测的"割裂"现象之一；交卷前没审核的比例会被统计。
17. **V2 审核拿到的证据由代码解析**（`evidence_for`），而不是主 Agent 转述。理由：否则审核检查的只是复述。审核提示里的三条"不承诺"规则直接读 `cs_style_guide` 第 11–13 行，没有重新打一遍字。
18. **V2 各角色的上限**：主 Agent 8 步 / 6 次派单，子 Agent 6 步 / 6 次调用，审核 2 步。

### 指标与工具
19. **"无来源事实"的覆盖规则**：回复里的日期只要和某条 claim 里的日期有一种读法相同（缺年份的可以匹配任何年份）就算覆盖；金额精确到分；订单号字面相同。理由：确定性检查宁可漏报。
20. **新增"路由与自报风险标签不一致率"**：没有金标也能衡量 V0-a 的路由可靠性。
21. **`demo.py` 在 `eval/labels` 少于 100 条时拒绝用真实模型或回放展示**，除非加 `--allow-before-labels`。理由：防止你在盲标前被锚定。
22. **`run_matrix.sh` 的优先级**：V0-b 和 V1 各 3 次 → V0-a 和 V2 → 三次故障注入 → Haiku → 其余重复。理由：任务书说预算不够时优先保证 V0-b 和 V1。
23. **5 个练习的测试默认跳过**，这样整个测试套件保持全绿，你删掉一个跳过标记就开始一个练习。

## 3. 无标签指标对比和三个初步结论

**没有。** 只有模拟模型的冒烟数字，它们不能用来比较架构。我不从代码结构推出"结论"写在这里；推理出来的预期写在 `docs/design_decisions.md`（V1 适用性表、V2 成本预期），全部标了 **[待实测]**。

## 4. 你接下来必须亲手做的事（按顺序）

先指出一个盲点：这个项目到现在已经有沙箱、契约、评分、标注工具、三种架构、无标签指标、运行矩阵、演示脚本，**但还没有一个真实数字**。按你的档案【高】"先研究、建模、搭工具，关键行动被推后"，现在最该防的就是继续加工具。下面每一步都是只有你能做的行动，不是新系统。

1. **审定 `OVERNIGHT_REPORT.md` 3.1、3.2 里的政策和策略疑点**（30 分钟，先看标 ★ 的）。改了 KB 就跑 `tools/check_data.py`。
2. **拿到 key，跑 `scripts/run_matrix.sh calibrate`**（估计不到 $1）。看真实的 $/条，决定预算（见开头的估算）。这一步只看汇总数字，不打开 outputs。
3. **用 `tools/label.py` 盲标至少 100 条**，然后：
   ```bash
   .venv/bin/python tools/agreement.py eval/labels eval/_sealed/generation_notes.jsonl
   .venv/bin/python tools/split.py
   ```
4. **盲标完成之前，不要打开 `runs/*/outputs/`，也不要用 `demo.py --allow-before-labels`。** 迭代时读 trace 只看工具调用、错误和 claims（见 `docs/iteration_log.md` 的说明）。如果做不到只看这些，就先标完再迭代。
5. **跑基线和迭代**：`scripts/run_matrix.sh baseline`，然后按 `docs/iteration_log.md` 至少迭代 V0 三轮，V1、V2 各一轮，再 `scripts/run_matrix.sh final v1`。
6. **用已有 runs 算金标指标，把真实数字填进 README 第 3 节**：
   ```bash
   .venv/bin/python scoring/score.py runs/V0-b/v1-r1 runs/V0-b/v1-r2 runs/V0-b/v1-r3 --split dev --out reports/v0b-dev.md
   .venv/bin/python scoring/score.py runs/V1/v1-r1 runs/V1/v1-r2 runs/V1/v1-r3 --split dev --out reports/v1-dev.md
   .venv/bin/python scoring/score.py runs/V0-a/v1-r1 runs/V2/v1-r1 --split dev --out reports/v0a-v2-dev.md
   # test 集只在最后跑一次：把 --split dev 换成 --split test
   .venv/bin/python scoring/label_free.py runs/V0-a/v1-r1 runs/V0-b/v1-r1 runs/V1/v1-r1 runs/V2/v1-r1 --out reports/label_free.md
   ```
   同时更新 README 第 4 节（三个发现）和 `docs/interview_prep.md` 里所有【实测后填】。
7. **完成 `docs/V0_walkthrough.md` 第 9 节的 5 个小改动**：删掉 `tests/test_v0_exercises.py` 里对应测试的 `@TODO`，改到全绿。面试会追问 V0，这一步决定这段代码是不是"你的"。

## 5. 没做的事，以及原因

| 没做 | 原因 |
|---|---|
| 任何真实 API 调用：V0 三轮迭代、V1 / V2 各一轮迭代、故障注入对比、Haiku 消融、每个配置跑 3 次 | 没有 API key（任务书规定此时不调用真实 API） |
| prompt caching 的实测节省 | 同上；设计文档里给了估算（每次全量运行约省 $6），标了待实测 |
| V1 "在哪类消息上稳、在哪类上卡住"的数据举证 | 同上；现在是从代码结构推出的假设表，每条写了验证方法 |
| README 结果表的数字、三个发现 | 无标签指标待实测，金标指标待金标；不填估计值 |
| 价格核实 | 没有联网查 docs.claude.com；`pricing.yaml` 标了未核实 |
| 可选项（LLM 判官 + 20 条校准样本、审核队列页面） | 前置条件"前面全部完成且预算剩 30% 以上"不满足：真实迭代都没做 |
| 读 `eval/_sealed/`、写 `eval/labels/`、改 KB / 策略表 / 意图表 / 契约 / 评分规则 | 铁律。`git diff afb1830 -- eval/ contract/ scoring/score.py scoring/route.py` 为空 |
| push | 任务书要求不 push |

## 最终检查清单

- [x] 全部测试通过（137 passed, 5 skipped），`check_data.py` 仍然 0 错误
- [x] 全程没有读取 `eval/_sealed/`（代码里没有任何读取它的路径；本次会话也没有打开过）
- [x] `eval/labels/` 没有被写入（只有 `.gitkeep`）
- [x] README 和报告中没有任何依赖金标的数字，也没有逐条预测结果
- [x] 每个阶段都有 commit，没有 push


---

## 更新 · 2026-10-27：改用 DeepSeek，完成校准

- **改动**：你指定用 DeepSeek V4.1 Flash。走 DeepSeek 的 Anthropic 兼容接口（`https://api.deepseek.com/anthropic`），同一个 anthropic SDK，只换 `base_url` 和 key，没有新增依赖。`config/models.yaml` 新增 `provider`（默认 `deepseek`，模型 `deepseek-flash`；`--provider anthropic` 可切回 Claude）。key 在 `.env`（gitignore），变量名 `DEEPSEEK_API_KEY`。
- **和任务书的冲突**：铁律 6 原本只允许 Anthropic API，这次按你当下的指令放开为 DeepSeek。
- **兼容接口的差异**：`cache_control` 被忽略（DeepSeek 对相同前缀自动缓存），`tool_result.is_error` 被忽略（错误信息仍在结果正文里，模型能看到），不发顶层 `cache_control` 和 `effort`。
- **价格**：输入 $0.14、缓存命中 $0.0028、输出 $0.28 / 百万 tokens，来自第三方汇总，官方定价页未取到 → **未核实**。
- **校准实测（V0-b，10 条，汇总）**：契约合规 10/10，$0.0007 / 条，缓存命中率 95.1%，平均 2.2 步，延迟 p50 约 12 秒、p95 约 24 秒；无来源事实回复占比 22.2%（2 条），是第一个可迭代的失败类别。另有 1 条单消息探测 `probe-1`。
- **预算推算**：一次 180 条 V0 全量约 $0.13；V2 调用次数约 2 倍。任务书的完整矩阵（约 15 次全量 + 迭代重跑）估计 $3–5。`.env` 里 `ITER_BUDGET_USD=5`（85% 熔断在 $4.25）。样本只有 10 条，消息长度差异大，按 ±50% 看。
- Haiku 那一档已从 `run_matrix.sh` 去掉；"便宜模型消融"改为以后需要时用 `--provider anthropic --model claude-haiku-5-5`。
