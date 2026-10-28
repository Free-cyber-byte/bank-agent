# 插件注册表（Plugins）

v0.3.0 引入插件注册表：把零步银行的能力拆成七个可观测、可启停的插件，统一由 GET /api/plugins 展示，并允许在设置页停用非核心能力。v0.6.0 起注册表可扩展：MCP_SERVERS 声明的每个外部 MCP 服务会额外挂一个 mcp-<name> 插件（默认仍是七个）。核心原则只有一条——**停用插件只能减少功能，不能放宽任何资金安全边界**。

实现：backend/app/plugins.py（335 行，标准库，无第三方依赖）；路由：backend/server.py（GET /api/plugins、POST /api/plugins/<id>/toggle）。

## 1. 设计来源

借鉴 DSH（DeepSeek Harness）的 capability 注册表思路：

- 单一事实来源：能力清单写在 REGISTRY 常量里，状态接口、计数、审计、设置页都从这一份清单派生，不在各处重复维护。
- 声明式描述：每个插件声明 id、名称、类别、版本、描述、提供的工具/技能/路由、依赖的环境变量、启用方式（how）。
- 核心不可停用：资金安全相关的 policy 与 audit 标记为 core，toggleable=false，接口层直接拒绝停用请求。
- 可降级优先：外部服务类插件（mcp-fx、mcp-risk）停用或不可用时，业务回退内置能力，而不是报错中断。

## 2. 七个插件

注册表顺序固定为 policy、audit、mcp-fx、mcp-risk、intent-llm、skill-pack、channel-im；供前端设置页按此顺序展示。未配置 MCP_SERVERS 时注册表就是这七个；配置后每个外部服务按 name 生成 mcp-<name> 插件，插在 mcp-risk 之后、按配置顺序排列（受 MCP_MAX_SERVERS=8 限制），kind=mcp、toggleable=true，字段结构与内置插件一致，停用只影响该服务。

| id | 名称 | kind | 可停用 | 提供的工具 / 技能 / 路由 | 依赖环境变量 |
| --- | --- | --- | --- | --- | --- |
| policy | 权限分级与审批 | core | 否 | /api/approval、/api/demo/totp | 无 |
| audit | 哈希链审计 | core | 否 | /api/audit、/api/events | 无 |
| mcp-fx | 汇率服务 | mcp | 是 | fx_convert；/api/mcp/status | MCP_ENABLED、MCP_FX_URL、MCP_TIMEOUT_MS、MCP_FX_FALLBACK_RATE |
| mcp-risk | 反欺诈风控 | mcp | 是 | risk_screen（状态随 /api/mcp/status 展示，自身无独立路由） | MCP_ENABLED、MCP_RISK_URL、MCP_RISK_BLOCK_SCORE |
| intent-llm | 模型意图通道 | model | 是 | llm.parse；/api/llm/mode、/api/llm/status | DEEPSEEK_API_KEY、DEEPSEEK_MODEL、DEEPSEEK_BASE_URL |
| skill-pack | 技能卡包 | skill | 是 | 六张技能卡 bills/cards/cross/subscriptions/transfer/wealth；/api/skills | 无 |
| channel-im | IM 渠道 | channel | 是 | /api/channel/status | CHANNEL_TOKEN、CHANNEL_PORT、CHANNEL_ENABLED |

kind 共五类：core（核心能力）、mcp（外部服务）、model（模型通道）、skill（技能包）、channel（渠道）。核心两项的 locked_reason 固定为「核心能力不可停用」。

## 3. 状态机

每个插件对外只有三个运行态：

| status | 含义 | 判定 |
| --- | --- | --- |
| enabled | 已启用且工作正常 | 开关为开；mcp 类插件探测通过（或该插件没有探测项） |
| degraded | 已启用但外部依赖不可用 | 开关为开；mcp 探测失败（服务没起、超时、MCP_ENABLED=0） |
| disabled | 被用户显式停用 | 开关为关，优先于探测结果 |

- 探测只针对 mcp 类插件（probe 字段为 fx 或 risk），一次状态接口调用只探测一次，结果复用给两项，不重复请求。
- core 插件的 enabled 恒为 true，状态接口里仍带 toggleable=false 与 locked_reason，供前端置灰按钮。
- degraded 是运行态、不落盘：重启后按当次探测重新计算；disabled 来自状态文件，跨重启保留。
- mcp-<name> 插件（v0.6.0）：探测对应该外部服务；停用后 /api/mcp/status 该项 status=disabled、last_error=插件已停用、不发起外部调用；不影响 mcp-fx / mcp-risk 与其它服务。

