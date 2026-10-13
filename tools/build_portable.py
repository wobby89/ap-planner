#!/usr/bin/env python3
"""把整个程序打包成一个便携版 zip —— 目标电脑不需要装 Python。

做法：把 Python 官方的**嵌入式发行包**（embeddable package）放进包里当运行时，
程序本身一行代码都不用改。选它而不是 PyInstaller 的理由：

  * 纯标准库即可运行（本项目后端零第三方依赖），嵌入式包正好没带 pip/site-packages；
  * 体积小（约 11 MB）、启动快、不触发杀毒软件的"打包器"误报；
  * 文件全部可见，出问题能直接看出是哪一步。

产出（都在 dist/ 下）：

    dist/ap-planner-portable/        可直接双击运行的文件夹
    dist/ap-planner-portable.zip     发给别人的那个 zip

用法：

    python tools/build_portable.py                 构建（首次会联网下载运行时）
    python tools/build_portable.py --no-zip        只生成文件夹，不压缩

运行时压缩包会缓存在 .build/ 下，重复构建不会再下载。
"""

from __future__ import annotations

import argparse
import datetime
import json
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / ".build"
DIST_DIR = ROOT / "dist"
PKG_NAME = "ap-planner-portable"
PKG_DIR = DIST_DIR / PKG_NAME
ZIP_PATH = DIST_DIR / (PKG_NAME + ".zip")

# 嵌入式运行时的版本。换版本时改这里就行，URL 是 python.org 的固定地址。
PY_VERSION = "3.13.12"
PY_FLAVOR = "embed-amd64"
PY_TAG = "".join(PY_VERSION.split(".")[:2])          # 3.13.12 -> "313"
PY_ARCHIVE = f"python-{PY_VERSION}-{PY_FLAVOR}.zip"
PY_URL = f"https://www.python.org/ftp/python/{PY_VERSION}/{PY_ARCHIVE}"
PY_PTH = f"python{PY_TAG}._pth"

# 打进包里的程序目录（相对仓库根）
APP_DIRS = ["backend", "frontend"]

# 便携版 README 里会写到的端口，必须与 backend/server.py 的默认值一致
DEFAULT_PORT = 8765


# ---------------------------------------------------------------- 小工具
def log(step: str, text: str = "") -> None:
    print(f"[{step}] {text}".rstrip())


def write_text(path: Path, text: str) -> None:
    """统一写成 UTF-8 + CRLF。

    CRLF 是给 .bat 用的：cmd.exe 对纯 LF 的批处理在个别分支（goto / 多行块）
    上会出错，所以批处理一律 CRLF。UTF-8 则是因为文件内容里有中文。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write(text)


def human(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size /= 1024.0
    return f"{size:.1f} GB"


# ---------------------------------------------------------------- 运行时
def fetch_runtime() -> Path:
    """返回嵌入式包的本地路径，没有就先下载。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = CACHE_DIR / PY_ARCHIVE
    if cached.is_file() and cached.stat().st_size > 1_000_000:
        log("缓存", f"{cached.name}（{human(cached.stat().st_size)}）")
        return cached

    log("下载", f"Python {PY_VERSION} 嵌入式包 …")
    log("", PY_URL)
    try:
        with urllib.request.urlopen(PY_URL, timeout=180) as resp:
            data = resp.read()
    except OSError as exc:
        raise SystemExit(
            f"下载失败：{exc}\n"
            f"可以手动下载 {PY_ARCHIVE} 放到 {CACHE_DIR} 后重跑。"
        )
    cached.write_bytes(data)
    log("下载", f"完成 {human(len(data))}")
    return cached


def unpack_runtime(archive: Path, dest: Path) -> None:
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(dest)

    # 嵌入式包自带的 ._pth 会把 sys.path 钉死成"只有 zip + 当前目录"，
    # 让 PYTHONPATH / 用户 site-packages 失效。本项目两个入口都会自己插
    # sys.path，所以保持原样最安全（顺带避免误加载机器上别的 Python 包）。
    pth = dest / PY_PTH
    log("运行时", f"{PY_ARCHIVE} → runtime/")
    if pth.is_file():
        log("", f"（保留 {PY_PTH}，运行时与系统 Python 完全隔离）")


