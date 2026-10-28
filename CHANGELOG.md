# 变更日志

本文件记录 ZeroStep 的重要变更。格式参考 Keep a Changelog，版本号遵循语义化版本（MAJOR.MINOR.PATCH），日期为北京时间，章节固定为 Added / Changed / Fixed / Security。

v0.2.0 为 2026-10-27 启动的能力批次，代码合并与验收由 Lead 统一复核；尚未通过验收的接口与命令在对应位置单独注明。

## [0.5.0] - 2026-10-28（进行中，未打 tag）

v0.5.0 是「消费洞察 + 逐字流式回答」批次：新增只读的消费洞察能力（统一口径的月度统计、环比、可省金额、预算预测），并把最终回答改成 SSE 逐字流式输出，同时提供「停止输出」的接口。资金边界、审批 TTL（300 秒）、撤销窗口（60 秒）、审计哈希链与既有接口字段均不变；新增能力全部是增量。

### Added

- 消费洞察模块 backend/app/insight.py（纯标准库、只读、整数分）：spending_insights(conn, user_id) 返回冻结结构（as_of / window / totals / top_categories / anomalies / budget / savings），totals 与 top_categories 复用 mock_core.bill_summary 的近 30 天口径，环比为「再往前 30 天」SQL 聚合；detect_subscriptions(conn, user_id, window_days=120) 按商户归一 + 相邻间隔 25–35 天 + 金额差 ≤10% 识别周期扣款，并标注 registered / sub_id；每项另带 category 与 subscription_like（category=订阅 或命中已登记订阅才为 true），房租/还款/水电/通讯等固定周期扣费进独立 fixed_cycles 单列说明，不计入可省金额。
- 意图 insight：nlu 规则 + llm 的 ALLOWED_INTENTS / INTENT_ALIASES（洞察、消费洞察、花费分析、我最近花钱、钱都花哪了、能省、省钱、有什么可以省、订阅在烧钱、账单洞察）。计划固定为单步 LOW 只读工具 spending_insights，绝不自动执行转账、申购等写操作。
- 确定性回答：insight 回复 ≤180 字，覆盖本月支出与环比、最大分类、可省金额（月/年）、预算预测，所有数字只来自工具返回；run_turn 结果新增顶层 insight 字段（含 savings 建议数组，仅作建议）；固定周期扣费会单独说明，不计入可省金额。
- SSE 逐字流式：事件序 answer_start → answer_delta ×n（每块 12 字符，i 从 0）→ answer_end → [insight_card] → done；块间延时由 ZS_STREAM_DELAY_MS 控制（缺省 18ms，测试传 0）。请求体 stream_text=false 时退回旧的整段 message 事件，老客户端不用改。
- POST /api/turn/cancel：按 thread_id 停止输出，返回 {cancelled, emitted_chunks}；流式响应补 answer_end（cancelled=true）与 note「已停止输出」；未在流式时返回 {cancelled:false, reason:"not_streaming"}。
- 审计新增事件 answer_cancelled（tool=stream，risk=low，payload 只含 thread_id / emitted_chunks / chars_total），取消不撤销已执行的工具。
- 新增任务 t47–t56（消费洞察统计口径、可省金额、异常明细、预算预测、SSE 事件序、取消、非流式兼容、LLM 模式洞察等）。

### Changed

- VERSION 由 0.4.1 升为 0.5.0；/api/health 的 version 随仓库根 VERSION 变化，需重启进程生效。
- policy：spending_insights、detect_subscriptions 加入 LOW_TOOLS（只读、无需审批）。
- engine：build_plan 增加 insight 单步计划；_exec_tool 接入两个只读工具；build_reply 增加洞察摘要；_finalize 只在 insight 意图且拿到结果时附 insight 字段。nlu 增加一条最小洞察规则（契约 A 未列 nlu，属最小偏离，旧 t01–t46 关键词实测 0 命中）。

### Fixed

- 洞察 top_categories 的 pct 原按「占支出总额的百分比」计算会出现负数，改为份额百分比（如购物 47%、居住 19%）。
- 流式模式下过滤 message 事件，避免前端先整段渲染、再逐字追加造成重复气泡。

### Security

- 洞察是纯只读能力：计划里只有 spending_insights / detect_subscriptions 两个 LOW 工具，savings 只作建议，不会触发任何资金动作、审批或撤销。
- 取消只停止文本输出，不改变已执行工具的结果；审计事件 answer_cancelled 记录取消发生，供回溯。
- 技能卡与 LLM 提示词提示一律不得放宽风控或放行审批（沿续 v0.4.1 边界）。

### 兼容性与验证状态

- 增量契约：/api/chat 既有字段不变（新增可选 stream_text）；SSE 旧客户端仍收到 message + done；非流式 /api/chat 响应新增 insight（仅 insight 意图出现）。
- 本机实测（2026-10-28）：py_compile 6 文件通过（insight.py / engine.py / server.py / nlu.py / policy.py / llm.py）；scripts/run_tasks.py 56/56 通过、0 失败；frontend/_selftest.js PASS=238 / FAIL=0；scripts/e2e_stream.py 33/33 通过（逐字事件序与拼接、stream_text=false 退回 message、不存在 thread_id 返回 not_streaming、中途取消 emitted_chunks 与 answer_end.cancelled=true、note 已停止输出、审计恰 1 条 answer_cancelled、取消后余额不变且审计链 valid、端口无残留）。洞察回答实测 178 字（按每块 12 字符为 15 块，末块可短），full_stream_s 0.61。
- 文档：README.md 与 docs/（api.md 升 v1.7、security.md、evaluation.md）同步 0.5.0——新增消费洞察、订阅识别、逐字流式与 POST /api/turn/cancel 的契约与安全口径，评测说明改为 56 条并补流式 e2e 33/33 与前端自检 238 项；历史版本小节保留当时数字。
- 说明：detect_subscriptions 在种子数据上会识别 10 条周期扣款（含房租 / 电费 / 话费 / 还款 / 储蓄转账，均满足契约周期规则）；savings 只取已登记订阅或 category=订阅，故 monthly potential 仍为 ¥255.00。

