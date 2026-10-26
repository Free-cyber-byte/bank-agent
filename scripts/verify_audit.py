"""校验审计哈希链完整性。

用法：
    python scripts/verify_audit.py
    python scripts/verify_audit.py --db backend/data/bank.db --limit 200
退出码：0 链完整，1 链被破坏。
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app import audit, config, db  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description="ZeroStep 审计链校验")
    parser.add_argument("--db", default="", help="数据库路径，默认 config.DB_PATH")
    parser.add_argument("--limit", type=int, default=200, help="最多展示多少条事件")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args(argv)
    path = args.db or str(config.DB_PATH)
    if not Path(path).exists():
        print("数据库不存在：" + path)
        return 2
    conn = db.connect(path)
    chain = audit.verify(conn)
    items = audit.entries(conn, limit=args.limit)
    if args.json:
        print(json.dumps({"chain": chain, "items": items}, ensure_ascii=False, indent=2))
    else:
        print("审计链：%d 条事件，valid=%s" % (chain.get("checked", 0), chain.get("valid")))
        if not chain.get("valid"):
            print("破坏位置：seq=%s 原因=%s" % (chain.get("broken_at"), chain.get("reason")))
        print("-" * 90)
        print("%-5s %-19s %-22s %-14s %-6s %-10s" % ("seq", "时间", "事件", "工具", "风险", "哈希前 8 位"))
        for item in items:
            ts = str(item.get("ts", ""))[:19].replace("T", " ")
            print("%-5s %-19s %-22s %-14s %-6s %-10s" % (
                item.get("seq"), ts, str(item.get("event_type", ""))[:22], str(item.get("tool", ""))[:14],
                item.get("risk_level", ""), str(item.get("hash", ""))[:8]))
    conn.close()
    return 0 if chain.get("valid") else 1


if __name__ == "__main__":
    sys.exit(main())
