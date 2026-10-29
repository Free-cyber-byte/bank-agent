"""技能卡（Skill Cards）注册与路由（v0.2.0，零依赖）。

每张技能卡是一份可评审的 markdown 文件（YAML front matter + 正文），代码只负责
「按触发词评分 -> 排序 -> 取前 3」，不做任何业务判定：金额、限额、风控与审批
仍由 policy / engine 用代码完成，技能卡只提供领域上下文与操作边界。

约定：
- 技能卡目录固定为 <仓库根>/skills/*.skill.md，front matter 手写解析，不引入 PyYAML；
- 每张卡至少 3 条 examples，且每条 example 只命中本卡（用于回归自检）；
- match 支持传文本或 intent 字典（engine 传的是 intent），任何异常都不影响主流程。
"""
from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config

MAX_MATCHES = 3
_LIST_FIELDS = ("triggers", "tools", "examples", "slots", "clarify", "forbidden")
_TEXT_FIELDS = ("id", "name", "description")

# intent 名 -> 技能卡 id（engine 用 intent 字典调用 match，避免拿不到原句）
_INTENT_SKILLS = {
    "transfer": ["transfer"],
    "query_bills": ["bills"],
    "budget_plan": ["bills"],
    "wealth_query": ["wealth"],
    "wealth_purchase": ["wealth"],
    "card_query": ["cards"],
    "card_apply": ["cards"],
    "card_freeze": ["cards"],
    "card_unfreeze": ["cards"],
    "subscription_query": ["subscriptions"],
    "subscription_cancel": ["subscriptions"],
    "cross_scenario": ["cross"],
    "wealth_redeem": ["wealth"],
    "card_limit": ["cards"],
    "card_spend": ["cards"],
    "anomaly_scan": ["bills"],
    "annual_report": ["bills"],
    "scheduled_transfer": ["transfer"],
    "credit_repay": ["cards"],
    "query_balance": [],
    "query_transactions": [],
    "query_fx": [],
    "greeting": [],
    "unknown": [],
    "attack": [],
}


class SkillCard:
    """一张技能卡的只读数据视图。"""

    __slots__ = ("id", "name", "description", "triggers", "tools", "examples",
                 "slots", "clarify", "forbidden", "body", "path", "order")

    def __init__(self, order: int, path: Path) -> None:
        self.id = ""
        self.name = ""
        self.description = ""
        self.triggers: List[str] = []
        self.tools: List[str] = []
        self.examples: List[str] = []
        self.slots: List[str] = []
        self.clarify: List[str] = []
        self.forbidden: List[str] = []
        self.body = ""
        self.path = path
        self.order = order

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "triggers": list(self.triggers),
            "tools": list(self.tools),
            "examples": list(self.examples),
            "slots": list(self.slots),
            "clarify": list(self.clarify),
            "forbidden": list(self.forbidden),
        }


_LOCK = threading.RLock()
_CARDS: Optional[List[SkillCard]] = None
_HITS: Dict[str, int] = {}


def _skills_dir() -> Path:
    """技能卡目录：每次按当前配置计算，不缓存路径。"""
    root = Path(getattr(config, "ROOT_DIR", Path(__file__).resolve().parents[2]))
    return root / "skills"


def _split_list(value: str) -> List[str]:
    """逗号/顿号分隔的清单，去掉空项与引号。"""
    items: List[str] = []
    for part in re.split(r"[,，、]", value or ""):
        item = part.strip().strip('"').strip("'")
        if item:
            items.append(item)
    return items


