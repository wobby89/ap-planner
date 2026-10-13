#!/usr/bin/env python3
"""server.py 的端口策略测试（开发用，不依赖打包产物）。

盯的是三件"双击启动"时必须成立的事：

  1. 端口上有本服务在跑时，再启动一个实例 —— 应该认出"已经在运行"并直接退出，
     而不是起第二个服务或报端口占用；
  2. 端口被别的程序占着 —— 应该自动改用下一个可用端口，并且打印的 URL 是
     实际端口（不是请求的那个）；
  3. 端口被占着时，不能"抢注"别人的端口（Windows 上 SO_REUSEADDR 允许这么做，
     抢了就会两个进程互相抢连接）。

    python tests/server_ports_check.py              默认用 8795 起的三个连续端口
    python tests/server_ports_check.py --base 9500
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "backend" / "server.py"

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'通过' if ok else '失败'}] {name}{' — ' + detail if detail else ''}")


def start(port: int) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(SERVER), "--port", str(port)],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        # 不加这个，进程被 kill 时 stdout 缓冲区里的输出会丢，测试看不到提示文本
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )


def kill(proc: subprocess.Popen) -> bytes:
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    try:
        out, _ = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:              # pragma: no cover
        proc.kill()
        out = b""
    return out


def wait_exit(proc: subprocess.Popen, timeout: float = 30) -> bytes:
    """等进程自己退出再收输出。

    这一步不能直接杀：正常退出的分支（比如"端口都用不了"）输出很短，抢在它
    print 之前 taskkill 就会把输出丢掉，测试会误报。
    """
    try:
        out, _ = proc.communicate(timeout=timeout)
        return out
    except subprocess.TimeoutExpired:              # pragma: no cover
        return kill(proc)


def ping(port: int, timeout: float = 25) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=2):
                return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.3)
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="server.py 端口策略测试")
    parser.add_argument("--base", type=int, default=8795, help="起始端口（会用到 +1 / +2）")
    args = parser.parse_args(argv)

    base = args.base
    if not SERVER.is_file():
        print(f"找不到 {SERVER}")
        return 2

    # ---------------------------------------------------------------- 1) 重复启动
    print("\n一、端口上已有本服务时再启动一个")
    first = start(base)
    try:
        record("第一个实例起来了", ping(base), f"http://127.0.0.1:{base}/")
        second = start(base)
        out = kill(second)
        text = out.decode("utf-8", "replace")
        record("第二个实例认出已在运行并退出", "已经在运行" in text and second.returncode == 0,
               f"退出码 {second.returncode}")
        record("第二个实例没有打印端口冲突错误",
               "占用" not in text and "Traceback" not in text)
    finally:
        kill(first)

    # ---------------------------------------------------------------- 2) 端口被占
    print("\n二、端口被别的程序占用")
    foreign = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    foreign.bind(("127.0.0.1", base + 1))
    foreign.listen(1)
    print(f"  已用普通 socket 占住 {base + 1}")
    third = start(base + 1)
    try:
        served = ping(base + 2, timeout=25)
        out = kill(third)
        text = out.decode("utf-8", "replace")
        record("自动改用下一个可用端口", served, f"http://127.0.0.1:{base + 2}/")
        record("提示里说明了端口被占用并给出实际端口",
               f"改用 {base + 2}" in text and f"127.0.0.1:{base + 2}" in text,
               text.strip().splitlines()[0][:80] if text.strip() else "（无输出）")
        record("没有抢注别人占用的端口", "已经在运行" not in text)
    finally:
        kill(third)
        foreign.close()

    # ---------------------------------------------------------------- 3) 全是占用
    print("\n三、连续 11 个端口都被占用")
    hold: list[socket.socket] = []
    for offset in range(11):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", base + 20 + offset))
        s.listen(1)
        hold.append(s)
    try:
        busy = start(base + 20)
        text = wait_exit(busy).decode("utf-8", "replace")
        record("给出明确提示而不是 traceback",
               busy.returncode == 1 and "都用不了" in text and "Traceback" not in text,
               f"退出码 {busy.returncode} · 输出 {text.strip()[:70]!r}")
    finally:
        for s in hold:
            s.close()

    failed = [r for r in results if not r[1]]
    print(f"\n{'=' * 60}")
    if failed:
        print(f"失败 {len(failed)} / {len(results)} 项")
        return 1
    print(f"全部通过（{len(results)} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
