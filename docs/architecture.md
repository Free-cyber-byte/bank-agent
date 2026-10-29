# ZeroStep 架构说明

本文描述 ZeroStep（零步银行）的内部分层、一次对话的完整流水线、跨场景计划的 DAG 组织方式、审批中断与恢复机制、幂等键设计，以及事件与审计的持久化方式。所有函数名与常量都对应 backend/app 下的真实实现（行号以当前仓库为准）。

---

## 1. 设计原则

1. **理解与执行分离**：自然语言只被解析为「意图 + 槽位」，资金动作由后端确定性工具执行。模型/规则输出错一个槽位，最坏结果是用户看到一条追问，而不是一笔错误转账。
2. **先校验、后执行**：policy.evaluate 在真正调用任何资金工具之前完成金额、收款人、余额、产品、累计额度等参数级校验，不通过就直接拒绝，连计划都不执行。
3. **模型不可信、只做翻译**：大模型（llm.py）只被允许输出意图与槽位，且输出必须通过 schema、金额原文出处、通讯录匹配等校验；风险分级、审批、执行、回读与审计永远在代码层，模型无法放行、降级或选择工具。
4. **全过程可回放**：每次意图、计划、工具调用、审批、撤销都同时落 events（面向 UI 的实时事件）与 audit（面向取证的哈希链），任何一次资金变动都能沿审计链回溯到当时的参数与结果。

---

## 2. 分层架构

| 层 | 模块 | 职责 |
| --- | --- | --- |
| 表现层 | frontend/index.html、app.js、styles.css | 原生单页应用，消费 SSE 事件流，渲染对话、审批弹层、TOTP 演示与审计面板 |
| 接口层 | backend/server.py | 零依赖 http.server；REST + SSE；静态资源与路径穿越防护；统一错误体 {error:{code,message}}；v0.6.1 起加 CORS Origin 白名单、Host 校验、全局 body 上限（413/411）、limit clamp、可选 API_TOKEN 与演示接口本机限制 |
| 编排层 | backend/app/engine.py | run_turn 六阶段编排、build_plan 计划生成、execute_steps 执行、_finalize 回复、resume 审批恢复、undo_action 撤销 |
| 解析与安全层 | llm.py、nlu.py、policy.py、approvals.py、totp.py、second_factor.py、locks.py、audit.py | 解析（规则 / 模型 / 自动）、模型输出的严格校验与失败回退、意图与攻击识别、三级权限判定、审批单与 TTL、RFC 6238 动态口令、短信 / 人脸因子、会话异常锁定、哈希链审计 |
| 领域层 | backend/app/mock_core.py | 转账、理财申赎、卡限额与刷卡、预约、订阅、还款、提醒的模拟实现；金额一律整数分；DomainError 统一错误码 |
| 调度层 | backend/app/scheduler.py | 后台线程每 5 秒检查到期预约，调用领域层执行并写审计；只决定「何时执行」，不判定能不能执行 |
| 存储层 | backend/app/db.py + SQLite | 建表、索引、WAL、确定性种子数据；20 张表覆盖账户、流水、卡交易、预约、审批、checkpoint、事件、审计、会话锁 |
| 能力注册层 | backend/app/plugins.py | 七个内置插件的注册表（core / mcp / model / skill / channel）、状态探测、启停与持久化；只决定能力是否可用，不参与资金判定 |
| 渠道接入层 | backend/channels/server.py、wecom.py、wecom_crypto.py、backend/app/push.py | IM webhook 与企微回调的协议转换、验签解密、幂等与去重；主动触达的文案生成与投递。只搬运文本与确认状态，不解析意图、不判定风险 |
| MCP 接入层 | backend/app/mcp_registry.py、mcp_client.py、backend/external/bank_mcp_server.py | 通用 MCP 服务注册表（MCP_SERVERS）、Bearer / Headers / OAuth2 鉴权、streamable-http 会话与 SSE、只读四道闸与降级；永远只读，不参与资金判定 |

依赖方向单向向下：编排层可以调用安全层与领域层，领域层不感知 HTTP 与自然语言，安全层只依赖存储层。

---

## 3. 一次对话的六阶段流水线

入口是 engine.run_turn(conn, session_id, text, sink)（engine.py:1164）。它先取会话、创建 Emitter，然后严格按下列顺序推进。

数据流（模型只在中间一段出现）：

    用户输入
       │
       ▼
    攻击检测 nlu.detect_attack（代码规则，最先执行，模型不参与）
       │  命中 → 直接 blocked，不建计划、不调工具
       ▼
    解析 llm.parse（rule：nlu.parse ／ llm：大模型（默认 DeepSeek）／ auto：自动选）
       │  输出 意图 + 槽位 + source(rule|llm|fallback) + latency_ms
       ▼
    计划 build_plan（意图 → 步骤 + 边；缺参数/对象不存在在这里拒绝）
       │
       ▼
    风险分级 policy.evaluate（low / medium / high / blocked，唯一对钱判定的地方）
       │
       ▼
    审批（写审批单 + checkpoint，挂起等用户 confirm / TOTP）
       │
       ▼
    执行 execute_steps（幂等键 + mock_core；工具返回先消毒再展示）
       │
       ▼
    回读 mock_core.verify_balance（写后比对余额）
       │
       ▼
    回复 _finalize（组织文案、撤销信息、风险提示）→ 审计哈希链落盘

| 阶段 | 真实函数 | 输入 → 输出 | 失败/短路分支 | 关键事件与审计 |
| --- | --- | --- | --- | --- |
| 0 锁定检查 | locks.info(conn, session_id)（engine.py:1169） | 会话 → {locked, locked_until, remaining_s} | 已锁则 return status=locked，不进入后续阶段 | 事件 message + security_lock；审计 turn_locked（tool=security_lock，high） |
| 1 意图解析 | llm.parse(text, ctx_doc) → nlu.parse 或模型 | 文本 + 上下文 → intent{name, confidence, source, slots, model, latency_ms, reason} | intent.name 为 attack 时立即 return blocked，并记一次安全事件计数 | 事件 intent（含 source）；审计 attack_blocked |
| 2 计划生成 | engine.build_plan(conn, user_id, intent, text) | intent → {steps, edges, planner_block} | planner_block 非空时 return blocked | 事件 plan；审计 turn_blocked（tool=planner） |
| 3 策略门 | policy.evaluate(conn, user_id, steps, intent) | steps → {risk_level, requires_approval, challenge, blocked, reasons, summary} | blocked 非空时 return blocked | 事件 tool_end（step_id=gate）；审计 turn_blocked（tool=policy_gate） |
| 3.5 审批挂起 | approvals.create + _save_checkpoint | 高风险计划 → status=awaiting_approval；HIGH 的 factors 为 [totp,sms,face] | 不执行任何资金动作 | 事件 approval_required；审计 approval_required |
| 4 执行 | engine.execute_steps(conn, ctx, emit) → _exec_tool | ctx → {results, action_ids, failed} | DomainError 中断并 break | 事件 tool_start/tool_end；审计 tool_result、tool_error |
| 5 校验 | mock_core.verify_balance（在 execute_steps 内） | 转账结果 → {ok, expected, actual} | 不一致置 ctx.verify_issue | 审计 verify_balance |
| 6 回复 | engine.build_reply + engine._finalize | results → reply、undo、status | 执行失败或校验失败 → status=blocked | 事件 message；审计 response |

会话状态机有四态：ok（完成）、awaiting_approval（挂起等审批）、blocked（拒绝或执行失败）、locked（v0.4.1：会话因连续可疑操作被临时锁定）。resume 恢复成功后回到 ok/blocked。

v0.4.1 在阶段 1 之前多了一步「锁定检查」：run_turn 入口先读 session_locks（engine.py:1169），已锁则直接返回 status=locked、code=SESSION_LOCKED，不解析、不建计划、不执行任何工具。

### 阶段 1：意图解析（llm.parse，三模式分派）

run_turn 先组装上下文文档 ctx_doc：payees、products、subscriptions、cards、accounts，外加用户风险等级 fixed 为稳健型，然后调用 llm.parse(text, ctx_doc)（llm.py:909）。llm.py 是解析层的唯一入口，按当前模式分派：

| 模式 | 生效条件 | 实际解析 |
| --- | --- | --- |
| rule | 未配置 DEEPSEEK_API_KEY，或 mode=rule，或 ZEROSTEP_DISABLE_LLM=1 | nlu.parse（纯规则/正则） |
| llm | 已配置 Key 且 mode=llm 或 mode=auto | 代码规则守卫 → 大模型（默认 DeepSeek，可换任意 OpenAI 兼容服务）→ _normalize 严格校验 |
| auto（默认） | 启动默认值 | 有 Key 走模型，无 Key 等价于 rule |

模式可在运行时通过 POST /api/llm/mode 切换（只影响当前进程，重启回到 auto），前端顶栏有对应分段控件。

