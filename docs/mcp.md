# MCP 外部只读服务（v0.6.0：通用接入）

> 一句话：汇率换算与反欺诈评分两项只读能力通过 MCP 接入，正常时提供实时数据，异常时自动降级到内置实现，业务照常执行、能动钱的边界不变。v0.6.0 起同一套客户端升级为通用网关：任何符合 MCP 语义的外部服务（含银行开放平台）都能用 MCP_SERVERS 一段 JSON 接入，支持 Bearer / Headers / OAuth2 鉴权、streamable-http 会话与 SSE 响应。

本文档的接口名、字段与默认值来自 实施契约_v0.2.0.md §3.1–§3.3、§3.6–§3.7，并已按 backend/app/mcp_client.py、backend/external/、server.py 的四条只读路由与 scripts/mcp_e2e.py（7/7）校对（2026-10-27）；v0.6.0 的通用接入部分按 backend/app/mcp_registry.py、mcp_client.py、backend/external/bank_mcp_server.py 与 scripts/mcp_gateway_e2e.py（17/17）校对（2026-10-28）；渠道部分见 docs/channel.md（channel_e2e 9/9）。

## 一、设计原则

1. 只读：fx_convert 与 risk_screen 都不产生资金动作，不是转账、申购、撤单等写操作。
2. 外部数据只当数据：返回值不拼进提示词，不参与意图解析，不能降低风险等级（fail-safe：外部结果只能加严）。
3. 全部可降级：连接失败、超时、JSON-RPC error 都退化为内置实现，评测与演示不因外部服务缺席而失败。
4. 零依赖：服务端用标准库 http.server + json 实现，客户端用标准库 urllib 发 HTTP POST。
5. 可观测：每次调用写审计（mcp_call，降级时另记 mcp_fallback），页面有外部服务状态卡与降级 InfoBar。
6. v0.6.0 扩展到任意外部 MCP 服务后原则不变：只接入只读工具；写操作在客户端就被拒绝、不转发，接入更多服务不等于放宽资金边界。

## 二、组成

| 文件 | 职责 |
| --- | --- |
| backend/app/mcp_client.py | 通用 MCP 网关：http / streamable-http 两种传输、会话与 SSE、鉴权、只读四道闸、状态汇总；绝不抛异常 |
| backend/app/mcp_registry.py | 服务注册表：解析 MCP_SERVERS（JSON）、归一 transport 与鉴权、密钥 env 引用、超时与数量上限；解析永不抛异常 |
| backend/external/fx_server.py | 汇率服务，端口 8300，工具 fx_convert |
| backend/external/risk_server.py | 反欺诈服务，端口 8301，工具 risk_screen |
| backend/external/bank_mcp_server.py | 银行开放平台演示服务，端口 8310：Bearer / OAuth2、会话、SSE；只读工具 + 永远拒绝的 bank_transfer |
| backend/external/selftest.py | 进程内起停两服务，跑 initialize / list / call / 超时 / 关闭 5 场景 |
| scripts/mcp_gateway_e2e.py | 17 项通用接入端到端（注册表 / 鉴权 / 会话 / SSE / 写拒绝零转发 / 脱敏 / 坏配置 / 降级） |
| scripts/mcp_probe.py | 接入真实银行前的自检：只读探测 tools/list，列出工具并标出写风险（不调用任何工具） |
| backend/app/config.py | MCP_ENABLED、MCP_FX_URL、MCP_RISK_URL、MCP_TIMEOUT_MS、MCP_FX_FALLBACK_RATE、MCP_SERVERS、MCP_MAX_SERVERS、MCP_ALLOW_WRITE_TOOLS |
| backend/server.py | GET /api/mcp/status |
| backend/app/engine.py | _exec_tool 增加 fx_convert / risk_screen 分支；build_plan 插入 risk_screen；审计 mcp_call / mcp_fallback |

## 三、配置

