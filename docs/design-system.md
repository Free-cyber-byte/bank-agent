# ZeroStep 设计规范（v0.3.2 · Win11 Fluent）

> 目标：像一家银行的正式产品与精算工作台，而不是「AI 套壳聊天页」；同时让视觉本身可复核、可验收。
> 依据：前端方案_Win11_Fluent.md 第 15 节（最终取值）+ 已冻结的前端文件（frontend/index.html 477 行 / 24,606 B、styles.css 974 行 / 57,760 B、app.js 2,434 行 / 112,932 B、_selftest.js 1,043 行 / 67,175 B，2026-10-27；node frontend/_selftest.js PASS=182 FAIL=0）；_preview/win11_v4.html 只作视觉参照，实现以三文件为准；品牌固定 A。
> 本文件是 v0.3.2 的前端验收依据（视觉语言延续 v4，v0.3.2 增补移动端与 PWA）；所有取值均可回指 index.html / styles.css / app.js / manifest.webmanifest / sw.js 的实际实现。

## 一、要避开的「AI 味」

| 反模式 | 为什么像 AI 套壳 | v4 的做法 |
| --- | --- | --- |
| 深蓝紫底 + 青/金渐变 + 发光 | 2023 年以来 AI 产品模板的默认皮肤 | 中性灰底 #F3F5F8 + 单一蓝强调 #0B5FD0（占屏 ≤5%）；全站仅账户卡与品牌标两处渐变，glow 为 0 |
| 大圆角 + 玻璃拟态 + 光晕阴影 | 装饰先于信息 | 圆角 6 / 9 / 14 / 16px 四档，1px 发丝边框，阴影只用中性色且仅浮层用 level-2 |
| 机器人头像 / 闪烁光标 / sparkle / emoji 图标 | 聊天玩具感 | 无头像、无 emoji；状态用色点 + 文字，图标一律内联 SVG |
| 气泡对话占满全屏 | 聊天框思维 | 对话是一条「业务流水」：意图 → 计划 → 工具 → 结论，工具行可展开 |
| 到处都是彩色 | 颜色失去语义 | 颜色只表示状态（自动 / 待确认 / 高风险 / 失败 / 降级） |
| 10–11px 小字省钱式排版 | 信息密度靠缩小字号，字小到不可读 | 17 档字号压到 7 档基础字阶，最小 12px；小字对比度全部 ≥4.5:1 |
| 大号渐变标题 + 营销腔 | 像落地页 | 页标题 28px/36 行高，一句话 + 数据；空态给 3 个真实示例句 |

## 二、视觉令牌

### 2.1 色彩

浅色（默认，底 #F3F5F8）：

| 用途 | 变量 | 值 | 对比度 |
| --- | --- | --- | --- |
| 页面底 | --bg | #F3F5F8 | — |
| 卡片/面板 | --surface | #FFFFFF | — |
| 次级面 | --surface-2 | #F8FAFC | — |
| 三级面/轨道 | --surface-3 | #EEF2F7 | — |
| 发丝线 | --hairline | #E4E8EF | — |
| 强发丝线 | --hairline-strong | #CBD3DE | — |
| 正文 | --text-1 | #0B1220 | 16.4:1 |
| 次级正文 | --text-2 | #48566B | 7.5:1 |
| 辅助/说明 | --text-3 | #55637A | 5.57:1（旧 #6B7A8F 仅 4.00，已废弃） |
| 弱化/时间戳 | --text-4 | #5F6D85 | 4.79:1（旧 #94A3B8 仅 2.35，且 13 处承载信息） |
| 主色 | --accent / hover / soft / line | #0B5FD0 / #0A51B4 / #EAF2FD / #BBD3F5 | 主色占屏 ≤5% |
| 深色块（账户卡） | --navy-1 / --navy-2 / --navy-3 | #0A2038 / #123A66 / #17548F | 白字 12:1 以上 |
| 成功 | --ok / soft | #067647 / #E7F6EE | 用于「已执行」「链校验通过」 |
| 需确认 | --warn / soft | #9A5B00 / #FDF1E0 | 用于审批、降级提示 |
| 高风险/拦截 | --err / soft | #C42B1C / #FDECEA | 用于拦截、失败 |

