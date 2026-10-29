# ZeroStep API 契约（v1.10）

v1.10（2026-10-28）对应 v0.6.1：不新增业务接口，收口 HTTP 边界与审批语义——CORS 由 `*` 改为 Origin 白名单、Host 校验、请求体 512 KiB 上限（413）、`limit` clamp 1..200（400）、可选 API_TOKEN（401）、演示因子接口只接受本机连接（403 DEMO_LOCAL_ONLY）、审批绑定会话与原子 CAS（§0、§2）；v1.9（2026-10-28）新增 v0.6.0 的 MCP 通用接入字段：/api/mcp/status 每个服务新增 label / transport / auth{type,configured,token_cached} / session / read_only，顶层可选 config_error；配了 MCP_SERVERS 时 /api/plugins 额外出现 mcp-<name>（纯增量，无破坏性变更）；v1.8（2026-10-28）新增 v0.5.1 的 /api/session wealth 字段与 accounts.kind 字段口径（§1，纯增量，无破坏性变更）；v1.7（2026-10-28）新增 v0.5.0 的消费洞察与逐字流式：只读工具 spending_insights 与 detect_subscriptions 的结果结构（§4）、insight 意图与 insight_card 事件、/api/chat 的 answer_start / answer_delta / answer_end 事件序（含 stream_text=false 降级）、POST /api/turn/cancel 与 answer_cancelled 审计（§2、§5、§7）；v1.6 的 v0.4.1 多因子强验证与会话锁定（审批 factors、POST /api/approval 的 method、GET /api/demo/sms、POST /api/demo/face、status=locked 与 security_lock 事件）、v1.5 的预约与调度、v1.4 的主动触达、v1.3 的 LLM 状态、v1.2 的插件注册表与分页均保持不变。

零依赖 Python 标准库实现。所有接口返回 JSON（UTF-8），金额单位一律为「分」（整数），前端负责格式化。

## 0. 通用约定

- Base URL: http://127.0.0.1:8000
- 认证：默认演示模式无登录，用 session_id 区分会话；设置 `API_TOKEN` 后，除 GET /api/health 外所有请求必须带 `X-ZeroStep-Token`，不匹配返回 401 {"error":{"code":"UNAUTHORIZED",...}}。
- 浏览器边界（v0.6.1）：响应不再固定 `Access-Control-Allow-Origin: *`，只有 Origin 在 `CORS_ALLOW_ORIGINS`（默认 http://127.0.0.1:8000,http://localhost:8000）内才回显该 Origin 并带 `Vary: Origin`；OPTIONS 预检只对允许的 Origin 返回 200。带未授权 Origin 的写请求直接 403 CORS_ORIGIN_DENIED；Host 头主机名不在 {127.0.0.1, localhost, ::1} ∪ `HOST_ALLOW` 内返回 403 HOST_DENIED（防 DNS rebinding）。
- 请求体上限 512 KiB：所有 POST 在路由前按 `Content-Length` 校验（覆盖不读 body 的接口），超限 413 {"error":{"code":"PAYLOAD_TOO_LARGE",...}}；分块传输（Transfer-Encoding: chunked）无法预知大小，直接 411 LENGTH_REQUIRED；JSON 解析失败 400 BAD_JSON。
- `limit` 参数统一 clamp 到 1..200，非法整数 400 BAD_REQUEST（不再把 `-1` 当全表或让 int() 抛出 500）。
- 未预期异常只返回 500 {"error":{"code":"INTERNAL_ERROR","message":"服务器内部错误"}}，异常细节只写 stderr 日志，不回给客户端。
- 演示接口（GET /api/demo/totp、GET /api/demo/sms、POST /api/demo/face、POST /api/admin/reset）只接受本机回环连接（127.0.0.1 / ::1），其它来源 403 DEMO_LOCAL_ONLY；`DEMO_MODE=0` 时仍返回 403 DEMO_MODE_DISABLED。
- 错误返回：HTTP 4xx/5xx，body 为 { "error": { "code": "CODE", "message": "中文说明" } }；code 为机器可读常量（如 BAD_REQUEST / INVALID_OTP / INVALID_SMS / INVALID_FACE / INVALID_METHOD / APPROVAL_EXPIRED / DEMO_MODE_DISABLED / BAD_MODE / NOT_FOUND / SKILL_NOT_FOUND / INVALID_BODY / PLUGIN_NOT_FOUND / PLUGIN_CORE_LOCKED）。
- 例外：少数内部异常兜底为 { "ok": false, "error": "服务不可用" }（无 code 层），见对应接口说明。
- 金额：amount_cents 整数，单位分；展示为 ¥1,234.56。
- 时间：ISO-8601 带 +08:00 时区，例如 2026-10-26T21:30:00+08:00。

## 1. 会话

### POST /api/session
请求体：忽略（服务端固定用默认用户 u001 建会话，见 backend/server.py:203；前端 POST 空 JSON 即可）。
返回：
    {
      "session_id": "s_xxx",
      "user": { "user_id": "u001", "name": "张伟", "risk_level": "稳健型" },
      "accounts": [ { "account_id": "acc_001", "kind": "checking", "name": "工资卡", "number_masked": "**** 8821", "balance_cents": 1286050, "currency": "CNY" } ],
      "cards": [ ... ],
      "subscriptions": [ ... ],
      "payees": [ ... ],
      "wealth": { "products": [ ... ], "holdings": [ ... ], "holding_total_cents": 0, "holding_total_display": "¥0.00" },
      "llm_mode": "rule",
      "llm_status": { "mode": "auto", "effective": "rule", "enabled": false, "provider": "DeepSeek", "model": "deepseek-flash", "base_host": "api.deepseek.com", "key_masked": "", "last_error": null },
      "demo_mode": true
    }

说明：accounts 的账户类型字段名是 kind（checking 活期 / credit 信用卡 / savings 储蓄），不是 type；前端 acctKind(a) = a.kind || a.type 兼容旧数据。wealth 为 v0.5.1 新增（见下）。

### GET /api/session/{session_id}
返回当前会话：user / accounts / cards / subscriptions / payees / wealth / pending_approval（可能为 null）/ llm_mode / llm_status（结构见第 3 节）/ demo_mode

wealth（v0.5.1 新增，与 POST /api/session 同一份 session_snapshot）：
    {
      "products": [ ... ],                       // 在售理财产品
      "holdings": [ { "product_id": "p_001", "name": "稳健 30 天", "amount_cents": 1000000, ... } ],
      "holding_total_cents": 1000000,            // 持仓合计，单位分（= sum(holdings.amount_cents)）
      "holding_total_display": "¥10,000.00"
    }

前端在回合结束、审批通过与撤销完成后调用本接口重新渲染资产构成与账户列表（只读，不写库、不触发任何资金动作）；调用失败时保留旧数字并静默，不阻塞对话。字段实现见 backend/app/engine.py:46 session_snapshot 与 backend/app/mock_core.py:253 list_wealth。

### POST /api/admin/reset
重置演示数据库为种子数据（评委一键还原）。返回 { "reset": true, "seed_version": "..." }；DEMO_MODE=0 时返回 403 DEMO_MODE_DISABLED。

实现方式：在服务进程的同一 SQLite 连接上按表 DELETE 全部业务表后重新灌种子（不删除 bank.db 文件），因此可以反复点击「重置演示」，也不会在 Windows 上因文件被占用而失败（早期实现用 os.unlink 会抛 WinError 32）。

## 2. 对话（核心）

### POST /api/chat
请求：{ "session_id": "s_xxx", "text": "帮我查一下这个月账单", "stream": true }

stream=false（默认，脚本/评测用）：
    {
      "session_id": "s_xxx",
      "status": "ok",                  // ok | awaiting_approval | blocked | locked | error
      "reply": "这个月共支出 ¥3,240.50，最大一类是餐饮……",
      "intent": { "name": "query_bills", "confidence": 0.95, "source": "rule", "slots": { "months": 1 } },
      "plan": { "steps": [ { "step_id": "s1", "title": "汇总近 1 个月账单", "tool": "get_bill_summary", "risk": "low" } ], "edges": [] },
      "executed": [ { "step_id": "s1", "tool": "get_bill_summary", "ok": true, "result": {...} } ],
      "approval": null,                // 或审批对象，见下
      "audit": [ { "seq": 12, "event_type": "tool_result", "tool": "get_bill_summary", "risk_level": "low", "hash": "ab12..." } ],
      "thread_id": "t_xxx"
    }

