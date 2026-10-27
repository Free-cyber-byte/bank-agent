# ZeroStep 零步银行

> **一句话定位：说出来就办好，钱不会乱动。**

ZeroStep（零步银行）是一个面向「手机银行 AI 助手」场景的可运行参考实现：用户用一句自然语言说出需求，Agent 走完「理解 → 规划 → 策略校验 → 执行 → 回读 → 回复」六阶段流水线，把查询、转账、理财、卡片、订阅、跨场景分账真正做完。它同时是一份可被审计、可被评测的工程样本：资金动作全部由确定性工具执行，模型只负责产出意图与槽位，任何高风险动作都必须经过确认或 TOTP 动态口令，整条事件链用 SHA-256 哈希链固化。

本仓库为参赛作品（hackathon demo），后端零第三方依赖，仅使用 Python 标准库，30 条评测任务实测全部通过。

---

## 核心亮点

1. **零依赖、单命令启动**：后端只用 http.server + sqlite3 + 标准库实现 HTTP/SSE 服务，无需 pip install、无需 Node 构建；Python 3.10+ 下执行 python backend/server.py 即可打开完整界面，不配置任何 API Key 也能跑完全部功能。
2. **确定性安全内核**：自然语言只被解析成「意图 + 槽位」，真正的资金变动由 policy 三级闸门与 mock_core 工具执行；模型无法绕过策略门直接改账，越权与注入在进入计划前就被拦截。
3. **大模型接入（三模式）**：解析层真实接入大模型（默认 DeepSeek deepseek-flash，改两个环境变量即可换成任意 OpenAI 兼容服务），支持 rule / llm / auto 三种模式，默认 auto——无 Key 自动回落规则，有 Key 由模型输出意图与槽位；模型输出经严格 schema、金额原文溯源与通讯录匹配校验，攻击判定始终由代码先行，模型不可信、不可授权。
4. **可中断、可恢复**：超过 1000 元或新收款人等高风险动作会挂起为审批态，靠 checkpoint 与 thread_id 持久化上下文，审批 TTL 300 秒；前端关掉再回来仍能继续同一个线程。
5. **可审计、可复盘**：每一次意图、策略决策、工具调用、审批、撤销都写入 events 与 audit 表，审计用 canonical JSON + SHA-256 + prev_hash 组成哈希链，scripts/verify_audit.py 可离线验证链是否被篡改；转账支持 60 秒内撤销，并在写后回读余额做二次校验。
6. **可评测、可扩展**：tasks/ 下 30 条任务覆盖 7 个类别（账单、转账、理财、卡片、订阅、跨场景、安全攻击），scripts/run_tasks.py 一条命令跑完全部断言，两种解析模式实测均 30/30 通过，并带 --filter、--category、JSON 报告等选项，新增用例只需加一个 JSON 文件。

---

## 界面一览

四态截图（1600×1000，原生 JS，零依赖，无渐变/无外链资源）：

![业务台空态](docs/screenshots/01-desk.png)
*业务台：余额 hero + 资产构成微条 + 2 列快捷任务，中央三张示例指令*

![查询对话](docs/screenshots/02-query.png)
*一轮对话：意图 chip → 编号执行计划 → 工具 KPI 卡 → 结论卡，右栏竖向执行轨迹*

![审批回单](docs/screenshots/03-approval.png)
*审批回单：44px 金额 + 四行发丝表格 + 来源与理由 + 执行后预览 + 单次 nonce 声明*

![审计抽屉](docs/screenshots/04-audit.png)
*审计抽屉：SHA-256 哈希链逐条可校验，可导出 audit.log*

---

## 架构一览

    浏览器（frontend/ 原生 JS 单页应用）
        │  POST /api/chat（SSE 流式）、POST /api/approval、POST /api/undo
        ▼
    HTTP 层  backend/server.py（http.server，零依赖，含路径穿越防护）
        ▼
    编排层  backend/app/engine.py
        run_turn：parse_intent → plan → policy_gate → act → verify → respond
                 parse_intent = llm.parse（按 rule / llm / auto 分派到规则或模型）
        resume  ：审批恢复（checkpoint + thread_id）
        ▼
    解析与安全  llm.py（可选模型解析，三模式） · nlu.py（规则解析/攻击识别） ·
                policy.py（三级权限） · approvals.py（审批单/TTL） ·
                totp.py（RFC 6238） · audit.py（哈希链）
        ▼
    领域层  backend/app/mock_core.py（转账/理财/卡片/订阅/还款/提醒，金额整数分）
        ▼
    存储层  SQLite（backend/data/bank.db）：账户、流水、转账、审批、
            会话、checkpoint、事件、审计链

