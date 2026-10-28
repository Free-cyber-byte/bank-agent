---
id: bills
name: 账单消费分析
description: 查账单、看消费构成与环比，帮用户定位花得多的分类
triggers: 账单, 消费, 支出, 流水, 开销, 花了多少
tools: get_bill_summary, get_recent_transactions
examples: 查一下这个月的账单, 这个月消费了多少, 看看我的支出明细
slots: months, category
clarify: 查几个月？, 只看某个分类吗？
forbidden: 不得编造不存在的消费, 不得把收入算进支出
---

## 适用场景

用户问这个月花了多少、钱花在哪、某类消费多少，或想看最近流水。

## 处理流程

1. 用 `get_bill_summary` 取月度汇总与分类占比；用户指定分类时再用 category 过滤。
2. 用 `get_recent_transactions` 取明细核对，金额展示用两位小数。
3. 回复里给出总额、Top 分类和一句可执行的省钱建议，不堆砌流水。

## 需要补充的信息

- 月份：默认本月，用户说「上个月」要覆盖 months。
- 分类：只有用户明确提到时才过滤，不要替他决定。

## 安全边界

- 只读数据，技能卡不允许触发任何资金操作。
- 金额只展示、不改写，审计链以数据库记录为准。