# ---------------------------------------------------------------- 启动脚本
# 这两段模板必须保持**纯 ASCII**。cmd.exe 在 UTF-8 代码页下读 .bat 时会因为
# 多字节字符而丢同步，把一行的尾巴当成命令去执行（实测：带中文的 rem 行后面
# 会冒出「不是内部或外部命令」）。所以中文一律交给 Python 打印 —— Python 输出
# UTF-8，配合下面的 chcp 65001 显示正常。中文说明都在 README.txt 里。
START_BAT = r"""@echo off
rem ===========================================================================
rem  AP Channel Planner - portable launcher
rem    Double-click: scans Wi-Fi once, starts a local server, opens the browser.
rem    Close this window to stop the server.
rem
rem  Keep this file pure ASCII - see tools/build_portable.py for the reason.
rem  Chinese text is printed by Python, and README.txt has the full guide.
rem ===========================================================================
setlocal
cd /d "%~dp0"

rem Switch the console to UTF-8 so Python's Chinese output renders correctly
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "PYTHONNOUSERSITE=1"
rem Print immediately: without this, a redirected log loses the startup lines
set "PYTHONUNBUFFERED=1"

set "PY=%~dp0runtime\python.exe"
if not exist "%PY%" (
  echo.
  echo [ERROR] Bundled runtime not found:
  echo         %PY%
  echo         Please extract the WHOLE folder first, then run start.bat again.
  echo.
  pause
  exit /b 1
)

rem %* passes extra arguments through, e.g.  start.bat --port 8899
"%PY%" "%~dp0app\backend\server.py" --preload --open %*

if errorlevel 1 (
  echo.
  echo [ERROR] The server exited. If the port is busy, try another one:
  echo         start.bat --port 8899
  echo.
  pause
)

endlocal
"""

SCAN_BAT = r"""@echo off
rem ===========================================================================
rem  Scan once and write the result into app\frontend\data\, without the server.
rem  Then app\frontend\index.html can be opened directly by double-click.
rem  Keep this file pure ASCII (see tools/build_portable.py).
rem ===========================================================================
setlocal
cd /d "%~dp0"

chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "PYTHONNOUSERSITE=1"
set "PYTHONUNBUFFERED=1"

set "PY=%~dp0runtime\python.exe"
if not exist "%PY%" (
  echo.
  echo [ERROR] Bundled runtime not found:
  echo         %PY%
  echo         Please extract the WHOLE folder first, then run scan-once.bat again.
  echo.
  pause
  exit /b 1
)

"%PY%" "%~dp0app\backend\cli.py" --table %*

echo.
pause
endlocal
"""

DIAGNOSE_BAT = r"""@echo off
rem ===========================================================================
rem  Print a scan diagnostic report: every wireless adapter found, every WLAN
rem  API call, and the exact error codes.
rem
rem  Run this when scanning fails on a machine, then send the whole window
rem  content back. Keep this file pure ASCII (see tools/build_portable.py).
rem ===========================================================================
setlocal
cd /d "%~dp0"

chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "PYTHONNOUSERSITE=1"
set "PYTHONUNBUFFERED=1"

set "PY=%~dp0runtime\python.exe"
if not exist "%PY%" (
  echo.
  echo [ERROR] Bundled runtime not found:
  echo         %PY%
  echo         Please extract the WHOLE folder first, then run diagnose.bat again.
  echo.
  pause
  exit /b 1
)

"%PY%" "%~dp0app\backend\cli.py" --diagnose

echo.
pause
endlocal
"""

PLACEHOLDER_WARNING = (
    "便携版里没有预置扫描数据（那台机器的 Wi-Fi 和这里不一样，带过来没有意义）。"
    "双击 start.bat 会自动扫描本机；页面已经开着的话，点右上角「重新扫描」。"
)


