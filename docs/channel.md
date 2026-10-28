# 渠道接入与主动触达（v0.3.2）

> 一句话：渠道层把外部消息翻译成主服务的对话请求，也把主服务的结论送出去；它只搬运文本与确认状态，不解析意图、不判定风险，钱怎么动始终由主服务决定。

本文档对应 backend/channels/server.py（IM webhook）、backend/channels/wecom.py（企业微信回调）、backend/channels/wecom_crypto.py（标准库加解密）与 backend/app/push.py（主动触达），并已按 scripts/channel_e2e.py、scripts/channel_wecom_e2e.py、scripts/push_e2e.py 的实际断言校对（2026-10-27）。

## 一、定位：从「入站」到「双向」

v0.2.0 的渠道层只解决入站：IM webhook 收到一句话，转发给主服务，再把结论回给用户。v0.3.2 补上出站：主服务可以主动生成「订阅扣费」「大额支出」「还款到期」三类提醒并投递到渠道；同时新增企业微信回调适配器，让渠道形态从一种变成三种。

不变的是边界：

1. 渠道层不持有业务数据，不直连 SQLite，不执行任何资金工具。
2. 渠道层不解析意图、不判断风险、不拼审批参数；所有金额、收款人、额度校验都在主服务。
3. 渠道层不接触审批凭据：回包永远没有 approval_id、nonce、thread_id、confirmation.token。
4. 主动推送只读账务数据，推送失败不改变账户状态，但每次推送都写审计。

## 二、总体架构

    入站（三种形态）
      浏览器 / PWA            IM webhook                 企业微信回调
      直接访问 8000           8400 /message              8401 /wecom
          │                       │                          │
          │                       ▼                          ▼
          │              channels/server.py         channels/wecom.py
          │              token 鉴权、10s 幂等         验签、AES 解密、MsgId 去重
          │                       │                          │
          └───────────────────────┴──────────────┬───────────┘
                                                 ▼
                                    主服务 backend/server.py :8000
                                    POST /api/chat、/api/approval
                                    六阶段流水线 + policy + 审批 + 审计
                                                 │
                      ┌──────────────────────────┴──────────────────────────┐
                      ▼                                                     ▼
              回复给发起方（reply / needs_confirmation）        主动触达 push.py
                                                                    POST /api/push
                                                                    三类提醒文案
                                                                          │
                                                                          ▼
                                                              CHANNEL_PUSH_URL
                                                              （任一渠道接收端）

主服务是唯一的业务与安全边界。三个入站进程都可以不启动；它们不在时，Web 页面、43 条评测与资金链路完全不受影响。

## 三、三种渠道形态

| 形态 | 入口 | 鉴权方式 | 适用场景 | 不起时的表现 |
| --- | --- | --- | --- | --- |
| Web / PWA | 主服务 8000 静态页面 | 无登录，演示单用户 | 全功能演示、手机安装到桌面 | 主服务不起则全部不可用 |
| IM webhook | 渠道进程 8400 POST /message | X-Channel-Token 共享 token | 任意 IM 平台回调、命令行联调 | 设置页渠道卡显示 enabled=false，业务不受影响 |
| 企业微信回调 | 企微进程 8401 GET/POST /wecom | URL 验签 + AES 消息加密 | 企业微信自建应用 | 设置页与主服务不受影响，仅企微不可达 |

三种形态共用同一套主服务接口与会话模型，因此同一个用户在 Web 上说「给妈妈转 8 元」，在 IM 里回复「确认」，仍然落在同一个 thread 上。渠道进程用 sessions.json 保存 user_id → session_id 映射，重启后能续上同一会话。

## 四、统一消息处理

入站适配器只做协议转换，真正的业务处理集中在 backend/channels/server.py 的 handle_message：

1. 取 user_id 对应的 session_id（没有则新建并写入 sessions.json）。
2. 查 10 秒幂等缓存：同 user_id + 同 text 直接返回上次结果，标 cached=true，不重复调用主服务。
3. 判断是否是「确认 / 取消」：是则调用主服务 POST /api/approval，用渠道进程内存里的 approval_id；否则调用 POST /api/chat。
4. 把主服务结果翻译成渠道回包：直接完成回 reply，需要审批回 needs_confirmation + preview + hint。
5. 每条收发写 messages.jsonl 一行：ts、user_id、direction、text、status、cached。

