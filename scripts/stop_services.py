#!/usr/bin/env python3
"""停止 ZeroStep 本地服务：按端口找到监听进程并结束（默认 8000/8300/8301）。

用法：
    python scripts/stop_services.py              # 停 8000/8300/8301
    python scripts/stop_services.py --ports 8010 # 只停指定端口
    python scripts/stop_services.py --dry-run    # 只打印，不真的杀

只依赖标准库，Windows 用 netstat + taskkill，macOS/Linux 用 lsof + kill。
"""

import argparse
import os
import re
import socket
import subprocess
import sys
import time

DEFAULT_PORTS = (8000, 8300, 8301)


def _listening_pids_windows():
    """返回 [(port, pid)]；读不到端口表时返回 None（区别于「确实没有服务在跑」）。"""
    try:
        res = subprocess.run(["netstat", "-ano"], capture_output=True,
                             text=True, timeout=20, errors="replace")
        out = res.stdout or ""
    except (OSError, subprocess.SubprocessError) as exc:
        print("读取端口占用失败：%s" % exc)
        return None
    if not out.strip():
        return None
    found = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or not parts[0].upper().startswith("TCP"):
            continue
        if parts[3].upper() != "LISTENING":
            continue
        local = parts[1]
        pid_text = parts[4]
        port_text = local.rsplit(":", 1)[-1]
        if not port_text.isdigit() or not pid_text.isdigit():
            continue
        found.append((int(port_text), int(pid_text)))
    return found


def _listening_pids_posix(port):
    for cmd in (["lsof", "-ti", "tcp:%d" % port], ["fuser", "-n", "tcp", str(port)]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=20,
                                 errors="replace").stdout
        except (OSError, subprocess.SubprocessError):
            continue
        pids = [int(x) for x in re.findall(r"\d+", out)]
        if pids:
            return pids
    return []


def _port_open(port):
    """端口还有人接吗（判据用真实连接，不依赖被沙箱限制的 tasklist）。"""
    for family, addr in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(0.4)
        try:
            if sock.connect_ex((addr, port)) == 0:
                return True
        except OSError:
            pass
        finally:
            sock.close()
    return False


def _terminate_windows(pid):
    """taskkill 被拒时的兜底：直接调 TerminateProcess。"""
    try:
        import ctypes
        PROCESS_TERMINATE = 0x0001
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if not handle:
            return False
        try:
            return bool(kernel32.TerminateProcess(handle, 1))
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


def _kill(pid, port):
    """结束进程并确认端口真的空出来了。返回 True 表示这次确实停掉了。"""
    try:
        if os.name == "nt":
            res = subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                                 capture_output=True, timeout=20)
            rc = res.returncode
            if rc != 0:
                msg = (res.stderr or res.stdout or b"").decode("utf-8", "replace").strip()
                print("  taskkill 返回 %d：%s，改用更强的结束方式" % (rc, msg[:120] or "无输出"))
                if not _terminate_windows(pid):
                    print("  仍然结束不了，请在任务管理器里结束 PID %d" % pid)
                    return not _port_open(port)
        else:
            os.kill(pid, 15)
    except (OSError, subprocess.SubprocessError) as exc:
        print("  结束进程 %d 失败：%s" % (pid, exc))
        return False
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if not _port_open(port):
            return True
        time.sleep(0.25)
    return False


def main(argv=None):
    parser = argparse.ArgumentParser(description="停止 ZeroStep 本地服务")
    parser.add_argument("--ports", nargs="+", type=int, default=list(DEFAULT_PORTS),
                        help="要停止的端口，默认 8000 8300 8301")
    parser.add_argument("--dry-run", action="store_true", help="只检查，不结束进程")
    args = parser.parse_args(argv)

    if os.name == "nt":
        listening = _listening_pids_windows()
        if listening is None:
            print("读不到端口占用表（可能被安全策略或权限限制）。请手动处理：")
            print("    netstat -ano | findstr :8000")
            print("    taskkill /PID <上一步最后一列的数字> /F")
            print("或打开任务管理器 → 详细信息 → 按 PID 结束。")
            return 2
    else:
        listening = []
        for port in args.ports:
            listening.extend((port, pid) for pid in _listening_pids_posix(port))

    print("ZeroStep 零步银行 · 停止本地服务")
    print("-" * 44)
    killed = 0
    failed = 0
    for port in args.ports:
        pids = sorted({pid for p, pid in listening if p == port and pid != os.getpid()})
        if not pids:
            print("端口 %d：没有服务在跑" % port)
            continue
        for pid in pids:
            if args.dry_run:
                print("端口 %d：进程 %d 占用中（--dry-run，未结束）" % (port, pid))
                continue
            print("端口 %d：结束进程 %d ..." % (port, pid))
            if _kill(pid, port):
                print("  已结束")
                killed += 1
            else:
                print("  没结束掉（可能权限不足），请在任务管理器里结束 PID %d" % pid)
                failed += 1
    print("-" * 44)
    if args.dry_run:
        print("检查完毕（未做任何修改）")
    elif failed:
        print("停止 %d 个，失败 %d 个。" % (killed, failed))
    elif killed:
        print("已停止 %d 个服务；随时可用 start.bat 重新启动。" % killed)
    else:
        print("没有需要停止的服务。")
    return 0


if __name__ == "__main__":
    sys.exit(main())