**共同前置：攻击检测在模型之前。** 无论哪种模式，非 rule 路径都先调用 nlu.detect_attack（nlu.py:93）：依次匹配越权访问、验证码钓鱼、提示注入、陌生收款人四类规则，命中即返回 name=attack、attack_type 与 source=rule，模型不会被调用。rule 路径则把 detect_attack 放在 nlu.parse 内部的第一步。

**规则路径**（nlu.parse，nlu.py:261）依次做：

1. _normalize：全角转半角、统一小写与空白，避免用全角字符绕过规则。
2. detect_attack：四类攻击规则，命中即返回 attack。
3. 关键词/正则意图分类：问候、余额、账单、流水、预算、转账、理财申购、持仓、卡片、订阅、跨场景分账等。
4. 槽位抽取：extract_amount（中文数字经 _cn_to_int 换算，金额统一转分）、extract_payee（在 payee 列表中做名/账号匹配，并判定 payee_is_new）、extract_months、extract_category。
5. parse_cross（nlu.py:467）：跨场景语句拆成 allocations 列表（savings/wealth/credit/transfer/生日/预算），供 build_plan 展开。

**模型路径**（llm.py，v0.3.1 加固后）：

1. call_model（llm.py:526）先按剩余预算计算单次超时，再调用 _build_payload（llm.py:458）组装请求：model、messages、temperature=0、max_tokens=800、response_format=json_object、stream=false；默认再带 thinking={type:disabled} 关闭思考模式。
2. messages[0].content 固定为 SYSTEM_PROMPT 原文（llm.py:115，2395 字符 / 4485 字节，进程内逐字节不变）；用户原话与命中的技能卡提示一起放进 messages[1] 的 user JSON：{utterance, skill_hint}。技能提示不拼进 system，保证 system 前缀稳定、可命中上下文缓存，降低输入费用。
3. 单次 HTTP 超时 8 秒（DEEPSEEK_TIMEOUT），一次解析总预算 10 秒（DEEPSEEK_BUDGET，含重试等待）；单次超时取 min(8s, 剩余预算)。
4. 只对可恢复错误重试 1 次（DEEPSEEK_RETRY）：429、5xx、网络超时、空 content、非 JSON；鉴权 401、参数 400/403/404/422、余额 402 立即失败，不重试。重试前等待 600ms（DEEPSEEK_RETRY_BACKOFF_MS），且剩余预算不足 2 秒时不再重试。
5. 连续失败 3 次（DEEPSEEK_BREAKER_THRESHOLD）后熔断 60 秒（DEEPSEEK_BREAKER_COOLDOWN）：冷却期内 llm.parse 不发任何模型请求，直接走规则解析，intent.source=rule 且带 degraded=true 与 fallback_reason；冷却结束自动恢复。
6. 错误统一映射为中文：ModelCallError 带 code、message、hint、retryable、kind；HTTP 401/402/404/429/5xx、网络超时、坏 JSON、空 content 都有对应中文说明与修复建议，写入 _last_error / _last_error_code / _last_error_hint，通过 GET /api/llm/status 暴露。
7. 每次成功调用记录用量：prompt_tokens、completion_tokens、total_tokens、prompt_cache_hit_tokens、prompt_cache_miss_tokens，并按空闲时段单价估算 last_cost_yuan 与 total_cost_yuan，汇总在 stats（calls / ok / fail / retries / timeouts / empty / circuit_skipped / last_latency_ms / last_usage / last_cost_yuan / total_cost_yuan / total_tokens）。
8. 系统提示只允许一份 JSON：intent（23 个预定义意图之一，含 v0.4.0 六类新意图）、confidence、amount_yuan、single_limit_yuan、year、merchant、schedule_hint、op、payee_name、payee_account、from_kind、months、category、product_hint、from_currency、to_currency、exchange_hint、sub_hint、card_hint、daily_limit_yuan、allocations、reminder、reason；未在原文出现的字段必须为 null，禁止猜测。
9. _normalize（llm.py:710）做严格校验后才产出 intent：意图必须在白名单；金额经 _normalize_amount（llm.py:688）校验（必须与原文数字等值，否则改用规则金额并标 amount_adjusted；超过 5 万元硬上限则截断并标 amount_clamped）；收款人经 _canon_payee（llm.py:626）/ _match_payee（llm.py:647）去代称并与通讯录匹配，账号必须 12–19 位且出现在原文；产品、订阅、卡必须命中真实种子数据（_match_product llm.py:656、_match_sub llm.py:667、_match_card llm.py:679）；v0.4.0 另加单笔限额、年度、商户与预约 op/run_at 的清洗，金额、年份、商户必须能在原文找到出处；月份、品类、来源账户、日限额等枚举与区间越界一律回退规则值。
10. 成功时 intent.source=llm，并带 model 与 latency_ms；调用失败（HTTP 错误、网络不可达、超时、空 content、非 JSON）时回退到规则解析结果，intent.source=fallback，附带 fallback_reason 与 latency_ms；熔断短路时 intent.source=rule 并带 degraded=true。三种来源都会写进事件与审计的 source 字段。

**模型接入加固（v0.3.1）一览**：

| 机制 | 默认值 | 作用 |
| --- | --- | --- |
| 关闭思考 | DEEPSEEK_THINKING=disabled | JSON 抽取不需要思维链；避免输出预算被推理占用、降低耗时与费用，并让 temperature=0 生效 |
| 单次超时 | DEEPSEEK_TIMEOUT=8 秒 | 一次 HTTP 请求最多等 8 秒 |
| 单轮预算 | DEEPSEEK_BUDGET=10 秒 | 含重试等待的总预算，超预算立即回退规则 |
| 输出上限 | DEEPSEEK_MAX_TOKENS=800 | 给 JSON 留足空间，避免截断 |
| 重试 | DEEPSEEK_RETRY=1，间隔 600ms | 只重试 429/5xx/网络/空 content/坏 JSON |
| 熔断 | 连续 3 次失败，冷却 60 秒 | 冷却期内直接规则解析，不再打模型 |
| 错误提示 | last_error_code / last_error_hint | 把 HTTP 码与修复建议翻译成中文给用户 |

**模型与代码的边界**：

| 环节 | 模型 | 代码层 |
| --- | --- | --- |
| 意图与槽位 | 输出候选值 | 白名单校验，非法值回退规则结果 |
| 金额 | 可给出 amount_yuan | 必须原文有出处，超限截断，参与后续全部金额校验 |
| 收款人 | 可给出姓名/账号 | 必须命中通讯录或原文，新收款人仍走 HIGH |
| 风险分级 | 无权限 | policy.evaluate 独立判定 |
| 审批 / TOTP | 无权限 | approvals + totp |
| 工具选择与执行 | 无权限 | build_plan + execute_steps + mock_core |
| 审计 | 无权限 | audit 哈希链 |

因此模型输出再离谱，最坏结果也只是「意图或槽位不对，被策略门拦下或回退到规则结果」，不会直接产生资金动作。

### 阶段 2：计划生成（build_plan）

build_plan（engine.py:163）把意图翻译成可执行的步骤列表：每步由 _step(step_id, title, tool, args, risk) 构造，含 step_id、title、tool、args、risk；edges 为线性依赖，steps[i] 指向 steps[i+1]。

计划阶段就做「不可能就拒绝」的判断：卡不存在（CARD_NOT_FOUND）、订阅不存在（SUBSCRIPTION_NOT_FOUND）、缺金额（AMOUNT_REQUIRED）、没有可用步骤（NO_STEP）都在这里返回 planner_block，避免把无效参数带进策略门。

greeting 与 unknown 意图直接生成回复早退，不进入策略门。

### 阶段 3：策略门（policy.evaluate）

policy.evaluate（policy.py:73）是唯一对「钱」做判定的地方，输出五件事：整体风险等级 risk_level（low/medium/high）、是否需要审批 requires_approval、挑战类型 challenge（confirm 或 totp）、拒绝原因 blocked、人类可读的 reasons 与 summary。

- 被拒绝：审计 turn_blocked、emit 一个 step_id=gate 的 tool_end、用 _blocked_reply 输出标准拦截文案，返回 blocked。
- 需要审批：进入阶段 3.5。
- 低风险：直接进入阶段 4。

策略门返回后，run_turn 会把每个步骤的 risk 抬到不低于整体 risk_level，便于前端按步展示风险标签与审计记录统一口径。

### 阶段 3.5：审批挂起

需要审批时依次执行：_approval_preview 生成人类可读的预览与金额、对手方 → approvals.create 写入审批单（approval_id 形如 ap_ 加 12 位十六进制，nonce 形如 n_ 加 16 位十六进制，expires_at = 创建时间 + 300 秒）→ ctx 记 approval_id 与 status=awaiting_approval → _save_checkpoint 把整份 ctx 以 thread_id 为键写入 checkpoints 表 → 审计 approval_required → emit approval_required → 返回 status=awaiting_approval。

v0.4.1 起，风险等级为 high 的审批单参数里多了 factors = ["totp","sms","face"]（engine.py:1222），序列化后随 approval 对象下发；前端据此渲染因子选择器。该字段本身不改变 challenge 仍是 totp，只是在确认时允许用 SMS 或人脸令牌替代动态码。