---

## 快速开始

环境要求：Python 3.10 及以上（CI 覆盖 3.10 与 3.12），无第三方依赖。

    git clone https://github.com/Free-cyber-byte/bank-agent.git
    cd bank-agent
    python backend/server.py

    # Windows 用户也可以直接双击 start.bat：它已内置 chcp 65001 + PYTHONUTF8=1，
    # 中文不会乱码，并自动打开浏览器；默认端口 8000，可用 set PORT=8010 覆盖。
    # 若在旧版 PowerShell 里手动起服务看到乱码，先执行 $env:PYTHONUTF8 = "1"。

服务默认监听 127.0.0.1:8000，浏览器打开 http://127.0.0.1:8000 即可对话。首次启动会自动建库并写入确定性种子数据（3 个用户、6 个月流水、5 个收款人、4 支理财、5 个订阅）。

不需要任何 API Key 也能完整运行：默认的 auto 模式在没有 Key 时等价于规则解析，30 条评测全部通过。想体验模型解析，把 Key 写进仓库根目录的 .env 文件（推荐，已被 .gitignore 排除）即可，界面顶栏可在「规则 / 自动 / 模型」之间切换（「模型」即调用大模型解析，悬停可看到当前厂商与模型名）。

配置模型（二选一，环境变量优先于 .env）：

    # 方式一：.env 文件（推荐，一次配置长期有效）
    copy .env.example .env        # Windows；Linux/macOS 用 cp
    # 用记事本打开 .env，把 DEEPSEEK_API_KEY= 后面填上你的 Key

    # 方式二：临时环境变量（只对当前窗口有效）
    $env:DEEPSEEK_API_KEY = "sk-..."   # PowerShell
    set DEEPSEEK_API_KEY=sk-...        # cmd

Key 只在本机读取，不写入仓库、数据库、事件或审计日志；接口与页面只显示掩码后的后四位。没有 .env 文件时程序静默跳过，行为与未配置完全一致。

常用环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| PORT | 8000 | HTTP 端口 |
| HOST | 127.0.0.1 | 监听地址 |
| DEMO_MODE | 1（开启） | 设为 0 关闭 /api/demo/totp 等演示接口 |
| DEEPSEEK_API_KEY | 空 | 可选。设置后启用 LLM 解析（默认 auto 模式下生效）；不设置时自动使用规则解析，功能与评测结果不受影响。可写在 .env 中 |
| DEEPSEEK_BASE_URL | https://api.deepseek.com | 可选。OpenAI 兼容接口地址，接入其他兼容服务时只需改这一项 |
| DEEPSEEK_MODEL | deepseek-flash | 可选。模型名，需与接口支持的型号一致 |
| DEEPSEEK_TIMEOUT | 12 | 可选。单次模型调用超时秒数，超时自动回退规则解析 |
| ZEROSTEP_DISABLE_LLM | 空 | 设为 1 时即使有 Key 也强制走规则模式 |

不是只能接 DeepSeek：接口走 OpenAI 兼容协议（POST {base_url}/chat/completions + Bearer + JSON 模式），把 DEEPSEEK_BASE_URL / DEEPSEEK_MODEL / DEEPSEEK_API_KEY 换成对应厂商即可，界面上的厂商与模型名会随之变化。常见示例（仅供填写参考，除 DeepSeek 外未逐一实测，以各厂商文档为准，且对方需支持 JSON 输出）：

| 服务 | DEEPSEEK_BASE_URL | DEEPSEEK_MODEL 示例 |
| --- | --- | --- |
| DeepSeek（默认） | https://api.deepseek.com | deepseek-flash |
| 阿里云百炼（OpenAI 兼容） | https://dashscope.aliyuncs.com/compatible-mode/v1 | qwen-plus |
| Moonshot | https://api.moonshot.cn/v1 | moonshot-v1-8k |
| 智谱 GLM | https://open.bigmodel.cn/api/paas/v4 | glm-4-flash |

