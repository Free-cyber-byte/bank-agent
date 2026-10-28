#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ZeroStep 测试桩：OpenAI 兼容的 /chat/completions 假服务。

用途：在没有真实 API Key、也不想花真钱的前提下，确定性地验证
  * 请求体是否符合预期（thinking 关闭、max_tokens、system 前缀稳定、skill_hint 位置）
  * 各类失败路径（401/402/404/429/5xx/超时/空 content/坏 JSON）
  * 客户端重试、熔断、回退与用量统计

仅用于测试与本地演示，绝不参与生产。零第三方依赖，只用标准库。

用法：
  python scripts/llm_stub_server.py                 # 默认 127.0.0.1:8320
  set LLM_STUB_MODE=429_once & python scripts/llm_stub_server.py
  set LLM_STUB_PORT=8321 & set LLM_STUB_MODE=slow & set LLM_STUB_DELAY=3 & python scripts/llm_stub_server.py

环境变量：
  LLM_STUB_PORT   监听端口，默认 8320
  LLM_STUB_HOST   监听地址，默认 127.0.0.1
  LLM_STUB_MODE   行为模式，默认 ok
                  ok | 401 | 402 | 404 | 429_once | 429_always | 500_always
                  | empty_once | badjson_once | slow
  LLM_STUB_DELAY  slow 模式延迟秒数，默认 3
  LLM_STUB_USAGE_CACHE_HIT   usage 里命中缓存的 token，默认 1024

管理接口（测试用）：
  GET  /health          -> {"ok": true, "mode": "ok", "calls": 0}
  GET  /_stats          -> {"ok": true, "mode": "...", "calls": N, "payloads": [...]}
  POST /_reset          -> 清零计数与记录
  POST /_mode           -> body {"mode": "401"} 运行时切换模式