**此刻没有任何资金动作发生。** 用户拒绝、超时或直接关掉页面，账面都不会变化。

### 阶段 4-6：执行、校验、回复

execute_steps（engine.py:687）逐步执行计划：

1. 每步先 emit tool_start（含 args_preview 与 risk），再算幂等键，调用 _exec_tool 分发到 mock_core 对应函数。
2. 工具返回后先过 _scan_untrusted：命中 _SUSPECT_PATTERNS 就把可疑指令替换为「[已屏蔽的可疑指令]」，在 ctx.untrusted 记账，并审计 untrusted_content_blocked。
3. emit tool_end（含 ok、latency_ms、result），审计 tool_result。
4. 若该步是 create_transfer 且返回 balance_after_cents，立即调 mock_core.verify_balance 回读账户余额，审计 verify_balance；不一致就置 ctx.verify_issue，进入 _finalize 时把状态改为 blocked 并追加提示。
5. 任何一步抛 DomainError：emit 失败版 tool_end、审计 tool_error、记录 failed 并 break，后续步骤不再执行。

_finalize（engine.py:1065）负责收尾：执行失败时用 _blocked_reply（余额不足、超限）或「操作未完成：原因」组织回复并审计 turn_blocked；成功时 build_reply 生成自然语言回复与 undo 信息；若 ctx.untrusted 非空，在回复前置「⚠️ 安全提示：…」并审计 injection_ignored；最后 emit message、审计 response，返回包含 session_id、thread_id、status、reply、blocked、intent、plan（steps 与 edges）、executed、approval、undo、audit、events 的完整结果。SSE 场景下这份结果与事件流内容一致。

---

## 4. 跨场景计划的 DAG

计划是「步骤 + 有向边」的结构，当前实现里 edges 是由 build_plan 生成的**线性链**（steps[i] 依赖 steps[i+1] 之前的所有步骤），跨场景语句则被展开成一条串行链。选串行而非并行，是因为跨场景分账的每一步都要看前一步的资金结果（余额是否还够买理财），串行能保证失败即停、不留半成品。

转账（t06–t10）是两段式数据依赖：

    s1 get_payees（读收款人，判断是否新收款人、有无账号）
        │
        ▼
    s2 create_transfer（用 s1 的判定结果执行转账）

先读后写的意义在于：策略门需要的 payee_is_new、payee_account 都来自 s1，避免首笔向陌生账户直接放款。

跨场景分账（t23「发工资了，3000 存到储蓄卡，2000 买稳利宝，1000 还信用卡」）展开为：

    s1 create_transfer（留存储蓄卡，内部划转 internal=True）
        │
        ▼
    s2 purchase_wealth（申购稳利宝）
        │
        ▼
    s3 repay_credit（还信用卡）

三步串行，任一失败即中断，前面已完成的步骤保留（资金类动作本来就不该回滚成「假成功」）。

单笔还款（v0.6.2，t57/t58）不经过跨场景展开，`credit_repay` 直接生成一步：

    s1 repay_credit（from_account_id = 工资卡，或用户指定的储蓄卡）
       args: amount_cents / from_account_id / from_kind / credit_account_id / credit_last4

还款不是转账：信用卡是账户而不是收款人，所以既不需要 get_payees，也不走 create_transfer 的收款人校验。金额超过实际欠款时在计划阶段按欠款截断（审批卡金额必须等于实扣金额），无金额、已还清、出账账户余额不足、拿信用卡当出账账户分别在 planner_block / policy / mock_core 三处拦截（AMOUNT_REQUIRED / NO_OUTSTANDING_DEBT / INSUFFICIENT_FUNDS / INVALID_FROM_ACCOUNT）。

生日提醒（t24「下周三我妈生日，转 800 给她，再定个提醒」）展开为：

    s1 create_transfer（给妈妈 800 元，MEDIUM 确认）
        │
        ▼
    s2 create_reminder（提醒时间由 _next_wednesday 计算为下周三 09:00）

预算类语句（t04、t25）只生成 budget_analysis 单步，纯查询不受限。

---

## 5. 审批中断与恢复

**持久化位置**：checkpoints 表（thread_id 主键、state_json 存整份 ctx、updated_at）。ctx 里包含 intent、steps、edges、turn_key、approval_id 等全部恢复所需信息，所以恢复不依赖内存。

**恢复入口**：engine.resume(conn, session_id, approval_id, decision, code, sink)（engine.py:1239）：

1. approvals.check（approvals.py:60）依次校验：审批单存在、状态为 pending、未超过 300 秒 TTL（过期会顺手 mark expired）、decision 只能是 approve 或 reject、challenge 为 totp 时必须通过所选因子。v0.4.1 的签名是 check(conn, approval_id, decision, code, user_id, method="")：method 缺省为 totp（向后兼容），可选 sms / face；分别调 totp.verify_code（窗口 ±1）、second_factor.sms_verify 或 face_verify。失败码：INVALID_OTP / INVALID_SMS / INVALID_FACE / INVALID_METHOD，另有 APPROVAL_EXPIRED（410）、APPROVAL_ALREADY_DECIDED（409），均给中文提示。审批单在校验失败时保持 pending，可在 TTL 内重试或换因子。
2. _load_checkpoint(ap.thread_id) 取回 ctx；找不到则 410 CHECKPOINT_NOT_FOUND。
3. decision=reject：mark rejected、审计 approval_rejected、回复固定文案「好的，已取消这笔操作，没有发生任何资金变动。」并 emit done。
4. decision=approve：mark approved、审计 approval_granted，把 approval_id 写回 ctx，execute_steps 接着跑，最后 _finalize 收尾。

**恢复的幂等性**：审批单一旦 approved/rejected 就不可再次提交（APPROVAL_ALREADY_DECIDED），下游转账另有幂等键兜底，因此网络重试、用户双击、SSE 重连都不会造成重复扣款。

**审批 TTL 300 秒**：配置项 APPROVAL_TTL_SECONDS=300。过期后审批单不可用，必须重新发起，避免「半小时前的弹窗」被误点通过。前端拿到 expires_at 后自行倒计时。

**撤销窗口 60 秒**：const UNDO_WINDOW_SECONDS=60。engine.undo_action（engine.py:1276）先按 transfer_id 查 transfers，命中走 mock_core.undo_transfer，否则按申购走 mock_core.undo_purchase；超窗抛 UNDO_EXPIRED，由 server 转成 400 并发中文提示。撤销本身也审计（event_type=undo，risk_level=medium），并生成反向冲正流水，而不是删除原流水。

**已知边界**：checkpoint 以 thread_id 为键且 INSERT OR REPLACE，同一线程同时只保留一份待恢复状态；如果用户在同一线程连续发起两笔待审批动作，后一笔会覆盖前一笔的 checkpoint（审批单本身仍在 approvals 表，但恢复状态只剩最新一份）。单用户单线程顺序操作不受影响，多审批并发是已知限制，见 docs/security.md。

---

## 6. 幂等键设计

每一轮对话在 run_turn 中生成 turn_key（形如 tk_ 加 12 位十六进制，engine.py:1166），每个步骤在执行时拼出幂等键（engine.py:697）：

    idem_<thread_id>_<turn_key>_<step_id>

三段的含义：thread_id 锁定会话线程、turn_key 锁定本轮对话、step_id 锁定具体步骤。同一轮内不同步骤键不同；跨轮即使 step_id 相同，也因为 turn_key 不同而不会互相吞掉。

幂等由 transfers 表的 idempotency_key UNIQUE 约束落地：mock_core.create_transfer 命中已有键时直接返回原结果并带 deduplicated=True，不再重复记账；内部划转（internal=True）也只在首次入账一次。审批通过后的恢复执行、HTTP 重试、误触重复提交，都会命中同一个键。

申购（purchase_wealth）当前未使用幂等键，其重复执行由审批单一次性状态兜底（APPROVAL_ALREADY_DECIDED）。若将来要支持「同一轮内自动重试」，应把幂等键也接入申购路径——这是明确的可改进点。

---

## 7. 事件与审计持久化

两套记录各司其职：

| 表 | 面向 | 内容 | 特性 |
| --- | --- | --- | --- |
| events | 前端/回放 | session_id、seq、type、payload_json、ts | 按会话自增 seq，append-only，可通过 /api/events?after= 增量回放 |
| audit | 取证/合规 | seq、ts、session_id、actor、event_type、tool、args_hash、result_hash、risk_level、approval_id、prev_hash、hash | 全局自增 seq，SHA-256 链式哈希，可离线验链 |

**Emitter**（engine.py:65）在构造时读取当前会话的 MAX(seq)，每次 emit 自增并 INSERT events 后 commit，再追加到内存列表并回调 sink——server.py 传入的 sink 就是 SSE 推送函数，所以前端看到的事件与库里完全一致。

