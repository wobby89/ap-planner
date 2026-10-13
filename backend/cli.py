"""命令行入口：扫描一次，把结果导出成前端需要的两个数据文件。

    python backend/cli.py               扫描并写入 frontend/data/
    python backend/cli.py --table       扫描后额外在终端打印一张表
    python backend/cli.py --json-only   只打印 JSON，不写文件
    python backend/cli.py --diagnose    打印诊断：网卡清单 + 每一步的调用结果
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import wlan_scanner  # noqa: E402
import wlanapi  # noqa: E402

PROJECT_ROOT = _HERE.parent
DATA_DIR = PROJECT_ROOT / "frontend" / "data"


def _fix_console_encoding() -> None:
    """Windows 控制台默认是 GBK，打印中文 / 特殊字符会炸。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):            # pragma: no cover
            pass


def write_snapshot(payload: dict, data_dir: Path) -> tuple[Path, Path]:
    """同时写 .json 和 .js。

    .js 版本让 index.html 直接双击打开也能看到真实数据（不受 file:// 的
    fetch 限制）；.json 版本给人和其它工具看。
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)

    json_path = data_dir / "scan.json"
    json_path.write_text(text, encoding="utf-8")

    js_path = data_dir / "scan.js"
    js_path.write_text(
        "/* 由 backend/cli.py 生成的扫描快照，请勿手改。 */\n"
        "window.__WLAN_SCAN__ = " + text + ";\n",
        encoding="utf-8",
    )
    return json_path, js_path


def render_table(payload: dict) -> str:
    columns = [
        ("SSID", "ssid", 26),
        ("BSSID", "bssid", 17),
        ("频段", "band", 4),
        ("信道", "channel", 4),
        ("中心频率", "frequency", 8),
        ("频宽", "width", 4),
        ("信号", "rssi", 6),
        ("PHY", "phy", 9),
        ("加密", "security", 22),
    ]
    lines = [
        "  ".join(title.ljust(width) for title, _, width in columns),
        "  ".join("-" * width for _, _, width in columns),
    ]
    for row in payload["bss"]:
        cells = []
        for _, key, width in columns:
            value = row.get(key, "")
            if key == "ssid":
                value = value if value else "(隐藏 SSID)"
            elif key == "rssi":
                value = f"{value} dBm"
            elif key == "frequency":
                value = f"{value} MHz"
            elif key == "width":
                value = f"{value} MHz"
            elif key == "channel":
                value = str(value) if value else "—"
            cells.append(str(value).ljust(width))
        lines.append("  ".join(cells))
    return "\n".join(lines)


def render_summary(payload: dict) -> str:
    interface = payload["interface"]
    counts = {}
    for row in payload["bss"]:
        counts[row["band"]] = counts.get(row["band"], 0) + 1
    parts = [f"{band} GHz {n} 条" for band, n in sorted(counts.items()) if band]
    lines = [
        f"无线接口：{interface['name'] or '—'}（{interface['state'] or '—'}）",
        f"当前连接：{interface['current_ssid'] or '无'}"
        + (f" / {interface['current_bssid']}" if interface["current_bssid"] else ""),
        f"扫描时间：{payload['scanned_at']}",
        f"发现 BSS：{len(payload['bss'])} 条（{'、'.join(parts) if parts else '无'}）",
    ]
    for warning in payload.get("warnings", []):
        lines.append(f"提示：{warning}")
    if payload.get("error"):
        lines.append(f"错误：{payload['error']}")
    return "\n".join(lines)


def render_diagnostics(payload: dict) -> str:
    """把一次扫描的逐步调用结果打出来。

    换电脑之后"扫不到 Wi-Fi"的原因有好几种（位置权限、网卡被禁用、WlanSvc 没起、
    机器根本没有无线网卡……），光看现象分不出来。把这段内容回传过来就能直接定位。
    """
    lines = [
        "=" * 62,
        "AP 信道规划助手 · 扫描诊断",
        "=" * 62,
        "把下面整段内容复制回传给开发者，比只描述现象有用得多。",
        "",
        "【无线网卡与调用过程】",
    ]
    diagnostics = payload.get("diagnostics") or []
    lines.extend(diagnostics or ["  （没有采集到诊断信息）"])

    lines += ["", "【扫描结论】", render_summary(payload)]

    if not payload.get("ok"):
        lines += [
            "",
            "【怎么处理】",
            "  · 提示“访问被拒绝 / 错误码 5” —— 这不是管理员权限问题，"
            "而是 Windows 的位置隐私管控：",
            f"    打开 {wlanapi.LOCATION_SETTINGS_URI}"
            "（设置 → 隐私和安全性 → 位置），先打开“定位服务”，",
            "    再在下面的应用列表里允许本程序访问位置，然后重新扫描。",
            "  · 提示 WlanSvc / 错误码 1062 —— 在“服务”里启动 WLAN AutoConfig。",
            "  · 提示“找不到无线网卡” —— 这台机器可能压根没有 Wi-Fi 网卡。",
            "  · 提示 Wi-Fi 开关 / 飞行模式 —— 打开 Wi-Fi 后重新扫描。",
        ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    _fix_console_encoding()
    parser = argparse.ArgumentParser(
        description="扫描本机 WLAN 环境并导出前端数据快照"
    )
    parser.add_argument("--table", action="store_true", help="在终端打印扫描结果表")
    parser.add_argument("--json-only", action="store_true", help="只输出 JSON，不写文件")
    parser.add_argument("--diagnose", action="store_true",
                        help="打印诊断信息：枚举到的无线网卡、每一步的调用结果与错误码")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR,
                        help=f"快照输出目录，默认 {DATA_DIR}")
    parser.add_argument("--timeout", type=float, default=12.0,
                        help="等待扫描完成的秒数，默认 12")
    args = parser.parse_args(argv)

    if args.diagnose:
        payload = wlan_scanner.scan(timeout=args.timeout, diagnose=True)
        print(render_diagnostics(payload))
        return 0 if payload["ok"] else 1

    payload = wlan_scanner.scan(timeout=args.timeout)

    if args.json_only:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["ok"] else 1

    print(render_summary(payload))
    if args.table and payload["bss"]:
        print()
        print(render_table(payload))

    if payload["ok"]:
        json_path, js_path = write_snapshot(payload, args.data_dir)
        print()
        print(f"已写入 {json_path}")
        print(f"已写入 {js_path}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