深色：

| 用途 | 变量 | 值 |
| --- | --- | --- |
| 页面底 / 卡片 / 次级 / 三级 | --bg / --surface / --surface-2 / --surface-3 | #111417 / #1A1E23 / #20252B / #252B32 |
| 发丝线 / 强发丝线 | --hairline / --hairline-strong | #2B3138 / #3B444E |
| 正文 / 次级 / 辅助 / 弱化 | --text-1 … --text-4 | #F2F5F8 / #B4BEC9 / #93A0AB / #808B96 |
| 主色 | --accent | #5AA2FF |
| 成功 / 需确认 / 高风险 | --ok / --warn / --err | #4ED08A / #F5C044 / #FF8A80 |

规则：渐变只允许两处（账户卡 152deg navy 三段、品牌标 155deg 蓝）；glow = 0；彩色阴影 = 0；颜色只表状态；数值不用彩色强调，靠字重与字号区分。

### 2.2 字体与字号（7 档）

字体栈：

    --font-ui: "Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI", "Noto Sans SC", system-ui, sans-serif
    --font-display: "Segoe UI Variable Display", "Segoe UI", "Microsoft YaHei UI", "Noto Sans SC", system-ui, sans-serif
    --font-mono: "Cascadia Mono", "Consolas", "SFMono-Regular", monospace

| 用途 | 字号 / 行高 / 字重 | 说明 |
| --- | --- | --- |
| 正文 / 表格 / 输入 | 14px / 1.6 / 400 | 唯一正文档 |
| 辅助 / 说明 / 时间戳 | 12px / 1.6 / 400 | 原 10、10.5、11、11.5 全部并入 |
| 行内数据 / 卡片标题 / 按钮 | 13px / 500–600 | 原 12.5 |
| 品牌字标 | 16px / 1 / 600 | 顶栏 ZeroStep |
| 页标题 | 28px / 36px / 600 / -0.024em | hero、技能页、外部服务页统一 |
| 概览指标 | 24px / 30px / 600 | 原 25px |
| 金额 | 40px / 600 / tabular | 账户卡与审批卡统一；货币符号 0.6em |

中文正文一律 letter-spacing:0；只有大标题负字距、英文大写标签 +0.06~0.08em；所有数字 tabular-nums lining-nums；只加载 400 / 500 / 600 三档，去掉 text-rendering:optimizeLegibility。

### 2.3 间距、圆角、阴影

- 间距：8px 网格，常用 4 / 8 / 12 / 14 / 16 / 18 / 22 / 24 / 32 / 40。
- 圆角：--r-1 6px（按钮、输入内件）、--r-2 9px（分段控件）、--r-3 14px（侧栏块、输入条、审批卡）、--r-4 16px（stage、账户卡）、--r-pill 999px（chip、徽章）。
- 阴影：--shadow-1 = 0 1px 2px rgba(16,24,40,.06)（卡片静态）；--shadow-2 = 0 20px 46px -14px rgba(11,18,32,.26)（浮层：审批弹层、抽屉；深色为 0 24px 56px -16px rgba(0,0,0,.72)）。
- 分隔优先 1px 发丝线而非阴影；阴影只用中性色，不允许彩色投影。

### 2.4 动效

- 曲线：--ease = cubic-bezier(.2,.8,.2,1)；印章专用 --ease-stamp = cubic-bezier(.2,.9,.2,1)。
- 时长：--t-fast 150ms（hover / 焦点）、--t-base 200ms（展开 / 切换）、--t-slow 260ms（印章 / 金额逐位）。
- 三段签名动效，只在这三处出现：
  1. 审批通过盖章：印章 260ms 落下 + settle 120ms，合计 380ms；
  2. 审计哈希逐位：行间隔 32ms、位间隔 24ms、帧 56ms，逐位锁定后显示「链校验通过」；
  3. 金额逐位入场：步长 22ms，总时长不超过 628ms。
- prefers-reduced-motion: reduce 时直接落终态，不等待 628ms；不做常驻动画、不做渐变扫光。