渠道进程的幂等只是防 IM 重推；真正的资金幂等仍由主服务的幂等键（thread_id + turn_key + step_id）保证。

## 五、IM webhook（8400）

### 5.1 配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| CHANNEL_ENABLED | 1 | 设为 0 时不启动渠道进程 |
| CHANNEL_PORT | 8400 | 渠道监听端口；主服务状态接口读同一变量 |
| CHANNEL_TOKEN | 空（演示值 zerostep-demo-token） | 请求头 X-Channel-Token 必须与之一致 |
| CHANNEL_MAX_BODY | 32768 | 请求体上限，超出 413 |
| CHANNEL_IDEMPOTENT_SECONDS | 10 | user_id + text 幂等窗口 |
| 主服务地址 | http://127.0.0.1:8000 | 依次读 ZEROSTEP_MAIN_URL / MAIN_URL / ZEROSTEP_API_URL |

### 5.2 GET /health

    GET http://127.0.0.1:8400/health

返回渠道自身存活与配置摘要，不触发业务调用：

    {
      "status": "ok",
      "enabled": true,
      "port": 8400,
      "main_url": "http://127.0.0.1:8000",
      "sessions": 1,
      "pending": 0,
      "ts": "2026-10-27T20:27:54+08:00"
    }

pending 是渠道进程内存里待确认的审批数，重启归零。

### 5.3 POST /message

    POST http://127.0.0.1:8400/message
    Content-Type: application/json
    X-Channel-Token: <CHANNEL_TOKEN>

    {"user_id": "u_demo", "text": "给妈妈转 8 元"}

错误：

| HTTP | 响应 | 触发条件 |
| --- | --- | --- |
| 401 | {"error":"UNAUTHORIZED"} | token 缺失或不匹配 |
| 503 | {"error":"CHANNEL_DISABLED"} | CHANNEL_ENABLED=0 |
| 413 | {"error":"BODY_TOO_LARGE"} | 请求体超过 CHANNEL_MAX_BODY |
| 400 | {"error":"BAD_REQUEST","message":"缺少 user_id 或 text"} | 字段缺失或为空 |
| 400 | {"error":"BAD_REQUEST","message":"请求体不是合法 JSON"} | 非法 JSON |
| 400 | {"error":"BAD_REQUEST","message":"请求体必须是 JSON 对象"} | 顶层不是对象 |
| 404 | {"error":"NOT_FOUND"} | 非 /message 路径 |
| 405 | {"error":"METHOD_NOT_ALLOWED"} | GET /message |

响应三态：

| 场景 | 响应 |
| --- | --- |
| 直接完成 | {"reply": str, "status": str} |
| 需要确认 | {"needs_confirmation": true, "preview": str, "hint": "回复 确认 或 取消"} |
| 命中 10 秒幂等 | 同上，另带 cached: true |

IM 渠道转发主服务 15 秒超时；主服务不可达或超时返回 HTTP 200 的友好文案，不伪造成功。企业微信回调的转发超时是另一套（默认 5 秒，见 §6.1）。

## 六、企业微信回调（8401）

### 6.1 配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| WECOM_ENABLED | 1 | 设为 0 时回调返回 503「企业微信渠道已停用」 |
| WECOM_PORT | 8401 | 回调监听端口 |
| WECOM_HOST | 127.0.0.1 | 监听地址；局域网联调改 0.0.0.0 |
| WECOM_TOKEN | 空（回退 CHANNEL_TOKEN） | 回调 Token，用于 sha1 验签 |
| WECOM_AES_KEY | 空 | 43 位 EncodingAESKey |
| WECOM_CORP_ID | 空 | 企业微信 CorpID，解密后校验 |
| WECOM_TIMEOUT | 5 | 转发主服务的硬超时秒数（WECOM_TIMEOUT 可调） |
| WECOM_DEDUPE_SECONDS | 300 | 同一 MsgId 去重窗口（WECOM_DEDUPE_SECONDS 可调） |