def _parse_card(path: Path, order: int) -> Optional[SkillCard]:
    """手写解析 front matter；格式不合法时返回 None，不影响其它卡。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    meta: Dict[str, str] = {}
    closing = -1
    for index in range(1, len(lines)):
        line = lines[index].strip()
        if line == "---":
            closing = index
            break
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip().lower()] = value.strip()
    if closing < 0:
        return None
    card = SkillCard(order, path)
    for field in _TEXT_FIELDS:
        setattr(card, field, meta.get(field, ""))
    for field in _LIST_FIELDS:
        setattr(card, field, _split_list(meta.get(field, "")))
    if not card.id:
        card.id = path.name.split(".")[0]
    if not card.name:
        card.name = card.id
    card.body = "\n".join(lines[closing + 1:]).strip()
    return card


def _load_cards() -> List[SkillCard]:
    """从磁盘加载全部技能卡；目录缺失、单卡损坏都只是少一张卡，绝不抛异常。"""
    directory = _skills_dir()
    if not directory.is_dir():
        return []
    cards: List[SkillCard] = []
    try:
        paths = sorted(directory.glob("*.skill.md")) or sorted(directory.glob("*.md"))
    except OSError:
        return []
    for order, path in enumerate(paths):
        card = _parse_card(path, order)
        if card is not None:
            cards.append(card)
    return cards


def _ensure_loaded() -> List[SkillCard]:
    global _CARDS
    with _LOCK:
        if _CARDS is not None:
            return _CARDS
    loaded = _load_cards()
    with _LOCK:
        if _CARDS is None:
            _CARDS = loaded
        return _CARDS


def reload() -> Dict[str, Any]:
    """重新读盘（技能卡文件改动后调用），同时清空命中计数。"""
    global _CARDS
    with _LOCK:
        _CARDS = None
        _HITS.clear()
    cards = _ensure_loaded()
    return {"count": len(cards), "ids": [card.id for card in cards]}


def _normalize(text: str) -> str:
    """匹配前的归一化：小写、去掉所有空白，中文不受影响。"""
    return re.sub(r"\s+", "", str(text or "")).lower()


def _bump(ids: List[str]) -> None:
    if not ids:
        return
    with _LOCK:
        for card_id in ids:
            _HITS[card_id] = int(_HITS.get(card_id, 0)) + 1


def _match_text(text: str, cards: List[SkillCard]) -> List[str]:
    normalized = _normalize(text)
    if not normalized:
        return []
    scores: List[Any] = []
    for card in cards:
        score = 0
        for trigger in card.triggers:
            key = _normalize(trigger)
            if not key:
                continue
            if key in normalized:
                score += 3 if len(key) >= 2 else 1
        if score > 0:
            scores.append((score, card))
    if not scores:
        return []
    scores.sort(key=lambda item: (-item[0], item[1].order))
    top = scores[0][0]
    picked = [card for score, card in scores if score == top or score > top * 0.6]
    return [card.id for card in picked[:MAX_MATCHES]]


def _match_intent(intent: Dict[str, Any], cards: List[SkillCard]) -> List[str]:
    name = str(intent.get("name") or "").strip().lower()
    text = intent.get("text") or intent.get("utterance")
    if isinstance(text, str) and text.strip():
        return _match_text(text, cards)
    if name in _INTENT_SKILLS:
        known = {card.id for card in cards}
        return [card_id for card_id in _INTENT_SKILLS[name] if card_id in known]
    return []


def match(text: Any) -> List[str]:
    """匹配技能卡，返回 id 列表（最多 3 张，按分数降序）。任何异常都不外抛。"""
    try:
        cards = _ensure_loaded()
        if not cards:
            return []
        if isinstance(text, dict):
            ids = _match_intent(text, cards)
        else:
            ids = _match_text(str(text or ""), cards)
        _bump(ids)
        return ids
    except Exception:  # noqa: BLE001 - 技能路由失败不能影响主流程
        return []


def all() -> List[Dict[str, Any]]:
    """返回全部技能卡（字段扁平，便于 API 直接输出）。"""
    try:
        return [card.to_dict() for card in _ensure_loaded()]
    except Exception:  # noqa: BLE001
        return []


def get(card_id: str) -> Optional[Dict[str, Any]]:
    """按 id 取一张技能卡；不存在返回 None。"""
    target = str(card_id or "").strip()
    if not target:
        return None
    for card in _ensure_loaded():
        if card.id == target:
            return card.to_dict()
    return None


def summary(skill_ids: Optional[List[str]] = None) -> List[str]:
    """把命中的技能卡压缩成一行一条的提示（供 LLM system prompt 注入）。"""
    try:
        by_id = {card.id: card for card in _ensure_loaded()}
    except Exception:  # noqa: BLE001
        return []
    if skill_ids is None:
        chosen = list(by_id.values())[:MAX_MATCHES]
    else:
        chosen = [by_id[card_id] for card_id in skill_ids if card_id in by_id]
    lines: List[str] = []
    for card in chosen:
        line = "技能卡「%s」：触发词 %s；参考工具 %s" % (
            card.name,
            "/".join(card.triggers[:6]) or "无",
            "/".join(card.tools[:6]) or "无",
        )
        if card.forbidden:
            line += "；边界 %s" % "、".join(card.forbidden[:2])
        lines.append(line[:160])
    return lines


def status() -> Dict[str, Any]:
    """技能卡总览：条数 + 每张卡的元数据 + 本次进程内命中次数。"""
    try:
        cards = _ensure_loaded()
    except Exception:  # noqa: BLE001
        cards = []
    with _LOCK:
        hits = dict(_HITS)
    items: List[Dict[str, Any]] = []
    for card in cards:
        item = card.to_dict()
        item["hits"] = int(hits.get(card.id, 0))
        items.append(item)
    return {"count": len(items), "skills": items}


def count() -> int:
    return len(_ensure_loaded())
