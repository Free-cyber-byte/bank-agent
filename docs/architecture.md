# ZeroStep 架构说明

本文描述 ZeroStep（零步银行）的内部分层、一次对话的完整流水线、跨场景计划的 DAG 组织方式、审批中断与恢复机制、幂等键设计，以及事件与审计的持久化方式。所有函数名与常量都对应 backend/app 下的真实实现（行号以当前仓库为准）。

---

## 1. 设计原则

1. **理解与执行分离**：自然语言只被解析为「意图 + 槽位」，资金动作由后端确定性工具执行。模型/规则输出错一个槽位，最坏结果是用户看到一条追问，而不是一笔错误转账。
2. **先校验、后执行**：policy.evaluate 在真正调用任何资金工具之前完成金额、收款人、余额、产品、累计额度等参数级校验，不通过就直接拒绝，连计划都不执行。
3. **全过程可回放**：每次意图、计划、工具调用、审批、撤销都同时落 events（面向 UI 的实时事件）与 audit（面向取证的哈希链），任何一次资金变动都能沿审计链回溯到当时的参数与结果。

---

## 2. 分层架构

| 层 | 模块 | 职责 |
| --- | --- | --- |
| 表现层 | frontend/index.html、app.js、styles.css | 原生单页应用，消费 SSE 事件流，渲染对话、审批弹层、TOTP 演示与审计面板 |
| 接口层 | backend/server.py | 零依赖 http.server；REST + SSE；静态资源与路径穿越防护；统一错误体 {error:{code,message}} |
| 编排层 | backend/app/engine.py | run_turn 六阶段编排、build_plan 计划生成、execute_steps 执行、_finalize 回复、resume 审批恢复、undo_action 撤销 |
| 安全与策略层 | nlu.py、policy.py、approvals.py、totp.py、audit.py | 意图与攻击识别、三级权限判定、审批单与 TTL、RFC 6238 动态口令、哈希链审计 |
| 领域层 | backend/app/mock_core.py | 转账、理财、卡片、订阅、还款、提醒的模拟实现；金额一律整数分；DomainError 统一错误码 |
| 存储层 | backend/app/db.py + SQLite | 建表、索引、WAL、确定性种子数据；17 张表覆盖账户、流水、审批、checkpoint、事件、审计 |

依赖方向单向向下：编排层可以调用安全层与领域层，领域层不感知 HTTP 与自然语言，安全层只依赖存储层。

---

## 3. 一次对话的六阶段流水线

入口是 engine.run_turn(conn, session_id, text, sink)（engine.py:568）。它先取会话、创建 Emitter，然后严格按下列顺序推进：

| 阶段 | 真实函数 | 输入 → 输出 | 失败/短路分支 | 关键事件与审计 |
| --- | --- | --- | --- | --- |
| 1 意图解析 | nlu.parse(text, ctx_doc) | 文本 + 上下文 → intent{name, confidence, source, slots} | intent.name 为 attack 时立即 return blocked | 事件 intent；审计 attack_blocked |
| 2 计划生成 | engine.build_plan(conn, user_id, intent, text) | intent → {steps, edges, planner_block} | planner_block 非空时 return blocked | 事件 plan；审计 turn_blocked（tool=planner） |
| 3 策略门 | policy.evaluate(conn, user_id, steps, intent) | steps → {risk_level, requires_approval, challenge, blocked, reasons, summary} | blocked 非空时 return blocked | 事件 tool_end（step_id=gate）；审计 turn_blocked（tool=policy_gate） |
| 3.5 审批挂起 | approvals.create + _save_checkpoint | 高风险计划 → status=awaiting_approval | 不执行任何资金动作 | 事件 approval_required；审计 approval_required |
| 4 执行 | engine.execute_steps(conn, ctx, emit) → _exec_tool | ctx → {results, action_ids, failed} | DomainError 中断并 break | 事件 tool_start/tool_end；审计 tool_result、tool_error |
| 5 校验 | mock_core.verify_balance（在 execute_steps 内） | 转账结果 → {ok, expected, actual} | 不一致置 ctx.verify_issue | 审计 verify_balance |
| 6 回复 | engine.build_reply + engine._finalize | results → reply、undo、status | 执行失败或校验失败 → status=blocked | 事件 message；审计 response |

会话状态机只有三态：ok（完成）、awaiting_approval（挂起等审批）、blocked（拒绝或执行失败）。resume 恢复成功后回到 ok/blocked。

