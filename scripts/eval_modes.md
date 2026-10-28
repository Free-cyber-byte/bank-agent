# ZeroStep 规则模式 vs LLM 模式对比

生成时间：2026-10-27 11:18:58；模型：deepseek-flash；Key 可用：是

| 指标 | 规则模式 | LLM 模式 |
| --- | --- | --- |
| 30 条标准用例通过率 | 30/30 | 30/30 |
| 10 条口语化变体听懂率 | 4/10 | 10/10 |
| 安全用例通过（5 条） | 5/5 | 5/5 |
| 解析耗时 均值 / p95 | 0.03 ms / 0.04 ms | 1219.37 ms / 1969.92 ms |

## 口语化变体明细（LLM 模式）

| 用例 | 用户说法 | 期望意图 | 实际意图 | 结果 |
| --- | --- | --- | --- | --- |
| v01 | 这个月钱都花哪儿了 | query_bills | query_bills | ✓ |
| v02 | 我卡里还剩多少 | query_balance | query_balance | ✓ |
| v03 | 帮我把八百块转给我妈 | transfer | transfer | ✓ |
| v04 | 腾讯视频那个会员帮我退了 | subscription_cancel | subscription_cancel | ✓ |
| v05 | 我那几张卡都还好吗 | card_query | card_query | ✓ |
| v06 | 钱到账了，8000 存到储蓄卡，3000 买稳健的，2000 还信用卡 | cross_scenario | cross_scenario | ✓ |
| v07 | 最近有没有什么不对劲的消费 | query_transactions | query_transactions | ✓ |
| v08 | 下个月怎么攒点钱 | budget_plan | budget_plan | ✓ |
| v09 | 我想买点稳利宝，一千块 | wealth_purchase | wealth_purchase | ✓ |
| v10 | 把信用卡先冻了吧 | card_freeze | card_freeze | ✓ |

## 同一批变体在规则模式下的表现（说明词表覆盖的边界）

- v01「这个月钱都花哪儿了」：期望 query_bills，实际 unknown
- v04「腾讯视频那个会员帮我退了」：期望 subscription_cancel，实际 subscription_query
- v07「最近有没有什么不对劲的消费」：期望 query_transactions，实际 query_bills
- v08「下个月怎么攒点钱」：期望 budget_plan，实际 unknown
- v09「我想买点稳利宝，一千块」：期望 wealth_purchase，实际 unknown
- v10「把信用卡先冻了吧」：期望 card_freeze，实际 card_query
