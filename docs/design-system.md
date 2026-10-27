# ZeroStep 设计规范（v2 · 去 AI 味）

> 目标：看起来像一家银行的正式产品，而不是「AI 套壳聊天页」。
> 依据：Mercury / Stripe / Ramp / Brex / Wise / Revolut / Plaid 的 dashboard 拆解、Humbleteam 对 AI 支付确认的研究、2025 UX Design Awards 获奖项目 ING Aether 的访谈。

## 一、要避开的「AI 味」（现状问题）

| 反模式 | 为什么像 AI 套壳 | 改成 |
| --- | --- | --- |
| 深蓝紫底色 + 青/金渐变 + 发光 | 2023 年以来 AI 产品模板的默认皮肤 | 暖白纸感底 + 墨色文字 + 单一强调色 |
| 大圆角 + 玻璃拟态 + 阴影光晕 | 装饰先于信息 | 1px 细边框、极浅阴影、直角偏小的圆角 |
| 机器人头像 / 闪烁光标 / sparkle 图标 / emoji 当图标 | 聊天玩具感 | 无头像、无 emoji 图标；状态用色点与文字 |
| 气泡对话占满全屏 | 聊天框思维，不是银行工作台 | 对话是一条「工作流水」，工具调用是可展开的行 |
| 到处都是彩色 | 颜色失去语义 | 颜色只表示状态（成功/待确认/高风险/失败） |

## 二、视觉令牌

### 色彩（浅色为主，深色仅作可选主题的将来项）

| 用途 | 值 | 说明 |
| --- | --- | --- |
| canvas | #F5F4EF | 暖白，纸感 |
| surface | #FFFFFF | 卡片/面板 |
| surface-2 | #FAF9F5 | 次级块、表头 |
| border | #E4E1D8 | 1px 分隔 |
| ink | #16150F | 正文，近黑偏暖 |
| ink-2 | #55534A | 次级文字 |
| muted | #8A8779 | 说明/时间戳 |
| accent（品牌） | #0E5A3C | 银行墨绿，仅用于主按钮、选中态、品牌字 |
| accent-soft | #E7EFE9 | 选中背景 |
| risk-green | #16794A | 绿：查询/自动执行 |
| risk-amber | #A96A06 | 黄：需确认（≤1000 元） |
| risk-red | #B3261E | 红：强验证/拦截 |

规则：**渐变=0，glow=0，彩色面积≤10%**；金额、余额等数字用 tabular-nums 右对齐。

### 字体

- 字族：-apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", Roboto, sans-serif
- 字号：28（页面标题）/ 20（区块标题）/ 16（强调）/ 14（正文）/ 13（表格）/ 12（辅助）
- 行高 1.55；标题 letter-spacing -0.01em；数字 font-variant-numeric: tabular-nums

### 间距与形状

- 8px 网格：4/8/12/16/20/24/32/40
- 圆角：卡片 12，按钮 8，输入 8，徽章 999（仅状态）
- 阴影：0 1px 2px rgba(22,21,15,.05)；浮层 0 8px 28px rgba(22,21,15,.10)
- 边框：1px solid #E4E1D8（不叠加阴影做卡片）

## 三、布局（桌面优先，≥1280 舒适；≤1024 收侧栏；≤720 单列）

    ┌─ 顶栏 56px：ZeroStep 字标 · 账户切换 · 模式分段控件(规则/自动/DeepSeek) · 审计 · 更多 ┐
    ├─ 左侧 260px：账户总览（余额 tabular）｜风险图例 ｜ 快捷任务 ┤  ┌ 右侧 300px（可收起）：本次执行轨迹 ┐
    └─ 中央 760px：会话流水（用户一句 → Agent 计划 → 工具行 → 结果） ┘

- 中央是「工作流水」不是聊天气泡：用户消息靠右、无气泡边框，只一条 2px 左侧竖线；Agent 回复为普通正文段落。
- 工具调用：折叠行 [查询账单 · 已完成 0.2s]，展开显示参数与返回摘要；风险操作前面加 3px 色条。
- 审批：居中卡片（不是底部抽屉），首行是金额（28px tabular），下面四行关键信息（收款人/收款账户/付款账户/时间），再下面「来源」标签：你的原话 / 模型抽取（附原句）。冲突信息用黄底行显式列出，不许合并成一句安心话。
- 「确认环节不经过模型」这句必须出现在审批卡底部。

## 四、状态与文案

- 模式徽章三态：规则模式（确定性）/ 自动（可用模型）/ DeepSeek；点击打开模式切换。
- 每条 Agent 结论标 `source`：规则 / 模型 / 模型失败已回退（黄点）。
- 空态一句话 + 3 个真实示例句，不写「我是你的智能助手」。
- 禁用词：赋能、一站式、智能小助手、魔法、秒懂；用银行业务词：转账、账单、限额、强验证。

## 五、验收

1. 首屏截图给不相关人员看，第一反应应是「这是银行 App」而非「这是 AI 聊天」。
2. 页面内渐变/发光数量 = 0；emoji 图标数量 = 0。
3. 1280×800 与 1440×900 无横向滚动；1024 与 720 两档降级正常。
4. 金额全部右对齐、等宽数字、两位小数；卡号/账号一律掩码。

## 六、参考来源

- Fintech Dashboard Design: 9 Real Products, Analyzed — https://adminlte.io/blog/fintech-dashboard-design-examples/
  （Stripe：表格优先、颜色只表状态；Ramp：工作队列；Brex：异常优先；Wise：费用透明；Revolut：数字可读性）
- When an AI assistant prepares a payment, what should the user approve? — https://humbleteam.com/blog/fintech-ai-payment-confirmation
  （准备/授权分离、展示字段与来源、冲突必须显式、改动后重新校验、结果不确定≠失败）
- ING Aether — Banking with AI Agents and Gen UI（UX Design Awards 2025）— https://ux-design-awards.com/winners/2025-2-ing-aether-banking-with-ai-agents-and-gen-ui
  （批评点：多数银行把 AI 做成聊天框导致体验割裂；方向：AI 融入功能、可直达、自动化与用户控制平衡）