想快速感受一次完整交互，可直接跑演示脚本：

    python scripts/replay.py --script scripts/demo_flow.json

---

## 大模型接入（三模式）

解析层支持三种模式，可在界面顶栏或通过 POST /api/llm/mode 运行时切换：

| 模式 | 解析方式 | 适用场景 |
| --- | --- | --- |
| rule | nlu.py 规则/正则解析 | 离线演示、可复现评测、无 Key 环境 |
| llm | 调用大模型（默认 DeepSeek deepseek-flash）输出意图与槽位，再经代码层严格校验 | 口语化、开放表达 |
| auto（默认） | 有 Key 用 llm，无 Key 回落 rule | 默认体验 |

接入是真实 HTTP 调用，不是占位：backend/app/llm.py 用标准库 urllib 请求 OpenAI 兼容接口 /chat/completions，默认模型 deepseek-flash，temperature=0、response_format=json_object、单次超时 12 秒（均可用环境变量覆盖）。模型只被允许输出一份 JSON：16 个预定义意图之一、金额、收款人、月份、品类、产品、订阅、卡、分账列表、提醒与理由；未在原文出现的字段必须为 null，不允许猜测。

三道约束保证模型不影响资金安全：

1. 攻击判定在模型之前：每次解析先走 nlu.detect_attack 的代码规则，命中注入、钓鱼、越权或陌生收款人直接返回 attack，模型不参与判定，也不会被调用。
2. 输出必须过校验：金额必须在原文中找到出处，否则改用规则金额并在审批卡提示冲突；收款人必须命中通讯录或出现在原文；产品、订阅、卡必须匹配种子数据；字段越界一律回退规则值。
3. 风险与动作仍在代码层：模型只产出意图与槽位，build_plan、policy.evaluate、审批、执行、回读、审计全部由确定性代码完成，模型无法决定放行、降低风险或选择工具。

调用失败（网络不可达、超时、返回非 JSON、字段异常）会自动回退规则解析，事件与审计里记 source=fallback，失败原因通过 GET /api/llm/status 的 last_error 暴露；API Key 只从环境变量读取，不写入数据库、仓库与日志，状态接口只返回脱敏后的 key_masked。

实测（2026-10-27 11:18，deepseek-flash，temperature=0，超时 12 秒）：30 条标准用例两种模式均 30/30；10 条口语化变体规则听懂 4 条、模型听懂 10 条；安全用例 t26–t30 两种模式均 5/5。完整对比表见「评测」节，接口与字段见 docs/api.md，安全边界见 docs/security.md。

---

## 评测与任务

30 条任务分 7 类，分布为 5 / 5 / 4 / 4 / 4 / 3 / 5：

| 类别 | 任务 id | 条数 | 覆盖内容 |
| --- | --- | --- | --- |
| bills 账单查询 | t01–t05 | 5 | 账单汇总、明细查询、预算分析、余额查询 |
| transfer 转账 | t06–t10 | 5 | 小额转账（确认）、大额转账（TOTP）、新收款人拦截、超限拦截、余额不足拦截 |
| wealth 理财 | t11–t14 | 4 | 申购（TOTP + 撤销）、持仓查询、低于起购金额、超额申购拦截 |
| card 卡片 | t15–t18 | 4 | 卡列表、申请虚拟卡、卡状态管理 |
| subscription 订阅 | t19–t22 | 4 | 订阅查询、取消订阅（确认）、不存在的订阅拦截 |
| cross 跨场景 | t23–t25 | 3 | 工资分账、生日转账 + 提醒、旅行预算分析 |
| security 安全 | t26–t30 | 5 | 直接提示注入、验证码钓鱼、越权查询、陌生收款人、流水备注间接注入 |

运行方式：

    cd bank-agent
    $env:PYTHONIOENCODING = "utf-8"   # Windows 建议设置

    python scripts/run_tasks.py                          # 全量评测
    python scripts/run_tasks.py --category security -v   # 只跑安全类
    python scripts/run_tasks.py --filter t01,t06         # 指定任务
    python scripts/run_tasks.py --json scripts/eval_report.json   # 导出完整报告

    python scripts/verify_audit.py                       # 校验审计哈希链
    python scripts/verify_audit.py --db backend/data/bank.db --limit 200 --json

    python scripts/smoke18.py                           # 18 轮端到端演示流 + 审计链自证
    python scripts/smoke18.py --json scripts/smoke18_result.json

    python scripts/smoke_llm_fallback.py                # 模型不可用 → 自动回退规则（无需 Key）
    python scripts/smoke_llm_http.py                    # LLM 三模式 HTTP 冒烟（需 Key）