## 三、布局

    顶栏 60px sticky（预览稿额外有 38px 预览条，产品内不存在）
    ┌────────────┬──────────────────────────────┬────────────┐
    │ 左栏 264px │ 中央 minmax(0,1fr)            │ 右栏 296px │
    │ 账户卡      │ 页头 + 业务流水 + 输入条       │ 执行轨迹    │
    │ 快捷任务    │                              │            │
    └────────────┴──────────────────────────────┴────────────┘
    外留 18px 22px 22px，栏间距 18，内容最大宽 1540，页面高 calc(100vh - 98px)

- 三栏：264px minmax(0,1fr) 296px；中央 stage 内边距 40px 36px 30px；1440 视口中区内容 726px（调研底线 720）。
- 1280–1380：256 / 1fr / 288，间距 16，外留 16，stage 内边距 34/30 → 1280 时中区 610px。
- ≤1180：隐藏右栏（248 / 1fr），执行轨迹改为抽屉入口。
- 轨迹收起态：1381+ 左栏保持 264，1181–1380 保持 256，与展开态一致。
- 390 宽：单列纵向，账户卡在上、输入条贴底，审批卡按钮纵向，任何断点都不得出现横向滚动。
- 顶栏 60px sticky，浅色下 rgba(255,255,255,.82) + blur(14px) 半透明，深色 rgba(26,30,35,.86)。

## 四、组件清单