## [0.4.1] - 2026-10-28（进行中，未打 tag）

v0.4.1 是安全加固批次：把挂失 / 解挂从「确认」升为红色强验证，引入 TOTP / 短信 / 人脸三种因子，并对连续可疑或被拦的会话做临时锁定。资金工具、审批 TTL（300 秒）、撤销窗口（60 秒）与审计哈希链均不改动；新增能力全部是增量，缺省行为与 v0.4.0 兼容。

### Added

- 多因子实现 backend/app/second_factor.py（95 行，纯标准库、内存态、永不抛异常）：sms_send / sms_verify（6 位数字，默认 TTL 120 秒，单次使用，连续 5 次错误即作废）与 face_scan / face_verify（fc_ 前缀令牌，默认 TTL 60 秒，单次使用）。
- GET /api/demo/sms：返回当前短信验证码与剩余秒数；POST /api/demo/face：返回人脸确认令牌与剩余秒数。两者都受 DEMO_MODE 控制，关闭时 403（DEMO_MODE_DISABLED）。
- POST /api/approval 新增可选字段 method（totp / sms / face，缺省 totp）；审批对象新增 factors 数组（红色动作 ["totp","sms","face"]，其余为空数组）。
- 会话安全锁定 backend/app/locks.py（182 行）：同一会话在 10 分钟窗口内累计 3 次 attack_blocked 或 turn_blocked 即锁定 300 秒；锁定期间 /api/chat 直接返回 status=locked、code=SESSION_LOCKED。
- 新增表 session_locks；/api/admin/reset 会一并清空。
- 新增用例 t44 挂失银行卡（红色强验证）、t45 连续被拦触发会话锁定、t46 连续注入触发会话锁定。
- 前端：高风险审批卡新增「本次校验方式」三选一（动态码 / 短信 / 人脸），短信演示码一键填入、人脸令牌显示有效期；新增会话锁定 InfoBar（剩余秒数倒计时、禁用输入与发送、归零自动恢复）。

### Changed

- policy：set_card_status 从 MEDIUM_TOOLS 移入 HIGH_TOOLS，并新增规则 card_status_change（level=high，detail 带卡号后四位）；挂失 / 解挂统一走 totp 挑战，审批对象 factors 提示前端渲染三因子选择器。
- t16 冻结信用卡、t18 解冻信用卡的期望从 medium/confirm 改为 high/totp；两项任务本身仍通过。
- 评测任务从 43 条增至 46 条：card 由 6 增至 7（t44），security 由 8 增至 10（t45、t46）；类别总数仍为 8。
- VERSION 由 0.4.0 升为 0.4.1；/api/health 与插件 version 读仓库根 VERSION，需重启进程生效。
- 新增 SSE 事件 security_lock；审计新增事件 security_locked（tool=locks，risk high）、security_unlocked（risk medium）与 turn_locked（tool=security_lock）。

### Security

- 红色动作多因子：挂失 / 解挂等 HIGH 动作用动态码、短信或人脸任一因子确认；未传 method 时按 totp 处理，保证旧客户端可用。
- 因子校验在 approvals.check 内完成：TOTP 走 RFC 6238（窗口 ±1），短信与人脸走 second_factor，错误分别返回 INVALID_OTP / INVALID_SMS / INVALID_FACE，未知方式返回 INVALID_METHOD；审批单保持 pending 可在 TTL 内重试。
- 会话锁定只由代码层事件触发（注入或计划 / 策略拦截），模型输出无法触发也无法解除；锁定时回复明确说明「锁定期间不会执行任何资金动作」。
- 锁定与多因子都不改变资金边界：审批 TTL、金额阈值、幂等键、审计链照旧。

### 兼容性与验证状态

- 增量契约：/api/chat、审批、/api/audit、/api/plugins 既有字段不变；approval 只新增 factors，POST /api/approval 只新增可选 method。
- 本机实测（2026-10-28）：scripts/run_tasks.py 46/46 通过、0 失败；node frontend/_selftest.js PASS=206 FAIL=0（v0.4.0 基线 190，新增多因子 13 + 锁定 3）；scripts/security_factor_e2e.py 53/53（环境 4、挂失红 7、method 分发 8、人脸 4、短信 3、TOTP 3、锁定 10、隔离与重置 4、冷却恢复 4、收尾 6）。
- QA 最终回归（audit/qa-report-v0.4.1.md，2026-10-28 00:14:00–00:16:24，227 行 / 19,607 B）：17 步全部 exit 0、FREEZE_DIFF=none（85 文件 SHA 前后一致）、PORT_RESIDUE=none、VERSION_AT_END=0.4.1；capability_e2e 63/63（审计 valid checked=105）、smoke_normalize 10/10、skill_check 6/6、mcp_e2e 7/7、channel_e2e 9/9、plugin_e2e 17/17、push_e2e 13/13、channel_wecom_e2e 13/13、smoke18 18 轮（审计 valid checked=52、事件 84 条）、verify_audit valid（默认库 22 条）、smoke_llm_fallback 6/6、llm_e2e 9/9 场景 79/79。报告建议冻结 v0.4.1。