事件类型：session、intent、plan、tool_start、tool_end、approval_required、message、done、undo_done，以及 v0.4.1 的 security_lock（锁定态与触发锁定的那一轮都会 emit）。

审计事件类型：attack_blocked、turn_blocked、approval_required、approval_granted、approval_rejected、tool_result、tool_error、untrusted_content_blocked、verify_balance、injection_ignored、undo、response，以及 v0.4.1 的 security_locked（tool=locks，high）、security_unlocked（tool=locks，medium）、turn_locked（tool=security_lock，high）。

**哈希链算法**（audit.py）：把记录字段组成字典（含 prev_hash），用 canonical JSON（json.dumps 的 sort_keys=True、separators 为逗号与冒号、无多余空格）序列化后做 SHA-256；args_hash 与 result_hash 分别是参数与结果的 canonical JSON 哈希；首条记录 prev_hash 为 64 个 0（GENESIS）。verify 从第一条开始逐条重算并比对 prev_hash 与 hash，任何字段被改写都会返回 valid=false 并给出 broken_at 与原因。离线校验命令：

    python scripts/verify_audit.py --db backend/data/bank.db

---

## 8. 存储模型速览

| 表 | 用途 | 关键约束 |
| --- | --- | --- |
| accounts / transactions | 账户与流水 | 金额整数分；transactions 有 ts、account 索引 |
| transfers | 转账与内部划转 | idempotency_key UNIQUE，支撑幂等 |
| approvals | 审批单 | approval_id 主键、status、expires_at、challenge |
| checkpoints | 审批恢复快照 | thread_id 主键，state_json 存 ctx |
| sessions | 会话 | session_id → thread_id、user_id |
| events | 实时事件 | (session_id, seq)，可增量回放 |
| audit | 审计链 | 全局 seq，hash/prev_hash 链 |
| card_txns | 刷卡交易流水 | txn_id 主键、card_id + ts 索引；成功刷卡与限额校验共用 |
| scheduled_transfers | 预约转账 | sched_id 主键、run_at 索引；status 默认 pending，执行后写 transfer_id 与 executed_at |
| wealth_products / wealth_holdings / cards / subscriptions / payees / budgets / reminders | 业务对象 | 由 db.seed 确定性写入；payees 含 phone 列 |

db.py 使用 WAL 与外键约束；种子数据由 random.Random(42) 生成，保证每次建库结果一致，评测可重复。

---

## 9. 插件注册表与能力开关（v0.3.0）

v0.3.0 没有新增业务动作，而是把已有能力显式注册成插件，让它们可查看、可启停。插件层位于编排层旁路，不改变六阶段流水线的顺序。

### 9.1 注册表

plugins.py 的 REGISTRY 固定七个插件，每项声明 id、name、kind、provides（tools / skills / routes）、requires、detail（how / env / endpoints）与 toggleable：

| id | kind | 提供 | 是否可停用 |
| --- | --- | --- | --- |
| policy | core | 权限分级与审批（/api/approval、/api/demo/totp） | 否，锁定 |
| audit | core | 哈希链审计（/api/audit、/api/events） | 否，锁定 |
| mcp-fx | mcp | fx_convert 汇率换算 | 是 |
| mcp-risk | mcp | risk_screen 反欺诈评分 | 是 |
| intent-llm | model | llm.parse 模型意图通道 | 是 |
| skill-pack | skill | 六张技能卡 | 是 |
| channel-im | channel | IM 渠道适配层 | 是 |

- 状态判定顺序：插件被停用 → disabled；mcp 类插件探测不到外部服务 → degraded；否则 enabled。status() 永不抛异常，读取失败时退化为全部启用。
- 状态持久化：backend/data/plugins.json（路径可由 PLUGINS_FILE 覆盖），先写 .tmp 再 os.replace 原子替换；文件缺失、JSON 损坏或不是对象时按空状态处理，即全部启用。每次读取现读磁盘，不用进程内缓存。
- 版本字段统一读 config.VERSION（仓库根 VERSION 文件），重启后生效。

### 9.2 启停语义

POST /api/plugins/<id>/toggle 只接受布尔 enabled：

- 未知 id → 404 PLUGIN_NOT_FOUND「插件不存在」。
- core 插件 → 403 PLUGIN_CORE_LOCKED「核心能力不可停用」，传 true 也一样拒绝。
- 成功后写一条 plugin_toggled 审计（tool=plugins.toggle、args 含 plugin_id/enabled、risk=low），并由服务端立即生效；失败只写 stderr，不影响其他插件。
- 停用只收起能力，不放宽安全：engine._plugin_enabled 在读取状态异常时一律返回 True（宁可多执行能力，也不因插件文件损坏跳过安全相关路径）；policy、审批、TOTP 与哈希链审计始终执行。

### 9.3 停用后的运行路径

| 插件 | 停用后的行为 | 审计 / 回复 |
| --- | --- | --- |
| mcp-fx | 不发起外部调用，直接走内置参考价 | 审计 mcp_call source=disabled；回复前缀「汇率插件已停用，按内置参考价 … 估算」 |
| mcp-risk | 跳过外部反欺诈评分，不拦截 | 结果标 risk_plugin_disabled；转账/跨场景结论补「外部反欺诈核查未执行（插件已停用），policy 安全规则仍然生效」 |
| intent-llm | parse 回退规则模式，status 带 plugin_disabled=true | 回复来源标规则 |
| skill-pack | GET /api/skills 返回空列表并标 disabled | 解析退回纯关键词规则，policy 不变 |
| channel-im | 渠道进程 /message 返回 503「渠道插件已停用」，不写 messages.jsonl | 主服务 /api/channel/status 仍可读，enabled=false |

接口字段见 [api.md](api.md) §4，插件清单与运维说明见 [plugins.md](plugins.md)，前端呈现见 [design-system.md](design-system.md) §4.1。

---

## 10. 渠道接入与主动触达（v0.3.2）

v0.3.2 在不改六阶段流水线的前提下，把渠道从「单一路径的入站 webhook」扩展为「三种入站 + 一条出站」。主服务仍是唯一的业务与安全边界，三个入站进程都可以独立启停。

### 10.1 三种入站形态

| 形态 | 进程 | 入口 | 协议处理 |
| --- | --- | --- | --- |
| Web / PWA | backend/server.py :8000 | 静态页面 + REST + SSE | 无渠道协议，直接进 run_turn |
| IM webhook | backend/channels/server.py :8400 | POST /message | X-Channel-Token 鉴权、10 秒幂等、两步确认 |
| 企业微信 | backend/channels/wecom.py :8401 | GET/POST /wecom | URL 验签、AES 解密、MsgId 去重、加密回包 |

IM 与企微最终都调用 channels/server.py 的 handle_message，因此共享同一套 sessions.json、10 秒幂等、两步确认与 messages.jsonl；主服务只看到「一条用户消息」或「一次审批确认」，不知道它来自哪个渠道。

### 10.2 入站数据流

    外部消息
       │
       ▼
    渠道适配（鉴权 / 验签 / 解密 / 去重）
       │
       ▼
    handle_message（user_id → session_id，10 秒幂等）
       │
       ├── 文本是「确认 / 取消」 → POST /api/approval（内存里的 approval_id）
       └── 其他文本               → POST /api/chat
       │
       ▼
    主服务六阶段流水线（parse → plan → policy_gate → act → verify → respond）
       │
       ▼
    渠道回包白名单（reply，或 needs_confirmation + preview + hint）
       │
       ▼
    messages.jsonl 留痕 + 回给发起方

回包永远不包含 approval_id、nonce、thread_id 与 confirmation.token；审批凭据只存在渠道进程内存，确认动作由进程代为提交。

### 10.3 企业微信加解密边界

wecom_crypto.py 用纯标准库实现协议层，不引入第三方加密库：

- 签名：sha1(sorted([token, timestamp, nonce, encrypt]))，用 hmac.compare_digest 做定时安全比较。
- 解密：AES-128-CBC（IV = key），PKCS#7 补到 32 字节；明文为 random(16) + msg_len(4，网络序) + msg + corp_id。
- 加密回包：把回复按同样结构封装、加密，再计算 MsgSignature，组成 Encrypt / MsgSignature / TimeStamp / Nonce 的 XML。

验签在解密之前：签名失败直接 403，不进入解密与业务。CorpID 在解密后校验，不符按非法消息拒绝。

### 10.4 主动触达数据流

    定时器 / 设置页按钮 / 外部调用
       │
       ▼
    POST /api/push（kind = subscription / large_txn / repayment）
       │
       ▼
    push.py 读取账务数据（订阅、近 7 天流水、信用卡账单）
       │
       ▼
    生成 title + text
       │
       ├── CHANNEL_PUSH_URL 未配置 → not_configured，只返回文案
       └── 已配置 → POST {user_id, kind, title, text}
                     │
                     ├── 2xx → delivered
                     └── 不可达 / 超时 / 非 2xx → failed
       │
       ▼
    审计 push_message（actor=system，risk_level=low）
       │
       ▼
    返回 {ok, kind, user_id, title, text, pushed, delivered, channel, audit_seq, ts}