两条时间约束互相独立，不要混用：转发超时 5 秒（WECOM_TIMEOUT 可调，约束单次转发主服务的等待）/ MsgId 去重 300 秒（WECOM_DEDUPE_SECONDS 可调，约束同一消息的重复投递）。

Token、AESKey、CorpID 三者任一为空即视为「未配置」，GET 与 POST 都返回 503「企业微信渠道未配置（缺少 Token / EncodingAESKey / CorpID）」。GET /health 始终 200，只返回是否配置：

    {
      "ok": true,
      "service": "wecom",
      "enabled": true,
      "token_set": true,
      "aes_key_set": true,
      "corp_id_set": true,
      "dedupe_size": 3,
      "dedupe_hits": 0,
      "port": 8401,
      "main_url": "http://127.0.0.1:8000",
      "ts": "2026-10-27T22:30:00+08:00"
    }

### 6.2 GET /wecom：URL 验签

企业微信配置回调地址时会发 GET 请求，参数 msg_signature、timestamp、nonce、echostr：

1. 用 Token、timestamp、nonce、echostr 计算 sha1 签名，与 msg_signature 做定时安全比较。
2. 签名通过后解密 echostr，返回明文纯文本（不是 JSON）。
3. 缺参数 400；签名不匹配 403「签名校验失败」；其他路径 404。

### 6.3 POST /wecom：接收消息与被动回包

    POST http://127.0.0.1:8401/wecom?msg_signature=...&timestamp=...&nonce=...

请求体是 XML，外层含 Encrypt 节点。处理顺序：

1. 验签（sha1 + hmac.compare_digest），失败 403，不进入解密。
2. 解密 Encrypt：AES-128-CBC，IV 等于 key，PKCS#7 补位；明文结构为 random(16) + msg_len(4，网络序) + msg + corp_id。
3. 校验 CorpID；不符按非法消息处理。
4. 解析内层 XML，取 MsgType、FromUserName、Content、MsgId。
5. 只处理 MsgType=text：非文本回「暂时只支持文本消息，请直接回复文字指令（例如：查一下我的余额）。」；空内容回「消息内容为空，请直接回复文字指令。」
6. 按 MsgId 去重：300 秒内重复的 MsgId 直接回上次的回复，不再调用主服务。
7. 复用 handle_message 转发主服务（共享 sessions、10 秒幂等、两步确认、messages.jsonl），线程 join 硬超时 WECOM_TIMEOUT 秒。
8. 把回复重新加密成被动回包 XML（Encrypt / MsgSignature / TimeStamp / Nonce），200 text/xml 返回。

错误与降级：

| HTTP / 表现 | 触发条件 |
| --- | --- |
| 503 | 插件停用「企业微信渠道已停用」，或未配置三件套 |
| 403 | 签名校验失败 |
| 413 | 请求体超过 1MB「请求体过大」 |
| 400 | 空体「请求体为空」/ 坏 XML「请求体不是合法 XML」/ 缺 Encrypt「XML 缺少 Encrypt 节点」/ 解密失败「消息解密失败」 |
| 404 | 非 /wecom 路径 |
| 200 加密回包 | 主服务不可达「渠道服务暂时不可用，请稍后再试」；转发超时「处理超时，请稍后再试」；内部异常「服务暂时不可用，请稍后再试」 |

### 6.4 加解密实现

wecom_crypto.py 不依赖 cryptography，纯标准库实现：

- AES-128：自带 FIPS-197 S 盒与密钥扩展，CBC 模式，IV 取 key 本身。
- PKCS#7：补位到 32 字节块；EncodingAESKey 43 位补一个 = 后 base64 解 32 字节，取前 16 字节为 key。
- 签名：把 token、timestamp、nonce、encrypt 四个字符串排序后拼接，做 SHA-1 十六进制摘要。
- 比较：hmac.compare_digest，避免时序侧信道。
- 回包 XML：Encrypt 为密文 base64，MsgSignature 为对密文重新计算的签名。

