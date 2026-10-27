# ZeroStep API 契约（v1）

零依赖 Python 标准库实现。所有接口返回 JSON（UTF-8），金额单位一律为「分」（整数），前端负责格式化。

## 0. 通用约定

- Base URL: http://127.0.0.1:8000
- 认证：演示模式，无登录；用 session_id 区分会话。
- 错误返回：HTTP 4xx/5xx，body 为 { "error": { "code": "CODE", "message": "中文说明" } }；code 为机器可读常量（如 BAD_REQUEST / INVALID_OTP / APPROVAL_EXPIRED / DEMO_MODE_DISABLED / BAD_MODE / NOT_FOUND）。
- 金额：amount_cents 整数，单位分；展示为 ¥1,234.56。
- 时间：ISO-8601 带 +08:00 时区，例如 2026-10-26T21:30:00+08:00。

## 1. 会话

### POST /api/session
请求体：忽略（服务端固定用默认用户 u001 建会话，见 backend/server.py:133；前端 POST 空 JSON 即可）。
返回：
    {
      "session_id": "s_xxx",
      "user": { "user_id": "u001", "name": "张伟", "risk_level": "稳健型" },
      "accounts": [ { "account_id": "acc_001", "type": "checking", "name": "工资卡", "number_masked": "**** 8821", "balance_cents": 1286050, "currency": "CNY" } ],
      "cards": [ ... ],
      "subscriptions": [ ... ],
      "payees": [ ... ],
      "llm_mode": "rule",
      "llm_status": { "mode": "auto", "effective": "rule", "enabled": false, "provider": "deepseek", "model": "deepseek-flash", "base_host": "api.deepseek.com", "key_masked": "", "last_error": null },
      "demo_mode": true
    }

### GET /api/session/{session_id}
返回当前会话：user / accounts / cards / subscriptions / payees / pending_approval（可能为 null）/ llm_mode / llm_status（结构见第 3 节）/ demo_mode

### POST /api/admin/reset
重置演示数据库为种子数据（评委一键还原）。返回 { "reset": true, "seed_version": "..." }；DEMO_MODE=0 时返回 403 DEMO_MODE_DISABLED。

实现方式：在服务进程的同一 SQLite 连接上按表 DELETE 全部业务表后重新灌种子（不删除 bank.db 文件），因此可以反复点击「重置演示」，也不会在 Windows 上因文件被占用而失败（早期实现用 os.unlink 会抛 WinError 32）。

## 2. 对话（核心）

### POST /api/chat
请求：{ "session_id": "s_xxx", "text": "帮我查一下这个月账单", "stream": true }

stream=false（默认，脚本/评测用）：
    {
      "session_id": "s_xxx",
      "status": "ok",                  // ok | awaiting_approval | blocked | error
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

无审批时事件序列为 session → intent → plan → (tool_start → tool_end)* → message → done。

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
      "conflicts": ["金额超过单笔安全上限，已按上限截断并转人工"]
    }

### POST /api/approval
请求：{ "session_id":"s_xxx", "approval_id":"ap_xxx", "decision":"approve"|"reject", "code":"246810" }
- challenge.type = "confirm"：code 可省略。
- challenge.type = "totp"：必须传 6 位动态码，错误返回 400 {"error":{"code":"INVALID_OTP","message":"..."}}。
- 审批过期（默认 300 秒）返回 410 {"error":{"code":"APPROVAL_EXPIRED","message":"..."}}。
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
      "provider": "deepseek",
      "model": "deepseek-flash",
      "base_host": "api.deepseek.com",
      "key_masked": "sk-1234...abcd",
      "last_error": null
    }

字段说明：mode 是用户选择的模式；effective 是实际生效模式（未配置 Key 或 LLM 被禁用时恒为 rule）；enabled 表示是否配置了可用 Key；last_error 是最近一次模型调用失败的摘要（成功后清空），内容不含请求头与 Key；key_masked 在长度不超过 10 时形如前 2 位加三个星号，否则保留前 6 位与后 4 位。

### POST /api/llm/mode

请求：{ "mode": "rule" | "llm" | "auto" }