### 已知限制

- 短信验证码与人脸令牌只存在进程内存中，主服务重启即全部失效；多实例部署时各进程各自一套，无法共享。
- 人脸因子是「发放令牌 + 校验令牌」，不涉及真实生物识别与活体检测；短信也不真正外发，演示接口直接返回内容。
- 会话锁定按 session_id 计数，用户新建会话即可绕过；没有 IP、设备或用户级的封禁与告警。
- 多因子只覆盖 challenge=totp 的红色动作，MEDIUM 仍是单次确认；没有「必须两个因子同时通过」的复合校验。

## [0.4.0] - 2026-10-28（进行中，未打 tag）

v0.4.0 把「钱怎么花得更明白」补齐：新增 6 个意图（wealth_redeem / card_limit / card_spend / anomaly_scan / annual_report / scheduled_transfer），对外表现为理财赎回、卡限额调整、刷卡消费、异常交易扫描、年度账单、手机号转账与预约转账 7 类能力。资金动作仍然全部由确定性工具执行，policy 三级闸门、审批 / TOTP、撤销窗口与审计哈希链均不改动；预约转账只在创建时审批一次，到期由进程内调度器以幂等键执行并写审计。

### Added

- 6 个意图：wealth_redeem（理财赎回）、card_limit（调整卡限额）、card_spend（刷卡消费）、anomaly_scan（异常交易扫描）、annual_report（年度账单）、scheduled_transfer（预约转账，slots.op = create / list / cancel）。
- 8 个工具：redeem_wealth、set_card_limit、card_spend、scan_anomalies、annual_report、create_scheduled_transfer、list_scheduled_transfers、cancel_scheduled_transfer。
- 理财赎回 redeem_wealth：按持仓先进先出扣减，本金回到活期账户；错误码 AMOUNT_INVALID / INSUFFICIENT_HOLDING / ACCOUNT_NOT_FOUND；返回 product_name、amount_display、balance_after_display、action_id。
- 卡限额 set_card_limit：可改日累计与单笔限额，返回 before / after；限额小于等于 0、或单笔限额大于日累计时返回 LIMIT_INVALID。
- 刷卡消费 card_spend：虚拟卡 / 借记卡 / 信用卡均可，按卡类型与单笔、日累计限额校验；错误码 CARD_NOT_FOUND / CARD_FROZEN / AMOUNT_INVALID / CARD_SINGLE_LIMIT / CARD_DAILY_LIMIT；成功写 card_txns 并返回当日已用与剩余额度。
- 异常交易扫描 scan_anomalies：默认近 30 天，三条规则 amount_outlier（单笔超阈值，阈值取 1000 元与 P99 的较大者）、duplicate_charge（24 小时内同收款方同金额）、late_night_large（0–5 点且超过 500 元）；返回扫描笔数、可疑列表（最多 10 条）、规则计数、P99 与阈值。
- 年度账单 annual_report：按年汇总收入、支出、结余、储蓄率、笔数、月度分布、分类、Top 商户、支出最高月份与月均支出。
- 手机号转账：payees 表新增 phone 列，find_payee_by_phone 忽略空格与横线匹配通讯录；命中后复用 create_transfer，收款人核验规则不变。
- 预约转账：create_scheduled_transfer 校验金额与时间后登记 pending（不扣款），list_scheduled_transfers 支持按状态过滤，cancel_scheduled_transfer 可撤销未执行的预约；新增 scheduled_transfers 表与 card_txns 表。
- backend/app/scheduler.py：默认开启（SCHEDULER_ENABLED=1）、每 5 秒检查一次（SCHEDULER_INTERVAL=5）；到期预约经 mock_core.execute_due_transfers 执行，幂等键 sched_<sched_id>，成功写 scheduled_transfer_executed（risk medium）、失败写 scheduled_transfer_failed（risk high）。
- GET /api/scheduled：status 白名单 scheduled / pending / executed / failed / cancelled（scheduled 与 pending 归一），limit 1–50 默认 6，返回 scheduled、total、offset、limit、has_more。
- POST /api/scheduler/tick：手动触发一次到期检查，返回 checked、executed、failed、ts；不接受 body 指定时间。
- 种子数据升级：SEED_VERSION 2026.10.29，新增 4 条固定演示异常流水 DEMO_ANOMALY_001..004，收款人补手机号（妈妈 13800001111、房东王姐 13900002222、李明 13700003333）。
- 新增用例 t37 理财赎回、t38 卡日累计限额、t39 刷卡超单笔限额拦截、t40 异常扫描、t41 年度账单、t42 手机号转账、t43 预约转账创建 + 列表；评测任务 36 → 43 条（8 个类别不变）。

### Changed