### 6.5 回包安全

回包只允许两类内容：

- 普通回复：reply。
- 需要确认：「【需要二次确认】」+ preview + hint。

绝不包含 approval_id、nonce、thread_id、confirmation.token。审批凭据只存在渠道进程内存，用户回复「确认」时由进程代为提交。

### 6.6 企微验收表（QA 报告实测 13/13，2026-10-27）

| 编号 | 断言 | 实测 |
| --- | --- | --- |
| wxm-1 | 加解密实现自检（FIPS-197 官方向量） | PASS，Appendix B/C 均命中 |
| wxm-2 | GET /health 字段齐全且不泄露密钥 | PASS，enabled / token_set / aes_key_set / corp_id_set / dedupe_size / port / main_url 齐全，响应不含 Token / AESKey / CorpID 明文 |
| wxm-3 | GET /wecom 验签 + echostr 解密 | PASS，返回明文与 echostr 一致 |
| wxm-4 | 篡改签名 / 缺参 | PASS，篡改 msg_signature → 403；缺参 → 400 |
| wxm-5 | 主服务不可达 | PASS，200 加密回包「渠道服务暂时不可用，请稍后再试」，耗时 2.05s |
| wxm-6 | 合法消息回包 | PASS，外层 MsgSignature 独立验签、独立解密成功、XML 结构正确 |
| wxm-7 | 同一 MsgId 重发去重 | PASS，回包复读首次回复，主服务调用数不增，dedupe_hits ≥1 |
| wxm-8 | 两步确认 | PASS，首答含【需要二次确认】/ preview / hint，回「确认」后审批接口收到 1 次 decision=approve |
| wxm-9 | 回包敏感字段不泄露 | PASS，三个回包均不含 approval_id / thread_id / nonce / session 等测试凭据 |
| wxm-10 | 异常输入 | PASS，坏 XML 400、坏签名 403、CorpID 不符 400 |
| wxm-11 | 非文本消息 | PASS，200 友好提示「暂时只支持文本消息…」 |
| wxm-12 | 超时路径（WECOM_TIMEOUT=1，慢主服务 2.5s） | PASS，1.05s 返回「处理超时，请稍后再试」（阈值 <2.2s） |
| wxm-13 | 进程与端口清理 | PASS，8421 / 8423 / 57820 均无 LISTENING 残留，日志无 Traceback |

另有内置自检 python backend/channels/wecom.py --selftest，9/9 通过，覆盖 FIPS-197 AES 官方向量、PKCS#7、报文封装往返、验签与篡改、GET/POST 回包、MsgId 去重、主服务不可达与转发超时；产品自检只作参考，验收以上表独立实现的真实 HTTP 13 项为准。

## 七、主动触达：POST /api/push

### 7.1 请求

    POST http://127.0.0.1:8000/api/push
    Content-Type: application/json

    {"kind": "subscription"}

kind 三选一：subscription（订阅扣费，默认）、large_txn（大额支出）、repayment（还款提醒）。空对象按 subscription 处理。

### 7.2 三类文案

| kind | 数据来源 | 文案骨架 |
| --- | --- | --- |
| subscription | active 订阅按 next_charge 升序，优先 PUSH_SUBSCRIPTION_DAYS 天内 | 「订阅扣费提醒：iCloud 200G ¥6.00」，正文给出扣费日期与「回复取消订阅」提示 |
| large_txn | 今天最大一笔 out 流水；不够阈值则回看最近 7 天 | 「大额支出提醒：信用卡中心 ¥3,000.00」，正文给出占本月支出比例与「冻结卡片」提示 |
| repayment | credit 账户，25 日到期（已过则下月 25） | 「还款提醒：信用卡待还 ¥8,432.00」，正文给出到期日、剩余天数与「还信用卡」提示 |

无可用数据时不报 500，而是返回业务错误码：NO_SUBSCRIPTION / NO_TRANSACTION / NO_CREDIT_ACCOUNT；无欠款时还款提醒返回「还款提醒：本期已结清」。

