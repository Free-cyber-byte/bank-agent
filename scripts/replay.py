"""命令行演示器：无需浏览器，直接和 ZeroStep 对话。

用法：
    python scripts/replay.py                 交互模式（输入 quit 退出，audit 查看审计链，reset 重置）
    python scripts/replay.py --script scripts/demo_flow.json   按脚本自动演示
    python scripts/replay.py --db backend/data/replay.db --approve ask
"""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app import audit, config, db, engine, totp  # noqa: E402


def fresh_conn(db_path):
    for suffix in ("", "-wal", "-shm"):
        p = Path(db_path + suffix)
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass
    config.DB_PATH = db_path
    conn = db.connect()
    db.init_schema(conn)
    db.seed(conn)
    return conn


def show(result):
    intent = (result.get("intent") or {}).get("name")
    steps = [(s.get("tool"), s.get("title")) for s in (result.get("plan") or {}).get("steps", [])]
    print("")
    print("意图：%s" % intent)
    if steps:
        print("计划：" + " → ".join("%s(%s)" % (t, title) for t, title in steps))
    print("状态：%s" % result.get("status"))
    blocked = result.get("blocked")
    if blocked:
        print("拦截：%s %s" % (blocked.get("code"), blocked.get("message", "")))
    print("回复：" + str(result.get("reply", "")))
    undo = result.get("undo")
    if undo:
        print("可撤销：action_id=%s（60 秒内可调用 /api/undo）" % undo.get("action_id"))


def approve(conn, session_id, approval, uid):
    print("")
    print("需要审批：%s / %s" % (approval.get("risk_level"), (approval.get("challenge") or {}).get("type")))
    print("标题：%s" % approval.get("title"))
    print("金额：%s 元" % ((approval.get("amount_cents") or 0) / 100.0))
    print("理由：%s" % "；".join(approval.get("reasons") or []))
    result = engine.resume(conn, session_id, approval.get("approval_id"), "approve", totp.current_code(uid))
    show(result)
    return result


def handle(conn, session_id, text, uid):
    result = engine.run_turn(conn, session_id, text)
    show(result)
    if result.get("status") == "awaiting_approval":
        approve(conn, session_id, result.get("approval") or {}, uid)


def run_script(conn, session_id, script_path, uid):
    data = json.loads(Path(script_path).read_text(encoding="utf-8"))
    turns = data.get("turns") or data
    for item in turns:
        text = item if isinstance(item, str) else item.get("text", "")
        if not text:
            continue
        print("")
        print("用户：" + text)
        handle(conn, session_id, text, uid)
    chain = audit.verify(conn)
    print("")
    print("审计链：%d 条事件，valid=%s" % (chain.get("checked", 0), chain.get("valid")))


def main(argv=None):
    parser = argparse.ArgumentParser(description="ZeroStep 命令行演示器")
    parser.add_argument("--user", default="u001")
    parser.add_argument("--db", default=str(ROOT / "backend" / "data" / "replay.db"))
    parser.add_argument("--script", default="", help="按 JSON 脚本自动演示")
    args = parser.parse_args(argv)
    conn = fresh_conn(args.db)
    session = engine.create_session(conn, args.user)
    session_id = session["session_id"]
    print("ZeroStep 演示已就绪（用户 %s，会话 %s）" % (args.user, session_id))
    print("可以试试：看看我这个月的账单 / 给妈妈转 800 元 / 发工资了，3000 存到储蓄卡，2000 买稳利宝，1000 还信用卡")
    if args.script:
        run_script(conn, session_id, args.script, args.user)
        conn.close()
        return 0
    while True:
        try:
            text = input("用户> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            break
        if not text:
            continue
        if text in ("quit", "exit", "退出"):
            break
        if text == "audit":
            chain = audit.verify(conn)
            print("审计链：%d 条事件，valid=%s" % (chain.get("checked", 0), chain.get("valid")))
            for item in audit.entries(conn, session_id, 10):
                print("  %s %s %s %s" % (item.get("seq"), item.get("event_type"), item.get("tool"), str(item.get("hash"))[:8]))
            continue
        if text == "reset":
            conn.close()
            conn = fresh_conn(args.db)
            session = engine.create_session(conn, args.user)
            session_id = session["session_id"]
            print("演示数据已重置。")
            continue
        handle(conn, session_id, text, args.user)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