成功返回同 GET /api/llm/status，并额外带 switched：

    {
      "mode": "llm",
      "effective": "llm",
      "enabled": true,
      "provider": "deepseek",
      "model": "deepseek-flash",
      "base_host": "api.deepseek.com",
      "key_masked": "sk-1234...abcd",
      "last_error": null,
      "switched": true
    }

非法模式返回 400：

    {"error": {"code": "BAD_MODE", "message": "mode 只能是 rule / llm / auto"}}

说明：切换只在当前进程内生效，重启后回到 auto；无 Key 时把模式设为 llm 不报错，但 effective 仍为 rule。模型调用失败（网络不可达、超时、返回非 JSON）会自动回退规则解析，事件与审计里 intent.source 记为 fallback，失败原因见 last_error。

## 4. 查询

- GET /api/accounts?session_id=... → { "accounts": [...] }
- GET /api/audit?session_id=...&limit=100 → { "entries": [ ... ], "chain": { "valid": true, "checked": 21, "broken_at": null, "reason": null } }
  entry 字段：seq, ts, session_id, actor, event_type, tool, args_hash, result_hash, risk_level, approval_id, prev_hash, hash
- GET /api/events?session_id=...&after=0 → { "events": [ { "seq": 1, "type": "message", "payload": {...} } ] }  // 会话回放
- GET /api/health → { "status":"ok", "app":"ZeroStep 零步银行", "version":"1.0.0", "llm_mode":"rule", "demo_mode":true, "seed_version":"..." }
- GET /api/tasks → { "tasks": [ { "id":"t01", "title":"查账单", "category":"bills", "user_input":"帮我看看这个月花了多少钱", "expect": {...} } ] }  // 前端快捷按钮；user_input 即 turns[0]
- GET /api/demo/totp?user_id=u001 → { "user_id":"u001", "code":"246810", "seconds_left":18 }  // 仅 DEMO_MODE 开启时可用，否则 403 DEMO_MODE_DISABLED

## 5. 审计哈希链

- 每条审计行：payload = {seq, ts, session_id, actor, event_type, tool, args_hash, result_hash, risk_level, approval_id, prev_hash}；hash = sha256(canonical_json(payload))。canonical_json 指键排序 + 紧凑分隔符 + 不转义非 ASCII（audit.canonical）。prev_hash 是 payload 的一个普通字段，取上一行 hash，首行为 64 个 0；args_hash / result_hash 分别是对参数与结果做同样 canonical 序列化后的 sha256。
- GET /api/audit 会现场重算并返回 valid。
- scripts/verify_audit.py 可离线验链并检测篡改（返回退出码 0/1）。

## 6. 风险分级（与官方权限三级映射）

- low（绿）：纯查询与读取，自动执行，no approval。
- medium（黄）：小额转账（单笔 ≤ 1000 元且 24h 累计 ≤ 1000 元）、订阅查询/取消、虚拟卡申请/冻结。challenge.type=confirm，用户确认后执行。
- high（红）：大额转账（> 1000 元 或 24h 累计 > 1000 元）、修改密码/挂失、理财申购。challenge.type=totp，需 6 位动态码 + 确认。
- blocked：命中注入/越权/余额不足/可疑收款人时直接拦截，status=blocked，不执行任何资金操作，审计记录风险事件。
- 解析来源不参与分级：无论意图来自规则还是模型，风险等级只由工具、金额、收款人与阈值决定；模型输出没有放行、降级或选择工具的权限。

## 7. 前端事件约定（SSE 数据均为单行 JSON）

- intent：{name, confidence, source, slots, model, latency_ms, reason}；source 为 rule / llm / fallback，rule 路径下 model 为空串、latency_ms 为 null
- plan：{steps:[{step_id,title,tool,risk}], edges:[{from,to}]}
- tool_start：{step_id, tool, args_preview}
- tool_end：{step_id, tool, ok, latency_ms, result}
- approval_required：见上
- message：{delta}
- audit：{seq, event_type, tool, risk_level, hash, prev_hash, ts}
- done：{status, thread_id}