### 7.3 响应

    {
      "ok": true,
      "kind": "subscription",
      "user_id": "u001",
      "title": "订阅扣费提醒：iCloud 200G ¥6.00",
      "text": "...",
      "pushed": true,
      "delivered": true,
      "channel": {
        "configured": true,
        "status": "delivered",
        "url_masked": "http://127.0.0.1:8400/***",
        "latency_ms": 12,
        "error": ""
      },
      "audit_seq": 128,
      "ts": "2026-10-27T22:30:00+08:00"
    }

channel.status 三态：

| 状态 | 含义 |
| --- | --- |
| not_configured | 未配置 CHANNEL_PUSH_URL，只生成文案，不投递 |
| delivered | 已 POST 到接收端且返回 2xx |
| failed | 接收端不可达、超时或非 2xx；error 给出原因，不伪造成功 |

### 7.4 审计与配置

每次推送无论成功失败都写一条审计：event_type=push_message、tool=push、actor=system、risk_level=low；args 只记 kind、user_id、channel_configured，result 记 title、text、pushed、delivered、channel。推送正文会进入审计（便于复盘提醒内容），但不含渠道 token 与完整 URL。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| CHANNEL_PUSH_URL | 空 | 推送接收端完整地址；为空时 not_configured |
| CHANNEL_PUSH_TIMEOUT | 5 | 推送超时秒数，夹在 1–30 |
| PUSH_SUBSCRIPTION_DAYS | 3 | 订阅提醒提前天数 |
| PUSH_LARGE_TXN_CENTS | 30000 | 大额支出阈值（分），默认 300 元 |

### 7.5 推送验收

scripts/push_e2e.py 13/13 通过（2026-10-27，QA 报告）：未配置时 not_configured、三类文案与数字、push_message 审计且哈希链 valid、非法 kind 400 BAD_KIND、delivered 与 url_masked 正确、桩服务真实收包、不可达时 failed、只读账务不变（total_cents=5,786,050）、进程退出无端口残留；另含 PWA 静态资源 3 组——manifest 200 + application/manifest+json + 1,045 B 字节一致，sw.js 200 + 2,689 B 逐字节一致，5 个图标（192 / 512 / 两个 maskable / apple-touch-icon 180）尺寸与字节一致。

## 八、启动与演示

四进程全部可选，只起主服务也能完整演示 Web 端：

    # 主服务（必需，8000）
    python backend/server.py

    # IM 渠道（可选，8400）
    $env:CHANNEL_TOKEN = "my-demo-token"
    python backend/channels/server.py

    # 企业微信回调（可选，8401）
    $env:WECOM_TOKEN = "wecom-token"
    $env:WECOM_AES_KEY = "43位EncodingAESKey"
    $env:WECOM_CORP_ID = "ww1234567890"
    python backend/channels/wecom.py

    # 主动推送接收端（可选，任意 HTTP 端点；不配置则只有文案）
    $env:CHANNEL_PUSH_URL = "http://127.0.0.1:8400/push"

手机访问：主服务用 HOST=0.0.0.0 监听，手机浏览器访问 http://<电脑局域网IP>:8000。PWA 的 Service Worker 只在 localhost 或 HTTPS 下注册，局域网 HTTP 可正常使用页面，但没有离线缓存。

端口一致性：IM 渠道进程用 --port 实际监听，主服务 /api/channel/status 的 port 读 CHANNEL_PORT；两者不一致时状态页显示与实际监听不同，演示时让两边共用同一个值。企微端口同理，以 WECOM_PORT 为准。

## 九、安全设计