- VERSION 由 0.3.2 升为 0.4.0；/api/health 与插件 version 读仓库根 VERSION，需重启进程生效。
- 评测任务从 36 条增至 43 条，分布为 bills 6 / transfer 8 / wealth 5 / card 6 / subscription 4 / cross 4 / security 8 / mcp 2。
- 前端自检从 182 项增至 190 项：计划卡工具名全部中文化，首页快捷指令补新能力示例。
- policy hits 保持六字段 {rule, dimension, level, detail, value, threshold}；新增规则名 card_limit_change、card_limit_invalid、scheduled_transfer_created，异常扫描另在数据层输出 amount_outlier / duplicate_charge / late_night_large。
- 模型模式支持 v0.4.0 六类新意图：ALLOWED_INTENTS 由 17 增至 23 并补中文别名；SYSTEM_PROMPT 由 1739 增至 2395 字符，新增 single_limit_yuan / year / schedule_hint / op / merchant 五个字段与六意图说明；_normalize 处理新槽位，并新增两条「规则优先于模型改判」护栏。本地 stub 桩实测六类意图全部 source=llm、intent 正确。

### Fixed

- 前端计划卡此前会显示英文工具名（如 create_transfer），v0.4.0 统一映射为中文名称，未知工具回落原样显示。

### Security

- 三级矩阵补入新工具：异常扫描、年度账单、预约列表为 LOW 自动放行；理财赎回、卡限额调整、撤销预约为 MEDIUM 需确认；刷卡消费、预约创建为 HIGH 需 TOTP（预约创建金额在 1000 元以内时按 medium 走确认）。
- 参数级拦截前移：限额小于等于 0 或单笔大于日累计直接返回 LIMIT_INVALID，刷卡超单笔 / 超日累计返回 CARD_SINGLE_LIMIT / CARD_DAILY_LIMIT，均不进入资金执行。
- 预约转账的安全边界：创建时完成一次审批（确认或 TOTP），到期由调度器自动执行、不再二次打扰；执行走与手动转账相同的工具与审计，幂等键 sched_<sched_id> 防止重复扣款。
- 手机号转账不放宽收款人核验：命中通讯录仍按 payee_known / payee_new / payee_unverified 规则判定，陌生手机号首笔仍需强验证。
- 只读能力不写账务：异常扫描与年度账单只读流水与卡流水，自动放行，但仍在事件与审计中留痕。

### 兼容性与验证状态

- v0.4.0 不改 /api/chat、审批、/api/audit、/api/plugins、/api/push 等既有契约；新增接口与字段均为增量。
- 本机实测（2026-10-27）：scripts/run_tasks.py 43/43 通过、0 失败；node frontend/_selftest.js PASS=190 FAIL=0。QA 全量回归（audit/qa-report-v0.4.0.md）17 项全绿、15/15 步 exit 0、0 FAIL：capability_e2e 63/63、llm_e2e 9/9 场景 79/79、smoke18 18/18（审计 valid，checked=81）。

### 已知限制

- POST /api/scheduler/tick 不接受 body 里的 now / now_iso，时间统一取服务端当前时间，因此无法手工回放到期时刻；演示时可等待间隔或调小 SCHEDULER_INTERVAL。
- 预约转账只在创建时审批一次，到期自动执行不再二次确认——这是预约语义的一部分；若需要到期前二次确认，应另加「到期前推送确认」流程，当前未做。
- 跨场景提醒的标题与时间仍硬编码生日模板（engine.py 既有问题），不影响资金动作。
- 调度器是进程内后台线程，进程退出即停止；多实例部署时各进程都会 tick，幂等键可防重复扣款，但应保证只有一个常驻调度进程更稳妥。
- 无浏览器像素级验证，?theme=classic 主题未做；前端自检为 DOM 行为断言，不等于视觉验收。

## [0.3.2] - 2026-10-28（进行中，未打 tag）

v0.3.2 把渠道从「入站 webhook」扩展成「双向触达」：新增企业微信回调解密/验签适配器、主动推送 API、手机端收口与可安装 PWA。资金链路、策略门、审批、TOTP 与审计哈希链均不改动；新增的推送动作只读账务数据，且每次都写审计。

### Added

- 企业微信回调适配器：backend/channels/wecom.py（722 行）与 backend/channels/wecom_crypto.py（316 行，纯标准库实现 AES-128-CBC / PKCS#7 / SHA-1 验签）。默认端口 8401；GET /wecom 处理 URL 验签与 echostr 解密，POST /wecom 验签、解密 XML、转发主服务并把回复加密成被动回包；GET /health 返回 token_set / aes_key_set / corp_id_set / dedupe_size / dedupe_hits 等配置摘要，不返回密钥。
- 企业微信配置项：WECOM_PORT、WECOM_HOST、WECOM_TOKEN（为空回退 CHANNEL_TOKEN）、WECOM_AES_KEY（43 位 EncodingAESKey）、WECOM_CORP_ID、WECOM_ENABLED、WECOM_TIMEOUT（默认 5 秒）、WECOM_DEDUPE_SECONDS（默认 300 秒）。
- POST /api/push 主动触达：kind 支持 subscription / large_txn / repayment（空对象默认 subscription）；返回 ok / kind / user_id / title / text / pushed / delivered / channel（configured、status、url_masked、latency_ms、error）/ audit_seq / ts；渠道状态三态 not_configured / delivered / failed。
- backend/app/push.py：三类文案生成（订阅扣费、大额支出、信用卡还款到期）与投递；每次推送写 event_type=push_message、tool=push、actor=system、risk_level=low 的审计，args 只记 kind / user_id / channel_configured。
- PWA：frontend/manifest.webmanifest（standalone、4 条 icons 含两个 maskable，另 apple-touch-icon 共 5 个 PNG、theme #0B5FD0）与 frontend/sw.js（外壳预缓存、/api/ 永不缓存、导航 network-first 回落到 index.html）。
- 手机端收口：≤720px 顶栏只留品牌、当前页名与「更多」，模式切换移入抽屉；底部 tabbar 四个入口（工作台 / 技能 / 插件 / 设置，56px + 安全区）；审批弹层按 visualViewport 限高。
- 设置页「模拟推送」卡：三种 kind、一键预览、四态结果（已推送 / 未配置 / 失败 / 服务暂时不可用），结果同时写审计。
- 测试脚本：scripts/push_e2e.py（13 项断言，含 PWA 静态资源 3 组）、scripts/channel_wecom_e2e.py（13 项断言）、backend/channels/wecom.py --selftest（9 项内置自检）。