stream=true：Content-Type: text/event-stream，事件按序：
    event: session
    data: {"session_id":"s_xxx","thread_id":"t_xxx"}

    event: intent
    data: {"name":"transfer","confidence":0.96,"source":"rule","slots":{"amount_cents":800,"payee_name":"妈妈"},"model":"","latency_ms":null,"reason":""}

    event: plan
    data: {"steps":[{"step_id":"s1","title":"校验收款人","tool":"get_payees","risk":"low"},{"step_id":"s2","title":"向「妈妈」转账 ¥800.00","tool":"create_transfer","risk":"medium"}],"edges":[{"from":"s1","to":"s2"}]}

    event: tool_start
    data: {"step_id":"s1","tool":"get_payees","args_preview":"收款人：妈妈"}

    event: tool_end
    data: {"step_id":"s1","tool":"get_payees","ok":true,"latency_ms":3,"result":{"payee_id":"p_002","name":"妈妈","is_new":false}}

    event: approval_required
    data: {"approval_id":"ap_xxx","risk_level":"medium","challenge":{"type":"confirm"},"title":"向「妈妈」转账 ¥800.00","amount_cents":80000,"currency":"CNY","counterparty":"妈妈（招商银行 **** 6621）","reasons":["金额在单笔 1000 元以内","收款人是常用联系人"],"preview":"执行后工资卡余额将从 ¥12,860.50 变为 ¥12,852.50","expires_at":"2026-10-26T21:41:00+08:00","nonce":"n_xxx","counterparty_account_masked":"6221 **** 6621","payer_account_masked":"6222 **** 8821","source":"rule","source_detail":"","conflicts":[]}

    event: message
    data: {"delta":"正在为你准备……"}   // 可多次，前端拼接

    event: audit
    data: {"seq":13,"event_type":"approval_required","tool":"create_transfer","risk_level":"medium","hash":"cd34...","prev_hash":"ab12...","ts":"..."}

    event: done
    data: {"status":"awaiting_approval","thread_id":"t_xxx"}

无审批时事件序列为 session → intent → plan → (tool_start → tool_end)* → answer_start → (answer_delta)* → answer_end → [insight_card] → done（v0.5.0 起，见下「逐字流式与中断」）；v0.4.x 及 stream_text=false 时为 message → done。

#### 逐字流式与中断（v0.5.0）

v0.5.0 起 /api/chat 的 stream 分支默认把回答按 12 字符切块逐条下发，块间延时由环境变量 ZS_STREAM_DELAY_MS 控制（默认 18ms，演示用）。事件序列在最后一个 tool_end 之后为：

    event: answer_start
    data: {"turn_id":"turn_abc123","thread_id":"t_abc123","chars":178}

    event: answer_delta
    data: {"turn_id":"turn_abc123","i":0,"text":"近 30 天你一共支出 "}

    event: answer_delta
    data: {"turn_id":"turn_abc123","i":1,"text":"**¥24,484.70**，比上期多 48%。"}

    event: answer_end
    data: {"turn_id":"turn_abc123","chunks":15,"elapsed_ms":270,"cancelled":false}

    event: insight_card
    data: {"insight":{ ... }}

    event: done
    data: {"status":"ok","thread_id":"t_abc123","undo":null,"approval":null}

- 完整回答由 answer_delta 按 i 顺序拼接得到，delta 条数与 answer_end.chunks 相等；answer_start.chars 是回答总字符数（最后一个 delta 可短于 12 字符）。
- insight_card 只在 intent.name=insight 时出现，携带 spending_insights 的完整结果，结构见 §4「v0.5.0 只读工具」。
- 兼容：请求体带 "stream_text": false 时不发 answer_* 与 insight_card，退回旧的整段 message 事件（v0.4.x 行为）；不传或传 true 一律逐字流式。

中断用独立接口，不占用 /api/chat：

    POST /api/turn/cancel
    请求：{ "thread_id": "t_abc123" }

流式输出进行中取消，HTTP 200：

    { "cancelled": true, "emitted_chunks": 4 }

服务端随即停止再发 answer_delta，补发 answer_end（cancelled=true）与 note 事件 { "text": "已停止输出" }，done 与 insight_card 照常下发；同时写一条审计 answer_cancelled（event_type=answer_cancelled，tool=stream，risk_level=low，payload 含 thread_id / emitted_chunks / chars_total）。

thread_id 当前不在流式阶段（已结束、拼错或服务重启后），HTTP 200：

    { "cancelled": false, "reason": "not_streaming" }

body 不是 JSON 对象时返回 400：{ "error": { "code": "BAD_REQUEST", "message": "请求体必须是 JSON 对象" } }。

- 取消只作用于输出：已执行的工具、已写入的账务与幂等键都不回滚，audit 中 tool_end 仍然保留，因此「停止」不会制造半笔交易；要回滚资金动作请走 POST /api/undo（见下）。
- 重复取消不需要特殊处理：首次取消后流式阶段结束，同一 thread_id 再次取消按 not_streaming 返回。
#### 会话锁定（v0.4.1）

同一会话在 10 分钟窗口内累计 3 次可疑或被拦操作（audit 里的 attack_blocked 或 turn_blocked）后，服务端按下述规则锁定该会话：锁定默认 300 秒，锁定期间 /api/chat 不再解析、不建计划、不执行任何工具，直接返回：

    {
      "session_id": "s_xxx",
      "status": "locked",
      "code": "SESSION_LOCKED",
      "reply": "会话因连续可疑指令已被临时锁定，请在 286 秒后再试。锁定期间不会执行任何资金动作。",
      "locked_until": "2026-10-28T21:35:00+08:00",
      "lock_remaining_s": 286,
      "intent": null,
      "plan": { "steps": [], "edges": [] },
      "executed": [],
      "approval": null,
      "undo": null,
      "audit": [ ... ],
      "events": [ ... ]
    }

stream=true 时先 emit message（锁定说明），再 emit security_lock，最后 done 的 status 为 locked。触发锁定的那一轮本身仍返回 status=blocked，只在 reply 末尾追加「（N 分钟内第 K 次被拦操作，会话已临时锁定 300 秒。）」。

- 阈值与时长来自 config：SECURITY_LOCK_THRESHOLD=3、SECURITY_LOCK_WINDOW_SECONDS=600、SECURITY_LOCK_SECONDS=300，均可环境变量覆盖。
- 计数依据是审计表内的会话事件，与模型输出无关；锁表为 session_locks，/api/admin/reset 会清空。
- 状态码：锁定响应按实现是 **HTTP 200**，靠 body 里的 status=locked / code=SESSION_LOCKED 区分（不是 423）；契约草稿曾写 423，以本实现为准。

### 审批对象字段

approval 对象（非流式响应的 approval 与 SSE 的 approval_required 为同一结构）字段：

| 字段 | 说明 |
| --- | --- |
| approval_id / nonce / expires_at | 审批单 ID、防重放随机串、过期时间（默认 300 秒） |
| risk_level / challenge / reasons | 风险级、挑战方式（confirm / totp）、判定理由 |
| title / preview / amount_cents / currency | 预览标题、执行后余额变化说明、金额（分）、币种 |
| counterparty | 对手方摘要（姓名 + 银行 + 卡号脱敏） |
| counterparty_account_masked | 对手方账号/卡号脱敏（超 8 位保留前 4 后 4，否则前 2 后 2） |
| payer_account_masked | 付款账户脱敏 |
| source | 意图解析来源：rule / llm / fallback |
| source_detail | 模型给出的解析理由；规则模式为空串 |
| conflicts | 模型输出与原文或规则冲突的提示数组（如金额回退、金额截断），无冲突为空数组 |
| factors | v0.4.1：红色动作可选的强验证因子数组（["totp","sms","face"]），其他等级为空数组；前端据此渲染因子选择器 |

示例（LLM 模式，金额被截断时）：

    {
      "approval_id": "ap_9f2c1d0a7b3e",
      "risk_level": "high",
      "challenge": {"type": "totp"},
      "title": "向「妈妈」转账 ¥2000.00",
      "amount_cents": 200000,
      "currency": "CNY",
      "counterparty": "妈妈（招商银行 **** 6621）",
      "reasons": ["金额超过单笔 1000 元", "需要动态口令强验证"],
      "preview": "执行后工资卡余额将从 ¥12,860.50 变为 ¥10,860.50",
      "expires_at": "2026-10-27T11:31:00+08:00",
      "nonce": "n_1a2b3c4d5e6f7a8b",
      "counterparty_account_masked": "6221 **** 6621",
      "payer_account_masked": "6222 **** 8821",
      "source": "llm",
      "source_detail": "用户要求给妈妈转 2000 元",
      "conflicts": ["金额超过单笔安全上限，已按上限截断并转人工"],
      "factors": ["totp", "sms", "face"]
    }