| 常量 / 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| MCP_ENABLED | True | 设为 0 时完全不调用外部服务，全部走内置实现 |
| MCP_FX_URL | http://127.0.0.1:8300/rpc | 汇率服务 JSON-RPC 地址 |
| MCP_RISK_URL | http://127.0.0.1:8301/rpc | 反欺诈服务 JSON-RPC 地址 |
| MCP_TIMEOUT_MS | 10000 | 读超时（毫秒） |
| MCP_CONNECT_TIMEOUT_MS | 2000 | 连接超时（毫秒），与读超时分离 |
| MCP_TOOLS_CACHE_SECONDS | 60 | tools/list 结果缓存秒数 |
| MCP_FX_FALLBACK_RATE | USD 0.1403、HKD 1.0921、EUR 0.1287、JPY 20.85 | 降级时使用的内置参考价（1 CNY = X 外币），仅演示用、不是实时行情 |
| MCP_RISK_BLOCK_SCORE | 60 | 外部风控拦截阈值（环境变量可调）：score 达到该值、或 risk_level=high、或命中名单时，转账直接拦截 |
| MCP_SERVERS | 空 | 通用服务注册表（JSON 数组，字段见第十一节）；为空时行为与 v0.5.1 一致：servers 仍为 fx / risk 两项、transport=http |
| MCP_MAX_SERVERS | 8 | 注册表最多服务数，超出部分忽略并在 config_error 写明原因 |
| MCP_ALLOW_WRITE_TOOLS | 0 | 全局写开关；仅对 read_only=false 服务的非 write_tools 工具生效，显式 write_tools 永远拒绝 |

默认配置下不必起外部服务：主服务单起时，状态接口显示 degraded=true，汇率回落内置表，反欺诈评分跳过，43 条用例仍全部通过（v0.4.0 实跑 run_tasks.py 43/43；v0.2.0 的 t31–t36 在降级路径下同样 PASS）。

v0.3.0 起，mcp-fx / mcp-risk 还可能因插件开关停用：这时 /api/mcp/status 对应项 status=disabled、last_error=插件已停用，业务侧不发起外部调用而是直接走内置降级；policy 与哈希链审计不变，见 [plugins.md](plugins.md)。

## 四、协议与接口

### 4.1 JSON-RPC 2.0 子集

每次新建/复用连接后按固定顺序握手：

    initialize → notifications/initialized → tools/list（缓存 60 秒）→ tools/call

- 传输：HTTP POST，Content-Type: application/json，请求体为 JSON-RPC 2.0 对象。
- 白名单：只允许 tools/list 返回的工具名；未知工具直接拒绝，不转发。
- 异常：连不上、超时、JSON-RPC error 一律返回 ok=False、degraded=True，不重试、不中断对话。
- 线程安全：同进程可被多线程调用（engine 的请求处理是多线程的），客户端内部加锁。
- 缓存：tools/list 结果缓存 60 秒，避免每次调用都握手。

### 4.2 Python 接口

    status() -> {
      "enabled": bool,
      "servers": [
        {
          "name": "fx" | "risk" | "bank",
          "label": str,
          "url": str,
          "transport": "http" | "streamable-http",
          "ok": bool,
          "latency_ms": int,
          "tools": [str],
          "last_error": str,
          "degraded": bool,
          "auth": {"type": "none" | "headers" | "bearer" | "oauth2",
                   "configured": bool, "token_cached": bool},
          "session": bool,
          "read_only": bool
        }
      ],
      "config_error": str,
      "checked_at": str
    }

name 为注册名：MCP_SERVERS 为空时为 fx / risk，否则取配置里的 name（插件 id 为 mcp-<name>）。auth 只暴露类型、是否已配置、是否已缓存 token，不含任何密钥明文；session 表示当前是否已建立过 Mcp-Session-Id；config_error 仅在 MCP_SERVERS 解析失败或部分非法时非空（此时回退 fx/risk，主流程不受影响）。GET /api/mcp/status 在 server.py 里再补每行 status 与顶层 ok，字段见 docs/api.md §4。

    call_tool(server: str, tool: str, args: dict) -> {
      "ok": bool,
      "data": dict | None,
      "latency_ms": int,
      "degraded": bool,
      "source": str,
      "error": str
    }

source 取值：成功时取服务返回的 source（fx 为 external-fx，risk 为 external-risk；服务未标注则为 mcp），失败或未启用时为 degraded。业务层来源另由 engine 返回值记录：external（外部成功）、fallback（内置参考价）、skipped（风控未执行），并与审计事件 mcp_call / mcp_fallback 一一对应。

### 4.3 两个工具