### Changed

- VERSION 由 0.3.1 升为 0.3.2；/api/health 与插件 version 随仓库根 VERSION 变化，需重启进程生效。
- 前端自检从 134 项增至 182 项，新增 PWA、手机端收口与模拟推送断言。
- 渠道文档从「IM 入站适配」升级为「双向触达」：三种渠道形态（Web / IM webhook / 企微回调）、统一消息处理入口、主动触达与三态结果。
- README 启动说明从三进程改为四进程：主服务 8000（必需）、IM 渠道 8400（可选）、企微回调 8401（可选）、外部 fx/risk 8300/8301（可选），并写明各自不起时的降级表现。

### Security

- 企微回调强制验签：sha1(sorted([token, timestamp, nonce, encrypt])) 与 msg_signature 做 hmac.compare_digest；签名失败 403，不进入解密与业务。
- 消息体 AES-128-CBC 解密（IV = key，PKCS#7 补位到 32 字节），封装含 random(16) + msg_len(4) + msg + corp_id，CorpID 不符视为非法消息。
- 回调入口限流式防护：请求体上限 1MB（413）、Token / AESKey / CorpID 未配置 503、非法 XML 或缺 Encrypt 400、解密失败 400，异常统一降级为可读文案，不泄露内部错误。
- 重放与重复投递：MsgId 去重窗口默认 300 秒（WECOM_DEDUPE_SECONDS），命中时返回上次回复；渠道进程另有 10 秒 user_id + text 幂等。
- 回包白名单：只返回 reply 或 needs_confirmation + preview + hint，绝不带 approval_id、nonce、thread_id 或 confirmation.token。
- 主动推送：CHANNEL_PUSH_URL 只以 scheme://host/*** 形式展示（url_masked）；推送失败不伪造成功，不改变账务；审计记录推送标题与结果，不记录完整渠道地址与凭据。
- 密钥边界：WECOM_TOKEN / WECOM_AES_KEY / CHANNEL_PUSH_URL 只从环境变量读取，日志、/health、/api/channel/status 只返回是否配置与掩码，不回显明文。

### 兼容性与验证状态

- v0.3.2 不改 /api/chat、审批、/api/audit、/api/plugins 等既有契约；新增接口与字段均为增量。企微回调是独立进程，不起不影响主服务、Web 页面与 36 条评测。
- 本机实测与 QA 全量回归（audit/qa-report-v0.3.2.md，2026-10-27）：15/15 步 exit 0、0 FAIL——run_tasks 36/36、smoke_normalize 10/10、skill_check 6/6、mcp_e2e 7/7、channel_e2e 9/9、plugin_e2e 17/17、push_e2e 13/13、channel_wecom_e2e 13/13、smoke18 18/18（审计 checked=81、事件 111）、verify_audit valid、前端 PASS=182 FAIL=0、smoke_llm_fallback 6/6、llm_smoke 两态 exit 0、llm_e2e 8/8·52/52；QA 结论：全绿、建议冻结 v0.3.2。

### 已知限制

- 企微适配器只在本机 Mock/自检环境验证，未接入真实企业微信后台；真实环境的可信域名、IP 白名单、加解密库差异仍需联调。
- 只处理 MsgType=text：图片、语音、文件、事件回调统一回「暂时只支持文本消息」；不做富文本卡片与群聊。
- MsgId 去重表在进程内存，重启即清空；多实例部署时各进程各自去重，需要外部去重存储。
- PWA 的 Service Worker 只在 localhost 或 HTTPS 下生效；局域网 HTTP 访问可正常使用页面，但不会注册离线缓存。
- 主动推送是演示能力：不真正发短信/企微消息，只把内容 POST 到 CHANNEL_PUSH_URL；未配置时返回 not_configured。
- 版本号在进程启动时读取仓库根 VERSION，升级需重启；企微与 IM 渠道的 token 仍无轮换与告警。

## [0.3.1] - 2026-10-28（进行中，未打 tag）

v0.3.1 只加固模型接入链路：把思考模式默认关闭、收紧超时与输出预算、补重试与熔断、把错误翻译成中文，并给设置页一个「测试连接」入口。规则解析、策略门、审批、TOTP、审计与资金边界均不改动；模型仍只是不可信的解析器。

### Added