推送只读账务数据，不调用 mock_core 的任何写接口，因此不会改变余额、流水或审批状态。推送失败也不抛出到主流程，只在响应与审计里记录。

### 10.5 隔离与降级

- 入站进程与主服务进程隔离：渠道挂了不影响 Web、评测与资金链路；主服务挂了，渠道返回 HTTP 200 的可读降级文案，避免 IM 平台无限重试。
- 企微协议与密钥只在 wecom.py 进程内：主服务不接触 WECOM_TOKEN、WECOM_AES_KEY 与 CorpID。
- 渠道层的 10 秒幂等与 MsgId 去重只是防重复投递；真正的资金幂等仍由第 6 节的 thread_id + turn_key + step_id 保证。
- 主动触达没有重试与补偿：失败只记录当次结果，不做队列重放，也不会因为推送失败而重复扣款。

接口字段见 [api.md](api.md) §4，协议、配置与验收见 [channel.md](channel.md)，密钥与验签的安全边界见 [security.md](security.md) §9。
---

## 11. 资金动作扩展与预约调度（v0.4.0）

v0.4.0 在六阶段流水线之外只多了两个东西：更多确定性工具，以及一个「到点触发」的调度器。流水线顺序、策略门位置、审批与审计入口都不变。

### 11.1 新增意图与工具

| 意图 | 工具 | build_plan 要点 |
| --- | --- | --- |
| wealth_redeem | redeem_wealth | 解析持仓或产品 id，缺省取第一笔持仓与全额；金额非法/超持仓在执行前由领域层拦截 |
| card_limit | set_card_limit | 优先信用卡，从文本抽取日累计与单笔两个槽位；负数、0、单笔大于日累计在 policy 或领域层拦截 |
| card_spend | card_spend | 按文本选择虚拟卡/借记卡/信用卡，从「在 X 消费」抽取商户；写 card_txns 并累计当日额度 |
| anomaly_scan | scan_anomalies | 默认近 30 天，只读；三条规则在领域层产出可疑列表 |
| annual_report | annual_report | 按年汇总，只读；缺省当前年 |
| scheduled_transfer | create_scheduled_transfer / list_scheduled_transfers / cancel_scheduled_transfer | slots.op 区分 create / list / cancel；create 需要收款人、金额与时间三要素，缺一即 planner_block |

只读能力（异常扫描、年度账单、预约列表）走 LOW 直接执行；赎回、限额、撤销预约走 MEDIUM 确认；刷卡与预约创建按金额进入 MEDIUM 或 HIGH。所有工具仍然只在 execute_steps 内被调用，且每次调用都写 tool_result 审计。

**模型模式已支持六类新意图**：llm.ALLOWED_INTENTS（llm.py:55）由 17 增至 23，INTENT_ALIASES 补了赎回、改额度、刷卡、异常交易、年度账单、预约转账等中文说法；SYSTEM_PROMPT（llm.py:115）由 1739 增至 2395 字符，新增六意图说明与 single_limit_yuan / year / schedule_hint / op / merchant 五个字段；_normalize（llm.py:710）负责清洗这些新槽位。本地 stub 桩实测六类意图全部 source=llm、intent 正确，模型模式与规则模式都能走通新能力。

模型仍然只提议意图，分类与时间由代码兜底：规则解析判成 anomaly_scan 时，_normalize 不允许模型改判成查流水（llm.py:712–714）；原句出现「明天 / 3 天后 / 9 点」等时间、规则判成 scheduled_transfer 时，模型改判立即转账会被拉回预约（llm.py:715–717），预约时间只以规则解析的 run_at 为准（llm.py:822–826）。详见 [security.md](security.md) §7。

### 11.2 预约转账的生命周期

    用户：「明天上午 9 点给妈妈转 100 元」
        │
        ▼
    nlu / llm 解析出 scheduled_transfer + run_at + 金额 + 收款人
        │
        ▼
    build_plan → policy_gate（1000 元内 confirm，超过 totp）
        │
        │  审批通过
        ▼
    execute_steps → mock_core.create_scheduled_transfer
        │  只登记，不扣款；写 scheduled_transfers(status=pending)
        ▼
    scheduler 每 5 秒 / POST /api/scheduler/tick
        │  run_at <= now 的 pending 记录
        ▼
    mock_core.execute_due_transfers
        │  调 create_transfer，幂等键 sched_<sched_id>
        ├── 成功 → status=executed，写 transfer_id / executed_at，审计 scheduled_transfer_executed（medium）
        └── 失败 → status=failed，审计 scheduled_transfer_failed（high）
        ▼
    GET /api/scheduled 可查全部状态

**为什么只在创建时审批一次**：预约语义本身就是「到点替我执行」，如果到期还要再确认，用户不在场时预约永远不会执行，等于没有预约能力。补偿措施是：创建时用完整审批（confirm / TOTP）确认金额、收款人与时间；执行复用与手动转账完全相同的 create_transfer 与 policy 参数校验；幂等键 sched_<sched_id> 保证重复 tick 不重复扣款；成功与失败都写审计。

### 11.3 调度器实现

- scheduler.py 的 _setting 先读 config 同名属性、再读环境变量：SCHEDULER_ENABLED 默认 1（0 / false / no / off 关闭），SCHEDULER_INTERVAL 默认 5 秒（最小 1 秒）。
- 后台线程名为 zerostep-scheduler、daemon=True，同一进程只启动一次；每轮自建数据库连接调用 tick，异常只记 last_error，不终止线程。
- tick 不接受外部时间参数，统一用服务端当前时间；这也是 /api/scheduler/tick 的已知限制。
- 调度器只负责「何时执行」；「能不能执行」仍由 create_transfer 的 policy 校验、余额校验与领域错误码决定，失败不会重试。

### 11.4 存储与种子

- 新增 card_txns（刷卡流水，txn_id 主键、card_id + ts 索引）与 scheduled_transfers（预约登记，sched_id 主键、run_at 索引）两张表。
- payees 增加 phone 列；find_payee_by_phone 忽略空格与横线，用于手机号转账命中通讯录。
- SEED_VERSION 升到 2026.10.29，并新增 4 条固定演示异常流水 DEMO_ANOMALY_001..004，保证异常扫描每次都能演示出三条规则；SEED_VERSION 变化时旧演示库会自动重建（仅演示语义）。
- 接口字段见 [api.md](api.md) §4 v0.4.0 段，用例与断言见 [evaluation.md](evaluation.md)，风险分级见 [security.md](security.md) §2。

---

## 12. 多因子与异常锁定（v0.4.1）

v0.4.1 把「红色动作只能输动态码」扩展为「红色动作可三选一因子」，并给连续可疑会话加一层临时熔断。两者都不改变六阶段流水线的顺序，也不改变资金边界。

### 12.1 三种强验证因子

| 因子 | 实现 | 有效期 | 一次性 | 错误码 |
| --- | --- | --- | --- | --- |
| totp | totp.py，RFC 6238 / HMAC-SHA1 / 30 秒步长 / ±1 窗口 | 30 秒步长 | 每步可用 | INVALID_OTP |
| sms | second_factor.sms_send / sms_verify，6 位数字 | 默认 120 秒（SMS_TTL） | 是；连续 5 次错误作废（SMS_MAX_ATTEMPTS） | INVALID_SMS |
| face | second_factor.face_scan / face_verify，fc_ + 32 位十六进制令牌 | 默认 60 秒（FACE_TTL） | 是 | INVALID_FACE |

完整链路：

    用户点「确认」并选择因子
        │
        ├─ 动态码：前端直接展示 TOTP（DEMO_MODE 下由 /api/demo/totp 提供）
        ├─ 短信：GET /api/demo/sms → 6 位 code（内存态）
        └─ 人脸：POST /api/demo/face → fc_ 令牌（内存态）
        │
        ▼
    POST /api/approval {approval_id, decision:"approve", method:"totp"|"sms"|"face", code}
        │
        ▼
    approvals.check(..., method=...)（approvals.py:60）
        │  challenge == "totp" 时才校验因子；confirm 直接通过
        ├─ 失败 → INVALID_OTP / INVALID_SMS / INVALID_FACE / INVALID_METHOD，审批单保持 pending
        └─ 成功 → mark approved，审计 approval_granted(result.method=...)
        ▼
    engine.resume → execute_steps → _finalize

因子代码与业务代码分离：second_factor.py 只有内存字典与标准库 secrets / hmac，不读写数据库，也不参与风险分级；它只回答「这个 code / 令牌现在有效吗」。