### 规则模式与 LLM 模式对比

同一批用例在两种解析模式下各跑一遍（脚本 scripts/compare_modes.py，结果落在 scripts/eval_modes.md 与 eval_modes.json；2026-10-27 11:18 生成，模型 deepseek-flash，temperature=0，超时 12 秒）：

| 指标 | 规则模式 | LLM 模式 |
| --- | --- | --- |
| 30 条标准用例通过率 | 30/30 | 30/30 |
| 10 条口语化变体听懂率 | 4/10 | 10/10 |
| 安全用例通过（t26–t30） | 5/5 | 5/5 |
| 解析耗时 均值 / p95 | 0.03 ms / 0.04 ms | 1219.37 ms / 1969.92 ms |

复跑：

    python scripts/compare_modes.py            # 需要 DEEPSEEK_API_KEY，否则 llm 模式自动回落规则
    python scripts/compare_modes.py --no-llm   # 只跑规则模式，离线可跑

结论：模型解析把口语化表达的听懂率从 4/10 提升到 10/10，代价是单次解析约 1.2 秒；标准用例与安全用例结果不变，说明风险分级、审批与拦截完全由代码层决定，接入模型没有放宽任何安全边界。规则模式保留为默认回退路径，保证无 Key、断网时演示与评测仍可复现。

实测结果（本机 Python 3.14.6，2026-10-26）：

    结果：30/30 通过，0 失败

执行器会为每条任务创建独立临时数据库（.eval_tmp/任务id/bank.db），重建种子数据后运行，互不污染；需要审批的用例会自动用当前 TOTP 走完 approve 流程，因此测的是完整链路而不是桩函数。断言字段与新增用例方法见 docs/evaluation.md。

另有一条端到端演示流用于现场复现：scripts/smoke18.py 在临时库上顺序跑 17 轮真实对话（账单、流水、余额、四类转账、理财、订阅、卡片、分账、跨场景、四类攻击）再撤销最后一笔，结束后校验审计链。实测输出：18 轮 0.41 秒、审计链 valid=True 共 56 条、事件 111 条、业务表 17 张，完整结果见 scripts/smoke18_result.json。

---

## 安全设计摘要

| 风险等级 | 判定依据 | 用户动作 |
| --- | --- | --- |
| LOW | 纯查询类工具（账单、流水、余额、持仓、收款人列表等） | 自动放行 |
| MEDIUM | 1000 元以内转账、取消订阅、虚拟卡申请/状态变更、信用卡还款 | 弹层确认（confirm） |
| HIGH | 超过 1000 元或 24 小时累计超 1000 元、新收款人首笔、理财申购 | 输入 TOTP 动态口令（totp） |
| BLOCKED | 金额非法、超 5 万元硬上限、余额不足、产品不存在/未达起购、新收款人无账户信息 | 直接拒绝并给出原因 |

关键机制：

- **1000 元阈值**：对齐「小额可确认、大额必强验证」的直觉边界，同时按单笔与 24 小时累计两个维度判定，防止拆单绕过；演示系统另有 5 万元单笔硬上限。
- **TOTP 标准实现**：totp.py 按 RFC 6238 用标准库实现，HMAC-SHA1、30 秒步长、6 位数字、±1 个时间窗容错，无需 pyotp。
- **审计哈希链**：audit.py 对排序后的 canonical JSON 做 SHA-256，并把上一条 hash 作为 prev_hash 字段一并哈希，任何一条被改写都会在 verify 时定位到 broken_at。
- **提示注入四层防御**：入口规则检测 → 意图与执行隔离（攻击意图不建计划）→ 工具返回内容消毒（可疑指令替换为占位符并标记）→ 策略门 + 审批 + 哈希链兜底。
- **模型只出意图、不出决策**：LLM 模式沿用同一套策略门与审批；模型输出经 schema、金额原文溯源与通讯录匹配校验，攻击判定和金额/收款人校验都在代码层，调用失败自动回退规则，模型不可信。
- **可撤销与写后回读**：转账/申购 60 秒内可撤销（生成冲正流水）；转账成功后回读余额，不一致即标记审计异常并提示用户。