- POST /api/llm/test：模型连通性测试，恒返回 HTTP 200，成功用 ok / latency_ms / usage / cost_yuan / result 表达，失败用 error / hint / code（真正发出请求的失败另带 error_code / error_hint）表达；不写审计链、不计入业务会话。它是一次真实模型调用，会计入进程内 stats 与连续失败计数；无 Key 时本地短路，不发起请求。
- 设置页「模型接入」卡新增「测试连接」按钮，展示当前模式、实际生效通道、Key 掩码、思考模式、超时/预算、最近一次调用的 tokens 与耗时，以及熔断状态。
- GET /api/llm/status 新增字段：stats（调用次数、成功/失败、tokens、缓存命中、最近耗时）、breaker（连续失败次数、阈值、是否熔断、冷却剩余秒数）、last_error_code、last_error_hint；熔断打开时 degraded=true（单次失败未达阈值不算降级）。
- scripts/llm_stub_server.py：零依赖 OpenAI 兼容测试桩，可模拟 ok / 401 / 402 / 404 / 429_once / 429_always / 500_always / empty_once / badjson_once / slow，并记录每次请求体，用于校验 thinking、max_tokens、system 前缀与技能提示位置。
- scripts/llm_e2e.py：无需 Key 的桩测试（8 个场景、52 项检查），覆盖成功链路、请求体契约、401 不重试、429 重试、空 content 重试、5xx 熔断与冷却恢复、超时回退、测试连接接口。
- scripts/llm_smoke.py：有 Key 时的一条命令冒烟，打印厂商、模型、超时/预算、思考模式、最大输出与一次真实调用结果。

### Changed

- 思考模式默认关闭（DEEPSEEK_THINKING=disabled，请求显式携带 thinking={"type":"disabled"}）：DeepSeek 官方文档中思考模式默认开启且 effort=high，思维链会占用输出预算、拉长单次耗时并抬高成本；本项目只做 JSON 抽取，不需要推理链，且思考模式下 temperature 不生效。
- 单次模型调用超时由 12 秒收紧为 8 秒（DEEPSEEK_TIMEOUT=8）；新增单轮总预算 10 秒（DEEPSEEK_BUDGET=10），一次解析内的重试与等待都计入预算。
- max_tokens 由 600 提升到 800（DEEPSEEK_MAX_TOKENS=800），避免 JSON 被截断，同时仍限制输出成本。
- 系统提示词固定为稳定前缀，命中的技能卡提示移入 user 消息，不再追加到 system：同一进程内 system 前缀保持不变，提升上下文缓存命中率，降低输入费用。
- VERSION 由 0.3.0 升为 0.3.1。

### Fixed

- 空 content 与 JSON 截断：解析失败时重试一次（DEEPSEEK_RETRY=1），重试间隔 600ms（DEEPSEEK_RETRY_BACKOFF_MS=600）。
- 429 与 5xx：按同一重试策略快速重试，避免一次限流直接回退规则。
- 错误码中文提示：401（Key 无效）、402（余额不足）、404（模型或接口不存在）、429（限流）、5xx（服务异常）、超时、返回非 JSON、空 content 统一映射为中文 error_hint。
- 连续失败熔断：同一进程连续 3 次失败（DEEPSEEK_BREAKER_THRESHOLD=3）后短路，冷却 60 秒（DEEPSEEK_BREAKER_COOLDOWN=60）内直接走规则解析，冷却结束自动恢复尝试。

### Security

- API Key 仍只从环境变量 / .env 读取，不写入仓库、数据库、事件或审计；错误信息与日志不回显 Key，状态接口只返回 key_masked。
- 模型调用不进入审计明文：response 审计只写 intent 名称与结果来源（llm / fallback / rule），intent 事件写 model 与 latency_ms；tokens、缓存命中与成本只出现在 GET /api/llm/status 的 stats 中，不进入哈希链。提示词、用户原话与模型原始输出均不落审计。
- 重试与熔断都发生在解析层，失败只会回退规则解析；策略门、审批、TOTP 与哈希链审计照常执行。

### 兼容性与验证状态

- /api/llm/status 原有字段（mode / effective / enabled / provider / model / base_host / key_masked / last_error）全部保留，新字段为增量字段，旧客户端可忽略。
- 失败路径语义不变：单次真实失败回退规则解析，intent.source=fallback 并写 last_error；连续 3 次失败后熔断，冷却期内短路为 intent.source=rule（未调用模型），与 fallback 区分。新增的 last_error_code / last_error_hint 只是更易读的补充。
- v0.3.1 实测（2026-10-27 本机）：scripts/llm_e2e.py 8/8 场景、52/52 项检查通过（桩服务，无需真实 Key）；scripts/run_tasks.py 36/36 通过；node frontend/_selftest.js PASS=134 FAIL=0；smoke_llm_fallback 6/6、llm_smoke exit 0。正式 QA 报告 audit/qa-report-v0.3.1.md 已出：13 项验证全绿、0 FAIL，报告建议冻结 v0.3.1。

### 已知限制

- 关闭思考模式后，极少数需要多步推理的复杂口语表达可能解析不如思考模式，但失败会回退规则解析，不影响资金安全；如需更强推理可显式设 DEEPSEEK_THINKING=enabled，代价是更慢更贵。
- 成本估算按 DeepSeek 官方空闲时段单价（缓存命中输入 0.02 元/百万 tokens、未命中输入 1 元/百万 tokens、输出 4 元/百万 tokens）：cost = hit×0.02/1e6 + miss×1/1e6 + completion×4/1e6。实测样例 hit=1024、miss=156、completion=96 得 ¥0.00056048，单次约 0.56 厘，1000 次约 ¥0.56；高峰时段（北京时间周一至周五 9:00–12:00、14:00–18:00）翻倍，约 ¥1.12。实际费用以账单为准。
- 熔断状态与 stats 只存在当前进程内，重启清零；多进程部署时各自独立计数。

## [0.3.0] - 2026-10-28（进行中，未打 tag）

v0.3.0 在 v0.2.0 基础上加入「插件注册表」与 /api/tasks 分页，属于能力可观测性与接口完善批次；资金安全语义不变。

### Added