### POST /api/approval
请求：{ "session_id":"s_xxx", "approval_id":"ap_xxx", "decision":"approve"|"reject", "code":"246810", "method":"totp" }
- challenge.type = "confirm"：code 可省略，method 无效。
- challenge.type = "totp"：必须传 6 位动态码；method 可选 totp / sms / face，缺省 totp。
  - method=totp：动态码错误返回 400 INVALID_OTP。
  - method=sms：code 为 GET /api/demo/sms 拿到的 6 位短信码，错误或过期返回 400 INVALID_SMS。
  - method=face：code 为 POST /api/demo/face 拿到的 fc_ 令牌，过期返回 400 INVALID_FACE。
  - method 不是三个值之一返回 400 INVALID_METHOD。
- 因子校验失败时审批单保持 pending，可在 TTL 内换因子重试。
- 审批过期（默认 300 秒）返回 410 {"error":{"code":"APPROVAL_EXPIRED","message":"..."}}；已决策返回 409 APPROVAL_ALREADY_DECIDED。
- v0.6.1 审批绑定（对应审查 P1）：
  - 审批单只能由创建它的 session_id 提交，跨会话提交返回 403 APPROVAL_SESSION_MISMATCH，审批单仍保持 pending、余额不变。
  - 决策是原子 CAS（`UPDATE ... WHERE status='pending'`）：同一 approval_id 第二次提交返回 409 APPROVAL_ALREADY_DECIDED，不会重复执行、也不会重复写 approval_granted。
  - 审批单记录了参数摘要 `params_digest`（数据库列，旧库启动时自动补）：参数被篡改返回 400 APPROVAL_PARAMS_MISMATCH；恢复执行前还会比对「审批时的 steps」与 checkpoint 里待执行的 steps，不一致同样 400 且不执行任何工具（防「审批 A 执行 B」）。
  - 动态码连续错误 5 次锁定 60 秒，期间返回 400 INVALID_OTP（文案不变）；演示接口 GET /api/demo/totp 仍返回当前真实码（v0.6.1 起密钥为每用户随机、不可由 user_id 推导）。
返回结构与 /api/chat stream=false 相同，status 为 ok / blocked。

### POST /api/undo
请求：{ "session_id":"s_xxx", "action_id":"tx_xxx" }   // action_id 见 execute 结果
说明：仅对「转账 / 理财申购」在 60 秒内有效，生成冲正流水并写入审计。
返回：{ "undone": true, "status": "ok", "action_id": "tx_xxx", "message": "已撤销，资金已回到工资卡", "reply": 同上, "audit": [最近 10 条审计], "events": [...] }。action_id 取执行结果中的 transfers/orders action_id（前端也从 executed[].result.action_id 兜底识别）。

## 3. 大模型模式（LLM）

解析层支持三种模式：rule（纯规则）、llm（调用模型）、auto（默认，有 Key 用 llm，无 Key 回落 rule）。模式只影响解析层，不影响计划、策略门、审批与审计。

### GET /api/llm/status

返回当前模式与模型配置状态（不返回 Key 明文）：

    {
      "mode": "auto",
      "effective": "llm",
      "enabled": true,
      "provider": "DeepSeek",
      "model": "deepseek-flash",
      "base_host": "api.deepseek.com",
      "key_masked": "sk-123...abcd",
      "last_error": "",
      "last_error_code": 0,
      "last_error_hint": "",
      "degraded": false,
      "breaker": {
        "open": false,
        "failures": 0,
        "threshold": 3,
        "cooldown_s": 60,
        "retry_after_s": 0,
        "last_reason": ""
      },
      "stats": {
        "calls": 12,
        "ok": 11,
        "fail": 1,
        "retries": 1,
        "timeouts": 0,
        "empty": 0,
        "circuit_skipped": 0,
        "last_latency_ms": 842,
        "last_usage": {
          "prompt_tokens": 1180,
          "completion_tokens": 96,
          "total_tokens": 1276,
          "prompt_cache_hit_tokens": 1024,
          "prompt_cache_miss_tokens": 156
        },
        "last_cost_yuan": 0.00056048,
        "total_cost_yuan": 0.0062,
        "total_tokens": 14036
      }
    }

intent-llm 插件停用时，effective 恒为 rule，并在响应中额外带 "plugin_disabled": true（启用时无此字段）。

字段说明：

- mode 是用户选择的模式（rule / llm / auto）；effective 是实际生效模式（未配置 Key、LLM 被禁用或插件停用时恒为 rule）；enabled 表示是否配置了可用 Key。
- provider 是按模型名与接口域名推断出的厂商展示名（默认 DeepSeek；换成其他 OpenAI 兼容服务后随之变化，未知时回退为接口域名）。
- key_masked 在长度不超过 10 时形如前 2 位加三个星号，否则保留前 6 位与后 4 位；不返回 Key 明文。
- last_error 是最近一次模型调用失败的摘要（成功后清空），内容不含请求头与 Key，截断到 160 字；last_error_code 是 HTTP 错误码（0 表示无错误）；last_error_hint 是中文修复建议。
- degraded 为 true 仅表示熔断已打开（连续失败达到阈值、冷却期内直接走规则解析）；单次调用失败但未达阈值时为 false，失败详情看 last_error / last_error_code / last_error_hint / breaker.failures。
- breaker 是熔断状态：open 是否熔断、failures 连续失败次数、threshold 触发阈值（DEEPSEEK_BREAKER_THRESHOLD，默认 3）、cooldown_s 冷却秒数（DEEPSEEK_BREAKER_COOLDOWN，默认 60）、retry_after_s 距可重试的剩余秒数（未熔断为 0）、last_reason 最近一次熔断原因。
- stats 是进程内统计（重启清零，不写审计）：calls 调用次数、ok 成功、fail 失败、retries 重试次数、timeouts 超时、empty 空 content、circuit_skipped 熔断跳过次数、last_latency_ms 最近一次耗时、last_usage 最近一次用量、last_cost_yuan 最近一次成本、total_cost_yuan 累计估算成本、total_tokens 累计 tokens。成本按 DeepSeek 官方空闲时段单价（缓存命中输入 0.02 元/百万、未命中输入 1 元/百万、输出 4 元/百万）估算，仅供参考。

以上新字段始终存在、永不为 null；未配置 Key 或尚未调用过模型时为零值（calls=0、last_usage={}、last_cost_yuan=0.0 等）。成本公式：cost = hit_tokens×0.02/1e6 + miss_tokens×1.0/1e6 + completion_tokens×4.0/1e6；实测样例 hit=1024、miss=156、completion=96 得 ¥0.00056048。

### POST /api/llm/mode

请求：{ "mode": "rule" | "llm" | "auto" }

成功返回同 GET /api/llm/status，并额外带 switched：

    {
      "mode": "llm",
      "effective": "llm",
      "enabled": true,
      "provider": "DeepSeek",
      "model": "deepseek-flash",
      "base_host": "api.deepseek.com",
      "key_masked": "sk-123...abcd",
      "last_error": "",
      "switched": true
    }

非法模式返回 400：

    {"error": {"code": "BAD_MODE", "message": "mode 只能是 rule / llm / auto"}}

说明：切换只在当前进程内生效，重启后回到 auto；无 Key 时把模式设为 llm 不报错，但 effective 仍为 rule。模型调用失败（网络不可达、超时、返回非 JSON）会自动回退规则解析，事件与审计里 intent.source 记为 fallback（尝试过但失败）；连续 3 次失败熔断后，冷却期内不再尝试模型，intent.source 记为 rule 且 degraded=true，与 fallback 区分。失败原因见 last_error。

### POST /api/llm/test

用当前配置真实发起一次最小模型调用，供设置页「测试连接」使用：成功返回耗时、用量与估算成本，失败返回中文原因与修复建议。无论成功失败一律返回 HTTP 200，且不写审计链、不改变会话；它仍是一次真实模型调用，会计入进程内 stats 与连续失败计数（有 Key 时 calls+1，失败时 fail+1 且 breaker.failures+1），无 Key 时直接短路、不发起请求也不计数。

请求体：忽略，可传空 JSON。

成功（已配置 Key、服务可达）：

    {
      "ok": true,
      "latency_ms": 27,
      "model": "deepseek-flash",
      "provider": "DeepSeek",
      "usage": {
        "prompt_tokens": 1180,
        "completion_tokens": 96,
        "total_tokens": 1276,
        "prompt_cache_hit_tokens": 1024,
        "prompt_cache_miss_tokens": 156
      },
      "cost_yuan": 0.00056048,
      "result": {
        "intent": "query_bills",
        "confidence": 0.93,
        "months": 1,
        "reason": "桩：识别为账单查询"
      }
    }

