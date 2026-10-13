"""本地 HTTP 服务：静态托管 frontend/，并对外提供一个实时扫描接口。

    GET /                 页面本身
    GET /api/ping         探活，前端用来判断服务在不在
    GET /api/scan         实时扫描一次（默认重新扫描，约 2–10 秒）
    GET /api/scan?fresh=0 直接返回上一次扫描结果，不重新扫描
    GET /api/scan?write=1 扫描完顺便刷新 frontend/data/ 下的快照文件

启动：

    python backend/server.py                 # 默认 http://127.0.0.1:8765
    python backend/server.py --port 9000
    python backend/server.py --open          # 启动后自动打开浏览器

端口被占用时会自动往后找可用的端口（最多试 10 个），不会直接报错退出。
"""

from __future__ import annotations

import argparse
import functools
import json
import socket
import sys
import threading
import urllib.request
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import cli as cli_module          # noqa: E402
import wlan_scanner               # noqa: E402

PROJECT_ROOT = _HERE.parent
FRONTEND_DIR = PROJECT_ROOT / "frontend"
DATA_DIR = FRONTEND_DIR / "data"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

# 扫描是独占的硬件操作，并发触发只会互相拖慢，这里串行化
_SCAN_LOCK = threading.Lock()
_LAST_SCAN: dict | None = None


def _get_scan(*, fresh: bool, write_snapshot: bool) -> dict:
    global _LAST_SCAN

    with _SCAN_LOCK:
        if not fresh and _LAST_SCAN is not None:
            return _LAST_SCAN

        payload = wlan_scanner.scan()
        if payload.get("ok"):
            _LAST_SCAN = payload
            if write_snapshot:
                try:
                    cli_module.write_snapshot(payload, DATA_DIR)
                except OSError as exc:
                    payload.setdefault("warnings", []).append(
                        f"快照写入失败：{exc}"
                    )
        return payload


class PlannerRequestHandler(SimpleHTTPRequestHandler):
    """静态文件 + /api/*。"""

    protocol_version = "HTTP/1.1"
    server_version = "ApPlanner/1.0"

    # ------------------------------------------------------------------
    # 响应工具
    # ------------------------------------------------------------------

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 允许直接用 file:// 打开页面时也能调到本服务
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def handle(self) -> None:
        """吞掉"客户端中途断开"这类错误。

        页面刷新、关标签、或用户等不及 /api/scan 直接走了，都会让连接在服务端
        写响应之前断掉（WinError 10053 / 10054）。这是浏览器的日常行为，不是
        故障，不该让控制台刷一堆 traceback 吓人。
        """
        try:
            super().handle()
        except (ConnectionError, TimeoutError):
            pass

    # ------------------------------------------------------------------
    # 路由
    # ------------------------------------------------------------------

    def do_GET(self) -> None:                       # noqa: N802  (stdlib 命名)
        route = urlparse(self.path)
        query = parse_qs(route.query)

        if route.path == "/api/ping":
            self._send_json({"ok": True})
            return

        if route.path == "/api/scan":
            fresh = query.get("fresh", ["1"])[0] not in ("0", "false", "no")
            write_snapshot = query.get("write", ["0"])[0] in ("1", "true", "yes")
            try:
                self._send_json(_get_scan(fresh=fresh, write_snapshot=write_snapshot))
            except Exception as exc:                # pragma: no cover - 兜底
                self._send_json(
                    {
                        "ok": False,
                        "scanned_at": "",
                        "interface": {"name": "", "state": "",
                                      "current_ssid": "", "current_bssid": ""},
                        "warnings": [],
                        "error": f"扫描时发生未预期的错误：{exc}",
                        "bss": [],
                    },
                    status=500,
                )
            return

        super().do_GET()

    def do_OPTIONS(self) -> None:                   # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ------------------------------------------------------------------
    # 本地工具不需要缓存，免得改完代码刷新看不到效果
    # ------------------------------------------------------------------

    def end_headers(self) -> None:
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))


class PlannerServer(ThreadingHTTPServer):
    """兜底：连接类错误不打印 traceback（见 PlannerRequestHandler.handle）。"""

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def build_server(host: str, port: int) -> ThreadingHTTPServer:
    handler = functools.partial(PlannerRequestHandler, directory=str(FRONTEND_DIR))
    return PlannerServer((host, port), handler)


def _ping_ourselves(host: str, port: int) -> bool:
    """这个端口上已经有本服务在跑吗？（双击两次 start.bat 的常见情况）"""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/ping", timeout=0.8) as resp:
            return resp.status == 200 and json.loads(resp.read().decode("utf-8")).get("ok") is True
    except Exception:
        return False


def _in_use(host: str, port: int) -> bool:
    """有没有别的程序在监听这个端口。

    不能只看 bind 成不成功：Windows 上 SO_REUSEADDR 允许"抢注"已被占用的端口，
    bind 会成功、但两个进程会互相抢连接。所以先主动连一下，连得上就别碰。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.3)
        return probe.connect_ex((host, port)) == 0


def _try_bind(host: str, port: int) -> ThreadingHTTPServer | None:
    try:
        return build_server(host, port)
    except OSError:
        return None


def _pick_server(host: str, port: int,
                 tries: int = 10) -> tuple[ThreadingHTTPServer, int] | None:
    """默认端口被占了就往后找 —— 便携版是双击启动的，这里甩一个 traceback
    给用户看太糟糕。两个判断都要：_in_use 防止抢注别人的端口，_try_bind 才是
    最终说了算的那个（_in_use 可能因为监听方 backlog 满而误判）。
    返回 (server, 实际端口)。"""
    for candidate in range(port, port + tries + 1):
        if _in_use(host, candidate):
            continue
        server = _try_bind(host, candidate)
        if server is None:
            continue
        if candidate != port:
            print(f"端口 {port} 已被其它程序占用，改用 {candidate}。")
        return server, candidate
    print(f"端口 {port} 及其后 {tries} 个端口都用不了，请用 --port 指定其它端口。",
          file=sys.stderr)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AP 信道规划助手本地服务")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    parser.add_argument("--preload", action="store_true",
                        help="启动时先扫描一次，让页面首屏就是实时数据")
    args = parser.parse_args(argv)

    if not FRONTEND_DIR.is_dir():
        print(f"找不到前端目录：{FRONTEND_DIR}", file=sys.stderr)
        return 1

    # 已经开着一个就不要再起第二个：双击两次 start.bat 时直接把浏览器指过去
    if _ping_ourselves(args.host, args.port):
        url = f"http://{args.host}:{args.port}/"
        print(f"服务已经在运行：{url}")
        if args.open:
            webbrowser.open(url)
        return 0

    server = _pick_server(args.host, args.port)
    if server is None:
        return 1
    server, port = server
    args.port = port

    url = f"http://{args.host}:{port}/"

    if args.preload:
        print("正在预扫描…")
        payload = _get_scan(fresh=True, write_snapshot=True)
        if payload.get("ok"):
            print(f"预扫描完成，发现 {len(payload['bss'])} 条 BSS。")
        else:
            print(f"预扫描失败：{payload.get('error')}")

    print(f"AP 信道规划助手已启动：{url}")
    print("按 Ctrl+C 停止。")

    if args.open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