- 插件注册表：backend/app/plugins.py（335 行），REGISTRY 七项（policy、audit、mcp-fx、mcp-risk、intent-llm、skill-pack、channel-im）；五类 kind（core、mcp、model、skill、channel）；core 两项不可停用（toggleable=false，locked_reason「核心能力不可停用」）。
- 新路由：GET /api/plugins（全量注册表 + 运行态 + 计数，永不抛异常）、POST /api/plugins/<id>/toggle（启停非核心插件）。
- 插件状态持久化：PLUGINS_FILE（默认 backend/data/plugins.json），文件不存在/损坏视为全部启用；写入用临时文件 + os.replace 原子替换；POST /api/admin/reset 恢复全启用；该文件已被 .gitignore 忽略，不进仓库。
- 审计事件 plugin_toggled（tool=plugins.toggle，args/result 记录 plugin_id 与 enabled，risk_level=low），纳入既有哈希链。
- GET /api/tasks 支持分页参数 limit（默认 6，范围 1-50）与 offset（默认 0）：返回 tasks / total / offset / limit / has_more；越界返回空数组且仍为 200。

### Changed

- 无参数 GET /api/tasks 行为不变，仍返回 { "tasks": [全部 36 条] }，老前端与脚本无需改动。
- GET /api/mcp/status：对应插件停用时该项 status="disabled"、ok=false、degraded=true、last_error="插件已停用"；顶层 ok 仍为 true。
- GET /api/skills：skill-pack 停用时返回 { "count": 0, "skills": [], "disabled": true }。
- GET /api/channel/status：channel-im 停用时 enabled=false、sessions 按 0；GET /api/llm/status 在 intent-llm 停用时 effective 恒为 rule 并带 plugin_disabled=true。
- 对话回复在 mcp-fx / mcp-risk 停用时分别加「汇率插件已停用」前缀与「外部反欺诈核查未执行（插件已停用），policy 安全规则仍然生效」尾注。

### Security

- 停用插件只减少功能、不放宽任何资金边界：policy 与 audit 为 core 不可停用；mcp-risk 停用后转账仍完整走 policy；intent-llm 停用后解析回退规则。
- toggle 接口严格校验请求体（非对象/enabled 非布尔值 → 400 INVALID_BODY），未知 id → 404，core → 403，非法请求不写状态、不写审计。
- 插件状态文件损坏或缺失时按「全部启用」处理，避免因配置文件异常阻断业务；状态写入失败不影响进程内已生效的开关。

### 兼容性与验证状态

- 旧接口字段全部保留：无参 GET /api/tasks 与 v0.2.0 完全一致，插件字段为新增字段，旧客户端可忽略。
- 2026-10-27 后端 v0.3.0 冻结后本地全量复跑：scripts/run_tasks.py 36/36 通过（退出码 0）；scripts/plugin_e2e.py 17/17 通过（覆盖注册表结构、toggle 400/403/404、停用持久化、mcp/skills/channel/llm 停用态、风险核查跳过、审计 plugin_toggled 与哈希链）。
- QA 回归（audit/qa-report-v0.3.0.md，2026-10-27 21:16 最终批次）9 项全绿、0 FAIL：run_tasks 36/36、smoke18 18/18（审计链 valid，checked=81，事件 111 条）、verify_audit valid 13 条、smoke_normalize 10/10、skill_check 6/6、mcp_e2e 7/7、channel_e2e 9/9、前端 node frontend/_selftest.js PASS=100 FAIL=0、plugin_e2e 17/17。

### 已知限制

- 插件 version 字段在进程启动时读取仓库根 VERSION，改 VERSION 后需重启才在 /api/plugins 中体现。
- v0.3.0 的完整 QA 数字（任务通过数、接口回归、前端截图）以 audit/qa-report-v0.3.0.md 为准；该报告已于 2026-10-27 出具，结论为全绿、0 FAIL，前端截图留档仍待人工执行。
- 插件状态是本地 JSON 且无鉴权，可被本机进程改写；停用只减少能力，policy、审批、TOTP 与审计不可停用。

## [0.2.0] - 2026-10-27（进行中，未打 tag）

### Added