1. 共享 token 鉴权：IM 请求头或企微验签不通过直接 401/403，不进入业务逻辑。
2. 独立加解密：企微消息在渠道进程内验签、解密、再加密，主服务不接触企微协议与密钥。
3. 不泄露审批凭据：三类渠道回包都经过白名单，审批凭据只存在渠道进程内存。
4. 渠道层不做业务判定：风险、额度、收款人校验全在主服务，渠道无法把 high 降级或跳步执行。
5. 体积与超时：IM 请求体 32KB、企微 1MB 上限；转发超时分别为 15 秒与 WECOM_TIMEOUT（默认 5 秒），避免慢请求占满线程。
6. 重放防护：MsgId 去重 300 秒 + 渠道 10 秒幂等；资金动作另有主服务幂等键兜底。
7. 推送安全：地址只以掩码展示；未配置不投递；失败不伪造成功；推送写审计但不记录凭据。
8. 留痕：messages.jsonl 记录收发，便于复盘；不记录 token 与审批凭据。

## 十、降级与错误

| 场景 | 表现 |
| --- | --- |
| IM 主服务未启动 | 渠道返回 HTTP 200 {reply:"渠道服务暂时不可用，请稍后再试", status:"error"}，不缓存为成功 |
| IM 主服务超时 | 返回超时提示，建议重试；不伪造成功 |
| 企微主服务不可达 / 超时 | 加密回包内为可读文案，HTTP 仍 200，避免企微无限重试 |
| CHANNEL_ENABLED=0 | 不启动渠道进程；主服务 /api/channel/status 显示 enabled=false |
| WECOM_ENABLED=0 或未配置 | 回调 503，Web 与主服务不受影响 |
| 企微验签失败 | 403，不解密、不转发 |
| 消息体过大 / 字段缺失 | IM 413 / 400；企微 413 / 400 |
| 审批已过期 | 主服务返回过期结果，渠道原样告知，不自动重提 |
| channel-im 插件停用 | 渠道进程返回 503「渠道插件已停用」，不写 messages.jsonl；主服务状态接口仍 200 且 enabled=false |
| 推送未配置 | 200 + not_configured，文案照常生成并写审计 |
| 推送接收端失败 | 200 + failed + error，不改变账务 |

渠道不可用不影响 Web 页面、43 条评测与资金链路。

## 十一、边界与非目标

- 不接真实 IM 平台与企业微信后台：IM 是演示级 webhook，企微是本地回调适配器，真实联调需配置可信域名、IP 白名单与正式应用。
- 不做多媒体、富文本卡片、群聊与事件回调；企微只处理文本消息。
- token、AESKey、CorpID 是共享密钥，演示值公开写在仓库与文档中；生产必须换值并接入密钥管理。
- messages.jsonl 含用户原文明文，仅本机演示；不要提交真实用户数据。
- 主动推送不是消息队列：无重试、无补偿、无去重表，失败只记录当次结果。

## 十二、已知限制

1. 企微适配器只在本机自检与 Mock 环境验证，未接真实企业微信后台。
2. 只处理 MsgType=text；非文本统一回友好提示。
3. MsgId 去重表在进程内存，重启清空；多实例部署需外部去重存储。
4. IM 渠道的审批状态只在进程内存，重启后旧待确认消息需重新发起。
5. 幂等缓存按 user_id + text 精确匹配，改一个字就会重走业务。
6. 同一 user_id 并发两条消息可能产生两次请求；资金幂等由主服务兜底，但日志各记一条。
7. 推送无重试与补偿，失败只体现在响应与审计里。
8. PWA 离线缓存依赖 HTTPS 或 localhost；局域网 HTTP 不注册 Service Worker。
9. IM 渠道 --port 与主服务 CHANNEL_PORT 不一致时状态页端口与实际监听不一致。

## 十三、验收与自检

    cd D:/DeepSeek/buildagent/bank-agent
    $env:PYTHONIOENCODING = "utf-8"

    python backend/channels/wecom.py --selftest     # 9/9
    python scripts/channel_e2e.py                   # 9/9（IM 入站）
    python scripts/channel_wecom_e2e.py             # 13/13（企微回调）
    python scripts/push_e2e.py                      # 13/13（主动触达，含 PWA）
    node frontend/_selftest.js                      # PASS=182 FAIL=0（含手机端与模拟推送）

以上为 2026-10-27 QA 全量回归报告（工作区根 audit/qa-report-v0.3.2.md）的数字：15/15 步 exit 0、0 FAIL，结论为全绿、建议冻结 v0.3.2。
