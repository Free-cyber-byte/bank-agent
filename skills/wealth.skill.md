---
id: wealth
name: 理财持仓与申购
description: 查理财持仓收益，或按风险等级申购理财产品，申购前必须确认金额与产品
triggers: 理财, 申购, 基金, 收益, 净值, 稳利宝, 买入
tools: list_wealth_products, get_wealth_holdings, purchase_wealth
examples: 申购 1000 元稳利宝, 看看我的理财收益, 最近基金净值怎么样
slots: product_id, amount_cents
clarify: 买哪个产品？, 申购多少钱？
forbidden: 不得承诺收益, 不得推荐超出用户风险等级的产品
---

## 适用场景

用户想了解理财持仓/收益，或要申购、买入某只产品。

## 处理流程

1. 查询类用 `get_wealth_holdings` / `list_wealth_products`，展示七日年化与持仓收益。
2. 申购类先确认产品与金额，金额换整数分，再走 approval 二次确认。
3. 回复里明确「理财非存款、收益有波动」，不承诺保本或具体收益。

## 需要补充的信息

- 产品：用户只说「买点理财」时要追问具体产品。
- 金额：申购金额不能默认，必须用户明确。

## 安全边界

- 技能卡不得替用户决定买什么、买多少。
- 风险等级由 policy 判定，技能卡无权提升用户风险偏好。
