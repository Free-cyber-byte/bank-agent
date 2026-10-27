# ZeroStep 设计规范（v3 · 私人银行对账单 + 精算工作台）

> 目标：看起来像一家银行的正式产品与精算工作台，而不是「AI 套壳聊天页」。
> 依据：Mercury / Stripe / Ramp / Brex / Wise / Revolut 的 dashboard 拆解、Humbleteam 对 AI 支付确认的研究、2025 UX Design Awards 获奖项目 ING Aether 的访谈。
> 本文件描述的是 **实际实现**（frontend/styles.css、index.html、app.js），改视觉时同步更新。

## 一、要避开的「AI 味」

| 反模式 | 为什么像 AI 套壳 | 本项目的做法 |
| --- | --- | --- |
| 深蓝紫底 + 青/金渐变 + 发光 | 2023 年以来 AI 产品模板的默认皮肤 | 暖白纸感底 + 墨色文字 + 单一墨绿强调色，渐变/发光为 0 |
| 大圆角 + 玻璃拟态 + 光晕阴影 | 装饰先于信息 | 1px 发丝边框、圆角 2–3px、极浅阴影 |
| 机器人头像 / 闪烁光标 / sparkle / emoji 图标 | 聊天玩具感 | 无头像、无 emoji 图标；状态用色点 + 文字，图标一律内联 SVG |
| 气泡对话占满全屏 | 聊天框思维 | 对话是一条「业务流水」：意图 → 计划 → 工具 → 结论，工具行可展开 |
| 到处都是彩色 | 颜色失去语义 | 颜色只表示状态（自动/待确认/高风险/失败） |
| 大号渐变标题 + 营销腔 | 像落地页 | 空态是「业务台」：一句标题 + 3 张示例指令卡 + 3 列能力说明 |

## 二、视觉令牌

### 色彩（浅色纸感；深色仅作将来可选项，未实现）

| 用途 | 变量 | 值 |
| --- | --- | --- |
| 页面底 | --bg | #EFEDE6 |
| 左栏/次级面 | --paper | #F5F4EF |
| 卡片 | --card | #FBFAF7 |
| 次级块 | --card-2 | #F2F0E9 |
| 正文 | --ink | #16150F |
| 次级文字 | --ink-2 | #3C3A32 |
| 说明/时间戳 | --muted | #77736A |
| 弱化 | --muted-2 | #9A968B |
| 发丝线 | --hair / --hair-strong | #D9D5C9 / #C4BFAF |
| 品牌墨绿 | --green / --green-deep | #0E5A3C / #0A432D |
| 选中底 | --green-soft / --green-line | #E7EFE9 / #B9CDBF |
| 需确认（黄） | --amber / --amber-soft / --amber-line | #9A6A12 / #F4EBD9 / #DCCBA9 |
| 高风险/拦截（红） | --red / --red-soft / --red-line | #A33A2A / #F6E9E6 / #DDB6AD |

规则：**渐变 = 0，glow = 0，彩色面积 ≤ 10%**；不使用紫色；金额与数字一律 tabular-nums 右对齐。

### 字体与字号刻度

- 字族：--sans = system-ui, -apple-system, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif；等宽：--mono = ui-monospace, "SFMono-Regular", "Cascadia Mono", "Segoe UI Mono", Consolas, monospace。
- 刻度（变量 --f11…--f40）：**11 / 12 / 13 / 14 / 16 / 20 / 26 / 34 / 40**。
  - 40：左栏余额 hero 数字（1280 及以下降到 34）
  - 34：业务台大标题（≤900 降到 26）；审批金额 44px（独立规则 .ap__amount）
  - 26 / 20：区块与抽屉标题
  - 16：强调数值、输入框
  - 14 / 13：正文
  - 12：表格与标签；11：脚注与提示
- 中文标签：12px、letter-spacing 0.02em、--muted；数字 letter-spacing -0.02em。
- 行高 1.55（正文 1.6–1.7）。