汇率服务（8300）。主口径是币种 + 金额（元），兼容口径是币种 + 金额（整数分）：

    fx_convert(from, to, amount)            # 主口径：from / to 为币种，amount 单位为元
    fx_convert(base, quote, amount_cents)   # 兼容口径：base / quote 为币种，amount_cents 单位为分
    -> {
      "from": str, "to": str,               # 同时回显 base / quote
      "rate": float,
      "amount": float, "amount_cents": int,
      "converted": float, "converted_cents": int,
      "as_of": str, "ts": str,              # 同一时刻的两种字段名
      "source": "external-fx"
    }

牌价用固定表 + 小抖动（±0.2%），不是实时行情；converted 与 converted_cents、as_of 与 ts 同时返回，便于不同调用方取用。engine 实际发送 base / quote / amount_cents。

反欺诈服务（8301）。收款账号与金额各有两种口径，各取其一即可：

    risk_screen(payee_name, payee_account_tail | payee_account, amount_cents | amount) -> {
      "risk_level": "low" | "medium" | "high",
      "blocked": bool,                      # 等价于 risk_level == "high"
      "hits": [str], "advice": str,
      "hit": bool, "list_name": str,
      "score": int, "reasons": [str],
      "payee_name": str, "payee_account_tail": str,
      "amount": float, "amount_cents": int,
      "source": "external-risk", "ts": str
    }

- 账号：payee_account 与 payee_account_tail 二选一；金额：amount（元）与 amount_cents（分）二选一。
- engine 实际发送 payee_name / payee_account_tail / amount_cents。
- 规则与分值：单笔 > 5000 元 +40；缺少收款人姓名 +20；陌生收款人（不在演示通讯录）+30；命中演示名单「张*」+60。score 封顶 100，score ≥ 60 为 high、≥ 30 为 medium、其余 low。
- 结果只作为风险分级的证据之一，不能把 high 降为 medium，也不能让未通过策略门的动作放行。

### 4.4 拦截阈值：外部结果只加严

engine 在真实动钱之前（_pre_execute_guard，仅 create_transfer）先跑一次 risk_screen。命中以下任一条件时抛 DomainError（code=RISK_BLOCKED），转账不执行、资金不变，并写 mcp_call 审计：

1. 返回 blocked=true，或 risk_level == "high"；
2. list_name 非空（命中演示风控名单）；
3. score ≥ MCP_RISK_BLOCK_SCORE（默认 60，环境变量可调）。

只加严不放松：外部结果不能降低本地策略的等级，也不能把被拦下的动作放行；外部服务不可用或关闭时，风控步骤整体跳过（skipped），按本地策略继续，不阻断正常转账。

### 4.5 HTTP 服务

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | /rpc | JSON-RPC 请求入口 |
| GET | /health | 健康检查，返回服务名与工具列表 |

启动参数：--port 覆盖端口；--slow 秒数用于模拟超时（只给自检与演示用）。

### 4.6 通用传输：streamable-http 与 SSE

v0.6.0 起由 transport 字段决定握手方式：

- http（默认）：与 4.1 一致，每次 POST 是独立请求，不带会话头。
- streamable-http：initialize 成功后保存响应头 Mcp-Session-Id，之后 tools/list、tools/call 都带该头；会话失效时自动重新 initialize 一次，仍失败则降级。响应 Content-Type 为 text/event-stream 时按 SSE 解析，取 data: 行里的 JSON-RPC 响应。

鉴权（auth.type）：

- none（默认）与 headers：自定义静态请求头（如 X-Api-Key），密钥值支持 env:VAR 或 ${VAR}。
- bearer：Authorization: Bearer <token>，同样支持 env 引用，可直接写 token 字符串。
- oauth2：先 POST token_url（client_credentials，带 client_id / client_secret / scope），按返回 expires_in 缓存 access_token；401 时清缓存并重取一次。

401 / 403 / invalid token 统一归为「鉴权失败」：status 显示 degraded，last_error 以「鉴权失败」开头，业务侧照常降级，不阻断对话。

## 五、降级行为

| 场景 | 客户端表现 | 业务表现 | 审计与页面 |
| --- | --- | --- | --- |
| 服务未启动 / 连接被拒 | ok=False、degraded=True | 汇率用内置表；反欺诈跳过 | status 显示 last_error；审计 mcp_fallback，回退时另记 mcp_call（source=fallback） |
| 读超时（>10 秒） | 同上，不重试 | 同上 | 同上 |
| JSON-RPC error 或返回结构异常 | ok=False、degraded=True | 同上 | 同上 |
| MCP_ENABLED=0 | 不发起调用 | 全部内置实现 | status 显示 enabled=false |
| 只有一个服务可用 | 单项 degraded | 可用项用外部数据，另一项降级 | 按服务分别标记 |

