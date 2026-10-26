"""规则意图解析 + 不可信内容检测。

零依赖：不配置大模型也能稳定解析本项目的 30 条评测用例；配置 DEEPSEEK_API_KEY
后可用大模型增强抽取，失败自动回退到这里。安全边界：模型只产出意图与槽位，
资金动作始终由确定性工具执行。
"""
import re
from typing import Any, Dict, List, Optional, Tuple

CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}

INJECTION_PATTERNS = [
    r"忽略(以上|之前|前面|所有).{0,12}(指令|提示|规则)",
    r"(ignore|disregard).{0,20}(previous|above|prior)",
    r"system\s*[:：]",
    r"你现在(是|扮演)",
    r"(立即|马上|赶紧).{0,6}(转账|汇款|把钱转)",
    r"把.{0,12}(全部|所有).{0,6}(钱|余额|资金).{0,6}(转|汇)",
    r"然后(回复|回答).{0,6}(已完成|成功)",
]
PHISHING_PATTERNS = [
    r"(把|告诉|发送|提供).{0,6}(验证码|动态码|密码|优盾|短信码)",
    r"我是.{0,8}(客服|银行工作人员|公安|工作人员)",
    r"(验证码|动态码).{0,6}(是多少|发我|给我)",
]
ESCALATION_PATTERNS = [
    r"(查|看|转|冻结).{0,6}(别人|他人|其他用户).{0,8}(账户|余额|流水|的钱)",
    r"(李娜|王强|u002|u003).{0,10}(账户|余额|流水|的钱|资产)",
    r"(绕过|跳过).{0,8}(审批|验证|风控|权限)",
    r"(管理员|admin).{0,8}(权限|模式|账号)",
]

def _normalize(text: str) -> str:
    trans = {"０": "0", "１": "1", "２": "2", "３": "3", "４": "4", "５": "5", "６": "6", "７": "7", "８": "8", "９": "9",
             "，": ",", "。": ".", "：": ":", "；": ";", "！": "!", "（": "(", "）": ")", "％": "%"}
    out = "".join(trans.get(ch, ch) for ch in text)
    return re.sub(r"\s+", " ", out).strip()


def detect_injection(text: str) -> Optional[str]:
    if not text:
        return None
    for pat in INJECTION_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(0)
    return None


def detect_attack(text: str) -> Optional[str]:
    t = _normalize(text)
    for pat in ESCALATION_PATTERNS:
        if re.search(pat, t, re.IGNORECASE):
            return "unauthorized_access"
    for pat in PHISHING_PATTERNS:
        if re.search(pat, t, re.IGNORECASE):
            return "credential_phishing"
    if detect_injection(t):
        return "prompt_injection"
    if re.search(r"(陌生|不明|未知).{0,10}(账户|收款人|卡号|账号|对象)|(转账|汇款|转给|打钱).{0,10}(陌生|不明|未知)", t):
        return "suspicious_payee"
    return None


def _cn_to_int(s: str) -> Optional[int]:
    if not s:
        return None
    total, section, number = 0, 0, 0
    for ch in s:
        if ch in CN_DIGITS:
            number = CN_DIGITS[ch]
        elif ch in CN_UNITS:
            unit = CN_UNITS[ch]
            if unit == 10000:
                section = (section + number) * unit
                total += section
                section, number = 0, 0
            else:
                if number == 0:
                    number = 1
                section += number * unit
                number = 0
        else:
            return None
    return total + section + number


def extract_amount(text: str) -> Optional[int]:
    t = _normalize(text)
    m = re.search(r"(\d+(?:\.\d{1,2})?)\s*(万|千)", t)
    if m:
        val = float(m.group(1)) * (10000 if m.group(2) == "万" else 1000)
        return int(round(val * 100))
    m = re.search(r"[¥￥]\s*(\d+(?:\.\d{1,2})?)", t)
    if m:
        return int(round(float(m.group(1)) * 100))
    m = re.search(r"(\d+(?:\.\d{1,2})?)\s*(?:元|块|rmb|RMB)", t, re.IGNORECASE)
    if m:
        return int(round(float(m.group(1)) * 100))
    m = re.search(r"([一二两三四五六七八九十百千万零〇]{1,8})\s*(?:元|块)", t)
    if m:
        v = _cn_to_int(m.group(1))
        if v:
            return v * 100
    m = re.search(r"(?:转|汇|打|申购|买入|买|充值|存)\s*(\d+(?:\.\d{1,2})?)", t)
    if m:
        return int(round(float(m.group(1)) * 100))
    return None