### 间距、形状、阴影

- 8px 网格：4 / 8 / 12 / 16 / 20 / 22 / 24 / 26 / 32 / 40。
- 圆角：卡片与面板 2–3px；按钮 2px；输入框 2px；色点/圆点 50%；不使用大圆角。
- 阴影：--shadow = 0 1px 2px rgba(22,21,15,.06)；浮层 --shadow-lg = 0 18px 48px rgba(22,21,15,.16)。
- 分隔：1px solid var(--hair)，优先用发丝线而不是阴影做卡片。

### 动效

- 曲线 --ease: cubic-bezier(0.2, 0.7, 0.2, 1)；时长 --t 180ms / --t-slow 220ms（160–220ms 区间）。
- 只用于 hover、展开、弹层、Toast；不做常驻动画、不做渐变扫光。
- prefers-reduced-motion: reduce 时全部动画/过渡降到 0.001ms。

## 三、布局

    顶栏 60px：ZeroStep 字标 · 账户（张伟/稳健型）· 模式分段(规则/自动/模型) · 实际徽章 · 审计 · 更多 · HH:mm
    左栏 280px（--paper）：余额 hero + 资产构成微条 + 3 行账户 + 图例 + 2 列快捷任务
    中央 1fr（--bg，max 760 居中）：业务台空态 / 业务流水，底部输入条
    右栏 320px（--bg）：本次执行轨迹（竖向时间线）

- 三栏：280px minmax(0,1fr) 320px；中央内容 max-width 760px 居中，chat 内边距 30/40。
- 响应式断点：
  - ≤1440：264 / 1fr / 292，chat 左右 32
  - ≤1280：248 / 1fr / 248，hero 数字降 34
  - ≤1024：236 / 1fr 两列，轨迹栏移到下方横条（max-height 210，可折叠），顶栏隐藏「账户」标签
  - ≤900：单列纵向（中央在上、左栏、轨迹），顶栏换行，空态示例卡单列
  - ≤560：紧凑版，隐藏账户区，审批按钮纵向，校验码/键位缩小
- 任何断点都不得出现横向滚动。

## 四、组件清单

| 组件 | 关键类 / id | 要点 |
| --- | --- | --- |
| 顶栏 | .topbar、.brand、.seg、.effective、#modeBadge | 模式分段是轨道式（灰轨 + 白滑块 + 细阴影）；徽章是带状态点的 chip（实际：规则/模型） |
| 余额 hero | .acct__hero、#acctTotal、#acctDelta | 标签 12px 灰、金额 40px tabular、下方一行净值说明 |
| 资产构成微条 | #acctMix、.mix__seg、.mix__dot | 存款/理财/信用卡三段（0 值段省略），图例 6px 圆点 + 12px 文字 |
| 账户行 | .acct__row、.acct__bal | 掩码等宽左对齐，金额右对齐；信用卡负数用红 |
| 快捷任务 | #chips、.task | 2 列紧凑网格，1 行标题 + 右侧类别小字，hover 只变 1px 边框 |
| 业务台空态 | #chatEmpty、.desk、.desk__card、.desk__caps | 大标题 + 输入引导 + 3 张示例指令卡（点击即发送）+ 3 列能力条 |
| 用户消息 | .msg--user | 右对齐、无气泡尾巴、--card-2 深底 |
| 意图卡 | .intent、.intent__name、.intent__chip | 单行 chip：意图名 + 置信度 + 来源（规则/模型），槽位小标签 |
| 执行计划 | .plan、.plan__step、.plan__num、.plan__dot--risk | 编号时间线：竖线 + 节点 + 右侧 mono 工具名，风险色点 |
| 工具 KPI 卡 | .tool、.tool__sum、.tool__body、.tool__line | 左标题右状态；数据行 label 左 / 值右对齐 mono；可展开看参数与返回 |
| 结论卡 | .turn__msg（左侧 3px 墨绿条） | 关键数字加粗（安全富文本渲染），正文 14px |
| 审计小条 | .auditchip、.auditchip__seq | 对话内哈希摘要，点击进审计抽屉 |
| 拦截卡 | .blockcard、.blockcard__reasons | 红色拦截：原因逐条列出，强调「未发生任何资金变动」 |
| 审批单 | .ap、.ap__amount、.ap__row、.ap__otp、.otp__cell、.keypad | 金额 44px 置顶，风险 chip 置顶，倒计时右上对齐；四行发丝表格（label 96px 左 / 值右 mono）；high 风险显示 6 位验证码格 + 数字键盘；底部固定「确认环节不经过模型」 |
| 轨迹栏 | .trace、.tr、.tr__dot、#traceMeta、#btnTraceToggle | 竖向时间线：状态点 + 标题 + mono 工具名 + 耗时右对齐；header meta 随步骤变化（进行中 2/3、N 步 · 已完成），空态给解释文案 |
| 审计抽屉 | #auditSheet、.auditrow、.hashline、#auditValid | 行表格化：序号 / 事件 / mono 工具 / 哈希前 8 位 + 复制按钮；点击展开 prev_hash/hash 全量；顶部链校验 badge（valid/invalid） |
| Toast | #toasts、.toast__actions | 执行成功后的 60 秒撤销入口；success/error 两态 |
| 更多菜单 | #moreMenu、.menu__item | 重置演示、API 健康、关于 |