降级不是失败：对话继续、断言仍通过，只在页面顶部标注「已降级」，绝不出现「服务不可用请重试」这类阻断提示。

## 六、启动与演示

三进程（推荐现场演示）：

    # 终端 1：汇率服务
    python backend/external/fx_server.py

    # 终端 2：反欺诈服务
    python backend/external/risk_server.py

    # 终端 3：主服务
    python backend/server.py

单进程（离线演示，自动降级）：

    python backend/server.py

银行开放平台演示（v0.6.0）：

    # 终端 A：银行 MCP 演示服务（Bearer + 会话 + SSE + OAuth2）
    python backend/external/bank_mcp_server.py --port 8310 --oauth

    # 终端 B：主服务指向该银行服务（.env 或命令行环境变量，Windows CMD 用 set）
    set MCP_SERVERS=[{"name":"bank","label":"演示银行","url":"http://127.0.0.1:8310/mcp","transport":"streamable-http","tools":["bank_account_summary","bank_quote"],"write_tools":["bank_transfer"],"auth":{"type":"bearer","token":"env:BANK_MCP_TOKEN"}}]
    set BANK_MCP_TOKEN=demo-bank-token
    python backend/server.py

    python scripts/mcp_gateway_e2e.py     # 17 项，进程内起停，不需要手工起服务

自检与端到端：

    python backend/external/selftest.py     # 5 场景，输出 PASS / FAIL
    python scripts/mcp_e2e.py               # 起停外部服务，4 条断言
    python scripts/run_tasks.py --filter t31,t32