- 上面是一次真实调用的返回（用 scripts/llm_stub_server.py 桩服务测得，2026-10-27；同一调用后 /api/llm/status 的 stats.calls 由 0 变 1、last_cost_yuan 为 0.00056048，可据此确认测试确实经过模型链路）；result 是模型返回的解析对象原样，字段同 SYSTEM_PROMPT 的 schema，仅用于确认链路可用，不进入业务。
- cost_yuan 按空闲时段单价估算；usage 原样保留返回的缓存命中/未命中 tokens。

未配置 Key（不是错误）：

    {
      "ok": false,
      "error": "未配置 API Key",
      "hint": "在 .env 填 DEEPSEEK_API_KEY 后重启服务",
      "code": null
    }

Key 无效（HTTP 401）：

    {
      "ok": false,
      "error": "API Key 无效或已失效",
      "hint": "检查 DEEPSEEK_API_KEY 是否正确、是否过期",
      "code": 401,
      "error_code": 401,
      "error_hint": "检查 DEEPSEEK_API_KEY 是否正确、是否过期"
    }

其他失败（余额不足 402、地址或模型不存在 404、限流 429、服务异常 5xx、超时）结构相同，code 为对应 HTTP 状态码，error 与 hint 为中文说明，并额外带 error_code / error_hint；超时类错误 code 为 null。未配置 Key 走本地短路，只有 error / hint / code=null，不带 error_code / error_hint。实现为 llm.test_connection()，承诺永不抛异常，前端只需读 ok 字段。

## 4. 查询

- GET /api/accounts?session_id=... → { "accounts": [...] , "cards": [...], "total_cents": 5786050 }
  accounts 项字段：account_id, kind(checking|savings|credit), name, number_masked, balance_cents, balance_display, currency, credit_limit_cents；v0.7.1 起每项再补卡片字段 card_id / card_kind / card_status / single_limit_cents / daily_limit_cents（按 number_masked 后四位匹配 cards，未绑卡时 card_id=""、限额为 0）。cards 为卡片表原始行（card_id, kind, number_masked, status, daily_limit_cents, single_limit_cents, created_at），total_cents 为非信用卡账户余额合计。
- GET /api/transactions?session_id=...&limit=20&account_id=acc_001 → { "transactions": [ ... ], "account_id": "acc_001" }
  limit 默认 20、范围 1-200；v0.7.1 起支持 account_id 过滤并在响应中回显，未传时为 null；account_id 不属于当前用户时返回 400 {"error": {"code": "BAD_ACCOUNT", "message": "没有这个账户，请检查 account_id"}}。
- GET /api/audit?session_id=...&limit=100 → { "entries": [ ... ], "chain": { "valid": true, "checked": 21, "broken_at": null, "reason": null } }
  entry 字段：seq, ts, session_id, actor, event_type, tool, args_hash, result_hash, risk_level, approval_id, prev_hash, hash
- GET /api/events?session_id=...&after=0 → { "events": [ { "seq": 1, "type": "message", "payload": {...} } ] }  // 会话回放
- GET /api/health → { "status":"ok", "app":"ZeroStep 零步银行", "version":"0.7.1", "llm_mode":"rule", "demo_mode":true, "seed_version":"2026.10.29" }  // version 读仓库根 VERSION 文件（唯一来源），当前 0.7.0；改 VERSION 后需重启进程生效
- GET /api/tasks[?limit=N&offset=M] → 任务列表，前端快捷按钮；user_input 即 turns[0]。
  无参数时与旧版完全一致，只返回 tasks（全部 59 条）：

    { "tasks": [ { "id": "t01", "title": "查账单", "category": "bills", "user_input": "帮我看看这个月花了多少钱", "expect": {...} }, ... ] }

  带 limit 参数时分页，limit 默认 6、范围 1-50（越界回落 6），offset 默认 0、负数按 0；offset 超出总数返回空数组且仍为 200：

    GET /api/tasks?limit=6&offset=6

    {
      "tasks": [ { "id": "t07", ... }, { "id": "t08", ... }, { "id": "t09", ... },
                 { "id": "t10", ... }, { "id": "t11", ... }, { "id": "t12", ... } ],
      "total": 59,
      "offset": 6,
      "limit": 6,
      "has_more": true
    }

  offset=54 时返回 t55–t59 共 5 条且 has_more=false；offset=60 时 tasks=[]、total 仍为 59、has_more=false。has_more 的判定为 offset + 本页条数 < total。分页以请求是否出现 limit 为准：只传 offset 不触发分页，仍返回全部 59 条；limit=0 或大于 50 按默认 6 处理，offset 为负数按 0 处理。
- GET /api/demo/totp?user_id=u001 → { "user_id":"u001", "code":"246810", "seconds_left":18 }  // 仅 DEMO_MODE 开启时可用，否则 403 DEMO_MODE_DISABLED

### v0.2.0 新增只读接口

以下接口随 v0.2.0 合并（已落盘，2026-10-27）；全部为 GET、无入参要求，返回 JSON。

#### GET /api/mcp/status

每次调用实时探测两个外部服务（initialize + tools/list），不抛异常。实测响应（2026-10-27，两个服务均在运行）：

    {
      "enabled": true,
      "servers": [
        {
          "name": "fx",
          "url": "http://127.0.0.1:8300/rpc",
          "ok": true,
          "latency_ms": 15,
          "tools": ["fx_convert"],
          "last_error": "",
          "degraded": false,
          "status": "ok"
        },
        {
          "name": "risk",
          "url": "http://127.0.0.1:8301/rpc",
          "ok": true,
          "latency_ms": 30,
          "tools": ["risk_screen"],
          "last_error": "",
          "degraded": false,
          "status": "ok"
        }
      ],
      "checked_at": "2026-10-27T20:33:03+08:00",
      "ok": true
    }

- 顶层 ok=true 表示状态接口本身可用；servers[].status 由该项 ok 推导：ok=true → "ok"，否则 "degraded"。
- 插件停用（mcp-fx 或 mcp-risk）时该项固定为 status="disabled"、ok=false、degraded=true、last_error="插件已停用"，不再探测；另一项照常。顶层 ok 仍为 true。
- enabled=false（MCP_ENABLED=0）：enabled=false、ok=true，servers 仍返回两项但 ok=false、degraded=true、status="degraded"、last_error="MCP 未启用（MCP_ENABLED=0）"。
- 服务连不上或超时：对应项 ok=false、degraded=true、status="degraded"、last_error 为失败摘要；主服务与对话不受影响。
- 探测内部异常时返回 500 { "ok": false, "error": "服务不可用" }。

v0.6.0 增量（2026-10-28）：每个 servers 项新增以下字段，既有字段与语义不变（MCP_SERVERS 为空时 servers 仍是 fx / risk 两项、transport=http，响应可直接对比 v0.5.1）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| label | str | 展示名；MCP_SERVERS 未写时同 name |
| transport | str | "http"（旧形态）或 "streamable-http"（会话 + SSE） |
| auth | object | {"type": "none"\|"headers"\|"bearer"\|"oauth2", "configured": bool, "token_cached": bool}；只回类型与状态，不含任何密钥明文 |
| session | bool | 是否已建立 Mcp-Session-Id（http 传输恒为 false） |
| read_only | bool | 该服务是否只读；默认 true |

另有两条补充规则：

- 服务名与插件 id：配了 MCP_SERVERS 时 servers[].name 取配置里的 name，对应插件 id 为 mcp-<name>（出现在 /api/plugins 中，紧跟在 mcp-risk 之后，按配置顺序）。
- 顶层 config_error：仅在 MCP_SERVERS 解析失败（坏 JSON）或部分配置非法时出现，值为中文原因；此时解析失败回退 fx/risk，部分非法则保留合法项。成功且全部合法时不出现该字段。
- 鉴权失败（401/403/invalid token/OAuth 失败）时该项 status="degraded"、last_error 以「鉴权失败」开头，不重试、不阻断对话。

#### GET /api/skills