| 组件 | 关键类 / id | 要点 |
| --- | --- | --- |
| 顶栏 | .topbar、.brand、.seg、.audit-btn、.avatar | 品牌 A 默认（data-v="a"）；模式分段三段（规则/自动/模型）为轨道式（--surface-3 轨道 + 白滑块）；右侧审计、更多、时间 |
| 账户卡 | .acct、.acct__bal、.digits | 全屏唯一深色块，152deg navy 渐变；金额 40px/600/tabular，货币符号 0.6em；金额逐位入场 22ms；资产构成微条 3 段（活期 / 信用卡 / 理财，按 acctKind(a) = a.kind || a.type 分类）；账户列表末行「理财 · 持仓」（.acct__row--wealth），金额取 wealth.holding_total_cents；回合结束、审批通过、撤销后自动刷新（v0.5.1） |
| 侧栏块 | .rail__block、.chips、.chip | 白卡 + 1px 发丝线 + shadow-1；chip 高 29、pill 圆角、hover 变 accent-soft |
| 页头 | .hero、.hero__eyebrow、.hero h1、.hero__sub | eyebrow 12px 大写 +0.08em；标题 28px；副标题 14px/23px 最大 54ch |
| 业务流水 | .msg--user、.intent、.plan、.tool、.turn__msg | 意图 chip（意图 + 置信度 + 来源规则/模型/技能）→ 编号执行计划（风险色点）→ 工具 KPI 卡（可展开参数与返回）→ 结论卡 |
| 审批卡 | #approval、.dlg、.dlg__amt、.dlg__rows、.dlg__rules、.dlg__timer | 金额 40px；发丝表格逐行显示收款人、收款账户、付款账户、验证方式（动态验证码 TOTP / 确认后执行）、时间（提交后即时执行），其下另有来源行；规则区 h4「为什么需要你确认」：hits 按 high>medium>low 排序取前 3 行，其余折叠「+N 条判定依据」，无 hits 时回落 reasons 前 4 条；执行后余额预览；底部固定「确认环节不经过模型」+ 单次 nonce 声明；真倒计时 04:55 每秒 −1，到点「已过期」并禁用确认；role="timer"，徽章 role="status" aria-live="polite" |
| 审计抽屉 | #drawer、.arows、.arow、.chk | 标题「审计哈希链」；导出 audit.log、刷新、关闭；链条目 序号 / 名称 / sub / hash 前 8 位，点击展开完整哈希；徽章从「校验中」到「链校验通过」；footer 写明 hash = sha256(prev_hash + canonical_json(条目)) |
| 技能页 | #scr-skills、.grid3、.scard | eyebrow「技能」+ h1「六类业务，同一套执行底座」；六卡按 API 顺序：账单消费分析 bills（get_bill_summary，低风险）、银行卡与卡片管理 cards（list_cards，中风险）、跨场景组合任务 cross（create_transfer，高风险）、订阅与自动续费 subscriptions（list_subscriptions，低风险）、转账汇款 transfer（get_accounts，高风险）、理财持仓与申购 wealth（list_wealth_products，中风险）；每卡显示名称、风险标签（低/中/高；高红、中黄）、首个工具名、描述与首条示例；数据 GET /api/skills（六卡来自 skills/*.skill.md，front matter 手写解析，单卡损坏只少一张）；页头含刷新与返回工作台 |
| 外部服务页 | #scr-services、.grid2、.infobar | eyebrow「外部服务 · MCP」+ h1「只读外部能力，全部可降级」；两卡固定为 fx（汇率）与 risk（反欺诈评分）；卡片显示 可用/降级 标签（servers[].status==="ok" 才可用）、URL、延迟 ms、last_error 截断 60 字；InfoBar：enabled=false 时「外部服务未启用，全部使用内置工具」，有降级时「N 个外部服务不可用，已降级处理」；数据 GET /api/mcp/status |
| 设置页渠道卡 | #scr-settings、.scard | 四行只读：渠道状态（已启用/未启用）、监听端口、访问令牌（token_configured 为真显示「已配置（打码）」，否则「未配置」）、活动会话（sessions）；页面不显示令牌明文、不提供写操作；数据 GET /api/channel/status；同屏运行时卡读 /api/health（版本、演示模式、解析模式、当前会话，附健康详情与重置演示） |
| 插件页 | #scr-plugins、.pluggrid、.plug、.plug__head、.plug__status | eyebrow「插件」+ h1「能力按插件注册，可单独启停」；两列卡片（≤900px 单列），卡内含类别 .plug__kind、名称 .plug__name、状态徽章 .plug__status（已启用 --ok / 已停用 --text-3 / 降级 --warn，前置 6px 色点）、版本 .plug__ver；描述两行截断；能力 chip .plug__chip 最多 4 个，超出显示 +N，无能力显示「无对外能力」；卡片底部左侧依赖或锁定原因、右侧开关；停用卡 opacity .62 且 chip 删除线 |
| 开关 | .switch、.switch__knob、.plug__switchwrap | 36×20 胶囊、底色 surface-3、边框 hairline-strong；knob 14px 白色、位移 16px；is-on 时底/边色 --ok；role=switch + aria-checked；核心插件 disabled 且文案「已锁定」，普通项显示「启用 / 停用」，请求中文案「启用中… / 停用中…」；focus-visible 2px accent |
| 手风琴详情 | .plug__detail、.plug__dgrid、.plug__dsec | 单开互斥，点击卡头展开；aria-expanded 同步、关闭时 hidden；四段：如何工作 / 环境变量 / 接口 / 依赖；空值显示「无需配置」「无对外接口」「无」；最近错误显示 detail.last_error；≤720px 单列 |
| 换一批 | #btnTasksRefresh、.chips、.chips--swap、.is-shake | 文案「换一批 · x/N」，title 说明目标批次；每批 6 条（GET /api/tasks?limit=6&offset=…）；切换时按钮 is-busy「加载中…」，指令 chips 160ms 淡出位移后换页；只有一批时 chips 抖动 280ms 并 toast「已经是这一批了」；reduced-motion 下时长压到 1ms |
| 执行轨迹 | .trace、.tr、.tr__dot | 竖向时间线：状态点 + 标题 + mono 工具名 + 耗时；header meta 显示 N 步 · 已完成 |
| Toast | #toasts、.toast__actions | 执行成功后 60 秒撤销入口；success / error 两态 |
| 空态 | #chatEmpty、.desk、.desk__card | 一句话 + 输入引导 + 3 张真实示例指令卡 + 3 列能力说明 |

### 4.1 插件页与开关（v0.3.0）