## 五、状态与文案

- 模式分段三态：**规则**（确定性）/ **自动**（有 Key 用模型，无 Key 回退规则）/ **模型**（强制模型，无 Key 时禁用）。不再把具体厂商当作模式名。
- 实际徽章：「实际：规则」/「实际：模型」，title 显示 provider · model · key 掩码；点击可看来源说明。
- 每条 Agent 结论标 source：规则 / 模型 / 模型失败已回退（黄点）。
- 审批区必须显示：金额、收款人、收款账户、付款账户、时间、来源（你的原话 / 模型抽取 + 原句）、需要确认的原因、执行后会发生什么、倒计时；冲突用黄底行显式列出。
- 「确认环节不经过模型」这句必须出现在审批卡底部。
- 空态一句话 + 3 个真实示例句，不写「我是你的智能助手」。
- 禁用词：赋能、一站式、智能小助手、魔法、秒懂；用银行业务词：转账、账单、限额、强验证。

## 六、验收

1. 首屏截图给不相关人员看，第一反应应是「这是银行 App」而非「这是 AI 聊天」。
2. 页面内渐变/发光数量 = 0；emoji 图标数量 = 0；无外链字体/图片/CDN。
3. 1440×900、1280×800、1024×768、768×900 均无横向滚动，降级正常。
4. 金额全部右对齐、等宽数字、两位小数；卡号/账号一律掩码。
5. 回复正文里的 Markdown 加粗标记要渲染成加粗，绝不出现字面星号；轨迹栏有步骤后不得仍显示「尚未开始」。

## 七、参考来源

- Fintech Dashboard Design: 9 Real Products, Analyzed — https://adminlte.io/blog/fintech-dashboard-design-examples/
  （Stripe：表格优先、颜色只表状态；Ramp：工作队列；Brex：异常优先；Wise：费用透明；Revolut：数字可读性）
- When an AI assistant prepares a payment, what should the user approve? — https://humbleteam.com/blog/fintech-ai-payment-confirmation
  （准备/授权分离、展示字段与来源、冲突必须显式、改动后重新校验、结果不确定≠失败）
- ING Aether — Banking with AI Agents and Gen UI（UX Design Awards 2025）— https://ux-design-awards.com/winners/2025-2-ing-aether-banking-with-ai-agents-and-gen-ui
  （批评点：多数银行把 AI 做成聊天框导致体验割裂；方向：AI 融入功能、可直达、自动化与用户控制平衡）