返回全部技能卡（顺序固定为 bills、cards、cross、subscriptions、transfer、wealth）与进程内命中次数。每张卡在 skills.status() 原有字段上补两个字段：risk（按工具动钱程度静态分级）与 tool（tools[0]，工具为空时为 read_only）：

    {
      "count": 6,
      "skills": [
        {
          "id": "bills",
          "name": "账单消费分析",
          "description": "查账单、看消费构成与环比，帮用户定位花得多的分类",
          "triggers": ["账单", "消费", "支出", "流水", "开销", "花了多少"],
          "tools": ["get_bill_summary", "get_recent_transactions"],
          "examples": ["查一下这个月的账单", "这个月消费了多少", "看看我的支出明细"],
          "slots": ["months", "category"],
          "clarify": ["查几个月？", "只看某个分类吗？"],
          "forbidden": ["不得编造不存在的消费", "不得把收入算进支出"],
          "hits": 0,
          "risk": "low",
          "tool": "get_bill_summary"
        }
      ]
    }

六张卡的 risk 映射（后端 server.py 静态表，未知 id 兜底 medium）：

| id | risk | tool（tools[0]） |
| --- | --- | --- |
| bills | low | get_bill_summary |
| cards | medium | list_cards |
| cross | high | create_transfer |
| subscriptions | low | list_subscriptions |
| transfer | high | get_accounts |
| wealth | medium | list_wealth_products |

- hits 为本次进程内的匹配计数，重启归零，仅用于技能页按热度排序。
- skill-pack 插件停用时返回空态：{ "count": 0, "skills": [], "disabled": true }；规则解析不受影响。
- 接口异常时返回 500 { "ok": false, "error": "服务不可用" }。

#### GET /api/skills/{id}

返回单张卡（skills.get 的原始字段，不含列表接口补的 risk / tool）：

    { "skill": { "id": "transfer", "name": "转账汇款", "triggers": ["转账", "汇款", "转给"], "tools": ["get_accounts", "get_payees", "risk_screen", "create_transfer"], "examples": ["给妈妈转 200 元"], "hits": 2, ... } }

id 不存在返回 404：

    { "error": { "code": "SKILL_NOT_FOUND", "message": "技能卡不存在" } }

#### GET /api/channel/status

IM 渠道配置与最近 5 条投递记录（不返回令牌明文）。实测（2026-10-27，渠道已启用且有 1 个会话）：

    {
      "enabled": true,
      "port": 8400,
      "webhook_url": "http://127.0.0.1:8400/message",
      "token_set": true,
      "token_configured": true,
      "token_masked": "zero***en",
      "sessions": 1,
      "message_log": "backend/channels/messages.jsonl",
      "recent": [
        { "ts": "2026-10-27T20:27:54+08:00", "user_id": "u001", "direction": "out",
          "text": "执行后「工资卡」余额将从 ¥12,860.50 变为 ¥12,660.50",
          "status": "awaiting_approval", "cached": false },
        { "ts": "2026-10-27T20:27:54+08:00", "user_id": "u001", "direction": "in",
          "text": "确认", "status": "ok", "cached": false }
      ]
    }

- token_configured 与 token_set 同义（均为是否存在非空 CHANNEL_TOKEN），保留两个名字是为了兼容旧调用方。
- sessions 为 channels/sessions.json 的条目数，文件不存在或解析失败按 0。
- recent 取 messages.jsonl 最后 5 条，单行 JSON 损坏则跳过；日志不存在时为空数组。
- 每次调用从 config 现读，改环境变量后无需重启即可看到变化。
- channel-im 插件停用时 enabled=false、sessions 按 0，其余字段照常返回；渠道进程 POST /message 也会返回 503「渠道插件已停用」。
- 接口异常时返回 500 { "ok": false, "error": "服务不可用" }。

### v0.3.0 新增接口：插件注册表

两项接口由 backend/app/plugins.py 提供，完整设计见 docs/plugins.md；此处只列 HTTP 契约。下面的示例按未配置 MCP_SERVERS 的默认注册表（七个插件）展示（MCP_ENABLED=0 时两个 mcp 项为 degraded，字段相同）。配置 MCP_SERVERS 后注册表多出 mcp-<name> 项，字段结构相同。

#### GET /api/plugins

返回七个插件的注册表、运行态与计数。永不因探测失败而抛错；内部异常兜底 500。

    {
      "ok": true,
      "count": 7,
      "enabled_count": 7,
      "kinds": [
        { "id": "core", "name": "核心能力" },
        { "id": "mcp", "name": "外部服务" },
        { "id": "model", "name": "模型通道" },
        { "id": "skill", "name": "技能包" },
        { "id": "channel", "name": "渠道" }
      ],
      "plugins": [
        {
          "id": "mcp-fx",
          "name": "汇率服务",
          "kind": "mcp",
          "version": "0.6.0",
          "description": "通过 MCP 调用外部汇率服务，只用于查询与展示；不可用时回退内置参考价，绝不影响资金动作。",
          "status": "enabled",
          "enabled": true,
          "toggleable": true,
          "provides": { "tools": ["fx_convert"], "skills": [], "routes": ["/api/mcp/status"] },
          "requires": [],
          "detail": {
            "how": "启用时优先调用外部 fx_convert 拿实时参考汇率；停用后不发起任何外部请求，直接按内置参考价估算并在回复里标注。",
            "env": ["MCP_ENABLED", "MCP_FX_URL", "MCP_TIMEOUT_MS", "MCP_FX_FALLBACK_RATE"],
            "endpoints": ["/api/mcp/status"]
          },
          "locked_reason": ""
        }
      ]
    }

- 默认（未配置 MCP_SERVERS）顺序固定 policy、audit、mcp-fx、mcp-risk、intent-llm、skill-pack、channel-im，count=7；配置了 MCP_SERVERS 时，每个外部服务额外挂一个 mcp-<name> 插件，插在 mcp-risk 之后、按配置顺序排列，count = 7 + 服务数（受 MCP_MAX_SERVERS 限制）。version 为进程启动时读取的 VERSION。
- status 三态：enabled（正常）、degraded（已启用但外部服务不可用）、disabled（被停用）。core 插件 enabled 恒 true、toggleable=false、locked_reason=「核心能力不可停用」。
- enabled_count 统计 enabled=true 的项（含 core）；mcp 类两项的探测在一次请求内只做一次。

#### POST /api/plugins/<id>/toggle

请求体：

    { "enabled": false }

成功 200：

    {
      "ok": true,
      "plugin": { "id": "mcp-fx", "status": "disabled", "enabled": false, ... },
      "counts": { "total": 7, "enabled": 6, "disabled": 1 }
    }

错误：

| HTTP | code | message | 触发条件 |
| --- | --- | --- | --- |
| 400 | INVALID_BODY | 参数不合法 | 请求体不是合法 JSON / 不是对象 / enabled 缺失或不是布尔值 |
| 403 | PLUGIN_CORE_LOCKED | 核心能力不可停用 | 对 policy 或 audit 做任何 toggle |
| 404 | PLUGIN_NOT_FOUND | 插件不存在 | id 不在注册表 |
| 500 | 无 code，body 为 { "ok": false, "error": "服务不可用" } | 服务不可用 | 状态写入等内部异常 |

成功的 toggle 会写一条 plugin_toggled 审计（tool=plugins.toggle，args={plugin_id, enabled}，result={ok, plugin_id, enabled}，risk_level=low）；失败请求不写状态、不写审计。

停用后的联动：/api/mcp/status 对应项 status="disabled"/ok=false/last_error="插件已停用"；/api/skills 返回 { "count": 0, "skills": [], "disabled": true }；/api/channel/status 的 enabled=false、sessions=0；/api/llm/status 的 effective=rule 且带 plugin_disabled=true；对话回复分别加「汇率插件已停用」前缀或「外部反欺诈核查未执行（插件已停用），policy 安全规则仍然生效」尾注。

### v0.3.2 新增接口：主动触达

#### POST /api/push

由 backend/app/push.py 提供，主服务主动生成一条业务提醒并投递到 CHANNEL_PUSH_URL。请求可选 user_id，缺省用当前默认用户。

    POST http://127.0.0.1:8000/api/push
    Content-Type: application/json

    {"kind": "subscription"}

kind 三选一：subscription（订阅扣费，默认）、large_txn（大额支出）、repayment（还款提醒）。空对象按 subscription 处理。

成功 200：

    {
      "ok": true,
      "kind": "subscription",
      "user_id": "u001",
      "title": "订阅扣费提醒：iCloud 200G ¥6.00",
      "text": "你的订阅「iCloud 200G」将在 ... 自动扣费 ¥6.00。...",
      "pushed": true,
      "delivered": true,
      "channel": {
        "configured": true,
        "status": "delivered",
        "url_masked": "http://127.0.0.1:8400/***",
        "latency_ms": 12,
        "error": ""
      },
      "audit_seq": 128,
      "ts": "2026-10-27T22:30:00+08:00"
    }

