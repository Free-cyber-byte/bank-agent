# Skill 技能包（v0.2.0）

> 一句话：六张业务卡把「用户怎么说」映射到「该用哪组工具、缺哪些槽位、哪些话不能接」，它只影响听懂与规划，不参与放行，也不接触账本。

本文档依据 backend/app/skills.py（2026-10-27 落盘，299 行）、skills/*.skill.md 六张卡与 backend/app/policy.py 的实际工具分级编写；接口字段与返回值已按实现校对。

## 一、为什么需要技能包

规则解析靠关键词，口语一变就漏；模型提示词没有业务边界，容易把「查理财」和「买理财」混在一起。技能卡把三件事写死在文件里：

1. 触发词：这句话大概率属于哪类业务；
2. 槽位与工具：要补齐哪些字段、允许调哪些工具；
3. 澄清与禁止：缺槽位先问，越界的话直接不接（例如「帮我绕过限额」）。

技能卡只影响 parse_intent 与提示词注入，风险分级、审批、执行、回读仍全部由 policy 与 engine 决定。

## 二、六张卡

| id | 名称 | 典型触发 | 允许的工具（以 policy 实际分级为准） | 风险提示 |
| --- | --- | --- | --- | --- |
| transfer | 转账汇款 | 转账、汇款、打钱、给某人转 X 元、工资分账 | get_accounts、get_payees、risk_screen、create_transfer | 转账：MEDIUM 起，高于 1000 元或新收款人为 HIGH |
| bills | 账单消费分析 | 账单、流水、这个月花了多少、支出构成、预算 | get_bill_summary、get_recent_transactions | 纯查询，LOW 自动 |
| wealth | 理财持仓与申购 | 理财、申购、持仓、收益、起购、风险等级 | list_wealth_products、get_wealth_holdings、purchase_wealth | 查询 LOW；申购 HIGH，走 TOTP |
| cards | 银行卡与卡片管理 | 卡、额度、冻结解冻、虚拟卡、挂失 | list_cards、apply_virtual_card、freeze_card、unfreeze_card | 申请与状态变更 MEDIUM，需确认 |
| subscriptions | 订阅与自动续费 | 订阅、自动续费、会员、取消订阅 | list_subscriptions、cancel_subscription | 查询 LOW；取消 MEDIUM，需确认 |
| cross | 跨场景组合任务 | 分账、发工资、生日转账 + 提醒、旅行预算 | create_transfer、purchase_wealth、create_reminder（组合调用） | 按每一步的工具分级，取最高风险 |

卡文件放在 bank-agent/skills/*.skill.md，用 YAML front-matter 手写解析（零依赖）。字段固定为 id、name、description 三个文本字段，加 triggers、tools、examples、slots、clarify、forbidden 六个清单字段；清单用中文逗号、顿号或英文逗号分隔，写在同一行，不用 YAML 方括号：

    id: transfer
    name: 转账汇款
    description: 转账前核对收款人、金额与出账账户，动钱之前必须二次确认
    triggers: 转账, 汇款, 转给, 打钱, 打给, 转钱, 给妈妈转, 给爸爸转, 房租
    tools: get_accounts, get_payees, risk_screen, create_transfer
    examples: 给妈妈转 200 元, 给爸爸转 500 元, 给房东转账 3000 元房租
    slots: payee_name, amount_cents, from_account_id
    clarify: 转给谁？, 转多少钱？, 从哪张卡出账？
    forbidden: 不得跳过二次确认, 不得替用户猜收款人, 不得放宽单笔限额

六个清单字段缺失时按空列表处理；id 缺失时用文件名（去 .skill.md）兜底，name 缺失时用 id 兜底。具体触发词、示例与槽位以卡文件为准。

## 三、接口

    all() -> [dict, ...]                     # 全部卡，每张卡是扁平 dict
    get(skill_id) -> dict | None             # 按 id 取，不存在返回 None
    match(text | intent) -> [id, ...]        # 返回卡 id，按命中分降序，最多 3 个
    summary(skill_ids) -> [str, ...]         # 每卡一行，注入 llm system prompt
    status() -> {
      "count": 6,
      "skills": [
        {"id": str, "name": str, "description": str, "triggers": [str], "tools": [str],
         "examples": [str], "slots": [str], "clarify": [str], "forbidden": [str], "hits": int}
      ]
    }
    reload() -> {"count": int, "ids": [str]} # 重新读盘并清空 hits
    count() -> int

- match 同时接受原句字符串与 intent 字典：传字典时优先用 text / utterance 字段做文本匹配，否则按 intent.name 查内置映射（如 transfer 对应 transfer 卡，未知意图返回空列表）；任何异常都返回 []，不影响主流程。
- match 只做「像不像这类业务」的判断，不产出金额与收款人。
- summary 每卡一行、最多 3 行，避免提示词膨胀；未命中任何卡时不注入。
- hits 为内存计数（每次 match 命中即累加），随 status() / GET /api/skills 暴露，用于观察技能使用情况；当前技能页按固定顺序展示六卡，不按 hits 排序。进程重启归零。
- status() 本身只输出卡字段 + hits；HTTP 接口 GET /api/skills 由 backend/server.py 额外补 risk（bills/subscriptions=low、cards/wealth=medium、transfer/cross=high，未知=medium）与 tool（tools[0]，为空时 read_only），见 docs/api.md。

## 四、接入位置

1. 模型解析（llm.py）：parse(text, ctx_doc) 先用 skills.match(text) 取候选卡，把 skills.summary(...) 作为一行领域提示追加进 system prompt（最多 3 张）；返回值带 skills 字段。
2. 编排（engine.py）：llm.parse 之后统一执行 sk.match(intent)（传 intent 字典），把结果写回 intent.skills 并随 intent 事件下发；技能路由失败只置空列表，不影响主流程。
3. 审计（engine.py _finalize）：intent.skills 非空时追加 skill_matched 事件（tool=skill_router，args={intent}，result={skills}，risk_level=low），随响应写库。
4. 页面（GET /api/skills、GET /api/skills/{id}）：技能页六卡按固定顺序（bills、cards、cross、subscriptions、transfer、wealth，即文件名排序）展示名称、风险标签、tool 名、说明与首条示例；触发词、槽位、forbidden 不出现在卡片上，可通过 /api/skills/{id} 取原始字段。hits 只在接口里返回，页面不展示也不排序。

## 五、演示与验证

    python scripts/skill_check.py      # 6 项自检（含 18 条示例各命中自身卡）
    python scripts/run_tasks.py --filter t33

    curl.exe -s http://127.0.0.1:8000/api/skills
    curl.exe -s http://127.0.0.1:8000/api/skills/transfer

t33 断言 expected_skill：一句典型转账话术必须命中 transfer，且不命中其他卡。

验收口径：

1. 六张卡 id 固定为 transfer、bills、wealth、cards、subscriptions、cross。
2. 18 条示例全部命中自身卡，跨卡零误命中。
3. 同一句话在规则模式与模型模式都能给出 skill 命中（模型不可用时以规则为准）。
4. 技能命中不改变风险等级与审批结果：t06、t07 等既有用例加卡前后结果一致。

## 六、降级与边界

- 插件停用：skill-pack 插件被停用后，GET /api/skills 返回 {count:0, skills:[], disabled:true}，页面显示空态；规则解析、policy、审批与审计不变，只是不再注入技能提示词，见 [plugins.md](plugins.md)。
- 卡文件缺失或解析失败：目录不存在、单张卡 front-matter 不合法或读盘报错时，该卡直接跳过；全部失败则 match 返回空列表，解析退回纯关键词规则。任何异常都被吃掉，对话不受影响，只少了提示词增强。
- 模型不可用：规则模式仍然命中卡（由 nlu.py 填 skills 字段），页面照常显示。
- 边界：技能卡不是权限系统。工具能否调用由 policy 决定；卡里写了 create_transfer 也不代表可以放行。
- 禁止把卡文件内容拼进用户可见回复；卡只服务解析与提示词。

## 七、新增一张技能卡

1. 在 skills/ 下新建 xxx.skill.md，按第二节字段写 front-matter 与一句说明。
2. id 必须唯一，且不能与六个既有 id 冲突。
3. examples 至少 3 条真实口语，逐条确认命中自身卡、不误命中其他卡。
4. 工具只能从 mock_core 已注册工具里选，风险等级由 policy 决定，不在卡里写阈值。
5. 跑 scripts/skill_check.py，18 条断言全过；再跑一次 run_tasks.py 确认无回归。
6. 更新本文档第二节表格与 docs/design-system.md 技能页清单。

## 八、已知限制

1. 手写 front-matter 解析，不支持 YAML 多行块与锚点，字段值请写单行。
2. 匹配基于触发词与示例的命中分，不做语义向量，同义改写仍可能漏。
3. hits 计数不持久化，页面刷新后从零累计。
4. 卡内容是静态文件，改完需手动调 skills.reload() 或重启服务才生效；进程内没有文件监听。