### 12.2 会话异常锁定

    run_turn（engine.py:1164）
        │
        ▼
    locks.info(conn, session_id)（:1169）
        ├─ 已锁 → 回复「会话因连续可疑指令已被临时锁定，请在 N 秒后再试。锁定期间不会执行任何资金动作。」
        │          审计 turn_locked（tool=security_lock，risk high），返回 status=locked
        └─ 未锁 → 继续
        │
        ▼
    攻击判定 / 计划拦截 / 策略拦截
        │  命中时先写 attack_blocked 或 turn_blocked 审计
        ▼
    locks.record_security_event(conn, session_id, user_id, kind, reason)（locks.py）
        │  统计本会话最近 600 秒内的 attack_blocked + turn_blocked 条数
        ├─ 未达阈值（3）→ 返回 count，正常 blocked 响应
        └─ 达到阈值且未锁 → UPSERT session_locks，锁定 300 秒
               审计 security_locked（tool=locks，args 含 reason / seconds，risk high）
               回复末尾追加「（N 分钟内第 K 次被拦操作，会话已临时锁定 300 秒。）」
               emit security_lock
        ▼
    后续请求在阶段 0 被拦下，不解析、不建计划、不调工具

三个常量都可由环境变量覆盖：SECURITY_LOCK_THRESHOLD=3、SECURITY_LOCK_WINDOW_SECONDS=600、SECURITY_LOCK_SECONDS=300。锁定与解锁（locks.unlock）都写审计：security_locked 为 high，security_unlocked 为 medium。

**为什么用「次数 + 时间窗」而不是单次触发**：一次注入可能是用户好奇或误粘贴，立即锁定体验太差；但连续 3 次被拦已经明显不是正常使用。窗口 10 分钟足以覆盖一次连续尝试，又不会让历史误触长期累积。

**为什么锁定发生在阶段 0**：锁定期间任何工具都不该被执行。把检查放在 run_turn 最前面，比在每个工具前重复判断更可靠，也让回复语义统一——用户看到的是「被锁定」，而不是某一步失败。

### 12.3 存储与恢复

- session_locks 表：session_id 主键、user_id、locked_until、reason、created_at；db.py:97-100 建表，RESET_TABLES 含该表，/api/admin/reset 会清空。
- 锁是「到期自动失效」而不是定时任务：locks.info 每次比较 locked_until 与当前时间，过期即视为未锁（旧行可留待下次 UPSERT 覆盖）。
- 锁状态在 SQLite 里，主服务重启不会丢锁；因子码与令牌只在内存，重启后全部失效，需要重新获取。
- 计数直接读审计表的 attack_blocked / turn_blocked 时间戳，不额外维护计数器，因此审计清理会同时影响计数。

### 12.4 边界与已知限制

- 因子码与令牌是单进程内存态：多实例部署时各进程互不可见；演示接口直接返回 code / 令牌，仅适合本机演示。
- 人脸是「令牌发放 + 校验」，没有真实摄像头、活体检测与生物特征存储。
- 锁定按 session_id 计数，用户新建会话即可绕过；没有 IP、设备、用户级封禁与告警。
- 多因子只作用于 challenge=totp 的红色动作，MEDIUM 仍是单次 confirm；不支持「必须两个因子同时通过」的复合校验。
- 锁定与因子都不改变金额阈值、幂等键、审计链；即使被锁定，已有审批单的 TTL 与撤销窗口也照常计算。

**接口字段见** [api.md](api.md) §2 与 §4 v0.4.1 段，风险矩阵与限制见 [security.md](security.md) §2、§4.1 与 §10，用例见 [evaluation.md](evaluation.md)。


---

## 13. 消费洞察与逐字流式（v0.5.0）

v0.5.0 加了两件互不干扰的事：一是只读的消费洞察（新增 insight 意图与两个 LOW 工具），二是把最终回答改成逐个字块下发的流式输出，并允许中途停止。六阶段流水线的顺序、三级授权、审批 TTL 300 秒、撤销窗口 60 秒与审计哈希链都不变；新增的取消只作用于「输出」，不作用于「已经执行的工具」。

### 13.1 洞察链路（只读，不改账）

    run_turn（engine.py:1164）
        │
        ├─ 阶段 0 锁定检查 → 阶段 1 parse_intent
        │     规则：洞察 / 花费分析 / 钱都花哪 / 能省 / 订阅在烧钱（nlu.py:367-369）
        │     模型：ALLOWED_INTENTS 含 insight
        ▼
    build_plan（engine.py:381-383）
        │   insight 固定单步：_step("s1", "生成消费洞察", "spending_insights", {})
        ▼
    policy_gate（policy.py:20-24）
        │   spending_insights / detect_subscriptions 都在 LOW_TOOLS：不确认、不审批
        │   计划里没有任何写工具，因此不会进入 approval_required 分支
        ▼
    act（engine.py:687-691 → insight.spending_insights）
        │   只读 SQL 聚合：近 30 天收支、上期环比、分类 Top5、异常、预算预测、可省建议
        ▼
    verify → respond（engine.py:756-795、982）
        │   _insight_reply 生成不超过 180 字的确定性摘要（实测 178 字）
        │   摘要里的数字全部取自工具返回，不在回复层重算
        ▼
    _finalize（engine.py:1157-1160）
        │   out["insight"] = spending_insights 的完整结果（savings 数组只作建议）
        ▼
    响应体 insight 字段 + SSE insight_card 事件（server.py:448-450）

以演示种子为例，一次洞察会得到：近 30 天支出 24,484.70 元、比上期多 48%、最大分类「购物」11,506.71 元（占 47%）、每月可省约 255.00 元（年化 3,060.00 元）、预计支出 25,300.86 元对预算 12,100.00 元（预计超支 13,200.86 元）。

**洞察只读，savings 不自动执行**：`savings` 每一项都带 action 对象（订阅类为 {kind: subscription_cancel, merchant}，分类优化类为 {kind: query_bills, category}），它只描述「可以做什么」，不携带工具调用；engine 不会把 savings 再送进计划，审批与幂等键也不会因此产生。t50 用「审计里不得出现写事件」把这条口径钉住：计划、执行、审计三层都只有 spending_insights。

### 13.2 订阅识别链路

    detect_subscriptions(conn, user_id, window_days=120)（insight.py:162，默认窗口 120 天）
        │
        ├─ 按商户归一（去掉编号、空格等噪声）
        ├─ 同一商户至少 3 次、相邻间隔落在 25–35 天
        ├─ 相邻金额差不超过 10%
        ├─ confidence = min(60 + 10 × (次数 - 2), 90)
        ▼
    逐项标注：registered（是否已登记订阅）、sub_id、category、subscription_like
        │   subscription_like = (category == "订阅") 或 命中已登记订阅
        ▼
    分流两条：
        ├─ subscription_like=true → 进 savings，计入可省金额（年化 = 月 × 12）
        └─ 固定周期（房租 / 还款 / 水电 / 通讯）→ 进 fixed_cycles 单列，不计入可省

演示种子实测：识别出 10 项周期扣费。其中 5 项为订阅（爱奇艺 VIP 25 元、网易云音乐 15 元、iCloud 200G 6 元、健身私教 199 元、财经周刊 10 元），合计可省 255 元/月、年化 3,060 元；另 5 项为固定周期（房东王姐房租 4,500 元、信用卡还款 3,000 元、储蓄转账 2,000 元、国家电网 286 元、中国移动 99 元），合计 9,885 元/月，单独说明、不当作「可省」。

**为什么固定周期要单列**：房租和还款不是「取消就省钱」的东西，把它们算进可省会直接把洞察变成误导。分流规则也解释了 contract 里 potential_save_monthly_cents 的口径——它只累加 subscription 类 savings，所以恒等于订阅月费之和，也恒等于订阅年化 ÷ 12。

### 13.3 逐字流式输出链路

    POST /api/chat（stream 分支，server.py:266-274）
        │  stream_text 不为 false 时走流式；为 false 时退回旧的整段 message
        ▼
    _stream_chat（server.py:374-467）
        │  先发 session / intent / plan / tool_start / tool_end
        ▼
    answer_start {turn_id, thread_id, chars}（:418）
        │
        ▼
    answer_delta {turn_id, i, text} × n（:428，每块 12 字符；末块可短）
        │  块间 sleep，时长取 ZS_STREAM_DELAY_MS，默认 18ms（:54-55、:430-431）
        ▼
    answer_end {turn_id, chunks, elapsed_ms, cancelled}（:435-437）
        │
        ▼
    insight_card {insight: {...}}（:448-450，仅 insight 意图）
        ▼
    done {status, thread_id, undo, approval, turn_id}（:451-453）
        ▼
    finally 清 _STREAMS（:455 起），thread_id 不再可取消

- **块大小与延时都是演示值**：12 字符让 178 字的洞察约 15 块，18ms 让整段回答在半秒到一秒内播完；测试把 ZS_STREAM_DELAY_MS 设为 0 以获得确定性。
- **拼接口径**：delta 按 i 顺序拼接应等于完整回答，answer_end.chunks 等于 delta 条数，answer_start.chars 等于回答总字符数——这三条是 e2e 的硬断言。
- **兼容路径**：`stream_text: false` 时不发 answer_* 与 insight_card，只发整段 message，老客户端不用改；不传或传 true 一律逐字流式。
- **前端职责**：前端只负责追加文本与在中断时丢弃未到片段，不参与内容生成；样式与交互见 [design-system.md](design-system.md)。

