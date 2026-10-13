#!/usr/bin/env python3
"""扫描回退逻辑的离线测试 —— 不碰真实硬件。

用假的 wlanapi 替身模拟"换到别的电脑上"真会碰到的几种局面，验证
wlan_scanner.scan() 会自己多试一遍、并且把失败原因如实带出来：

    0  一张无线网卡都没有
    1  两张网卡：第一张（虚拟网卡）扫不出东西，第二张有 —— 必须还能扫到
    2  WlanScan 返回 ERROR_ACCESS_DENIED(5) —— ok=False 且提示指向位置权限
    3  读 BSS 列表时被拒绝（错误码 5）—— 同上
    4  扫描成功、驱动也确实没有 AP —— ok=True + 警告（不能报成故障）

错误码 5 是最容易被误判的一条：Windows 从 2023 年起要求调用方拥有"精确位置"
权限才允许扫描 Wi-Fi，提权解决不了。所以这里专门钉住提示的措辞，避免以后又被
改回"请以管理员身份运行"。

    python tests/scanner_fallback_check.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import wlanapi        # noqa: E402
import wlan_scanner   # noqa: E402

FAILURES: list[str] = []
NOTES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    line = f"  [{'通过' if condition else '失败'}] {label}"
    if detail:
        line += f" — {detail}"
    (NOTES if condition else FAILURES).append(line)


def section(title: str) -> None:
    NOTES.append(f"\n{title}")


# ---------------------------------------------------------------- 替身

class FakeHandle:
    """替掉 WlanHandle，不真的去 WlanOpenHandle。"""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def close(self) -> None:
        pass


def adapter(name: str, state: int = 4) -> wlanapi.InterfaceInfo:
    """state=1 表示已连接，4 表示未连接。"""
    return wlanapi.InterfaceInfo(guid=wlanapi.GUID(), description=name, state=state)


def bss(ssid: str, bssid: str, freq_khz: int = 5180000, rssi: int = -50) -> wlanapi.BssEntry:
    return wlanapi.BssEntry(
        ssid=ssid, bssid=bssid, rssi=rssi, link_quality=80,
        center_freq_khz=freq_khz, phy_type=10, bss_type=1,
        capability=0x0411, beacon_period=100, ie_offset=0, ie_size=0,
    )


def run_scan(*, interfaces, scan_code: int = 0, bss_by_name=None,
             bss_error=None, diagnose: bool = False) -> dict:
    """装好替身跑一次 scan()，无论成功失败都把替身还原。"""
    saved = (
        wlan_scanner.WlanHandle,
        wlanapi.enum_interfaces,
        wlanapi.request_scan,
        wlanapi.get_bss_list,
        wlanapi.query_current_connection,
        wlan_scanner.RETRY_GAP,
        wlan_scanner.ROUND_GAP,
    )

    wlan_scanner.WlanHandle = FakeHandle
    wlanapi.enum_interfaces = lambda handle: list(interfaces)
    wlanapi.request_scan = lambda handle, info, **kw: wlanapi.ScanOutcome(
        code=scan_code, completed=(scan_code == 0)
    )

    def fake_get_bss(handle, info):
        if bss_error is not None:
            raise wlanapi.WlanApiError("WlanGetNetworkBssList", bss_error)
        return list((bss_by_name or {}).get(info.description, []))

    wlanapi.get_bss_list = fake_get_bss
    wlanapi.query_current_connection = lambda handle, info: None
    wlan_scanner.RETRY_GAP = 0          # 测试里不要真等
    wlan_scanner.ROUND_GAP = 0

    try:
        return wlan_scanner.scan(retries=1, diagnose=diagnose)
    finally:
        (wlan_scanner.WlanHandle,
         wlanapi.enum_interfaces,
         wlanapi.request_scan,
         wlanapi.get_bss_list,
         wlanapi.query_current_connection,
         wlan_scanner.RETRY_GAP,
         wlan_scanner.ROUND_GAP) = saved


# ---------------------------------------------------------------- 场景

section("0 · 一张无线网卡都没有")
payload = run_scan(interfaces=[])
check("返回 ok=False", payload["ok"] is False)
check("提示指出没有无线网卡", "没有找到可用的无线网卡" in payload.get("error", ""),
      payload.get("error", "").split("。")[0])

section("1 · 两张网卡，第一张扫不出东西")
virtual = adapter("Microsoft Wi-Fi Direct Virtual Adapter")
real = adapter("Intel(R) Wi-Fi 6E AX211 160MHz")
payload = run_scan(
    interfaces=[virtual, real],
    bss_by_name={
        real.description: [
            bss("Corp", "58:B4:BB:D5:1A:B4", 2437000, -52),
            bss("Corp", "5A:B4:BB:85:1A:B4", 5220000, -45),
        ],
    },
    diagnose=True,
)
check("ok=True（没有因为第一张网卡空手而归就放弃）", payload["ok"] is True)
check("读到第二张网卡的 2 条 BSS", len(payload["bss"]) == 2,
      f"{len(payload['bss'])} 条")
check("接口名用的是真正扫出结果的那张网卡",
      payload["interface"]["name"] == real.description,
      payload["interface"]["name"])
check("提示里说明本机有多张网卡",
      any("张无线网卡" in w for w in payload["warnings"]),
      " / ".join(payload["warnings"]) or "（无警告）")
read_lines = [n for n in payload.get("diagnostics", []) if "读到" in n]
check("诊断里两张网卡都有记录", len(read_lines) == 2,
      f"{len(read_lines)} 条")

section("1b · 网卡优选顺序")
connected = adapter("USB 无线网卡", state=1)
idle = adapter("内置无线网卡", state=4)
ordered = wlanapi.pick_interfaces([idle, connected])
check("已连接的网卡排在最前面", ordered[0] is connected, ordered[0].description)
check("未连接的跟在后面且不丢", len(ordered) == 2 and ordered[1] is idle,
      "、".join(i.description for i in ordered))

section("2 · WlanScan 返回 ERROR_ACCESS_DENIED(5)")
payload = run_scan(interfaces=[adapter("Intel(R) Wi-Fi 6 AX201")], scan_code=5)
check("返回 ok=False", payload["ok"] is False)
check("错误里带上错误码 5", "5" in payload.get("error", ""),
      payload.get("error", "")[:48])
check("提示指向「位置」权限", "位置" in payload.get("error", ""))
check("明确说明提权无效",
      "管理员身份运行并不能解决" in payload.get("error", ""))

section("3 · 读 BSS 列表时被拒绝")
payload = run_scan(interfaces=[adapter("Intel(R) Wi-Fi 6 AX201")], bss_error=5)
check("返回 ok=False", payload["ok"] is False)
check("错误里带上错误码 5", "5" in payload.get("error", ""),
      payload.get("error", "")[:48])
check("提示指向「位置」权限", "位置" in payload.get("error", ""))

section("4 · 扫描成功，但周围确实没有 AP")
payload = run_scan(interfaces=[adapter("Intel(R) Wi-Fi 6 AX201")],
                   bss_by_name={})
check("ok=True（不能把「没有 AP」报成故障）", payload["ok"] is True)
check("给出「没有扫描到任何 BSS」的警告",
      any("没有扫描到任何 BSS" in w for w in payload["warnings"]),
      " / ".join(payload["warnings"]) or "（无警告）")
check("接口信息仍然填好（便于人工判断）",
      payload["interface"]["name"] == "Intel(R) Wi-Fi 6 AX201",
      payload["interface"]["name"])

# ---------------------------------------------------------------- 报告
print("\n".join(NOTES))
print("\n" + "=" * 64)
if FAILURES:
    print("失败项：")
    print("\n".join(FAILURES))
    print("=" * 64)
    raise SystemExit(1)
print(f"全部通过（{sum('[通过]' in n for n in NOTES)} 项）")
print("=" * 64)
