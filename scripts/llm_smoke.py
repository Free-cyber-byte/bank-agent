#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ZeroStep 模型接入冒烟：一条命令确认「Key 配好没有、能不能通、花多少钱」。

与 scripts/smoke_llm_fallback.py 的区别：
  * smoke_llm_fallback.py —— 无 Key 时的回退验证（不需要网络与 Key）
  * llm_smoke.py          —— 有 Key 时的真实连通验证（会真的调用一次模型，产生极小费用）

用法：
  python scripts/llm_smoke.py            # 读 .env / 环境变量
  set DEEPSEEK_API_KEY=sk-xxx & python scripts/llm_smoke.py

退出码：0 通过或跳过（无 Key）；1 失败。
"""

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def load_env():
    """极简 .env 读取：只补未设置的变量，不覆盖已有环境变量。"""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception as exc:  # 读不到 .env 不算失败
        print("读取 .env 失败（忽略）:", exc)


def main() -> int:
    load_env()
    from backend.app import config, llm  # noqa: E402  必须在环境变量就绪后导入

    print("=" * 64)
    print("ZeroStep 模型接入冒烟")
    print("=" * 64)
    print("厂商        :", llm.provider_name())
    print("模型        :", config.LLM_MODEL)
    print("接口域名    :", config.LLM_BASE_URL)
    print("超时/预算   :", "%ss / %ss" % (getattr(config, "LLM_TIMEOUT", 8), getattr(config, "LLM_BUDGET", 10)))
    print("思考模式    :", getattr(config, "LLM_THINKING", "disabled"))
    print("最大输出    :", getattr(config, "LLM_MAX_TOKENS", 800))
    print("Key 状态    :", ("已配置 " + llm.key_masked()) if config.LLM_ENABLED else "未配置")

    if not config.LLM_ENABLED:
        print("")
        print("未配置 DEEPSEEK_API_KEY：跳过真实调用。")
        print("规则模式（无模型、零依赖）仍可完整演示：python backend/server.py")
        print("要测模型：在 bank-agent/.env 里填 DEEPSEEK_API_KEY=sk-... 后重跑本脚本。")
        return 0

    print("")
    print("正在调用模型……")
    started = time.time()
    result = None
    if hasattr(llm, "test_connection"):
        try:
            result = llm.test_connection()
        except Exception as exc:  # 防御：test_connection 承诺不抛
            print("test_connection 抛异常（应永不发生）:", type(exc).__name__, exc)
            result = None
    if not isinstance(result, dict):
        try:
            result = llm.call_model("查一下这个月支出最多的三类", skills_hint=[])
        except Exception as exc:
            print("调用失败:", type(exc).__name__, exc)
            return 1
        result = {"ok": True, "result": result}

    elapsed_ms = int((time.time() - started) * 1000)
    ok = bool(result.get("ok"))
    print("结果        :", "成功" if ok else "失败")
    print("耗时        :", result.get("latency_ms", elapsed_ms), "ms（脚本侧实测", elapsed_ms, "ms）")
    if not ok:
        print("错误码      :", result.get("error_code") or result.get("error"))
        print("提示        :", result.get("error_hint") or "")
    else:
        usage = result.get("usage") or {}
        if usage:
            print("tokens      : 输入", usage.get("prompt_tokens"), "/ 输出", usage.get("completion_tokens"),
                  "/ 合计", usage.get("total_tokens"))
            print("缓存命中    : 命中", usage.get("prompt_cache_hit_tokens"),
                  "/ 未命中", usage.get("prompt_cache_miss_tokens"))
        cost = result.get("cost_yuan")
        if cost is not None:
            print("本次费用    : 约 ¥%s" % cost)
        parsed = result.get("result")
        if parsed is not None:
            print("解析结果    :", json.dumps(parsed, ensure_ascii=False)[:400])

    status = llm.status()
    print("-" * 64)
    print("实际解析方式:", status.get("effective"), "| 模式:", status.get("mode"),
          "| 熔断:", "是" if status.get("degraded") else "否")
    stats = status.get("stats") or {}
    if stats:
        print("累计        : 调用", stats.get("calls"), "成功", stats.get("ok"), "失败", stats.get("fail"),
              "重试", stats.get("retries"), "| 累计约 ¥%s" % stats.get("total_cost_yuan"))
    last_hint = status.get("last_error_hint")
    if last_hint:
        print("最近错误提示:", last_hint)
    print("=" * 64)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