- MCP 外部只读服务：backend/app/mcp_client.py（JSON-RPC 2.0 子集，initialize → notifications/initialized → tools/list 缓存 60s → tools/call，工具白名单，失败不重试）；两个标准库服务 backend/external/fx_server.py（fx_convert，8300）与 risk_server.py（risk_screen，8301），支持 --port / --slow；backend/external/selftest.py 五场景自检。
- Skill 技能包：backend/app/skills.py + skills/*.skill.md 六张卡（transfer、bills、wealth、cards、subscriptions、cross），接口 all / get / match / summary / status；参与规则解析、模型提示词（最多 3 张）与页面展示。
- IM 渠道适配层：backend/channels/server.py（GET /health、POST /message、X-Channel-Token 鉴权、32KB 体积上限、两步确认、10 秒幂等缓存、messages.jsonl 留痕、sessions.json 会话映射、回包不含 approval_id/nonce/thread_id）。
- 新意图 query_fx（汇率/换汇/兑换/美元/日元/欧元/港币/兑人民币），规划为单步 fx_convert（low）；transfer 在 MCP 开启时资金步骤前插 risk_screen（low，只加严）。
- policy.evaluate 新增 hits 字段：规则名（amount_single_limit / amount_daily_limit / amount_hard_limit / payee_new / payee_known / payee_unverified / reversed_window / irreversible / frequency_5_in_10min / insufficient_funds / product_not_found / below_min_amount / amount_invalid）+ 带数字的 detail；审批卡用 hits 展示判定依据（按 high>medium>low 取前 3 行，其余折叠「+N 条判定依据」）。
- 新路由：GET /api/mcp/status、GET /api/skills、GET /api/skills/{id}、GET /api/channel/status；既有路由不变。
- 评测任务 t31–t36（外部汇率、外部关闭降级、技能命中、policy hits、注入绕过、账单+提醒跨场景）；脚本 skill_check.py、mcp_e2e.py、channel_e2e.py、smoke_normalize.py。
- 前端 v4（Win11 Fluent，品牌 A）：7 档字号、264/1fr/296 三栏、技能页、外部服务页、设置页渠道卡、审批卡规则行、审计抽屉逐位校验。

### Changed

- nlu 归一化：NFKC、零宽/控制字符清洗、西里尔/希腊同形字映射、注入检测同时检查去空格紧凑串。
- llm.parse 先做 skills.match(text)，命中后把 skills.summary 注入 system prompt；返回值新增 skills。engine 在解析后再用 skills.match(intent) 统一覆写 intent.skills（技能路由失败只置空，不影响主流程）。
- engine：_exec_tool 增加 fx_convert / risk_screen 分支（失败降级内置实现并记 mcp_fallback）；build_plan 调整见上；run_turn 写 policy_hits，_finalize 写 skill_matched，MCP 调用写 mcp_call 审计。
- 评测规模由 30 条扩到 36 条；run_tasks.py 增加可选字段解析。
- 设计规范从 v3（暖白纸感）整体改为 v4（前端方案第 15 节取值）。

### Security

- 外部结果只加严：MCP 返回值不拼进提示词、不降低风险等级；超时或失败时业务照常执行，资金边界不变。
- 渠道层不回传 approval_id / nonce / thread_id；渠道层不做任何业务判定；幂等缓存防重复提交。
- 注入四层防御补充 8 条绕过样本（零宽、全角、同形字、空格拆分等）全部检出。
- 审批 nonce、TTL 300 秒、60 秒撤销、审计哈希链语义不变；v0.2.0 只加字段，不改 t01–t30 判定结果与 blocked code。

### 已知限制

- 跨场景提醒的标题与时间固定为生日模板：backend/app/engine.py:261 生成 create_reminder 时标题固定「<妈妈/对方> 生日提醒：提前准备礼物」、时间固定为下周三（_next_wednesday()）。纯提醒类请求若被判为 cross_scenario，会得到这条模板文案；该路径不在 t01–t36 用例覆盖内。
- 渠道端口需与主服务一致：渠道进程用 --port 实际监听，主服务 /api/channel/status 的 port 读 CHANNEL_PORT；两者不一致时状态页端口与实际监听不符。演示与 channel_e2e.py 均让两边共用同一 CHANNEL_PORT。

### 兼容性与验证状态

- 旧接口字段全部保留，前端可继续使用；新字段对旧客户端无害。
- 三进程（8000/8300/8301）可起，或主服务单起外部自动降级；审计链 valid；渠道两步确认可用。渠道服务与四条新路由已合并并完成文档校对（2026-10-27）。
- 2026-10-26 基线：30/30 通过（Python 3.14.6）。
- 2026-10-27 全量复跑 scripts/run_tasks.py：36/36 通过，退出码 0（含 t31–t36）；backend/app/mcp_client.py（342 行）、backend/app/skills.py（299 行）、backend/external 四个文件与 scripts/mcp_e2e.py（311 行）、skill_check.py（151 行）、smoke_normalize.py（138 行）均已落盘。
- 2026-10-27 自检：backend/external/selftest.py 5/5；scripts/mcp_e2e.py 7/7；scripts/skill_check.py 6/6（含 18 条示例）；scripts/smoke_normalize.py 10/10；scripts/channel_e2e.py 9/9（401 鉴权、两步确认、10 秒幂等、回包无审批凭据、messages.jsonl 落盘）。
- 2026-10-27 文档同步：docs/api.md 四条只读接口改为真实响应字段（mcp/status 的 ok 与 status、skills 的 risk/tool、channel/status 的 token_configured 与 sessions），docs/channel.md 按冻结实现校对，docs/skills.md 收紧，README 目录树与 36 条评测结果更新。

## [0.1.0] - 2026-10-26

首个可运行基线，30 条评测任务实测 30/30。

### Added

- 六阶段流水线：parse_intent → plan → policy_gate → act → verify → respond（backend/app/engine.py）。
- 三级权限矩阵：LOW 自动、MEDIUM 确认、HIGH 走 TOTP、硬上限与余额等直接拦截（backend/app/policy.py）。
- RFC 6238 TOTP（backend/app/totp.py，标准库 HMAC-SHA1、30 秒步长、6 位、±1 窗）。
- canonical JSON + SHA-256 + prev_hash 审计哈希链与 scripts/verify_audit.py。
- 审批挂起与恢复：checkpoint + thread_id，审批 TTL 300 秒；转账/申购 60 秒内撤销。
- 解析三模式（rule / llm / auto），接入 OpenAI 兼容接口（默认 DeepSeek deepseek-flash），失败自动回退规则。
- 提示注入四层防御与安全用例 t26–t30。
- 30 条评测任务（7 类，5/5/4/4/4/3/5）与 run_tasks.py、smoke18.py、compare_modes.py。
- 零依赖单页前端（frontend/index.html、app.js、styles.css）与 v3 设计规范。