- channel.status：not_configured（未配置 CHANNEL_PUSH_URL，只生成文案）、delivered（接收端 2xx）、failed（不可达、超时或非 2xx）。
- 无可用业务数据时仍返回 200，另带 error 与 code：NO_SUBSCRIPTION / NO_TRANSACTION / NO_CREDIT_ACCOUNT；还款提醒在已结清时 title 为「还款提醒：本期已结清」。
- 每次推送都写审计：event_type=push_message、tool=push、actor=system、risk_level=low；args={kind,user_id,channel_configured}，result={title,text,pushed,delivered,channel}。

错误：

| HTTP | code | message | 触发条件 |
| --- | --- | --- | --- |
| 400 | INVALID_BODY | 请求体不是合法 JSON | body 无法解析 |
| 400 | INVALID_BODY | 请求体必须是 JSON 对象 | body 不是对象 |
| 400 | BAD_KIND | kind 只能是 subscription / large_txn / repayment | kind 缺失以外的非法值 |
| 400 | BAD_REQUEST | user_id 必须是字符串 | user_id 非字符串 |
| 500 | 无 code，{ "ok": false, "error": "服务不可用" } | 服务不可用 | 内部异常 |

### v0.3.2 独立服务：企业微信回调（8401）

企微回调不是主服务路由，而是独立进程 backend/channels/wecom.py，默认 127.0.0.1:8401。完整协议、加密实现与验收表见 [channel.md](channel.md) 第六节，这里只列对外契约。

| 方法与路径 | 用途 | 成功响应 | 主要错误 |
| --- | --- | --- | --- |
| GET /wecom | URL 验签 + echostr 解密 | 200 纯文本 echostr 明文 | 503 未配置/停用；403 签名失败；400 缺参数 |
| POST /wecom | 接收消息、转发主服务、被动加密回包 | 200 text/xml 加密回包 | 503；403 签名失败；413 超过 1MB；400 空体/坏 XML/缺 Encrypt/解密失败 |
| GET /health | 配置与去重摘要，不泄露密钥 | 200 {ok,service,enabled,token_set,aes_key_set,corp_id_set,dedupe_size,dedupe_hits,port,main_url,ts} | 无（恒 200） |

- 验签：sha1(sorted([token, timestamp, nonce, encrypt])) 与 msg_signature 做 hmac.compare_digest。
- 加解密：AES-128-CBC，IV = key，PKCS#7 补到 32 字节；EncodingAESKey 43 位。
- 去重：同一 MsgId 默认 300 秒内只处理一次；回包只含 reply 或 needs_confirmation + preview + hint，不带任何审批凭据。
- 主服务不可达或转发超时（WECOM_TIMEOUT 默认 5 秒）时仍返回 200 加密回包，内容为可读降级文案。

### v0.4.0 新增接口：预约转账与调度触发

#### GET /api/scheduled

列出预约转账（按 run_at 升序）。query 参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| status | 空（全部） | 白名单 scheduled / pending / executed / failed / cancelled；scheduled 与 pending 等价，返回时归一为 pending；非法值返回 400 |
| limit | 6 | 1–50，越界或非整数回 6；分页在服务层切片 |
| offset | 0 | 大于等于 0，负数按 0 |

    GET /api/scheduled?status=scheduled&limit=6&offset=0
    HTTP 200
    {
      "ok": true,
      "scheduled": [
        {
          "sched_id": "sch_9f3c2a1b4d7e",
          "payee_name": "妈妈",
          "payee_account_masked": "6222 **** **** 1234",
          "amount_cents": 10000,
          "amount_display": "¥100.00",
          "run_at": "2026-10-28T09:00:00+08:00",
          "status": "pending"
        }
      ],
      "total": 1,
      "offset": 0,
      "limit": 6,
      "has_more": false
    }

- total 是该用户在当前 status 过滤下的总条数；has_more = offset + 返回条数 < total。
- 数据层异常时返回空列表（ok 仍为 true，scheduled 为 []）。
- 非法 status 返回 400：

    {"ok": false, "error": "status 只能是 scheduled/executed/failed/cancelled（pending 同 scheduled）"}

#### POST /api/scheduler/tick

手动触发一次「到期预约执行」，便于演示与验收；主服务默认已启动后台调度器（SCHEDULER_ENABLED=1，SCHEDULER_INTERVAL=5 秒），正常运行时无需调用。

请求体：无。服务端时间取当前时刻，不接受 body 里的 now / now_iso（已知限制）。

    POST /api/scheduler/tick
    HTTP 200
    {
      "ok": true,
      "checked": 1,
      "executed": [
        {
          "sched_id": "sch_9f3c2a1b4d7e",
          "transfer_id": "tr_202610280900001",
          "amount_cents": 10000,
          "amount_display": "¥100.00",
          "payee_name": "妈妈",
          "run_at": "2026-10-28T09:00:00+08:00"
        }
      ],
      "failed": [],
      "ts": "2026-10-28T09:00:03+08:00"
    }

- checked 是本次检查的到期条数；executed 每条含 sched_id / transfer_id / amount_cents / amount_display / payee_name / run_at。
- failed 每条含 sched_id 与错误 code / message；失败写审计 scheduled_transfer_failed（risk high）。
- 执行成功写审计 scheduled_transfer_executed（risk medium），复用 create_transfer，幂等键 sched_<sched_id>，重复 tick 不会重复扣款。
- 调度器未启用或模块不可用：HTTP 200，另带 note：

    {"ok": true, "checked": 0, "executed": [], "failed": [], "ts": "...", "note": "调度器未启用"}

- 内部异常：HTTP 500 {"ok": false, "error": "调度执行失败"}。

#### v0.4.0 领域错误码

这些 code 出现在 /api/chat 的 tool_end.result 与审批 / 回单文案里，不改变 HTTP 层的错误体格式：

| code | 触发条件 |
| --- | --- |
| AMOUNT_INVALID | 金额缺失、非正数或超出解析范围 |
| INSUFFICIENT_HOLDING | 赎回金额超过该产品持仓 |
| ACCOUNT_NOT_FOUND | 账户不存在 |
| LIMIT_INVALID | 卡限额小于等于 0，或单笔限额大于日累计 |
| CARD_NOT_FOUND | 卡号不存在 |
| CARD_FROZEN | 卡已冻结 |
| CARD_SINGLE_LIMIT | 刷卡金额超过该卡单笔限额 |
| CARD_DAILY_LIMIT | 刷卡金额超过该卡日累计限额 |
| SCHEDULE_NOT_FOUND | 预约记录不存在 |
| SCHEDULE_NOT_CANCELLABLE | 预约已执行或已取消，不能再次取消 |

### v0.4.1 新增接口：多因子强验证

v0.4.1 的红色动作（challenge.type=totp，如挂失 / 解挂、大额转账、理财申购）可以在动态码之外改用短信或人脸令牌。两个演示接口受 DEMO_MODE 控制，关闭时一律 403 DEMO_MODE_DISABLED。

#### GET /api/demo/sms

    GET /api/demo/sms?user_id=u001
    HTTP 200
    {
      "ok": true,
      "user_id": "u001",
      "code": "472913",
      "seconds_left": 120,
      "ttl": 120
    }

- 生成 6 位数字短信码并放在进程内存中，默认 TTL 120 秒（SMS_TTL），单次使用，连续 5 次校验失败即作废（SMS_MAX_ATTEMPTS）。
- 不真正外发短信：演示接口直接把码返回给调用方。
- DEMO_MODE=0 时返回 403 {"error":{"code":"DEMO_MODE_DISABLED","message":"演示模式未开启，不返回短信验证码"}}。

#### POST /api/demo/face

    POST /api/demo/face
    { "user_id": "u001" }

    HTTP 200
    {
      "ok": true,
      "user_id": "u001",
      "token": "fc_9f3c2a1b4d7e5a6b7c8d9e0f1a2b3c4d",
      "seconds_left": 60,
      "ttl": 60
    }

- 发放一次性人脸令牌，默认 TTL 60 秒（FACE_TTL），成功校验后失效；错误令牌不消耗也不延期。
- 这里的「人脸」是令牌发放，不涉及真实生物识别与活体检测。
- body 不是 JSON 对象返回 400 BAD_REQUEST；user_id 不是字符串返回 400 BAD_REQUEST；DEMO_MODE=0 时 403 DEMO_MODE_DISABLED。

#### v0.4.1 审批错误码