- 布局：内容区顶部 .plugins__summary 一行汇总「共 N 个插件 · 已启用 x · 已停用 y · 降级 z」，右侧图例说明「开关即时生效，由服务端持久化；核心插件锁定不可停用」；下方 #pluginsGrid 两列卡片，卡片 align-items:start，展开详情不拉齐同排高度。
- 卡片结构四层：卡头（类别、名称、状态徽章、版本、右侧展开箭头）→ 描述（两行截断）→ 能力 chip（tools / skills / routes 依次取，最多 4 个）→ 卡脚（锁定原因或依赖列表 + 开关）；展开后追加 .plug__detail 四段网格。
- 状态与交互：.plug.is-off 整卡 opacity .62；.plug.is-busy opacity .8 且开关禁用；开关成功后本地更新卡片并联动重载技能、外部服务、渠道、模型与健康状态；失败回滚原状态，显示错误条或 toast（核心插件提示「核心能力不可停用」）。
- 键盘与无障碍：卡头 role=button tabindex=0，Enter / Space 切换展开并阻止默认滚动；开关为 role=switch aria-checked；详情 role=region aria-label「插件详情」。
- 动效：详情面板 fadein；换一批 chips 160ms 淡出再换页；prefers-reduced-motion 下所有动画时长 1ms、直接落终态；只有一批时抖动一次，不做循环动画。

### 4.2 移动端与 PWA（v0.3.2）

≤720px 形态：

- 顶栏收为三段：品牌（ZeroStep / 零步银行）+ 当前页名 .topbar__page + 右侧「更多」；.topnav 在 ≤1024px 已隐藏，≤720px 再隐藏 .topbar__mid（模式分段）、.effective（实际模式徽章）、审计文字按钮与头像，只留「更多」。
- 底部 tabbar 固定 4 项（工作台 / 技能 / 插件 / 设置），高度 56px + env(safe-area-inset-bottom)；body 同步加 padding-bottom，输入条与弹层不被遮挡。
- 解析模式切换收进「更多」菜单：.menu__modewrap 默认 display:none，≤720px 显示三段「规则 / 自动 / 模型」与提示，与桌面顶栏 .seg 共用同一状态。
- 审批弹层 .dlg 在 ≤720px 为 max-height:calc(100dvh - 24px) 且内部滚动；app.js 再用 visualViewport 把上限压到 max(260, 视口高 - 24) px，软键盘弹出时确认按钮仍可见。
- 触控热区按 ≥44px 验收：已显式实现 tabbar 按钮 56px、OTP 输入格 44px、审计行 min-height:44px；其余按钮以 32–40px 视觉高度加间距补足，390 宽人工点测待执行（见 §六 第 8 条）。

| 组件 | 关键类 / id | 要点 |
| --- | --- | --- |
| 底部 tabbar | #tabbar、.tabbar、.tabbar__btn | 4 项等宽（工作台 / 技能 / 插件 / 设置），图标 20px + 11px 文字；固定底部、z-index 90、rgba(255,255,255,.94) + blur(14px)、1px 上发丝线，深色 rgba(26,30,35,.94)；选中项 --accent 加粗，按压缩放 .97，focus-visible 2px 主色轮廓；总高 56px + 安全区，页面底部留白与 toast/待授权胶囊同步上移 |
| 推送卡 | #pushCard、.push、.push__kind、.push__result | 设置页「模拟推送」卡：三种 kind（订阅扣费 / 大额支出 / 还款到期）为 pill 分段，单选 aria-pressed；结果四态——已生成（默认）/ 已推送到渠道 / 未配置推送地址 / 投递失败；服务不可达时正文为「推送通道暂时不可用，业务不受影响。」；结果条左侧 2px 主色竖线，成功 --ok、失败 --err；卡底注明「不会真的发短信，结果同时写入审计」 |
| PWA 安装态 | manifest.webmanifest、sw.js、#tabbar | manifest：standalone（display_override standalone/minimal-ui）、theme_color #0B5FD0、background #F3F5F8、lang zh-CN、start_url /、scope /；5 个图标文件（192/512 各 any + maskable，另 apple-touch-icon）；sw.js 版本 0.3.2，只预缓存静态外壳（含 5 个图标），/api/ 一律走网络，activate 清理旧 zerostep- 缓存；注册失败静默降级，仅 localhost 或 HTTPS 生效 |

本版不新增颜色与字阶：tabbar 用 --surface / --text-3 / --accent / --hairline；推送结果条复用 --surface-2 / --hairline / --accent，成功失败复用 --ok / --err；PWA 的 theme_color 即品牌主色 #0B5FD0，浅色与深色均沿用 §2.1 令牌。

## 五、状态与文案

