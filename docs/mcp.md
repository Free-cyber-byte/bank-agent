# MCP 外部只读服务（v0.2.0）

> 一句话：汇率换算与反欺诈评分两项只读能力通过 MCP 接入，正常时提供实时数据，异常时自动降级到内置实现，业务照常执行、能动钱的边界不变。

本文档的接口名、字段与默认值来自 实施契约_v0.2.0.md §3.1–§3.3、§3.6–§3.7，并已按 backend/app/mcp_client.py、backend/external/、server.py 的四条只读路由与 scripts/mcp_e2e.py（7/7）校对（2026-10-27）；渠道部分见 docs/channel.md（channel_e2e 9/9）。

## 一、设计原则

1. 只读：fx_convert 与 risk_screen 都不产生资金动作，不是转账、申购、撤单等写操作。
2. 外部数据只当数据：返回值不拼进提示词，不参与意图解析，不能降低风险等级（fail-safe：外部结果只能加严）。
3. 全部可降级：连接失败、超时、JSON-RPC error 都退化为内置实现，评测与演示不因外部服务缺席而失败。
4. 零依赖：服务端用标准库 http.server + json 实现，客户端用标准库 urllib 发 HTTP POST。
5. 可观测：每次调用写审计（mcp_call，降级时另记 mcp_fallback），页面有外部服务状态卡与降级 InfoBar。

## 二、组成

| 文件 | 职责 |
| --- | --- |
| backend/app/mcp_client.py | JSON-RPC 客户端：握手、工具列表缓存、调用、状态汇总；绝不抛异常 |
| backend/external/fx_server.py | 汇率服务，端口 8300，工具 fx_convert |
| backend/external/risk_server.py | 反欺诈服务，端口 8301，工具 risk_screen |
| backend/external/selftest.py | 进程内起停两服务，跑 initialize / list / call / 超时 / 关闭 5 场景 |
| backend/app/config.py | MCP_ENABLED、MCP_FX_URL、MCP_RISK_URL、MCP_TIMEOUT_MS、MCP_FX_FALLBACK_RATE |
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
          "name": "fx" | "risk",
          "url": str,
          "ok": bool,
          "latency_ms": int,
          "tools": [str],
          "last_error": str,
          "degraded": bool
        }
      ]
    }

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
- 不做鉴权与 mTLS：服务只监听回环地址，作为本机演示组件。
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