### 13.4 取消链路（只停输出，不改账）

    POST /api/turn/cancel {thread_id}（server.py:250-265）
        │
        ├─ body 不是 JSON 对象 → 400 BAD_REQUEST「请求体必须是 JSON 对象」
        ├─ thread_id 不在 _STREAMS → 200 {cancelled:false, reason:"not_streaming"}
        └─ 正在流式 → 置 stop 标记 → 200 {cancelled:true, emitted_chunks:n}
                │
                ▼
    _stream_chat 循环发现 stop（:421-425）
                │  停止后续 answer_delta，已发出的片段不回撤
                ▼
    补发 answer_end {cancelled:true}（:435-437）+ note {text:"已停止输出"}（:440）
                │  insight_card 与 done 照常下发
                ▼
    审计 answer_cancelled：tool=stream，risk=low，
      args {thread_id, emitted_chunks, chars_total}，result {cancelled:true}（:442-445）

**取消不撤销已执行工具**：取消只写一个审计事件、只终止文本输出，已经发生的 tool_end、账务写入与幂等键都保留；t55 用「取消前后余额与支出合计不变、tool_end 仍在、仍有 insight_card」来验证这一点。**为什么这样设计**：如果取消能回滚，用户就有机会在「看到一半」时制造半笔交易或试探策略边界，审计也无法区分「真取消」与「恶意中断」。

重复取消不需要特殊处理：首次取消后流式阶段结束，同一 thread_id 再次取消按 not_streaming 返回（此时 _STREAMS 已清空）。

### 13.5 边界与已知限制

- 洞察是启发式分析：订阅识别依赖「间隔 25–35 天 + 金额差 ≤10%」，改价、跨月缺记录或一次性消费会被漏报或误报；结论只是参考，不构成财务建议。
- 流式的 12 字符与 18ms 是演示参数，不是性能指标；真实网络下需配合代理缓冲与前端丢弃策略调整。
- 取消只覆盖逐字输出阶段；审批挂起时的「取消」走撤销窗口（60 秒）与 approvals 的既有语义，两者不共用接口。
- 洞察卡的数据来自同一份 tool_result；如果未来改成流式边算边发，需要重新定义 insight_card 与 answer_end 的先后顺序。

## 14. 账户总览刷新链路（v0.5.1）

v0.5.1 修的是「钱动了，左边总览不动」：账户类型字段口径错、理财持仓没计入资产构成、操作完成后不重新取快照。三处改动都在会话快照层与前端展示层，不碰 policy、审批 TTL、幂等键与审计链。

### 14.1 会话快照新增 wealth（后端）

engine.py:46 的 session_snapshot(conn, session_id) 负责组装 GET /api/session/{id} 与 POST /api/session 的响应；engine.py:56 新增一项：

    "wealth": mock_core.list_wealth(conn, sess["user_id"])

数据来自 mock_core.py:253 list_wealth：

    session_snapshot（engine.py:46）
        ├─ session_id / user
        ├─ accounts / cards / subscriptions / payees
        ├─ pending_approval / llm_mode / llm_status / demo_mode
        └─ wealth ← mock_core.list_wealth（mock_core.py:253）
                └─ { products, holdings,
                     holding_total_cents = sum(holdings.amount_cents),
                     holding_total_display = yuan(total) }

GET /api/session/{id} 在 server.py:116 直接返回该快照；POST /api/session 复用同一函数，因此新建会话与刷新会话的字段完全一致（旧响应只有增量，不破坏既有字段）。

### 14.2 前端字段口径：acctKind()

旧代码按 a.type 判断账户类型，后端字段实际是 kind，结果三处都错：信用卡被当成存款计入「存款」总额、资产构成条的「理财」段恒为 0、账户脚注「信用卡待还」不出现。app.js:412 增加一处口径函数：

    function acctKind(a) { return (a && (a.kind || a.type)) || ''; }

renderAccounts（app.js:413）的总额、信用卡待还与资产构成全部改用它；renderMix（app.js:449）把 holdings 计入「理财」段；账户列表在常规行之后追加一行「理财 · 持仓 ¥X」（app.js:440-444，class acct__row--wealth），资金流向因此能看出申购后的钱去了哪里。

### 14.3 刷新触发点：refreshAccountSnapshot()

app.js:468 定义 refreshAccountSnapshot()：GET /api/session/{id} → 覆盖 state.accounts 与 state.wealth → renderAccounts；用 state.acctRefreshing 防重入，失败静默（保留旧数字，不弹错、不阻塞对话）。

| 触发 | 位置 | 说明 |
| --- | --- | --- |
| 回合结束（ok / blocked） | app.js:1070 finishTurn | 转账、申购、还款等写操作完成后自动刷新 |
| 审批通过 / 拒绝 | app.js:1547 applyApprovalResult → app.js:1564 finishTurn | 复用同一条结束路径，不单独调用 |
| 撤销完成 | app.js:981 undoAction | 原路退回后余额与总览同步 |
| 首屏加载 | 初始快照 | 直接用 POST /api/session 的返回渲染，不再额外请求 |

**为什么只是只读刷新**：快照接口不写库、不产生审计事件、不占幂等键；即使刷新请求失败，真实结果仍可从审计面板与对话里的 tool_result 看到，界面最多短暂显示旧数字，不会出现「界面显示成功、账务没动」或相反的情况。

### 14.4 静态资源缓存版本

frontend/sw.js:6 的 VERSION 与仓库根 VERSION 同步升级（v0.6.1 时为 0.6.1），CACHE_NAME 为 zerostep-shell-v0.6.1，activate 阶段删除旧前缀缓存。只改 app.js / styles.css 而不改这一行时，已安装的 PWA 会继续用旧外壳，这是「改了代码、页面没变」最常见的原因。

### 14.5 验证

frontend/_selftest.js:600-625 新增 9 条「资金流向与余额刷新」断言：启动时理财段仅最小宽度、启动无持仓行、刷新后工资卡余额跟随服务端、信用卡待还写进脚注、多出理财持仓行（4 行）、持仓行写明金额、理财段按持仓放宽、回合结束自动刷新余额、回合结束自动刷新持仓；前端自检总数 238 → 247。

**接口字段见** [api.md](api.md) §1（wealth 与 kind），用例与测试口径见 [evaluation.md](evaluation.md) §5，审计与安全边界见 [security.md](security.md)。

## 15. 数据专区与只读聚合层（v0.7.0）

专区是「看数」的入口，对话是「办事」的入口，两者共用同一套账本口径，但走不同的通路：对话走六阶段流水线并可能触发审批与资金动作；专区只读，从 `server.py` 直接进 `analytics.py`，**不经过 policy、不产生审批、不写任何表**。

### 15.1 分层位置

    browser frontend/app.js（zone 模块 + 手写 SVG 图表 + hash 路由）
        │  GET /api/dashboard?session_id=&window=
        │  GET /api/analytics?session_id=&from=&to=&granularity=
        ▼
    HTTP 层  backend/server.py（_guard → 会话校验 → 分派）
        ▼
    聚合层  backend/app/analytics.py（只读）
        dashboard(conn, user_id, window)      → GET /api/dashboard
        analytics(conn, user_id, start, end, granularity) → GET /api/analytics
        ▼
    领域层  backend/app/mock_core.py（同一批只读函数，供对话工具复用）
        list_accounts / account_total / list_transactions / bill_summary
        budget_analysis / scan_anomalies / list_subscriptions
        list_scheduled_transfers / list_wealth
        ▼
    SQLite  accounts / transactions / budgets / subscriptions
            scheduled_transfers / wealth_holdings / approvals
            （全部只读：SELECT）

**为什么不复用对话的 insight 工具**：`spending_insights` 是「一句话回答」的形态，字段围绕单轮对话裁剪；仪表盘需要的是「一次拿全页面」的宽对象（趋势 + 预算 + 构成 + 待办 + 最近流水）。两者共用底层聚合函数与口径，但各自组装响应，避免为了页面把对话工具的契约撑大。

### 15.2 两个入口的参数与校验

| 参数 | 位置 | 规则 |
| --- | --- | --- |
| `session_id` | query | 必填，经 `engine.session_owner` 校验；未知会话 404 `SESSION_NOT_FOUND` |
| `window` | `/api/dashboard` | `7d / 30d / 90d / 180d / 6m / 1y`；未知值**回落 30d**（看板不应因为拼错参数而 400） |
| `from` / `to` | `/api/analytics` | `YYYY-MM-DD`，缺省为近 180 天；解析失败、`from > to`、跨度 > 400 天均 400 `BAD_RANGE` |
| `granularity` | `/api/analytics` | `day / week / month`；日粒度桶数上限 120，超出 400 `BAD_RANGE` |

返回体的金额一律是整数分 + `*_display` 展示串（与对话工具同口径），前端不做二次换算，避免「页面算的和账本不一样」。