| code | HTTP | 触发条件 |
| --- | --- | --- |
| INVALID_OTP | 400 | 动态码错误或已过期（method 缺省或 totp） |
| INVALID_SMS | 400 | 短信码错误、过期或已被使用 |
| INVALID_FACE | 400 | 人脸令牌无效或已过期 |
| INVALID_METHOD | 400 | method 不是 totp / sms / face |
| APPROVAL_EXPIRED | 410 | 审批单超过 300 秒 TTL |
| APPROVAL_ALREADY_DECIDED | 409 | 审批单已批准或已拒绝 |

文案：INVALID_OTP「动态验证码不正确或已过期，请重新获取」、INVALID_SMS「短信验证码不正确或已过期，请重新获取」、INVALID_FACE「人脸确认已过期，请重新发起」、INVALID_METHOD「不支持的校验方式」。INVALID_SMS / INVALID_FACE / INVALID_METHOD 在 HTTP 层由 server.py 的 _APPROVAL_ERROR_MESSAGES 统一输出（INVALID_METHOD 会去掉 engine 版本里的「请选择动态码、短信或人脸」后缀），其余沿用 engine 文案；错误体统一为 {"error":{"code","message"}}。

### v0.5.0 只读工具：消费洞察与订阅识别

这两个不是 HTTP 路由，而是 /api/chat 里 insight 意图的执行步骤（工具名 spending_insights / detect_subscriptions），结果写入 tool_result 审计，流式时另由 insight_card 事件整包下发。两者都登记在 LOW_TOOLS，计划里只会出现只读步骤，不产生审批，也不执行写操作。

#### spending_insights

调用：spending_insights(conn, user_id)，无业务入参。返回结构（金额单位分；数值为演示种子 2026-10-28 实测）：

    {
      "as_of": "2026-10-28",
      "window": { "months": 1 },
      "totals": { "out_cents": 2448470, "in_cents": 2500001, "prev_out_cents": 1653372,
                  "delta_pct": 48, "potential_save_monthly_cents": 25500 },
      "top_categories": [
        { "category": "购物", "cents": 1150671, "pct": 47, "prev_cents": 303290, "delta_pct": 279 }
      ],
      "anomalies": [
        { "date": "2026-10-21", "merchant": "某商户", "cents": 0, "reason": "异常说明" }
      ],
      "budget": { "budget_cents": 1210000, "spent_cents": 2448470,
                  "projected_cents": 2530086, "over_cents": 1320086, "days_left": 3 },
      "savings": [
        { "id": "sv1", "kind": "subscription", "title": "爱奇艺 VIP", "monthly_cents": 2500,
          "yearly_cents": 30000, "detail": "连续 4 次自动扣费，最近一次 2026-10-08 扣款 ¥25.00",
          "action": { "kind": "subscription_cancel", "merchant": "爱奇艺 VIP" } }
      ],
      "fixed_cycles": { "count": 5, "monthly_cents": 988500,
                        "items": [ { "merchant": "房东王姐", "monthly_cents": 450000, "category": "居住" } ] }
    }

- top_categories 最多 5 条；pct 是占本期支出的整数百分比，delta_pct 是与上一周期的变化百分比（上期为 0 时记 0）。
- anomalies 最多 5 条，来自近 30 天异常扫描，字段固定为 date / merchant / cents / reason，条目内容随演示数据变化。
- budget.projected_cents 按已过天数线性外推月末支出，over_cents 是预计超支额。
- savings 中 kind=subscription 的条目计入 potential_save_monthly_cents（yearly_cents = monthly_cents × 12）；kind=category 是一条分类优化建议（该分类超预算按超支额，否则按压缩 10% 估算）；action 只是建议动作，不会自动执行。
- fixed_cycles 单列房租、还款、水电、通讯、储蓄转账等固定周期扣费，只做说明，不计入可省金额。演示种子识别 10 项周期扣费：5 项订阅合计 ¥255/月（年化 ¥3,060），5 项固定周期合计 ¥9,885/月。

#### detect_subscriptions

调用：detect_subscriptions(conn, user_id, window_days=120)。返回结构：

    {
      "window_days": 120,
      "registered_count": 5,
      "unregistered_monthly_cents": 0,
      "detected": [
        { "merchant": "爱奇艺 VIP", "monthly_cents": 2500, "occurrences": 4, "last_date": "2026-10-08",
          "confidence": 80, "registered": true, "sub_id": "sub_001",
          "category": "订阅", "subscription_like": true }
      ]
    }

- 判定规则：同一商户连续出现 3 次及以上、相邻间隔在 25–35 天、相邻金额差不超过 10%，才算周期扣费；confidence = 60 + 10 ×（次数 - 2），上限 90。
- detected 按 monthly_cents 降序；registered 表示命中已登记订阅（此时 sub_id 非空）；category 取最近一笔交易的分类；subscription_like 为 true 表示可归为订阅（已登记或分类为订阅），false 的进 fixed_cycles 做说明。
- unregistered_monthly_cents 是未登记订阅的每月合计；演示种子 5 项订阅均已登记，因此为 0。
### v0.7.0 新增只读聚合接口：数据专区

两个接口都只读：不写 transactions、不写 events、不写 audit，不产生审批，也不占用幂等键。金额一律整数分 + `*_display` 展示串。

#### GET /api/dashboard

    GET /api/dashboard?session_id=s_29fb3a938c63&window=30d

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| session_id | 是 | 会话 id；未知会话 404 `SESSION_NOT_FOUND` |
| window | 否 | `7d` / `30d` / `90d` / `180d` / `6m` / `1y`，默认 `30d`；**未知值回落 30d，不报错** |

响应（2026-10-29 演示种子实测，节选）：

    {
      "as_of": "2026-10-29",
      "window": { "kind": "30d", "days": 30, "from": "2026-09-30", "to": "2026-10-29" },
      "kpis": {
        "net_worth_cents": 7942850, "net_worth_display": "¥79,428.50",
        "available_cents": 5786050, "available_display": "¥57,860.50",
        "holdings_cents": 3000000,
        "credit_due_cents": 843200, "credit_due_display": "¥8,432.00",
        "month_out_cents": 2443271, "month_in_cents": 2500001, "net_flow_cents": 56730,
        "month": "2026-10",
        "deltas": { "month_out_pct": 47, "month_in_pct": 0, "net_flow_pct": -93 },
        "prev": { "month": "2026-09", "out_cents": 1653372, "in_cents": 2500001, "net_cents": 846629 },
        "sparklines": { "out_cents": [ … 14 个点 … ], "in_cents": [ … ], "days": [ … ] }
      },
      "trend": { "months": [ { "month": "2026-05", "in_cents": 2500001, "out_cents": 1728767, "net_cents": 771234 } ], "unit": "cents" },
      "budget": [ { "category": "购物", "limit_cents": 200000, "used_cents": 1158671,
                    "remaining_cents": -958671, "pct": 579, "state": "over" } ],
      "composition": { "total_out_cents": 2443271,
                       "by_category": [ { "category": "购物", "cents": 1158671, "display": "¥11,586.71", "pct": 47.1 } ],
                       "by_account": [ { "account_id": "acc_001", "kind": "checking", "number_masked": "**** 8821", "balance_cents": 1286050 } ],
                       "holdings_cents": 3000000 },
      "todo": { "rows": [ { "kind": "approval", "label": "待你确认的操作", "count": 4, "detail": "…", "target": "workbench" } ],
                "pending_approvals": 4, "anomaly_count": 3, "subscriptions_due": 2, "scheduled_pending": 0,
                "chain": { "valid": true, "checked": 22 } },
      "recent": [ { "txn_id": "T00349", "ts": "2026-10-29T09:12:00", "direction": "in",
                    "amount_cents": 1, "amount_display": "¥0.01", "category": "其他",
                    "counterparty": "扫码返现", "account_last4": "**** 8821" } ]
    }

口径：`net_worth_cents = available_cents + holdings_cents − credit_due_cents`；`deltas` 以「上月同日」为基线，基线期样本不足 ¥100 时该字段为 `null`（前端显示「—」）；`composition.by_category` 只取前 6 类，因此占比合计可能小于 100，`total_out_cents` 仍是完整总额；`recent` 固定 8 条，按 `ts` 倒序。

#### GET /api/analytics

    GET /api/analytics?session_id=s_29fb3a938c63&from=2026-05-01&to=2026-10-29&granularity=month

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| session_id | 是 | 未知会话 404 `SESSION_NOT_FOUND` |
| from / to | 否 | `YYYY-MM-DD`，缺省近 180 天；解析失败、`from > to`、跨度 > 400 天 → 400 `BAD_RANGE` |
| granularity | 否 | `day` / `week` / `month`，默认 `month`；日粒度桶数上限 120，超出 400 `BAD_RANGE` |