## 4. HTTP API

### GET /api/plugins

返回注册表全量 + 运行态 + 计数，永不抛异常（内部异常兜底为 500）。下面按七个插件全部可用时的形态展示字段（2026-10-27 本地实测：version 已读到 0.3.0；MCP_ENABLED=0 时 mcp-fx 与 mcp-risk 的 status 为 degraded、ok 为 false，其余字段一致）：

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
          "id": "policy",
          "name": "权限分级与审批",
          "kind": "core",
          "version": "0.3.0",
          "description": "对转账、还款等敏感动作做风险分级、二次确认与 TOTP 强验证，是资金安全的第一道闸门。",
          "status": "enabled",
          "enabled": true,
          "toggleable": false,
          "provides": { "tools": [], "skills": [], "routes": ["/api/approval", "/api/demo/totp"] },
          "requires": [],
          "detail": {
            "how": "内置核心能力：按 policy 规则给出风险等级与审批单，敏感动作必须二次确认；不允许停用。",
            "env": [],
            "endpoints": ["/api/approval", "/api/demo/totp"]
          },
          "locked_reason": "核心能力不可停用"
        }
      ]
    }

字段说明：

| 字段 | 说明 |
| --- | --- |
| ok / count / enabled_count | 接口自身可用；插件总数；当前启用数（含 core） |
| kinds | 五类 kind 的展示名，固定顺序 |
| plugins[].id/name/kind/description | 注册表静态字段 |
| plugins[].version | 进程启动时读取的 config.VERSION（仓库根 VERSION 文件），改版本后需重启 |
| plugins[].status/enabled | 运行态与开关；core 恒 enabled |
| plugins[].toggleable/locked_reason | 是否允许停用；不允许时的原因文案 |
| plugins[].provides | 该能力对外提供的 tools / skills / routes |
| plugins[].requires | 依赖的其他插件 id（当前七项均为空） |
| plugins[].detail.how/env/endpoints | 启用方式、相关环境变量、相关路由；endpoints 与 provides.routes 相同 |

### POST /api/plugins/<id>/toggle

请求体必须是一个 JSON 对象，且 enabled 为布尔值：

    { "enabled": false }

成功返回 200，plugin 为切换后的完整插件对象，counts 为全局计数（不含探测）：

    {
      "ok": true,
      "plugin": {
        "id": "mcp-fx",
        "name": "汇率服务",
        "kind": "mcp",
        "version": "0.3.0",
        "status": "disabled",
        "enabled": false,
        "toggleable": true,
        "provides": { "tools": ["fx_convert"], "skills": [], "routes": ["/api/mcp/status"] },
        "detail": { "env": ["MCP_ENABLED", "MCP_FX_URL", "MCP_TIMEOUT_MS", "MCP_FX_FALLBACK_RATE"] },
        "locked_reason": ""
      },
      "counts": { "total": 7, "enabled": 6, "disabled": 1 }
    }

错误码（均返回 JSON）：

| HTTP | code | message | 触发条件 |
| --- | --- | --- | --- |
| 400 | INVALID_BODY | 参数不合法 | 请求体不是合法 JSON、不是对象、缺少 enabled 或 enabled 不是布尔值 |
| 403 | PLUGIN_CORE_LOCKED | 核心能力不可停用 | 对 policy 或 audit 发起停用（enabled=false） |
| 404 | PLUGIN_NOT_FOUND | 插件不存在 | id 不在注册表中 |
| 500 | 无固定 code，body 为 { "ok": false, "error": "服务不可用" } | 服务不可用 | 写入状态文件或其他内部异常 |

注意：对 core 插件发 enabled=true 同样返回 403——核心能力不接受任何 toggle 写操作。错误体为 { "error": { "code": "...", "message": "..." } }，与全局错误约定一致。

### POST /api/admin/reset

既有一键重置会顺带调用 plugins.reset()，清空状态文件，恢复为「全部启用」。

## 5. 停用后会发生什么

停用插件不会让已有数据消失，也不会放宽资金操作；影响面限定在下表：