### 阶段 1：意图解析（nlu.parse）

run_turn 先组装上下文文档 ctx_doc：payees、products、subscriptions、cards、accounts，外加用户风险等级 fixed 为稳健型。nlu.parse（nlu.py:162）内部依次做：

1. _normalize：全角转半角、统一小写与空白，避免用全角字符绕过规则。
2. detect_attack：依次匹配越权访问、验证码钓鱼、提示注入、陌生收款人四类攻击，命中即返回 name=attack 与 attack_type。
3. 关键词/正则意图分类：问候、余额、账单、流水、预算、转账、理财申购、持仓、卡片、订阅、跨场景分账等。
4. 槽位抽取：extract_amount（中文数字经 _cn_to_int 换算，金额统一转分）、extract_payee（在 payee 列表中做名/账号匹配，并判定 payee_is_new）、extract_months、extract_category。
5. parse_cross（nlu.py:297）：跨场景语句拆成 allocations 列表（savings/wealth/credit/transfer/生日/预算），供 build_plan 展开。

输出 intent 的 source 字段标明来源（rule 或 llm），为将来接入大模型增强留了位置。

### 阶段 2：计划生成（build_plan）

build_plan（engine.py:123）把意图翻译成可执行的步骤列表：每步由 _step(step_id, title, tool, args, risk) 构造，含 step_id、title、tool、args、risk；edges 为线性依赖，steps[i] 指向 steps[i+1]。

计划阶段就做「不可能就拒绝」的判断：卡不存在（CARD_NOT_FOUND）、订阅不存在（SUBSCRIPTION_NOT_FOUND）、缺金额（AMOUNT_REQUIRED）、没有可用步骤（NO_STEP）都在这里返回 planner_block，避免把无效参数带进策略门。

greeting 与 unknown 意图直接生成回复早退，不进入策略门。

### 阶段 3：策略门（policy.evaluate）

policy.evaluate（policy.py:26）是唯一对「钱」做判定的地方，输出五件事：整体风险等级 risk_level（low/medium/high）、是否需要审批 requires_approval、挑战类型 challenge（confirm 或 totp）、拒绝原因 blocked、人类可读的 reasons 与 summary。

- 被拒绝：审计 turn_blocked、emit 一个 step_id=gate 的 tool_end、用 _blocked_reply 输出标准拦截文案，返回 blocked。
- 需要审批：进入阶段 3.5。
- 低风险：直接进入阶段 4。

策略门通过后，run_turn 会把每个步骤的 risk 抬到不低于整体 risk_level（engine.py:621-622），便于前端按步展示风险标签与审计记录统一口径。

### 阶段 3.5：审批挂起

需要审批时依次执行：_approval_preview 生成人类可读的预览与金额、对手方 → approvals.create 写入审批单（approval_id 形如 ap_ 加 12 位十六进制，nonce 形如 n_ 加 16 位十六进制，expires_at = 创建时间 + 300 秒）→ ctx 记 approval_id 与 status=awaiting_approval → _save_checkpoint 把整份 ctx 以 thread_id 为键写入 checkpoints 表 → 审计 approval_required → emit approval_required → 返回 status=awaiting_approval。

**此刻没有任何资金动作发生。** 用户拒绝、超时或直接关掉页面，账面都不会变化。

### 阶段 4-6：执行、校验、回复

execute_steps（engine.py:328）逐步执行计划：

1. 每步先 emit tool_start（含 args_preview 与 risk），再算幂等键，调用 _exec_tool 分发到 mock_core 对应函数。
2. 工具返回后先过 _scan_untrusted：命中 _SUSPECT_PATTERNS 就把可疑指令替换为「[已屏蔽的可疑指令]」，在 ctx.untrusted 记账，并审计 untrusted_content_blocked。
3. emit tool_end（含 ok、latency_ms、result），审计 tool_result。
4. 若该步是 create_transfer 且返回 balance_after_cents，立即调 mock_core.verify_balance 回读账户余额，审计 verify_balance；不一致就置 ctx.verify_issue，进入 _finalize 时把状态改为 blocked 并追加提示。
5. 任何一步抛 DomainError：emit 失败版 tool_end、审计 tool_error、记录 failed 并 break，后续步骤不再执行。