接口自测（Windows 下 curl 是 Invoke-WebRequest 别名，建议用 curl.exe；POST 用请求体文件，避免命令行引号转义）：

    curl.exe -s http://127.0.0.1:8300/health
    curl.exe -s http://127.0.0.1:8301/health
    curl.exe -s http://127.0.0.1:8000/api/mcp/status

    # rpc.json 内容（一行）
    {"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"fx_convert","arguments":{"base":"USD","quote":"CNY","amount_cents":800}}}

    curl.exe -s -X POST http://127.0.0.1:8300/rpc -H "Content-Type: application/json" --data-binary @rpc.json

## 七、界面

- 外部服务页（#scr-services）：两张服务卡（fx_convert、risk_screen），每卡显示 ok、latency_ms、degraded 与 last_error；底部 InfoBar 说明离线降级。
- 顶栏：任一服务 degraded 时出现「已降级」徽章（--warn）。
- 数据来源：GET /api/mcp/status，页面不直连 8300/8301。
- 页面与卡片已在前端落盘（.grid2 两张服务卡）；GET /api/mcp/status 已实现，返回 enabled / servers / checked_at，字段见 docs/api.md §4。

## 八、边界与非目标

- 不做写操作：MCP 不接转账、申购、撤单、改密码等资金或账户变更能力。
- 不接真实第三方：汇率与反欺诈都是本地演示服务，不调用外部网络。
- 不做重试、熔断、负载均衡：v0.2.0 只做「一次失败即降级」，把复杂度留给后续版本。
- 鉴权只做客户端侧：Bearer / 自定义头 / OAuth2 client_credentials；不做 mTLS、不做服务端签名校验、不接银行真实证书体系。
- 不接真实银行接口：bank_mcp_server.py 只是本地演示服务，工具返回演示数据，bank_transfer 永远拒绝；真实接入需银行开放平台提供 MCP 端点与凭证，届时只改 MCP_SERVERS，不改业务代码。
- 外部返回不参与提示词：模型看不到汇率原文与风险理由，避免把外部文本当指令。

## 九、已知限制

1. 牌价固定表 + 抖动，不代表实时汇率，不可用于真实报价。
2. risk_screen 的三条规则是演示规则，演示名单仅「张*」。
3. --slow 仅用于自检/演示，不应出现在正式启动参数里。
4. 外部服务与主服务同机，跨机部署需要额外配置 URL 与防火墙。
5. mcp_client.py、backend/external 与本文档已对齐（2026-10-27）；scripts/mcp_e2e.py 已落盘并实测 7/7。

## 十、验证清单

1. python backend/external/selftest.py 五场景全 PASS（2026-10-27 实测 5/5：握手、工具清单缓存、调用、慢服务超时、关闭降级）。
2. python scripts/mcp_e2e.py 七条断言全过（2026-10-27 实测 7/7：健康检查、状态探测、两个工具调用、engine 真实回复、停服降级、审计链 valid）。
3. run_tasks.py 中 t31（走外部）与 t32（关闭外部降级）通过。
4. 三进程或单进程两种起法都能完成 43 条评测，审计链 valid。
5. GET /api/mcp/status 在两种起法下分别显示 ok=true / degraded=true（接口已实现，字段见 docs/api.md §4）。
6. 审批卡的金额、收款人与风险等级在 MCP 开启与关闭时一致（外部结果只加严，不改边界）。
7. python scripts/mcp_gateway_e2e.py 17 项全过（2026-10-28 实测 17/17：未配置回退、3 服务解析、Bearer 会话、SSE 调用、服务端计数、写拒绝零转发、白名单、错误 token、OAuth2、密钥脱敏、插件动态出现、MCP_ENABLED=0、坏 JSON、部分非法、未知服务/工具）。
8. MCP_SERVERS 为空时 /api/mcp/status 与 v0.5.1 字段兼容（只新增字段），GET /api/plugins 仍为 7 项。
9. 接入新服务前先跑 `python scripts/mcp_probe.py <name>`：只读列出 tools/list，确认端点、鉴权与工具名，写风险工具不进白名单（2026-10-28 对演示银行服务实测 1/1 成功、3 个工具）。


## 十一、v0.6.0：通用 MCP 接入（银行开放平台）

v0.6.0 把「MCP 客户端」升级成「MCP 网关」：除内置 fx / risk 外，任何符合 MCP 语义的银行开放平台接口都可以用一段 MCP_SERVERS 配置接入。原则仍然是只追加不改语义——v0.5.x 的 fx/risk 行为、降级、审计与资金边界一字不改。

### 11.1 注册表与环境变量

MCP_SERVERS 是一个 JSON 数组（.env 里写单行）。每个元素字段：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| name | 是 | 服务名，小写字母开头，只含 a-z0-9_-，≤32 字符；插件 id 为 mcp-<name> |
| label | 否 | 展示名，缺省同 name |
| url | 是 | MCP 端点，http / https |
| transport | 否 | http（默认，旧 fx/risk 形态）/ streamable-http |
| tools | 否 | 只读白名单：只允许调用这些工具；空数组表示按服务 tools/list 全量只读 |
| write_tools | 否 | 显式写操作黑名单：无论白名单、read_only 与全局开关如何，一律拒绝 |
| read_only | 否 | 默认 true；仅 false 时非 write_tools 工具才可能在 MCP_ALLOW_WRITE_TOOLS=1 下放行（默认仍拒绝） |
| timeout_ms | 否 | 读超时，100–120000，默认取 MCP_TIMEOUT_MS |
| connect_timeout_ms | 否 | 连接超时，100–120000，默认取 MCP_CONNECT_TIMEOUT_MS |
| auth | 否 | 鉴权配置，见下 |

auth 三种写法（密钥一律用环境变量引用，不要写明文）：

    {"type": "bearer", "token": "env:BANK_MCP_TOKEN"}            # 也支持 "${BANK_MCP_TOKEN}"；token 直接写字符串等同 bearer
    {"type": "headers", "headers": {"X-Api-Key": "env:BANK_API_KEY"}}
    {"type": "oauth2", "token_url": "http://127.0.0.1:8310/oauth/token",
     "client_id": "demo-bank-client", "client_secret": "env:BANK_MCP_SECRET", "scope": "bank.read"}

固定规则：MCP_SERVERS 里的密钥只写 env:VAR / ${VAR} 引用；/api/mcp/status 只返回 auth.type 与 configured / token_cached 布尔，不返回任何明文。

### 11.2 银行开放平台演示服务

backend/external/bank_mcp_server.py（默认 8310，零依赖标准库实现）：

    python backend/external/bank_mcp_server.py --port 8310 --oauth

| 接口 | 说明 |
| --- | --- |
| POST /mcp | JSON-RPC：initialize / notifications/initialized / tools/list / tools/call |
| POST /oauth/token | client_credentials（--oauth 时开放；演示 client_id=demo-bank-client，client_secret=demo-bank-secret） |
| GET /health | 健康检查与计数：sessions / calls / write_calls / tokens |

工具：

| 工具 | 性质 | 说明 |
| --- | --- | --- |
| bank_account_summary(account_no?) | 只读 | 演示账户摘要（脱敏户名、余额、币种、最近流水） |
| bank_quote(from, to, amount) | 只读 | 演示牌价换算，用途等同于 fx_convert |
| bank_transfer(payee, amount) | 写 | 显式写操作，服务端永远返回 -32000「演示服务不执行任何资金动作」；主服务也根本不转发 |

启动参数：--port / --host / --token（Bearer token，默认 demo-bank-token）/ --oauth（开放 token 端点）/ --no-sse（tools/call 回普通 JSON，便于排查）/ --slow（模拟慢响应）。

### 11.3 只读治理：四道闸，顺序固定

1. 服务必须已声明：未在 MCP_SERVERS（或未内置）的服务名直接拒绝。
2. 写操作硬拦（先于白名单）：工具在 write_tools、或名字命中写前缀（transfer / pay / withdraw / submit / cancel / execute / delete / update / create / modify / refund 等）、或服务 read_only 且未开全局写开关 → 拒绝。白名单即使写错，也放不进写操作。
3. 白名单：tools 非空时只允许其中工具。
4. tools/list 声明校验：工具必须由服务声明，否则拒绝。

四道闸都在客户端完成，请求不会到达银行服务——e2e 用服务端 write_calls==0 且 calls 不增加来证明。

### 11.4 降级与错误语义

| 场景 | /api/mcp/status | 调用返回 |
| --- | --- | --- |
| 服务不可达 / 超时 | ok=false、degraded=true、last_error 写明原因 | ok=False、degraded=True |
| 鉴权失败（401/403/invalid token/OAuth 失败） | 同上，last_error 以「鉴权失败」开头 | ok=False、degraded=True |
| 会话缺失 / 失效 | 自动重建会话；仍失败则降级 | 同上 |
| MCP_ENABLED=0 | 全部 ok=false，last_error=「MCP 未启用（MCP_ENABLED=0）」 | ok=False、degraded=True |
| MCP_SERVERS 坏 JSON | 回退 fx/risk，config_error 写原因 | 内置服务照常 |
| 部分配置非法 | 保留合法项，error 汇总非法原因 | — |

### 11.5 验收

- scripts/mcp_gateway_e2e.py 17/17（进程内起停银行服务；覆盖未配置回退、多服务解析、Bearer 会话、SSE 调用、服务端计数、写拒绝且零转发、白名单、错误 token、OAuth2、密钥脱敏、两参兼容、插件动态出现、MCP_ENABLED=0、全局写开关不能放行显式 write_tools、坏 JSON、部分非法、未知服务/工具）。
- 回归不倒退：run_tasks 56/56、e2e_stream 33/33、capability_e2e 63/63、security_factor_e2e 53/53、plugin_e2e 17/17、mcp_e2e 7/7、selftest 5/5、smoke18 审计链 valid、skill_check 6/6、smoke_normalize 10/10、channel_e2e 9/9、wecom 9/9 + channel_wecom_e2e 13/13、push_e2e 13/13、前端 247/0。
- 演示：起银行服务并配好 MCP_SERVERS 后，GET /api/mcp/status 出现 bank 行（transport=streamable-http、auth.type=bearer、session=true），GET /api/plugins 多出 mcp-bank。
- scripts/mcp_probe.py 实测（2026-10-28）：对演示银行服务列出 3 个工具、把 bank_transfer 标为写风险、退出码 0；`--json` 输出含 tools / writes / auth / session。

### 11.6 接入真实银行开放平台（落地步骤）

前置材料（向银行 / 开放平台要）：MCP 端点 URL（一般 `https://.../mcp`）与传输方式（`streamable-http` 或 `http`）；鉴权方式与凭证（Bearer token / API Key 头 / OAuth2 client_credentials 的 token_url + client_id + client_secret + scope）；可用工具清单与用途（只读为主，哪些会动钱）；超时与限流要求；是否需要 IP 白名单。

**步骤一：写配置**（MCP_SERVERS 是单行 JSON 数组；密钥只写 env 引用，绝不进 JSON）

    # 1) Bearer token
    {"name":"bank","label":"XX 银行","url":"https://open.example.bank/mcp","transport":"streamable-http",
     "tools":["account_summary","fx_quote"],"write_tools":["transfer"],
     "auth":{"type":"bearer","token":"env:BANK_MCP_TOKEN"}}

    # 2) 自定义请求头（API Key + Secret）
    {"name":"bank","url":"https://open.example.bank/mcp","transport":"streamable-http",
     "auth":{"type":"headers","headers":{"X-Api-Key":"env:BANK_API_KEY","X-Api-Secret":"env:BANK_API_SECRET"}}}

    # 3) OAuth2 client_credentials（token 按 expires_in 自动缓存）
    {"name":"bank","url":"https://open.example.bank/mcp","transport":"streamable-http",
     "auth":{"type":"oauth2","token_url":"https://open.example.bank/oauth/token",
             "client_id":"env:BANK_CLIENT_ID","client_secret":"env:BANK_CLIENT_SECRET","scope":"read"}}

**步骤二：自检（不用起主服务）**

    set MCP_SERVERS=...            # 上面的 JSON（Windows CMD）；bash 用 export
    set BANK_MCP_TOKEN=...
    python scripts/mcp_probe.py bank        # 或 --json 输出机器可读结果

脚本只做 initialize → tools/list，绝不调用任何工具；退出码 0 表示端点可达、鉴权通过、工具清单拿到。标「写风险」的工具不要写进 tools 白名单。本机对演示银行服务的实测输出：

    [OK  ] bank（演示银行）
           streamable-http / http://127.0.0.1:8399/mcp / auth=bearer(已配置) / session
           工具 3 个
             - bank_account_summary               只读
             - bank_quote                         只读
             - bank_transfer                      写风险：勿加入白名单
    结果：1/1 个服务探测成功

**步骤三：最小白名单 + 重启主服务**

把步骤二确认过的只读工具填进 `tools`（生产建议显式列出，留空表示不限只读工具但仍受四道闸约束），重启 `backend/server.py`，再确认：

    curl.exe -s http://127.0.0.1:8000/api/mcp/status    # bank 行 ok=true、transport=streamable-http、auth.type=bearer、session=true
    curl.exe -s http://127.0.0.1:8000/api/plugins       # 出现 mcp-bank 插件

**步骤四：确认边界**

- 写工具写进 `write_tools` 只是「提前判写」，不是放行；`MCP_ALLOW_WRITE_TOOLS=1` 也放行不了显式 `write_tools`（见 11.3）。
- 银行 MCP 只影响对话里的外部数据来源与审计事件 `mcp_call` / `mcp_fallback`，不改变审批、撤销与资金边界。
- 服务不可用时自动降级：fx 回退内置参考价，其余工具返回 `ok=False`，对话不受影响。

**上线前检查清单**

- [ ] 端点为 https，证书链正常（私有 CA 需在部署环境信任）
- [ ] 凭证只在环境变量 / 密钥管理里；仓库、日志、status 均无明文（status 只回 type / configured / token_cached）
- [ ] `tools` 最小化：只接只读工具
- [ ] 会动钱的工具在 `write_tools` 里（即使工具名不像写操作也显式列出）
- [ ] `timeout_ms` 按银行 SLA 设置（默认 10000；慢接口别拖住对话）
- [ ] 银行侧 IP 白名单已加入部署机出口 IP
- [ ] 凭证轮换方式：换 token 只需改环境变量并重启进程（注册表指纹变化会自动重建会话）

**限制（诚实说明）**

- 不内置任何银行 SDK，也不做请求签名：若平台要求 HMAC 签名、mTLS 或双向证书，需要扩展 `backend/app/mcp_client.py` 的 `_rpc_headers()`（追加签名头）与 `_post()`（注入 ssl context），并在注册表透传对应字段；属小改动，但本仓库未内置。
- OAuth2 只实现 client_credentials（服务间只读调用）；授权码模式 / 用户态 token 不适用。
- 外部结果只加严不加宽：外部工具结果只用于展示与参考，不会降低风险等级或绕过审批。