| 停用插件 | 行为变化 | 是否影响资金安全 |
| --- | --- | --- |
| mcp-fx | 汇率查询不发外部请求，改用内置参考价；回复以「汇率插件已停用，按内置参考价 …… 估算」开头 | 否，仅展示 |
| mcp-risk | 跳过外部反欺诈核查；policy 仍完整生效；回复追加「外部反欺诈核查未执行（插件已停用），policy 安全规则仍然生效」 | 否，policy 兜底 |
| intent-llm | 解析强制回退 rule；/api/llm/status 的 effective 为 rule 且带 plugin_disabled=true | 否，解析层不参与分级 |
| skill-pack | /api/skills 返回 { count: 0, skills: [], disabled: true }；规则解析仍可用，技能页空态 | 否 |
| channel-im | /api/channel/status 的 enabled=false、sessions 按 0；渠道进程 POST /message 返回 503「渠道插件已停用」且不写消息日志 | 否，渠道层无业务判定 |
| policy / audit | 不允许停用 | — |

联动细节：

- GET /api/mcp/status：被停用的 mcp 项 status="disabled"、ok=false、degraded=true、last_error="插件已停用"；顶层 ok 仍为 true，另一项照常显示 ok 或 degraded。
- 渠道插件停用判断先于 CHANNEL_ENABLED：即使 CHANNEL_ENABLED=0，返回的也是 503「渠道插件已停用」，且不落 messages.jsonl。
- 停用 mcp-risk 只是不做外部核查，转账仍要过 policy；停用 mcp-fx 只影响汇率展示，不动任何余额。

## 6. 持久化

- 状态文件：config.PLUGINS_FILE，默认 backend/data/plugins.json，可用同名环境变量指向临时文件（测试用）。
- 文件内容形如 { "mcp-fx": false, "skill-pack": false }，只记录被显式切换过的非核心插件；未出现的 id 按启用处理。
- 读取策略：每次调用现读文件；文件不存在、JSON 损坏、顶层不是对象，一律视为空状态（全部启用），非布尔值忽略。删文件即可恢复默认，坏文件不会导致服务不可用。
- 写入策略：先写同目录临时文件再 os.replace 原子替换，避免半写坏文件；写入失败只打印 stderr，进程内开关仍然生效（内存状态优先），下次重启回到文件状态。
- POST /api/admin/reset 会删除状态文件，恢复全部启用；设置页的「全部恢复」走同一接口，插件状态与演示数据一并复位。
- 该文件已被 .gitignore 忽略，不会进入仓库；干净克隆或换机器默认全部启用。
- 版本号在进程启动时从仓库根 VERSION 读取（config._read_version），修改后必须重启服务才生效；插件卡片上的 version 字段同源。

演示前检查（QA 建议）：

1. 启动服务后先 GET /api/plugins，确认 ok=true、count=7、enabled_count=7；必要时在设置页点「全部恢复」或 POST /api/admin/reset，再刷新界面。
2. 若 mcp 类显示 degraded，说明外部服务未起或 MCP_ENABLED=0，属预期降级，不影响 43 条评测与演示主流程。
3. 停用过的插件会在插件页显示「已停用」，演示前建议恢复，避免汇率/技能等能力按降级路径展示。

## 7. 审计

每次成功的 toggle 写一条审计事件，事件名恰好为 plugin_toggled（不得改名，验收会按字面检查）：

    event_type: plugin_toggled
    tool: plugins.toggle
    args: { "plugin_id": "mcp-fx", "enabled": false }
    result: { "ok": true, "plugin_id": "mcp-fx", "enabled": false }
    risk_level: low

- 审计走既有哈希链，因此停用/启用记录与业务事件在同一条链上，可被 scripts/verify_audit.py 校验。
- 审计写入失败（数据库未绑定等）只打印 stderr，不阻断 toggle；此时状态已切换，事件缺失会在日志里体现。
- 被拒绝的 toggle（400/403/404）不写 plugin_toggled。

## 8. 与其它文档的边界

- 插件机制本身见本文；MCP 工具契约与会话降级见 docs/mcp.md；技能卡字段见 docs/skills.md；渠道协议见 docs/channel.md；接口字段总表见 docs/api.md。
- 本文只描述「能力开关」；风险分级、审批 TTL、TOTP、审计链的语义以 docs/security.md 为准。
- 验收状态：注册表与两条路由已由后端冻结（2026-10-27）。QA 回归（audit/qa-report-v0.3.0.md，2026-10-27 21:16 复跑）9 项全绿、0 FAIL：scripts/plugin_e2e.py 17/17（注册表结构、toggle 400/403/404、停用持久化、五项停用态联动、审计 plugin_toggled 与哈希链）、run_tasks 36/36、smoke18 18/18（审计链 valid，checked=81）、smoke_normalize 10/10、skill_check 6/6、mcp_e2e 7/7、channel_e2e 9/9、verify_audit valid 13 条、前端 node frontend/_selftest.js PASS=100 FAIL=0。