完整威胁模型、攻击用例与已知限制见 [docs/security.md](docs/security.md)。

---

## 目录结构

    bank-agent/
    ├── backend/
    │   ├── server.py              HTTP/SSE 服务入口（零依赖，278 行）
    │   ├── app/
    │   │   ├── config.py          路径、阈值、LLM 与演示开关
    │   │   ├── db.py              SQLite 建表、索引与确定性种子数据
    │   │   ├── nlu.py             规则意图解析 + 注入/钓鱼/越权检测
    │   │   ├── llm.py            模型解析（DeepSeek 三模式、严格校验与回退）
    │   │   ├── engine.py          六阶段编排、计划生成、审批恢复、撤销
    │   │   ├── policy.py          三级权限矩阵与参数级校验
    │   │   ├── approvals.py       审批单创建、TTL 校验、TOTP 联动
    │   │   ├── totp.py            RFC 6238 动态口令
    │   │   ├── audit.py           canonical JSON + SHA-256 哈希链
    │   │   └── mock_core.py       银行领域模拟（金额整数分、幂等、冲正）
    │   └── data/bank.db           运行期数据库（首次启动自动生成）
    ├── frontend/
    │   ├── index.html             单页界面
    │   ├── app.js                 SSE 消费、审批弹层、审计面板
    │   └── styles.css             样式
    ├── tasks/t01.json … t30.json  30 条评测任务（声明式断言）
    ├── scripts/
    │   ├── run_tasks.py           评测执行器
    │   ├── verify_audit.py        离线校验审计链
    │   ├── compare_modes.py       规则 / LLM 双模式对比评测
    │   ├── replay.py              命令行演示
    │   ├── demo_flow.json         6 轮演示脚本
    │   ├── smoke_http.py          HTTP 冒烟测试（REST + SSE 全链路）
    │   ├── smoke_llm_http.py      LLM 接口 HTTP 冒烟（三模式切换与状态）
    │   ├── smoke_llm_fallback.py  模型不可用时回退规则链路的冒烟测试
    │   ├── smoke18.py             18 轮端到端演示流 + 审计链自证
    │   └── smoke18_result.json    上述运行器的完整实测输出（供复核）
    ├── docs/
    │   ├── api.md                 HTTP 接口契约（含 LLM 三模式接口）
    │   ├── architecture.md        架构与流水线详解（三模式解析）
    │   ├── security.md            安全设计与攻击测试（含模型不可信组件）
    │   ├── design-system.md       界面设计规范（去 AI 味、视觉令牌、审批卡）
    │   └── evaluation.md          评测任务、断言与双模式对比
    ├── .env.example               本地配置模板（复制为 .env 后填 Key，.env 不进仓库）
    └── .github/workflows/ci.yml   CI：3.10/3.12 跑评测与演示

---

## 文档索引

| 文档 | 内容 |
| --- | --- |
| [docs/architecture.md](docs/architecture.md) | 分层架构、六阶段流水线、跨场景 DAG、审批中断与恢复、幂等键、事件与审计持久化 |
| [docs/security.md](docs/security.md) | 三级权限矩阵、阈值理由、TOTP 与哈希链实现、注入四层防御、攻击测试、已知限制 |
| [docs/evaluation.md](docs/evaluation.md) | 30 条任务表、断言字段说明、规则 / LLM 双模式对比与评测方法、新增任务方法 |
| [docs/api.md](docs/api.md) | HTTP/SSE 接口契约（含 LLM 状态与模式切换接口、审批对象新字段） |
| [docs/design-system.md](docs/design-system.md) | 前端 v2 设计规范：视觉令牌、三栏布局、审批卡与来源标注 |

---

## 免责声明

本项目是**纯模拟的银行 Agent 演示**：不接入任何真实银行、支付或清算系统，账户、余额、流水、理财与卡片均为 seed 生成的虚构数据；TOTP 密钥由 user_id 派生且 DEMO_MODE 下 /api/demo/totp 会直接返回当前口令，仅用于演示便利；数据库为本地 SQLite 单文件，无登录鉴权、无多租户隔离。**请勿将本项目用于任何真实资金场景。**