- 模式分段三态：规则（确定性）/ 自动（有 Key 用模型，无 Key 回退规则）/ 模型（强制模型）。厂商名不作模式名，放在 hover 提示里。
- 降级：外部服务页对应卡显示「降级」标签与 last_error，页头 InfoBar 提示「N 个外部服务不可用，已降级处理」；顶栏无独立降级徽章（v0.2.0 实现）；业务结论里不出现降级文案，只保证金额与边界不变。
- 插件状态三态：已启用（--ok）、已停用（--text-3）、降级（--warn），均配文字，不只靠颜色；停用卡整卡降透明度但名称、能力与依赖仍可读；不提供批量启停，核心插件开关不可点并给出原因。
- 外部数据来源：汇率结果标注 source（external-fx / builtin），审计记 mcp_call / mcp_fallback；外部结果只作为数据展示与加成风险证据，不拼进提示词。
- 每条 Agent 结论标 source：规则 / 模型 / 模型失败已回退 / 技能命中。
- 审批区必须显示：金额、收款人、收款账户、付款账户、时间、来源（原话或模型抽取 + 原句）、为什么需要确认（规则名 + 数字）、执行后会发生什么、倒计时、单次 nonce 声明。
- 「确认环节不经过模型」必须出现在审批卡底部。
- 空态一句话 + 3 个真实示例句，不写「我是你的智能助手」；禁用词：赋能、一站式、智能小助手、魔法、秒懂。
- 数字格式：千分位、两位小数、tabular；卡号/账号掩码；金额单位元，内部整数分。

## 六、验收

1. 1440×900 中区内容 726px，无横向滚动；1280 时 610px；390 宽单列不溢出。
2. 基础字阶 7 档（12 / 13 / 14 / 16 / 24 / 28 / 40），不出现 10、10.5、11、11.5；styles.css 另有窄屏与组件局部覆盖（12.5 / 13.5 / 15 / 17 / 20 / 34px，见 @media、OTP、结果卡与插件详情正文），不构成新字阶。
3. 12–14px 正文对比度 ≥4.5:1，--text-4 4.79:1 达标；深色模式文字 ≥5.3:1。
4. 渐变 2 处（账户卡、品牌标），glow 0，彩色阴影 0，emoji 图标 0，外链字体/图片/CDN 0。
5. prefers-reduced-motion 下动画直接落终态；无 628ms 等待。
6. 审批卡规则行 ≤3 且每行带数字；审计抽屉可校验、可导出 JSONL。
7. 截图口径（v0.3.2 起）：技能页、外部服务页、设置页渠道卡与推送卡、插件页（含展开一张）各一张，1440 与 390 各一张；另补 390 宽工作台（含底部 tabbar）与 PWA 安装态各一张（PWA 需 localhost 或 HTTPS）。各页已随前端冻结落盘（2026-10-27 核对 DOM），截图留档待人工执行。
8. 移动端：390 宽无横向滚动；顶栏只剩品牌、当前页名与「更多」；tabbar 4 项均可点且不遮输入条；模式切换只在「更多」菜单；审批弹层不超出 100dvh - 24px；触控热区人工点测 ≥44px。

## 七、参考来源

- 本仓库：前端方案_Win11_Fluent.md 第 15 节（最终取值）、_preview/win11_v4.html（视觉与动效实现参照）、audit/v4-review.md（12 步 DOM 桩审查）。
- 本仓库调研：_research/ui-type.md（字阶与配色）、_research/ui-motion.md（三段签名动效）、_research/ui-cases.md（13 款产品 10 条规则）。
- Fintech Dashboard Design: 9 Real Products, Analyzed — https://adminlte.io/blog/fintech-dashboard-design-examples/
  （Stripe：表格优先、颜色只表状态；Ramp：工作队列；Brex：异常优先；Wise：费用透明；Revolut：数字可读性）
- When an AI assistant prepares a payment, what should the user approve? — https://humbleteam.com/blog/fintech-ai-payment-confirmation
  （准备/授权分离、展示字段与来源、冲突必须显式、改动后重新校验、结果不确定不等于失败）
- ING Aether — Banking with AI Agents and Gen UI（UX Design Awards 2025）— https://ux-design-awards.com/winners/2025-2-ing-aether-banking-with-ai-agents-and-gen-ui