响应（节选）：

    {
      "as_of": "2026-10-29",
      "range": { "start": "2026-05-03", "end": "2026-10-29", "span_days": 180,
                 "granularity": "month", "buckets": 6, "prev": { "start": "2025-11-04", "end": "2026-05-02" } },
      "totals": { "in_cents": 15000001, "out_cents": 11125135, "net_cents": 3883233, "txn_count": 353,
                  "avg_day_out_cents": 61760, "avg_day_out_display": "¥617.60",
                  "prev_in_cents": 0, "prev_out_cents": 8367, "out_delta_pct": null, "in_delta_pct": null },
      "series": [ { "bucket": "2026-05", "in_cents": 2500001, "out_cents": 1728767, "net_cents": 771234, "count": 53 } ],
      "categories": [ { "category": "购物", "cents": 3162704, "pct": 28.45, "prev_cents": 0, "delta_pct": null,
                        "budget_cents": 1200000, "budget_pct": 264, "over_budget": true } ],
      "merchants": [ { "merchant": "房东王姐", "cents": 2700000, "pct": 24.3, "count": 6 } ],
      "transactions": [ { "txn_id": "T00349", "ts": "2026-10-29T09:12:00", "direction": "in", "amount_cents": 1,
                          "amount_display": "¥0.01", "category": "其他", "counterparty": "扫码返现", "account_last4": "**** 8821" } ],
      "anomalies": [ { "txn_id": "T00xxx", "ts": "2026-10-26T02:30:00", "amount_cents": 86000,
                       "amount_display": "¥860.00", "rule": "late_night_large", "level": "warn", "detail": "…" } ],
      "subscriptions": [ { "name": "爱奇艺 VIP", "amount_cents": 2500, "next_charge": "2026-11-04", "days_left": 6 } ],
      "savings": { "potential_cents": 950671, "potential_display": "¥9,506.71", "savings_rate": 0.2, "suggestions": ["…"] }
    }

约束：`series.length == range.buckets`（按桶补零，`day` 形如 `2026-10-29`、`week` 形如 `2026-W44`、`month` 形如 `2026-10`）；`transactions` 最多 100 条、按 `ts` 倒序；`anomalies` 最多 10 条；`merchants` 最多 10 条。

#### 只读性

两个接口都不写库。`scripts/dashboard_e2e.py` 的 `ana-17` / `ana-18` 在连续调用前后各取一次 `/api/audit` 与 `/api/transactions`，断言审计链 `checked` 不变、`valid` 仍为 `true`、流水条数不变。

### v0.7.1 账户快照与流水过滤

账户页只消费两个既有接口，未新增端点；两者都是只读，不写 transactions / events / audit。

`GET /api/accounts`（实测，2026-10-29）：

    {
      "accounts": [
        { "account_id": "acc_001", "kind": "checking", "name": "工资卡", "number_masked": "**** 8821",
          "balance_cents": 1286050, "balance_display": "¥12,860.50", "currency": "CNY", "credit_limit_cents": 0,
          "card_id": "card_001", "card_kind": "debit", "card_status": "active",
          "single_limit_cents": 2000000, "daily_limit_cents": 2000000 },
        { "account_id": "acc_003", "kind": "credit", "name": "信用卡", "number_masked": "**** 6712",
          "balance_cents": -843200, "balance_display": "-¥8,432.00", "credit_limit_cents": 3000000,
          "card_id": "card_002", "card_kind": "credit", "card_status": "active",
          "single_limit_cents": 3000000, "daily_limit_cents": 3000000 }
      ],
      "cards": [ { "card_id": "card_001", ... } ],
      "total_cents": 5786050
    }

字段规则：

| 字段 | 说明 |
| --- | --- |
| card_id / card_kind / card_status | 用 `number_masked` 后四位匹配 cards；未绑卡时三者均为空串，界面显示「无卡」 |
| single_limit_cents / daily_limit_cents | 卡片限额（分）；未绑卡为 0，界面不显示限额行 |
| total_cents | `mock_core.account_total`，只加非信用卡账户，等于「账户余额」KPI |

`GET /api/transactions` 的 account_id 过滤：

- 合法账户：只返回该账户流水，响应回显 `"account_id": "acc_003"`。
- 不带 account_id：返回全部流水，`"account_id": null`，行为与 v0.7.0 一致。
- 非法或他人账户：400 `BAD_ACCOUNT`，不为空数组——避免把「查不到」和「查错了」混为一谈。

两个接口都不产生写操作；`scripts/dashboard_e2e.py` 的 `acc-1` … `acc-6` 与 `ana-17/ana-18` 在连续取数前后校验审计链条数与 `checked` 不变。

## 5. 审计哈希链

- 每条审计行：payload = {seq, ts, session_id, actor, event_type, tool, args_hash, result_hash, risk_level, approval_id, prev_hash}；hash = sha256(canonical_json(payload))。canonical_json 指键排序 + 紧凑分隔符 + 不转义非 ASCII（audit.canonical）。prev_hash 是 payload 的一个普通字段，取上一行 hash，首行为 64 个 0；args_hash / result_hash 分别是对参数与结果做同样 canonical 序列化后的 sha256。
- GET /api/audit 会现场重算并返回 valid。
- 审计事件包含业务事件（tool_call / tool_result / approval_required / turn_blocked / mcp_call / mcp_fallback / skill_matched 等）、插件开关事件 plugin_toggled（tool=plugins.toggle，见 §4 v0.3.0 接口）、v0.5.0 的流式取消事件 answer_cancelled（tool=stream，risk low）与 v0.4.1 的会话安全事件 security_locked（tool=locks，risk high）/ security_unlocked（risk medium）/ turn_locked（tool=security_lock，risk high）。
- scripts/verify_audit.py 可离线验链并检测篡改（返回退出码 0/1）。

## 6. 风险分级（与官方权限三级映射）

- low（绿）：纯查询与读取（账单、流水、余额、持仓、收款人列表、异常扫描、年度账单、预约列表、v0.5.0 的消费洞察与订阅识别），自动执行，no approval。
- medium（黄）：小额转账（单笔 ≤ 1000 元且 24h 累计 ≤ 1000 元）、订阅查询/取消、虚拟卡申请、理财赎回、卡限额调整、撤销预约、1000 元以内的刷卡与预约创建。challenge.type=confirm，用户确认后执行。
- high（红）：大额转账（> 1000 元 或 24h 累计 > 1000 元）、挂失 / 解挂银行卡（v0.4.1 起由 medium 升为 high）、理财申购、超过 1000 元的刷卡与预约创建。challenge.type=totp，需 6 位动态码 + 确认；审批对象同时给出 factors=[totp,sms,face]，用户可选其中任一因子。
- blocked：命中注入/越权/余额不足/可疑收款人时直接拦截，status=blocked，不执行任何资金操作，审计记录风险事件。
- insight 意图固定为单个 LOW 只读步骤（spending_insights）：洞察与订阅建议不写库、不放行审批，建议动作要用户点按后按该动作重新定级。
- 解析来源不参与分级：无论意图来自规则还是模型，风险等级只由工具、金额、收款人与阈值决定；模型输出没有放行、降级或选择工具的权限。
- 预约执行不再二次授权：创建时已完成 confirm 或 totp，到期由调度器按幂等键执行；执行过程仍写审计，失败写 scheduled_transfer_failed（risk high）。

## 7. 前端事件约定（SSE 数据均为单行 JSON）

- intent：{name, confidence, source, slots, model, latency_ms, reason}；source 为 rule / llm / fallback，rule 路径下 model 为空串、latency_ms 为 null
- plan：{steps:[{step_id,title,tool,risk}], edges:[{from,to}]}
- tool_start：{step_id, tool, args_preview}
- tool_end：{step_id, tool, ok, latency_ms, result}
- approval_required：见上
- message：{delta}（v0.4.x 整段回答；v0.5.0 仅在 stream_text=false 时下发）
- answer_start：{turn_id, thread_id, chars}
- answer_delta：{turn_id, i, text}
- answer_end：{turn_id, chunks, elapsed_ms, cancelled}
- insight_card：{insight}（insight 意图的完整洞察结果，结构见 §4）
- note：{text}（取消时为「已停止输出」）
- audit：{seq, event_type, tool, risk_level, hash, prev_hash, ts}
- security_lock：{locked, locked_until, reason, remaining_s, count, threshold, just_locked}；出现在触发锁定的那一轮或锁定态的响应流中
- done：{status, thread_id}（status 可能是 locked）