def extract_months(text: str) -> Optional[int]:
    t = _normalize(text)
    if re.search(r"(本月|这个月|当月|近一个月|最近一个月)", t):
        return 1
    m = re.search(r"(?:近|最近|过去|前)\s*(\d+)\s*个?月", t)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*个?月", t)
    if m:
        return int(m.group(1))
    m = re.search(r"(?:近|最近|过去|前)\s*([一二三四五六七八九十]{1,3})\s*个?月", t)
    if m:
        return _cn_to_int(m.group(1))
    return None


def extract_payee(text: str, payees: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str], bool]:
    t = _normalize(text)
    for p in payees:
        name = p.get("name") or ""
        if name and name in t:
            return name, p.get("account_no"), bool(p.get("is_new"))
    m = re.search(r"(?:给|向|转给|转账给|打给)\s*([\u4e00-\u9fa5A-Za-z]{1,10}?)(?:转|汇|打|\d|元|块|，|,|。|$)", t)
    if m:
        name = m.group(1)
        for prefix in ("新朋友", "朋友", "我的", "同事", "新同事"):
            if name.startswith(prefix) and len(name) > len(prefix):
                name = name[len(prefix):]
        if name not in ("我", "自己", "我的", "储蓄卡", "工资卡"):
            return name, None, True
    m = re.search(r"(\d{12,19})", t)
    if m:
        return None, m.group(1), True
    return None, None, False


def extract_category(text: str) -> Optional[str]:
    t = _normalize(text)
    mapping = {"餐饮": ["餐饮", "吃饭", "外卖", "咖啡", "餐厅", "伙食"],
               "购物": ["购物", "买东西", "网购", "衣服", "数码"],
               "交通": ["交通", "打车", "地铁", "出行", "加油"],
               "娱乐": ["娱乐", "电影", "演出", "KTV"],
               "居住": ["房租", "居住", "水电"],
               "订阅": ["订阅", "会员"]}
    for cat, kws in mapping.items():
        if any(k in t for k in kws):
            return cat
    return None