def write_placeholder_snapshot(pkg: Path) -> None:
    """用一份中性空快照覆盖从开发机复制过来的扫描结果。

    开发机的 Wi-Fi 环境和别人的电脑完全不同。把开发机的扫描结果打进包里，
    别人打开页面会先看到一堆"根本不在现场"的 AP —— 不但没意义，还会掩盖
    "本机扫描其实失败了"这个事实（看起来像扫到了，实际是别人的数据）。
    宁可空着，让页面明确提示去扫描。
    """
    payload = {
        "ok": True,
        "scanned_at": "",
        "interface": {"name": "", "state": "",
                      "current_ssid": "", "current_bssid": ""},
        "warnings": [PLACEHOLDER_WARNING],
        "bss": [],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    data_dir = pkg / "app" / "frontend" / "data"
    write_text(data_dir / "scan.json", text)
    write_text(
        data_dir / "scan.js",
        "/* 便携版占位数据：真正的扫描结果由 start.bat 运行时写入。 */\n"
        "window.__WLAN_SCAN__ = " + text + ";\n",
    )
    log("数据", "app/frontend/data/ → 中性空快照（不带开发机的扫描结果）")


def readme_text() -> str:
    return f"""AP 信道规划助手 · 便携版
================================================================

这个包不需要安装 Python，也不需要联网。解压后双击 start.bat 就能用。


一、怎么启动
----------------------------------------------------------------
1. 把整个 ap-planner-portable 文件夹解压到任意位置
   （桌面、D 盘、U 盘都行；路径里有中文或空格也没关系）
2. 双击 start.bat
   黑色窗口里会先扫描一次本机周围的 Wi-Fi（约 2~10 秒），
   然后自动打开浏览器：http://127.0.0.1:{DEFAULT_PORT}/
3. 用完之后关掉那个黑色窗口，服务就停了


二、页面怎么用
----------------------------------------------------------------
第一步  扫描结果
        勾选属于本公司的 BSS（默认已按当前连着的 SSID 圈好），
        「归属 AP」那一列确认哪些 BSS 是同一台 AP 的两个频段。
第二步  确认相邻
        一台一台确认「谁靠近谁」—— 相邻的 AP 必须错开信道。
第三步  规划建议
        给出 5 GHz / 2.4 GHz 的信道与频宽建议，可导出成 txt。

右上角「重新扫描」会重新采集一次实时数据（需要黑窗口还开着）。


三、三个批处理分别是
----------------------------------------------------------------
start.bat       扫描 + 起服务 + 打开浏览器（平时就用这个）
scan-once.bat   只扫描一次并更新页面数据文件，不起服务
diagnose.bat    扫不到 Wi-Fi 时运行它，把窗口里的内容整段发回给开发者


四、换端口
----------------------------------------------------------------
    start.bat --port 8899
然后访问 http://127.0.0.1:8899/


五、常见问题
----------------------------------------------------------------
Q  提示「找不到无线接口」或 WlanSvc 相关错误
A  台式机很可能压根没有无线网卡；有网卡的话，检查「服务」里的
   WLAN AutoConfig（WlanSvc）是否在运行。

Q  页面打开是空白，或顶部显示「扫描不可用」
A  页面由本地服务提供，确认黑色窗口还开着、没有被关掉。

Q  换到别的电脑上，提示「扫描不可用」，或者一条 AP 都没有
A  按顺序查这三件事：
   1) **位置权限**。从 2023 年起，Windows 要求调用方拥有「精确位置」权限才允许
      扫描 Wi-Fi，否则 WlanScan / WlanGetNetworkBssList 一律返回「访问被拒绝」。
      打开 设置 → 隐私和安全性 → 位置，先打开「定位服务」，再在应用列表里
      允许本程序访问位置。注意：**用管理员身份运行不能解决这个问题**。
   2) Wi-Fi 是不是开着。飞行模式、Fn 快捷键关掉射频、网卡被禁用，扫描同样会失败。
   3) WLAN AutoConfig 服务（WlanSvc）有没有在运行（设置 → 服务）。
   还是不行就双击 diagnose.bat，把窗口里整段内容发回给开发者 —— 里面有每张
   网卡的状态和每一步调用的错误码，能直接定位是哪一环卡住。

Q  不小心双击了两次 start.bat
A  没关系。第二个窗口会认出「服务已经在运行」，把浏览器指到已经开着的那个页面，
   然后自己退出，不会起第二个服务、也不会报端口被占用。

Q  窗口里出现 [ERROR] 开头的英文提示
A  那是启动脚本自己的提示。批处理里故意不写中文：cmd.exe 在 UTF-8 代码页下
   读批处理会把中文行的后半截当成命令执行，冒出一堆假报错。所以脚本提示统一
   用英文，中文都由 Python 打印。最常见的是 "Bundled runtime not found" ——
   说明压缩包没解压完整，请把整个文件夹解压出来再运行。

Q  杀毒软件报警
A  runtime\\python.exe 是 Python 官方 {PY_VERSION} Windows 嵌入式版
   （从 python.org 原样下载，未做任何修改），许可证见 runtime\\LICENSE.txt；
   三个 .bat 只是用它去跑 app\\backend 下的脚本。

Q  怎么确认数据是真的
A  页面右上角会显示「实时扫描数据 + 时间」；点「重新扫描」可以再采一次。


六、包里有什么
----------------------------------------------------------------
start.bat / scan-once.bat / diagnose.bat   启动与诊断脚本
README.txt                  本文件
app\\backend\\                Python 后端：Windows WLAN API 扫描 + 本地服务
app\\frontend\\               网页（HTML / CSS / JS + 一份占位数据）
app\\README.md                技术说明：每一列的数据来源、接口、规划算法
runtime\\                    嵌入式 Python {PY_VERSION}（含 PSF 许可证）
BUILD-INFO.txt               打包信息


七、说明
----------------------------------------------------------------
只监听 127.0.0.1，局域网内其它机器访问不到，扫描数据不出本机。
需要源码方式运行（机器上已装 Python）请看 app\\README.md。
"""


def build_info_text() -> str:
    import platform

    now = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    return f"""AP 信道规划助手 · 便携版打包信息
================================================================
打包时间       {now}
打包机器       {platform.system()} {platform.release()} ({platform.machine()})
运行时         Python {PY_VERSION} embed-amd64（python.org 官方发行包）
源目录         {ROOT}
默认端口       {DEFAULT_PORT}

内容
----------------------------------------------------------------
app\\backend\\     后端：backend/wlanapi.py（ctypes 绑定 wlanapi.dll）
                        backend/ie_parser.py（802.11 IE 解析）
                        backend/wlan_scanner.py（扫描 + 归一化）
                        backend/server.py（本地服务，静态托管 + /api/scan）
                        backend/cli.py（命令行扫描并导出快照 / --diagnose）
app\\frontend\\    页面：index.html + css/style.css + js/*.js + data/
                   data/ 里是**中性占位数据**，真正的扫描结果由 start.bat 运行时写入
runtime\\          嵌入式 Python，程序与系统里装没装 Python 无关

扫描失败排查
----------------------------------------------------------------
换电脑后扫不到 Wi-Fi，优先怀疑 Windows 的「位置权限」（错误码 5）。
运行 diagnose.bat 可打印每张网卡的状态与每一步调用的错误码。

重新打包
----------------------------------------------------------------
在源码仓库根目录执行：
    python tools/build_portable.py
"""


# ---------------------------------------------------------------- 组装
def copy_app(dest: Path) -> None:
    for name in APP_DIRS:
        src = ROOT / name
        if not src.is_dir():
            raise SystemExit(f"缺少源目录：{src}")
        shutil.copytree(
            src, dest / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
        )
        log("程序", f"{name}/ → app/{name}/")

    # 开发文档一起带上，但先加一段便携版说明 —— 原文里的命令是针对源码仓库写的
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    note = (
        "> **便携版说明**：这个 zip 里只带了 `backend/` 与 `frontend/`，都放在 `app/` 下。\n"
        "> 启动请用**上一层目录的 `start.bat`**（已内置 Python 运行时，目标机器不用装）。\n"
        "> 若要自己敲命令，把下文里的 `python` 换成包内的运行时即可，例如：\n"
        "> `..\\runtime\\python.exe backend\\server.py --port 9000`。\n"
        "> `tests/`、`run_server.bat`、`scan_once.bat`、`tools/` 属于源码仓库，未打进便携版。\n"
        "\n---\n\n"
    )
    write_text(dest / "README.md", note + readme)
    log("程序", "README.md → app/README.md（已加便携版说明）")


def build(do_zip: bool) -> None:
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    if PKG_DIR.exists():
        shutil.rmtree(PKG_DIR)          # 只删 dist/ 下的产物，安全
    (PKG_DIR / "app").mkdir(parents=True)
    log("清理", f"{PKG_DIR.relative_to(ROOT)}")

    archive = fetch_runtime()
    unpack_runtime(archive, PKG_DIR / "runtime")

    copy_app(PKG_DIR / "app")
    write_placeholder_snapshot(PKG_DIR)

    write_text(PKG_DIR / "start.bat", START_BAT)
    write_text(PKG_DIR / "scan-once.bat", SCAN_BAT)
    write_text(PKG_DIR / "diagnose.bat", DIAGNOSE_BAT)
    write_text(PKG_DIR / "README.txt", readme_text())
    write_text(PKG_DIR / "BUILD-INFO.txt", build_info_text())
    log("脚本", "start.bat / scan-once.bat / diagnose.bat / README.txt / BUILD-INFO.txt")

    files = [p for p in PKG_DIR.rglob("*") if p.is_file()]
    folder_size = sum(p.stat().st_size for p in files)
    print()
    log("完成", f"文件夹 {PKG_DIR}（{len(files)} 个文件，{human(folder_size)}）")

    if not do_zip:
        return

    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(files):
            zf.write(path, arcname=str(Path(PKG_NAME) / path.relative_to(PKG_DIR)))
    log("压缩", f"{ZIP_PATH.name}（{human(ZIP_PATH.stat().st_size)}）")
    print()
    log("产出", str(ZIP_PATH))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="打包便携版 zip")
    parser.add_argument("--no-zip", action="store_true", help="只生成文件夹，不压缩")
    args = parser.parse_args(argv)

    build(do_zip=not args.no_zip)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
