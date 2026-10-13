#!/usr/bin/env python3
"""便携版冒烟测试：真的按用户的方式跑一遍打包好的东西。

做三件事（默认检查 dist/ap-planner-portable 文件夹）：

  1. 用包内的 runtime\\python.exe 跑一次扫描，确认 ctypes 能加载 wlanapi.dll；
  2. 启动 start.bat（临时副本，去掉 --open 免得弹浏览器窗口），检查控制台输出
     编码正常、没有假报错；
  3. 请求 /、/api/ping、/api/scan?fresh=0 和几个静态资源，确认服务真的在提供
     页面和数据。

另外两条是针对"发给别人之后才暴露"的问题：

  * 包里的前端数据必须是**中性占位**（0 条 BSS）—— 曾经把打包机的扫描结果
    打进包，别人打开页面看到一堆"根本不在现场"的 AP，反而以为扫描成功了；
  * --diagnose 必须能跑出报告 —— 换电脑排查就靠它。

全程不碰系统 Python。加 --from-zip 则先解压 dist 下的 zip 再检查 —— 那才是
真正会发给别人的东西，能顺带验出"zip 里少了文件"这类问题。

    python tools/check_portable.py                    检查文件夹（默认端口 8765）
    python tools/check_portable.py --port 8899
    python tools/check_portable.py --from-zip         解压 zip 到临时目录再检查
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = ROOT / "dist"
PKG_NAME = "ap-planner-portable"
PKG_DIR = DIST_DIR / PKG_NAME
ZIP_PATH = DIST_DIR / (PKG_NAME + ".zip")


def log(tag: str, text: str = "") -> None:
    print(f"[{tag}] {text}".rstrip())


def run(cmd: list[str], cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, timeout=timeout)


def wait_http(url: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.4)
    return False


def get(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


# ---------------------------------------------------------------- 检查项
def check_runtime(pkg: Path) -> bool:
    py = pkg / "runtime" / "python.exe"
    if not py.is_file():
        log("失败", f"找不到包内运行时：{py}")
        return False

    probe = (
        "import sys, ctypes;"
        "ctypes.WinDLL('wlanapi.dll');"
        "print(sys.version.split()[0]);"
        "print(sys.prefix)"
    )
    proc = run([str(py), "-c", probe], cwd=pkg)
    if proc.returncode != 0:
        log("失败", proc.stderr.decode("utf-8", "replace").strip()[:300])
        return False

    out = proc.stdout.decode("utf-8", "replace").split("\n")
    version, prefix = out[0].strip(), out[1].strip()
    log("运行时", f"Python {version} · wlanapi.dll 可加载")
    log("", f"prefix {prefix}")
    if PKG_NAME not in prefix or "site-packages" in prefix:
        log("失败", "运行时前缀不在包内，可能用到了系统 Python")
        return False
    return True


def check_scan(pkg: Path) -> bool:
    """用包内运行时真扫一次，确认扫描链路在本包里也能跑通。"""
    cli = pkg / "app" / "backend" / "cli.py"
    log("扫描", "用包内运行时扫描一次真实 Wi-Fi …")
    proc = run([str(pkg / "runtime" / "python.exe"), str(cli), "--json-only"], cwd=pkg)
    if proc.returncode != 0:
        log("失败", proc.stderr.decode("utf-8", "replace").strip()[:300])
        return False
    try:
        payload = json.loads(proc.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        log("失败", f"扫描输出不是合法 JSON：{exc}")
        return False
    if not payload.get("ok"):
        log("失败", f"扫描返回失败：{payload.get('error')}")
        return False
    log("", f"{len(payload['bss'])} 条 BSS · 接口 {payload['interface']['name']}")
    return True


def check_snapshot_neutral(pkg: Path) -> bool:
    """包里的前端数据必须是中性占位，不能夹带打包机的扫描结果。

    这件事真切踩过：把开发机的 scan.js 打进包，别人打开页面会先看到一堆
    "根本不在现场"的 AP —— 既没有意义，又会掩盖"本机扫描其实失败了"这个事实。
    注意要在启动服务之前检查：start.bat 的 --preload 会把真实结果写进去。
    """
    js = pkg / "app" / "frontend" / "data" / "scan.js"
    if not js.is_file():
        log("失败", f"找不到前端数据文件：{js}")
        return False

    text = js.read_text(encoding="utf-8", errors="replace")
    marker = "window.__WLAN_SCAN__"
    index = text.find(marker)
    if index < 0:
        log("失败", f"scan.js 里没有 {marker}")
        return False

    body = text[text.index("=", index) + 1:].strip()
    if body.endswith(";"):
        body = body[:-1].strip()
    try:
        payload = json.loads(body)
    except ValueError as exc:
        log("失败", f"scan.js 解析不出 JSON：{exc}")
        return False

    rows = payload.get("bss") or []
    if rows:
        log("失败", f"包内快照含 {len(rows)} 条 BSS，夹带了打包机的扫描结果")
        for row in rows[:3]:
            log("", f"  {row.get('ssid') or '(隐藏 SSID)'} / {row.get('bssid')}")
        return False

    log("数据", "包内快照是中性占位（0 条 BSS）")
    return True


def check_diagnose(pkg: Path) -> bool:
    """--diagnose 必须能跑出报告 —— 换电脑排查"扫不到 Wi-Fi"就靠它。

    这里只看输出内容，不看退出码：诊断在扫描失败时返回 1 是正常的。
    """
    cli = pkg / "app" / "backend" / "cli.py"
    proc = run([str(pkg / "runtime" / "python.exe"), str(cli), "--diagnose"], cwd=pkg)
    text = proc.stdout.decode("utf-8", "replace")

    if "扫描诊断" not in text or "无线网卡与调用过程" not in text:
        stderr = proc.stderr.decode("utf-8", "replace").strip()[:200]
        log("失败", f"--diagnose 没输出诊断内容：{stderr}")
        return False

    lines = [line for line in text.splitlines() if line.strip()]
    log("诊断", f"--diagnose 输出 {len(lines)} 行，含网卡清单与调用过程")
    return True


def check_console(out: bytes) -> bool:
    """启动脚本的控制台输出必须干净。

    这里盯的是一类真实踩过的坑：.bat 里写了中文、又先 chcp 65001，cmd.exe 读
    批处理时会丢同步，把中文行的后半截当命令执行，于是窗口里冒出一串
    "不是内部或外部命令"。所以断言：输出能按 UTF-8 正常解码、不含这类报错、
    并且能看到 Python 打的那句中文启动提示。
    """
    text = out.decode("utf-8", "replace")
    log("控制台", f"{len(out)} 字节输出")

    bad_markers = ["不是内部或外部命令", "is not recognized", "系统找不到指定的路径",
                   "The system cannot find the path", "Traceback", "SyntaxError"]
    hit = [m for m in bad_markers if m in text]
    if hit:
        log("失败", f"控制台输出里有报错：{hit[0]}")
        for line in text.splitlines()[:6]:
            log("", line.strip()[:120])
        return False

    if "\ufffd" in text:
        log("失败", f"输出不是合法 UTF-8（{text.count(chr(0xFFFD))} 个替换字符）")
        return False

    banner = [line.strip() for line in text.splitlines() if line.strip()]
    log("", " / ".join(banner[:3])[:150])
    if not any("预扫描" in line or "已启动" in line for line in banner):
        log("失败", "没看到服务启动的中文提示，可能 Python 输出编码不对")
        return False
    return True


def check_launcher(pkg: Path, port: int) -> bool:
    real = pkg / "start.bat"
    if not real.is_file():
        log("失败", "找不到 start.bat")
        return False

    # 去掉 --open，免得测试时弹一堆浏览器窗口
    text = real.read_text(encoding="utf-8").replace("--preload --open", "--preload")
    temp = pkg / "_check-start.bat"
    temp.write_text(text, encoding="utf-8", newline="\r\n")

    log("启动", f"_check-start.bat --port {port}（临时代理，去掉 --open）")
    proc = subprocess.Popen(
        ["cmd.exe", "/c", "_check-start.bat", "--port", str(port)],
        cwd=str(pkg),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )

    base = f"http://127.0.0.1:{port}"
    out = b""
    ok_http = False
    try:
        if not wait_http(base + "/api/ping", timeout=60):
            log("失败", "服务在 60 秒内没有起来")
            return False
        log("", "服务已就绪")

        status, body = get(base + "/")
        html = body.decode("utf-8", "replace")
        ok_index = status == 200 and "AP 信道规划助手" in html
        log("页面", f"GET / → HTTP {status} · {len(body)} 字节 · 含标题 {ok_index}")

        status, body = get(base + "/api/scan?fresh=0")
        payload = json.loads(body.decode("utf-8", "replace"))
        ok_api = status == 200 and payload.get("ok") is not None
        log("接口", f"GET /api/scan → HTTP {status} · ok={payload.get('ok')} · "
                    f"{len(payload.get('bss', []))} 条 BSS")

        ok_assets = True
        for asset in ("/css/style.css", "/js/app.js", "/js/store.js", "/data/scan.js"):
            status, body = get(base + asset)
            log("资源", f"GET {asset} → HTTP {status} · {len(body)} 字节")
            if status != 200 or not body:
                log("失败", f"{asset} 取不到")
                ok_assets = False
        ok_http = ok_index and ok_api and ok_assets
    finally:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True)
        try:
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:          # pragma: no cover
            proc.kill()
            out = b""
        temp.unlink(missing_ok=True)

    # 控制台输出要等进程结束才能收全，所以放在 finally 之后看
    return ok_http and check_console(out)


# ---------------------------------------------------------------- 入口
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="便携版冒烟测试")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--from-zip", action="store_true",
                        help="先解压 dist 下的 zip，检查真正发出去的那个包")
    args = parser.parse_args(argv)

    temp_root: Path | None = None
    if args.from_zip:
        if not ZIP_PATH.is_file():
            print(f"找不到 {ZIP_PATH}\n先执行 python tools/build_portable.py")
            return 2
        temp_root = Path(tempfile.mkdtemp(prefix="ap-planner-zip-"))
        with zipfile.ZipFile(ZIP_PATH) as zf:
            zf.extractall(temp_root)
        pkg = temp_root / PKG_NAME
        if not pkg.is_dir():
            print(f"zip 里没有 {PKG_NAME}/ 这一层目录，解压结构不对")
            return 2
        log("解压", f"{ZIP_PATH.name} → {pkg}")
    else:
        pkg = PKG_DIR
        if not pkg.is_dir():
            print(f"没有打包产物：{pkg}\n先执行 python tools/build_portable.py")
            return 2

    log("目标", str(pkg))
    try:
        results = [
            ("运行时可用", check_runtime(pkg)),
            ("真实扫描可用", check_scan(pkg)),
            ("扫描诊断可输出", check_diagnose(pkg)),
            # 必须在起服务之前：start.bat --preload 会把真实扫描结果写进 data/
            ("包里不含打包机的扫描数据", check_snapshot_neutral(pkg)),
            ("启动脚本 + 服务可用", check_launcher(pkg, args.port)),
        ]
    finally:
        if temp_root is not None:
            shutil.rmtree(temp_root, ignore_errors=True)
            log("清理", f"已删除临时解压目录 {temp_root}")

    print()
    failed = [name for name, ok in results if not ok]
    for name, ok in results:
        print(f"  {'[通过]' if ok else '[失败]'} {name}")
    if failed:
        print(f"\n有 {len(failed)} 项没通过。")
        return 1
    print("\n便携版可用：目标机器不需要装 Python。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