def parse(text: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
    raw = text or ""
    t = _normalize(raw)
    payees = ctx.get("payees", [])
    products = ctx.get("products", [])
    subs = ctx.get("subscriptions", [])
    cards = ctx.get("cards", [])
    accounts = ctx.get("accounts", [])

    attack = detect_attack(t)
    if attack:
        return {"name": "attack", "confidence": 0.95, "source": "rule", "slots": {"attack_type": attack}, "attack_type": attack}

    slots: Dict[str, Any] = {}
    amount = extract_amount(t)
    if amount is not None:
        slots["amount_cents"] = amount
    months = extract_months(t)
    if months:
        slots["months"] = months
    category = extract_category(t)
    if category:
        slots["category"] = category
    payee_name, payee_account, payee_new = extract_payee(t, payees)
    if payee_name:
        slots["payee_name"] = payee_name
    if payee_account:
        slots["payee_account"] = payee_account
    slots["payee_is_new"] = payee_new

    from_kind = "checking"
    if "储蓄卡" in t:
        from_kind = "savings"
    if "信用卡" in t:
        from_kind = "credit"
    slots["from_kind"] = from_kind

    # 产品匹配
    for p in products:
        if p["name"] in t:
            slots["product_id"] = p["product_id"]
            slots["product_name"] = p["name"]
            break
    if "product_id" not in slots:
        alias = {"稳利宝": "W001", "稳健": "W001", "增利宝": "W002", "指数": "W003", "零钱": "W004", "货币": "W004"}
        for k, pid in alias.items():
            if k in t:
                slots["product_id"] = pid
                break
    if ctx.get("risk_level") and slots.get("product_id"):
        slots["suitability_note"] = "客户风险等级为 %s" % ctx["risk_level"]

    # 订阅商户匹配
    for s in subs:
        merchant = s.get("merchant") or ""
        if merchant and (merchant in t or merchant.split(" ")[0] in t):
            slots["sub_id"] = s.get("sub_id")
            slots["merchant"] = merchant
            break
    if "sub_id" not in slots:
        for kw, merchant in [("爱奇艺", "爱奇艺"), ("网易云", "网易云"), ("icloud", "iCloud"), ("iCloud", "iCloud"), ("健身", "健身私教"), ("周刊", "财经周刊")]:
            if kw in t:
                slots["merchant"] = merchant
                for s in subs:
                    if merchant in (s.get("merchant") or ""):
                        slots["sub_id"] = s.get("sub_id")
                        break
                break

    # 卡片匹配
    if "虚拟卡" in t or "虚拟账户" in t:
        slots["card_kind"] = "virtual"
    elif "信用卡" in t:
        slots["card_kind"] = "credit"
    elif "储蓄卡" in t or "借记卡" in t:
        slots["card_kind"] = "debit"
    for c in cards:
        if c.get("number_masked", "")[-4:] and c["number_masked"][-4:] in t:
            slots["card_id"] = c["card_id"]
            break
    m = re.search(r"额度\s*(\d+)\s*(万|千|元|块)?", t)
    if m and "虚拟卡" in t:
        multi = {"万": 10000, "千": 1000}.get(m.group(2) or "", 1)
        slots["daily_limit_cents"] = int(m.group(1)) * multi * 100

    # 意图判定（顺序即优先级）
    is_greeting = bool(re.search(r"^(你好|您好|hi|hello|在吗|嗨)", t, re.IGNORECASE)) and len(t) < 20
    has_balance = bool(re.search(r"(余额|卡里|账户里|还有多少钱|还剩多少|总资产)", t))
    has_transfer = bool(re.search(r"(转账|汇款|转给|给.{0,8}转|打钱|打给)", t)) or (("转" in t) and ("给" in t or "到" in t))
    has_bill = bool(re.search(r"(账单|花了(多少|多少元)|消费|支出|收支|流水|查账|开销)", t))
    has_budget = bool(re.search(r"(预算|省钱|超支|攒钱|储蓄率|理财规划|记账)", t))
    has_wealth = bool(re.search(r"(理财|基金|申购|买入|投资|收益|净值)", t))
    has_card = bool(re.search(r"(银行卡|卡片|信用卡|虚拟卡|借记卡|冻结|解冻|挂失|额度|几张卡|我的卡|有哪些卡)", t))
    has_sub = bool(re.search(r"(订阅|会员|自动续费|续费|取消.*(会员|订阅))", t))
    has_save_alloc = bool(re.search(r"(存|转|放|划).{0,10}储蓄卡|储蓄卡.{0,8}(存|转|放)", t))
    has_wealth_alloc = bool(re.search(r"(买|申购|投|认购).{0,10}(理财|稳利宝|增利宝|指数|基金|货币)", t))
    has_credit_alloc = bool(re.search(r"还.{0,6}信用卡", t))
    alloc_count = sum(1 for x in (has_save_alloc, has_wealth_alloc, has_credit_alloc) if x)
    has_cross = bool(re.search(r"(分账|发工资|工资到账|生日|旅行|毕业旅行|订个提醒|提醒我)", t)) or (has_transfer and has_wealth) or alloc_count >= 2

    if is_greeting:
        return {"name": "greeting", "confidence": 0.9, "source": "rule", "slots": {}}
    cross_strong = alloc_count >= 2 or bool(re.search(r"(分账|发工资|工资到账|生日|订个提醒|提醒我)", t))
    if has_cross and cross_strong:
        return {"name": "cross_scenario", "confidence": 0.85, "source": "rule", "slots": slots}
    if has_card:
        if re.search(r"(冻结|挂失)", t):
            return {"name": "card_freeze", "confidence": 0.9, "source": "rule", "slots": slots}
        if "解冻" in t:
            return {"name": "card_unfreeze", "confidence": 0.9, "source": "rule", "slots": slots}
        if re.search(r"(申请|办一张|开一张|开通)", t):
            return {"name": "card_apply", "confidence": 0.9, "source": "rule", "slots": slots}
        return {"name": "card_query", "confidence": 0.9, "source": "rule", "slots": slots}
    if has_sub:
        if re.search(r"(取消|退订|关掉|关闭)", t):
            return {"name": "subscription_cancel", "confidence": 0.9, "source": "rule", "slots": slots}
        return {"name": "subscription_query", "confidence": 0.9, "source": "rule", "slots": slots}
    if has_wealth:
        if re.search(r"(申购|买入|买|投|认购)", t) and "amount_cents" in slots:
            return {"name": "wealth_purchase", "confidence": 0.9, "source": "rule", "slots": slots}
        return {"name": "wealth_query", "confidence": 0.88, "source": "rule", "slots": slots}
    if has_balance and not has_transfer:
        return {"name": "query_balance", "confidence": 0.9, "source": "rule", "slots": slots}
    if has_transfer and "amount_cents" in slots:
        return {"name": "transfer", "confidence": 0.94, "source": "rule", "slots": slots}
    if has_budget:
        return {"name": "budget_plan", "confidence": 0.88, "source": "rule", "slots": slots}
    if re.search(r"(流水|交易记录|交易明细|最近.{0,4}交易|异常交易|可疑交易|有没有异常)", t):
        return {"name": "query_transactions", "confidence": 0.9, "source": "rule", "slots": slots}
    if has_bill:
        return {"name": "query_bills", "confidence": 0.9, "source": "rule", "slots": slots}
    if has_transfer:
        return {"name": "transfer", "confidence": 0.6, "source": "rule", "slots": slots}
    return {"name": "unknown", "confidence": 0.3, "source": "rule", "slots": slots}

def parse_cross(text: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
    """解析组合任务（发工资分账 / 生日 / 旅行）里的多条资金指令。"""
    t = _normalize(text)
    allocs: List[Dict[str, Any]] = []

    def to_cents(num: str, unit: str) -> int:
        multi = {"万": 10000, "千": 1000}.get(unit or "", 1)
        return int(round(float(num) * multi * 100))

    m = re.search(r"(\d+(?:\.\d+)?)\s*(万|千)?\s*(?:元|块)?\s*(?:到|进|去|存到|转入|转)\s*(?:我的|本人)?储蓄卡", t)
    if m:
        allocs.append({"kind": "savings", "amount_cents": to_cents(m.group(1), m.group(2))})
    else:
        m = re.search(r"(\d+(?:\.\d+)?)\s*(万|千)?\s*(?:元|块)?\s*存(?:起来|储蓄)", t)
        if m:
            allocs.append({"kind": "savings", "amount_cents": to_cents(m.group(1), m.group(2))})
    m = re.search(r"(\d+(?:\.\d+)?)\s*(万|千)?\s*(?:元|块)?\s*(?:买|申购|投|认购)\s*(?:点|些)?(稳健|稳利宝|增利宝|指数|理财|基金|货币)", t)
    if m:
        allocs.append({"kind": "wealth", "amount_cents": to_cents(m.group(1), m.group(2)), "hint": m.group(3)})
    m = re.search(r"(\d+(?:\.\d+)?)\s*(万|千)?\s*(?:元|块)?\s*(?:还|还掉|归还)\s*信用卡", t)
    if m:
        allocs.append({"kind": "credit", "amount_cents": to_cents(m.group(1), m.group(2))})
    has_reminder = bool(re.search(r"(提醒|提醒我|订个提醒|日程)", t))
    if "生日" in t:
        payee_name, payee_account, payee_new = extract_payee(t, ctx.get("payees", []))
        amount = extract_amount(t)
        if amount and payee_name:
            allocs.append({"kind": "transfer", "amount_cents": amount, "payee_name": payee_name,
                           "payee_account": payee_account, "payee_is_new": payee_new})
        has_reminder = True
    return {"allocations": allocs, "reminder": has_reminder}