"""

import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODES = (
    "ok", "401", "402", "404", "429_once", "429_always",
    "500_always", "empty_once", "badjson_once", "slow",
)

_lock = threading.Lock()
_state = {"calls": 0, "payloads": [], "mode": (os.environ.get("LLM_STUB_MODE") or "ok").strip().lower()}


def _mode() -> str:
    with _lock:
        return _state["mode"]


def _set_mode(mode: str) -> bool:
    mode = (mode or "").strip().lower()
    if mode not in MODES:
        return False
    with _lock:
        _state["mode"] = mode
        _state["calls"] = 0
    return True


def _next_call() -> int:
    with _lock:
        _state["calls"] += 1
        return _state["calls"]


def _record(payload):
    with _lock:
        if len(_state["payloads"]) < 50:
            _state["payloads"].append(payload)


def _first_amount(text: str) -> float:
    for token in re.findall(r"\d+(?:\.\d+)?", text or ""):
        try:
            value = float(token)
        except ValueError:
            continue
        if value > 0:
            return value
    return 0.0


def _intent_for(text: str):
    """桩的意图规则：只按关键词返回，够覆盖 v0.4.0 六类新能力与既有四类。"""
    text = text or ""
    # v0.4.0 新能力（放在旧规则之前，避免「年度账单」被账单规则抢走）
    if "异常交易" in text or "可疑交易" in text or "异常消费" in text or "风险交易" in text:
        return {"intent": "anomaly_scan", "confidence": 0.92, "reason": "桩：识别为异常交易扫描"}
    if "年度" in text or "全年" in text:
        return {"intent": "annual_report", "confidence": 0.92, "year": 2026,
                "reason": "桩：识别为年度账单报告"}
    if "预约" in text or "定时" in text or (("明天" in text or "后天" in text or "天后" in text) and "转" in text):
        op = "create"
        if any(kw in text for kw in ("查", "看", "有哪些", "列表")):
            op = "list"
        elif any(kw in text for kw in ("取消", "删除", "撤销")):
            op = "cancel"
        payload = {"intent": "scheduled_transfer", "confidence": 0.9, "op": op,
                   "reason": "桩：识别为预约转账"}
        if op == "create":
            payload["amount_yuan"] = _first_amount(text)
            if "妈妈" in text:
                payload["payee_name"] = "妈妈"
            payload["schedule_hint"] = "明天上午9点"
        return payload
    if "赎回" in text or "取出" in text or "卖掉" in text:
        return {"intent": "wealth_redeem", "confidence": 0.9, "amount_yuan": _first_amount(text),
                "product_hint": "稳利宝" if "稳利宝" in text else None,
                "reason": "桩：识别为理财赎回"}
    if "限额" in text or "额度" in text:
        return {"intent": "card_limit", "confidence": 0.9, "daily_limit_yuan": _first_amount(text),
                "card_hint": "credit" if "信用" in text else None,
                "reason": "桩：识别为额度调整"}
    if "消费" in text or "刷卡" in text:
        merchant = None
        for name in ("京东", "淘宝", "美团", "星巴克", "超市"):
            if name in text:
                merchant = name
                break
        return {"intent": "card_spend", "confidence": 0.9, "amount_yuan": _first_amount(text),
                "card_hint": "debit" if ("储蓄" in text or "借记" in text) else ("credit" if "信用" in text else None),
                "merchant": merchant, "reason": "桩：识别为刷卡消费"}
    if "汇率" in text or "换汇" in text or "兑" in text:
        return {
            "intent": "query_fx", "confidence": 0.92,
            "from_currency": "USD", "to_currency": "CNY",
            "exchange_hint": "查汇率", "reason": "桩：识别为汇率查询",
        }
    if "转" in text and ("妈妈" in text or "爸爸" in text or "房东" in text):
        amount = 8
        for token in ("8", "8.00", "八"):
            if token in text:
                amount = 8
                break
        return {
            "intent": "transfer", "confidence": 0.95, "amount_yuan": amount,
            "payee_name": "妈妈" if "妈妈" in text else ("爸爸" if "爸爸" in text else "房东"),
            "from_kind": "checking", "reason": "桩：识别为转账",
        }
    if "账单" in text or "支出" in text or "花" in text:
        return {
            "intent": "query_bills", "confidence": 0.93, "months": 1,
            "reason": "桩：识别为账单查询",
        }
    return {"intent": "greeting", "confidence": 0.8, "reason": "桩：默认问候"}


def _ok_response(model: str, content: str, hit_tokens: int):
    return {
        "id": "chatcmpl-stub-%d" % int(time.time() * 1000),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model or "deepseek-flash",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": {
            "prompt_tokens": 1180,
            "completion_tokens": 96,
            "total_tokens": 1276,
            "prompt_cache_hit_tokens": hit_tokens,
            "prompt_cache_miss_tokens": 1180 - hit_tokens,
        },
    }


def _error_body(code: int, message: str):
    return {"error": {"message": message, "type": "stub_error", "code": code, "param": None}}


class Handler(BaseHTTPRequestHandler):
    server_version = "ZeroStepLLMStub/1.0"

    def log_message(self, fmt, *args):  # 静默，避免污染测试输出
        pass

    def _send(self, status: int, body):
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path in ("/health", "/_health"):
            with _lock:
                self._send(200, {"ok": True, "mode": _state["mode"], "calls": _state["calls"]})
            return
        if self.path == "/_stats":
            with _lock:
                self._send(200, {
                    "ok": True,
                    "mode": _state["mode"],
                    "calls": _state["calls"],
                    "payloads": list(_state["payloads"]),
                })
            return
        self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            body = {}

        if self.path == "/_reset":
            with _lock:
                _state["calls"] = 0
                _state["payloads"] = []
            self._send(200, {"ok": True})
            return

        if self.path == "/_mode":
            ok = _set_mode(str(body.get("mode") or ""))
            self._send(200 if ok else 400, {"ok": ok, "mode": _mode()})
            return

        if self.path not in ("/chat/completions", "/v1/chat/completions"):
            self._send(404, {"ok": False, "error": "not found"})
            return

        n = _next_call()
        _record(body)
        mode = _mode()

        if mode == "slow":
            try:
                delay = float(os.environ.get("LLM_STUB_DELAY", "3"))
            except Exception:
                delay = 3.0
            time.sleep(max(0.0, delay))
            mode = "ok"

        if mode == "401":
            self._send(401, _error_body(401, "Authentication Fails, Your api key is invalid"))
            return
        if mode == "402":
            self._send(402, _error_body(402, "Insufficient Balance"))
            return
        if mode == "404":
            self._send(404, _error_body(404, "Model Not Exist"))
            return
        if mode == "429_always" or (mode == "429_once" and n == 1):
            self._send(429, _error_body(429, "Rate Limit Reached"))
            return
        if mode == "500_always":
            self._send(500, _error_body(500, "Server Internal Error"))
            return

        messages = body.get("messages") or []
        user_text = ""
        if len(messages) > 1:
            content = (messages[1] or {}).get("content") or ""
            try:
                parsed = json.loads(content)
                user_text = str(parsed.get("utterance") or "")
            except Exception:
                user_text = str(content)
        model = body.get("model") or "deepseek-flash"

        try:
            hit_tokens = int(os.environ.get("LLM_STUB_USAGE_CACHE_HIT", "1024"))
        except Exception:
            hit_tokens = 1024

        if mode == "empty_once" and n == 1:
            self._send(200, _ok_response(model, "", hit_tokens))
            return
        if mode == "badjson_once" and n == 1:
            self._send(200, _ok_response(model, "这不是 JSON", hit_tokens))
            return

        intent = _intent_for(user_text)
        self._send(200, _ok_response(model, json.dumps(intent, ensure_ascii=False), hit_tokens))


def main() -> int:
    host = os.environ.get("LLM_STUB_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("LLM_STUB_PORT", "8320"))
    except Exception:
        port = 8320
    server = ThreadingHTTPServer((host, port), Handler)
    print("ZeroStep LLM stub 已启动: http://%s:%d  (mode=%s)" % (host, port, _mode()), flush=True)
    print("  管理接口: GET /_stats  POST /_reset  POST /_mode  GET /health", flush=True)
    print("  Ctrl+C 停止", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