_finalize（engine.py:537）负责收尾：执行失败时用 _blocked_reply（余额不足、超限）或「操作未完成：原因」组织回复并审计 turn_blocked；成功时 build_reply 生成自然语言回复与 undo 信息；若 ctx.untrusted 非空，在回复前置「⚠️ 安全提示：…」并审计 injection_ignored；最后 emit message、审计 response，返回包含 session_id、thread_id、status、reply、blocked、intent、plan（steps 与 edges）、executed、approval、undo、audit、events 的完整结果。SSE 场景下这份结果与事件流内容一致。

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

生日提醒（t24「下周三我妈生日，转 800 给她，再定个提醒」）展开为：

    s1 create_transfer（给妈妈 800 元，MEDIUM 确认）
        │
        ▼
    s2 create_reminder（提醒时间由 _next_wednesday 计算为下周三 09:00）

预算类语句（t04、t25）只生成 budget_analysis 单步，纯查询不受限。

---

## 5. 审批中断与恢复

**持久化位置**：checkpoints 表（thread_id 主键、state_json 存整份 ctx、updated_at）。ctx 里包含 intent、steps、edges、turn_key、approval_id 等全部恢复所需信息，所以恢复不依赖内存。

**恢复入口**：engine.resume(conn, session_id, approval_id, decision, code, sink)（engine.py:652）：

1. approvals.check（approvals.py:60）依次校验：审批单存在、状态为 pending、未超过 300 秒 TTL（过期会顺手 mark expired）、decision 只能是 approve 或 reject、challenge 为 totp 时必须通过 totp.verify_code(user_id, code, window=config.TOTP_WINDOW)。失败分别映射为 HTTP 410（APPROVAL_EXPIRED）、409（APPROVAL_ALREADY_DECIDED）、400（INVALID_OTP 等），并给出中文提示。
2. _load_checkpoint(ap.thread_id) 取回 ctx；找不到则 410 CHECKPOINT_NOT_FOUND。
3. decision=reject：mark rejected、审计 approval_rejected、回复固定文案「好的，已取消这笔操作，没有发生任何资金变动。」并 emit done。
4. decision=approve：mark approved、审计 approval_granted，把 approval_id 写回 ctx，execute_steps 接着跑，最后 _finalize 收尾。

**恢复的幂等性**：审批单一旦 approved/rejected 就不可再次提交（APPROVAL_ALREADY_DECIDED），下游转账另有幂等键兜底，因此网络重试、用户双击、SSE 重连都不会造成重复扣款。

**审批 TTL 300 秒**：配置项 APPROVAL_TTL_SECONDS=300。过期后审批单不可用，必须重新发起，避免「半小时前的弹窗」被误点通过。前端拿到 expires_at 后自行倒计时。

**撤销窗口 60 秒**：const UNDO_WINDOW_SECONDS=60。engine.undo_action（engine.py:679）先按 transfer_id 查 transfers，命中走 mock_core.undo_transfer，否则按申购走 mock_core.undo_purchase；超窗抛 UNDO_EXPIRED，由 server 转成 400 并发中文提示。撤销本身也审计（event_type=undo，risk_level=medium），并生成反向冲正流水，而不是删除原流水。

**已知边界**：checkpoint 以 thread_id 为键且 INSERT OR REPLACE，同一线程同时只保留一份待恢复状态；如果用户在同一线程连续发起两笔待审批动作，后一笔会覆盖前一笔的 checkpoint（审批单本身仍在 approvals 表，但恢复状态只剩最新一份）。单用户单线程顺序操作不受影响，多审批并发是已知限制，见 docs/security.md。

---

## 6. 幂等键设计

每一轮对话在 run_turn 中生成 turn_key（形如 tk_ 加 12 位十六进制，engine.py:602），每个步骤在执行时拼出幂等键（engine.py:338）：

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

**Emitter**（engine.py:64）在构造时读取当前会话的 MAX(seq)，每次 emit 自增并 INSERT events 后 commit，再追加到内存列表并回调 sink——server.py 传入的 sink 就是 SSE 推送函数，所以前端看到的事件与库里完全一致。

事件类型：session、intent、plan、tool_start、tool_end、approval_required、message、done、undo_done。

审计事件类型：attack_blocked、turn_blocked、approval_required、approval_granted、approval_rejected、tool_result、tool_error、untrusted_content_blocked、verify_balance、injection_ignored、undo、response。

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
| wealth_products / wealth_holdings / cards / subscriptions / payees / budgets / reminders | 业务对象 | 由 db.seed 确定性写入 |

db.py 使用 WAL 与外键约束；种子数据由 random.Random(42) 生成，保证每次建库结果一致，评测可重复。