### 15.3 仪表盘响应骨架

    {
      "as_of": "2026-10-29",
      "window": { "kind": "30d", "days": 30, "from": "2026-09-30", "to": "2026-10-29" },
      "kpis": {
        "net_worth_cents": 7942850,  "net_worth_display": "¥79,428.50",
        "available_cents": 5786050,  "available_display": "¥57,860.50",
        "holdings_cents": 3000000,   "holdings_display": "¥30,000.00",
        "credit_due_cents": 843200,  "credit_due_display": "¥8,432.00",
        "month_out_cents": 2443271,  "month_in_cents": 2500001, "net_flow_cents": 56730,
        "deltas": { "month_out_pct": 47, "month_in_pct": 0, "net_flow_pct": -93 },
        "prev": { "month": "2026-09", "out_cents": 1653372, "in_cents": 2500001, "net_cents": 846629 },
        "sparklines": { "out_cents": [ … 14 点 … ], "in_cents": [ … ], "days": [ … ] }
      },
      "trend": { "months": [ { "month": "2026-05", "in_cents": …, "out_cents": …, "net_cents": … } ], "unit": "cents" },
      "budget": [ { "category": "购物", "limit_cents": 200000, "used_cents": 1158671, "pct": 579, "state": "over" } ],
      "composition": { "total_out_cents": …, "by_category": [ { "category": "购物", "cents": …, "pct": 47.1 } ], "by_account": [ … ], "holdings_cents": … },
      "todo": { "rows": [ { "kind": "approval", "label": "待你确认的操作", "count": 4, "detail": …, "target": "workbench" } ],
                "chain": { "valid": true, "checked": 22 } },
      "recent": [ { "txn_id": "T00349", "ts": "2026-10-29T09:12:00", "direction": "in",
                    "amount_cents": 1, "amount_display": "¥0.01", "category": "其他",
                    "counterparty": "扫码返现", "account_last4": "**** 8821" } ]
    }

口径要点：

- `net_worth_cents = available_cents + holdings_cents − credit_due_cents`（信用卡余额为负，取反后作为待还）。
- `deltas` 与上月同日为基线；基线期样本不足 ¥100 时给 `null`（前端显示「—」）。
- `composition.by_category` 只取前 6 类，占比合计可能 < 100%，`total_out_cents` 是完整总额。
- `todo.chain` 来自 `audit.verify`，只读调用，不追加事件。

### 15.4 分析页响应骨架

    {
      "range": { "start": "2026-05-03", "end": "2026-10-29", "span_days": 180,
                 "granularity": "month", "buckets": 6, "prev": { "start": …, "end": … } },
      "totals": { "in_cents": 15000001, "out_cents": 11125135, "net_cents": 3883233,
                  "txn_count": 353, "avg_day_out_cents": 61760,
                  "prev_in_cents": …, "prev_out_cents": …, "out_delta_pct": null },
      "series": [ { "bucket": "2026-05", "in_cents": …, "out_cents": …, "net_cents": …, "count": … } ],
      "categories": [ { "category": "购物", "cents": 3162704, "pct": 28.45, "prev_cents": …,
                        "delta_pct": null, "budget_cents": …, "budget_pct": 264, "over_budget": true } ],
      "merchants": [ { "merchant": "房东王姐", "cents": 2700000, "pct": …, "count": 6 } ],
      "transactions": [ … 最多 100 条，ts 倒序 … ],
      "anomalies": [ … 最多 10 条 … ],
      "subscriptions": [ { "name": "爱奇艺 VIP", "amount_cents": 2500, "next_charge": "2026-11-04", "days_left": 6 } ],
      "savings": { "potential_cents": 950671, "potential_display": "¥9,506.71", "savings_rate": 0.20, "suggestions": [ … ] }
    }

桶的划分：`day` → `YYYY-MM-DD`、`week` → `YYYY-Www`（ISO 周）、`month` → `YYYY-MM`；区间两端按桶补零，保证 `series.length == buckets`，前端不需要处理空洞。

### 15.5 前端：hash 路由与图表模块

- 路由：`#/zone/dashboard`、`#/zone/analytics?preset=180d&from=…&to=…&gran=month`。`showView('zone')` 与 hash 双向同步，`state.hashReady` 之外（自测桩里没有 `window.location.hash`）所有 hash 操作静默跳过，保证不依赖浏览器历史对象也能渲染。
- 数据层：30 秒内复用缓存，页头常驻「更新于 hh:mm · 只读」；可见时 60 秒轮询，离开专区停止；失败渲染带「重试」的错误块。
- 图表：`zoneFlowSvg` / `zoneSparkSvg` / `zoneStacked` / `zoneHbars` 全部手写 SVG，按容器宽度出图（`zoneChartWidth()`，390 下折叠为紧凑刻度），每图带 `<details>` 数据表与 `aria-label`。
- 交互：类别表头排序、明细分页（每页 20）、CSV 导出（`﻿` BOM + CRLF，Excel 直开不乱码）、主题三态、命令面板 `Ctrl/Cmd+K`。

### 15.6 只读性的证明方式

不靠「我们没写」的自觉，靠断言：

- `scripts/dashboard_e2e.py` 的 `ana-17`：连续调用两个聚合接口前后各取一次 `/api/audit`，断言 `chain.checked` 数值不变且 `valid` 仍为 `true`。
- `ana-18`：同样前后比对 `transactions` 条数，断言不产生资金流水。
- `dash-11`：仪表盘 `todo.chain.valid` 必须为 `true`（专区自己也在读审计链，但只读）。
- 实测：v0.7.0 时 `chain.checked` 恒为 22，两个接口连续调用 20 次后 `transactions` 条数不变。

**接口字段见** [api.md](api.md) §4（v0.7.0 只读聚合接口），图表与布局规范见 [design-system.md](design-system.md) §七，自检口径见 [evaluation.md](evaluation.md) §3.5。

## 16. 账户页与账户快照增强（v0.7.1）

### 16.1 位置

账户页是**纯只读展示层**，不新增聚合模块：数据全部来自既有两个查询接口（`/api/accounts`、`/api/dashboard`）与一条带过滤的流水查询（`/api/transactions?account_id=`）。层级关系与数据专区相同：

    frontend/app.js (acc* 渲染函数)
        ├─ GET /api/accounts      → mock_core.list_accounts + list_cards + account_total
        ├─ GET /api/dashboard     → app.analytics.dashboard（KPI / 预算 / 构成 / 待办）
        └─ GET /api/transactions  → mock_core.list_transactions(account_id=...)
    所有接口只读：不写 transactions / events / audit

### 16.2 `_accounts_with_cards`

`backend/server.py` 里新增一个纯函数把两张表在内存里拼起来，避免前端为「余额在哪一页、限额在哪一页」做两次请求：

    def _accounts_with_cards(accounts, cards):
        by_tail = {}
        for c in cards or []:
            tail = str(c.get("number_masked") or "").replace("*", "").strip()
            if tail:
                by_tail[tail] = c
        out = []
        for a in accounts or []:
            row = dict(a)
            tail = str(row.get("number_masked") or "").replace("*", "").strip()
            card = by_tail.get(tail) or {}
            row["card_id"] = card.get("card_id") or ""
            row["card_kind"] = card.get("kind") or ""
            row["card_status"] = card.get("status") or ""
            row["single_limit_cents"] = int(card.get("single_limit_cents") or 0)
            row["daily_limit_cents"] = int(card.get("daily_limit_cents") or 0)
            out.append(row)
        return out

要点：匹配键是掩码后四位（`**** 8821` → `8821`），未绑卡时字段为空串 / 0，前端据此隐藏限额行；`accounts` 原有字段一个不改，旧客户端读到的结构仍向后兼容。

### 16.3 流水过滤

`GET /api/transactions` 增加 `account_id`：

- 先取当前用户的账户集合；`account_id` 存在但不在集合里 → 400 `BAD_ACCOUNT`（区分「这个账户没有流水」与「这个账户不是你的」）。
- 传 `account_id` 时下发到 `mock_core.list_transactions(..., account_id=...)`，`limit` 仍 clamp 在 1–200。
- 响应回显 `account_id`，前端据此判断筛选态（`null` 表示「全部」）。

### 16.4 前端只读约束

- 账户页所有动作按钮都只写 `data-acc-text`，点击后调用 `accPrefill(text)`：切回工作台、把这句话填进输入框、提示「确认后发送」。不发起任何写请求。
- 页面不渲染 `<form>`、不直接调 `/api/chat`，因此不会绕过「意图 → 计划 → 策略 → 审批 → 审计链」。
- 缓存 30 秒（`state.acc.at`），「刷新」按钮强制 `accFetch(true)`；切页时不轮询，避免在只读页上制造后台请求。

### 16.5 只读性证明

`scripts/dashboard_e2e.py` 的 `acc-*` 组连续调用 `/api/accounts`、`/api/transactions?account_id=` 与 `/api/audit`：审计链 `checked` 与 events 行数在调用前后不变；`ana-17/ana-18` 继续覆盖「聚合接口不写链」。前端自检里账户页的所有交互分支都在 fetch 桩上断言「没有发出写请求」。
