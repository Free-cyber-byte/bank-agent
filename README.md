# ZeroStep 零步银行

> **一句话定位：说出来就办好，钱不会乱动。**

ZeroStep（零步银行）是一个面向「手机银行 AI 助手」场景的可运行参考实现：用户用一句自然语言说出需求，Agent 走完「理解 → 规划 → 策略校验 → 执行 → 回读 → 回复」六阶段流水线，把查询、转账、理财、卡片、订阅、跨场景分账真正做完。它同时是一份可被审计、可被评测的工程样本：资金动作全部由确定性工具执行，模型只负责产出意图与槽位，任何高风险动作都必须经过确认或 TOTP 动态口令，整条事件链用 SHA-256 哈希链固化。

本仓库为参赛作品（hackathon demo），后端零第三方依赖，仅使用 Python 标准库，30 条评测任务实测全部通过。

---

## 核心亮点

1. **零依赖、单命令启动**：后端只用 http.server + sqlite3 + 标准库实现 HTTP/SSE 服务，无需 pip install、无需 Node 构建；Python 3.10+ 下执行 python backend/server.py 即可打开完整界面。
2. **确定性安全内核**：自然语言只被解析成「意图 + 槽位」，真正的资金变动由 policy 三级闸门与 mock_core 工具执行；模型无法绕过策略门直接改账，越权与注入在进入计划前就被拦截。
3. **可中断、可恢复**：超过 1000 元或新收款人等高风险动作会挂起为审批态，靠 checkpoint 与 thread_id 持久化上下文，审批 TTL 300 秒；前端关掉再回来仍能继续同一个线程。
4. **可审计、可复盘**：每一次意图、策略决策、工具调用、审批、撤销都写入 events 与 audit 表，审计用 canonical JSON + SHA-256 + prev_hash 组成哈希链，scripts/verify_audit.py 可离线验证链是否被篡改；转账支持 60 秒内撤销，并在写后回读余额做二次校验。
5. **可评测、可扩展**：tasks/ 下 30 条任务覆盖 7 个类别（账单、转账、理财、卡片、订阅、跨场景、安全攻击），scripts/run_tasks.py 一条命令跑完全部断言，实测 30/30 通过，并带 --filter、--category、JSON 报告等选项，新增用例只需加一个 JSON 文件。

---

## 架构一览

    浏览器（frontend/ 原生 JS 单页应用）
        │  POST /api/chat（SSE 流式）、POST /api/approval、POST /api/undo
        ▼
    HTTP 层  backend/server.py（http.server，零依赖，含路径穿越防护）
        ▼
    编排层  backend/app/engine.py
        run_turn：parse_intent → plan → policy_gate → act → verify → respond
        resume  ：审批恢复（checkpoint + thread_id）
        ▼
    安全与策略  nlu.py（意图/攻击识别） · policy.py（三级权限） ·
                approvals.py（审批单/TTL） · totp.py（RFC 6238） · audit.py（哈希链）
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
    python backend/server.py          # Windows PowerShell 若中文乱码，先执行 $env:PYTHONIOENCODING = "utf-8"

服务默认监听 127.0.0.1:8000，浏览器打开 http://127.0.0.1:8000 即可对话。首次启动会自动建库并写入确定性种子数据（3 个用户、6 个月流水、5 个收款人、4 支理财、5 个订阅）。

常用环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| PORT | 8000 | HTTP 端口 |
| HOST | 127.0.0.1 | 监听地址 |
| DEMO_MODE | 1（开启） | 设为 0 关闭 /api/demo/totp 等演示接口 |
| DEEPSEEK_API_KEY | 空 | 可选。配置后 config 中 LLM_ENABLED 为真、健康检查与事件里 llm_mode 变为 deepseek（当前版本解析链仍以规则引擎为准，大模型增强入口为预留，详见 docs/security.md 已知限制） |
| ZEROSTEP_DISABLE_LLM | 空 | 设为 1 强制关闭 LLM 模式 |

想快速感受一次完整交互，可直接跑演示脚本：

    python scripts/replay.py --script scripts/demo_flow.json

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
- **可撤销与写后回读**：转账/申购 60 秒内可撤销（生成冲正流水）；转账成功后回读余额，不一致即标记审计异常并提示用户。

完整威胁模型、攻击用例与已知限制见 [docs/security.md](docs/security.md)。

---

## 目录结构

    bank-agent/
    ├── backend/
    │   ├── server.py              HTTP/SSE 服务入口（零依赖，270 行）
    │   ├── app/
    │   │   ├── config.py          路径、阈值、LLM 与演示开关
    │   │   ├── db.py              SQLite 建表、索引与确定性种子数据
    │   │   ├── nlu.py             规则意图解析 + 注入/钓鱼/越权检测
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
    │   ├── replay.py              命令行演示
    │   ├── demo_flow.json         6 轮演示脚本
    │   ├── smoke_http.py          HTTP 冒烟测试（13 个接口 + SSE 全链路）
    │   ├── smoke18.py             18 轮端到端演示流 + 审计链自证
    │   └── smoke18_result.json    上述运行器的完整实测输出（供复核）
    ├── docs/
    │   ├── api.md                 v1 HTTP 接口契约
    │   ├── architecture.md        架构与流水线详解
    │   ├── security.md            安全设计与攻击测试
    │   └── evaluation.md          评测任务与断言说明
    └── .github/workflows/ci.yml   CI：3.10/3.12 跑评测与演示

---

## 文档索引

| 文档 | 内容 |
| --- | --- |
| [docs/architecture.md](docs/architecture.md) | 分层架构、六阶段流水线、跨场景 DAG、审批中断与恢复、幂等键、事件与审计持久化 |
| [docs/security.md](docs/security.md) | 三级权限矩阵、阈值理由、TOTP 与哈希链实现、注入四层防御、攻击测试、已知限制 |
| [docs/evaluation.md](docs/evaluation.md) | 30 条任务表、断言字段说明、运行方式与报告、新增任务方法 |
| [docs/api.md](docs/api.md) | HTTP/SSE 接口契约（v1，部分字段与实现有偏差，以 architecture.md 与代码为准） |

---

## 免责声明

本项目是**纯模拟的银行 Agent 演示**：不接入任何真实银行、支付或清算系统，账户、余额、流水、理财与卡片均为 seed 生成的虚构数据；TOTP 密钥由 user_id 派生且 DEMO_MODE 下 /api/demo/totp 会直接返回当前口令，仅用于演示便利；数据库为本地 SQLite 单文件，无登录鉴权、无多租户隔离。**请勿将本项目用于任何真实资金场景。**
