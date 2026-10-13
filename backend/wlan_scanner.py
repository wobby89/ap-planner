"""扫描本机无线环境，输出页面直接用得上的数据结构。

对外只暴露 scan() 一个函数，返回结构固定为：

    {
      "ok": true,
      "scanned_at": "2026-09-20T15:02:11+08:00",
      "interface": {
        "name": "Realtek RTL8852BE WiFi 6 802.11ax PCIe Adapter",
        "state": "已连接",
        "current_ssid": "CorpWiFi",
        "current_bssid": "A4:5E:60:01:01:00"
      },
      "warnings": ["..."],
      "bss": [
        {
          "ssid": "CorpWiFi", "bssid": "A4:5E:60:01:01:00",
          "band": "5", "channel": 36, "frequency": 5180, "width": 20,
          "rssi": -42, "phy": "802.11ax", "security": "WPA2/WPA3-CCMP"
        }
      ]
    }

bss 里的字段就是页面表格的列，一个不多一个不少。
"""

from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

import ie_parser
import wlanapi
from wlanapi import BssEntry, DOT11_PHY_NAMES, InterfaceInfo, WlanApiError, WlanHandle

__all__ = ["scan", "describe_bss", "ScanError"]

# 页面只用到 2.4 / 5 / 6 GHz 三档，其余（如 60 GHz）直接丢弃
_SUPPORTED_BANDS = ("2.4", "5", "6")

_LOCAL_TZ = timezone(timedelta(hours=8))


class ScanError(RuntimeError):
    """扫描无法完成。"""


def _now_iso() -> str:
    return datetime.now(_LOCAL_TZ).replace(microsecond=0).isoformat()


def describe_bss(entry: BssEntry) -> Optional[dict]:
    """把一条原始 BSS 记录转成页面表格的一行。"""
    freq_mhz = entry.center_freq_mhz
    if not freq_mhz:
        return None

    info = ie_parser.describe(
        entry.ie_blob,
        capability=entry.capability,
        freq_mhz=freq_mhz,
        bss_phy_type=entry.phy_type,
        phy_names=DOT11_PHY_NAMES,
    )
    if info["band"] not in _SUPPORTED_BANDS:
        return None

    return {
        "ssid": entry.ssid,
        "bssid": entry.bssid,
        "band": info["band"],
        "channel": info["channel"],
        "frequency": freq_mhz,
        "width": info["width"],
        "rssi": entry.rssi,
        "phy": info["phy"],
        "security": info["security"],
    }


def _dedupe(rows: List[dict]) -> List[dict]:
    """同一 BSSID 可能被多个网卡 / 多轮上报，保留信号最强的一条。"""
    best: Dict[str, dict] = {}
    for row in rows:
        key = row["bssid"]
        current = best.get(key)
        if current is None or row["rssi"] > current["rssi"]:
            best[key] = row
    return list(best.values())


def _sort_rows(rows: List[dict]) -> List[dict]:
    """按频段、信道、信号排序，让同频的 AP 挨在一起，便于人工核对。"""
    band_rank = {"6": 0, "5": 1, "2.4": 2}
    return sorted(
        rows,
        key=lambda r: (
            band_rank.get(r["band"], 9),
            r["channel"] or 9999,
            -r["rssi"],
            r["bssid"],
        ),
    )


# 刚扫描完时驱动常常还没把结果写进缓存，读到空就再等一会
RETRY_GAP = 1.5
# 所有网卡都扫不到东西时的整体重试轮数（刚开机 / 刚连上时缓存可能是空的）
ADAPTER_ROUNDS = 2
# 两轮之间的间隔
ROUND_GAP = 2.0


def _read_bss_with_retry(handle: WlanHandle, interface: InterfaceInfo,
                         *, retries: int, gap: float):
    """读 BSS 列表；读到空就等一会再读。

    返回 (entries, last_error)。last_error 非空表示最后一次仍是 API 报错
    （例如位置权限不足时的 ERROR_ACCESS_DENIED），必须带回去而不是当成"没 AP"。
    """
    entries: List[BssEntry] = []
    last_error = ""
    for attempt in range(max(1, retries)):
        try:
            entries = wlanapi.get_bss_list(handle, interface)
            last_error = ""
        except WlanApiError as exc:
            entries, last_error = [], str(exc)
        if entries:
            return entries, ""
        if attempt < retries - 1:
            time.sleep(gap)
    return entries, last_error


def _failure_message(failures: List[tuple]) -> str:
    """把逐张网卡的失败原因拼成一句能照着做的话。"""
    _, first_error = failures[0]
    message = first_error
    if len(failures) > 1:
        rest = "；".join(f"{info.description} → {err}" for info, err in failures[1:])
        message += f"（本机另外 {len(failures) - 1} 张网卡也没成功：{rest}）"
    return message


def scan(*, wait: bool = True, timeout: float = 12.0,
         retries: int = 3, diagnose: bool = False) -> dict:
    """扫描一次并返回结果。

    任何一步失败都不抛异常给调用方，而是返回 ok=False + error 字段，
    这样页面始终能拿到一个结构合法的响应，可以用统一的方式提示。

    换电脑之后最容易踩的三个坑，这里都按"自己多试一遍"处理：

      * 机器上不止一张无线网卡（物理网卡 + 虚拟网卡 / USB 网卡）——
        按优选顺序**逐张都试**，不再只看第一张；
      * BSS 列表暂时为空（刚开机、刚连上、驱动缓存还没写）—— 重试几轮；
      * WlanScan 直接失败（位置权限不足、Wi-Fi 开关没开）—— 把错误码带出去，
        和"周围确实没有 AP"区分开，否则用户看到的都是一句"扫不到"。

    diagnose=True 时会在返回值里多带一个 diagnostics 列表（逐步的调用结果），
    给 cli.py --diagnose 用；页面不读这个字段。
    """
    warnings: List[str] = []
    notes: List[str] = []
    detail = notes if diagnose else None

    try:
        handle = WlanHandle()
    except WlanApiError as exc:
        return _failure(str(exc), detail)

    try:
        try:
            interfaces = wlanapi.enum_interfaces(handle)
        except WlanApiError as exc:
            return _failure(str(exc), detail)

        if not interfaces:
            return _failure(
                "没有找到可用的无线网卡。台式机一般不带 Wi-Fi 网卡；"
                "笔记本请确认 Wi-Fi 开关已打开、驱动正常，"
                "并在“设置 → 网络和 Internet → WLAN”里能看到无线网卡。",
                detail,
            )

        ordered = wlanapi.pick_interfaces(interfaces)
        notes.append(f"枚举到 {len(ordered)} 张无线网卡：")
        for index, info in enumerate(ordered, 1):
            notes.append(
                f"  [{index}] {info.description}"
                f"｜状态 {info.state_name}"
                f"｜{'已连接' if info.is_connected else '未连接'}"
                f"｜{info.guid}"
            )
        if len(ordered) > 1:
            warnings.append(f"本机有 {len(ordered)} 张无线网卡，已逐张尝试扫描。")

        # 当前连接从任意一张已连接的网卡上取
        connection = None
        for info in ordered:
            try:
                found = wlanapi.query_current_connection(handle, info)
            except WlanApiError as exc:
                notes.append(f"  [{info.description}] 读取当前连接失败：{exc}")
                continue
            if found and found.ssid:
                connection = found
                notes.append(f"当前连接：{found.ssid} / {found.bssid}"
                             f"（来自“{info.description}”）")
                break

        collected: List[dict] = []
        used: Optional[InterfaceInfo] = None     # 真正扫出结果的网卡
        failures: List[tuple] = []               # 硬错误：WlanScan 或读列表失败
        silent: List[InterfaceInfo] = []         # 扫描成功、但确实一条都没有的网卡

        for round_index in range(ADAPTER_ROUNDS):
            for info in ordered:
                outcome = wlanapi.request_scan(handle, info, wait=wait, timeout=timeout)
                if not outcome.ok:
                    failures.append((info, outcome.error))
                    notes.append(f"  [{info.description}] {outcome.error}")
                    continue
                if not outcome.completed:
                    warnings.append(
                        f"网卡“{info.description}”没有返回“扫描完成”通知，"
                        "已改用驱动缓存的扫描结果。"
                    )

                entries, last_error = _read_bss_with_retry(
                    handle, info, retries=retries, gap=RETRY_GAP,
                )
                if last_error:
                    failures.append((info, last_error))
                    notes.append(f"  [{info.description}] {last_error}")
                    continue

                rows = [row for row in (describe_bss(e) for e in entries) if row]
                notes.append(
                    f"  [{info.description}] 第 {round_index + 1} 轮读到 "
                    f"{len(entries)} 条原始记录，可用 {len(rows)} 条"
                )
                if not entries:
                    silent.append(info)
                elif not rows:
                    warnings.append(
                        f"网卡“{info.description}”返回了 {len(entries)} 条原始记录，"
                        "但都没能解析出频段（个别驱动上报的中心频率为空或异常）。"
                    )

                if rows:
                    collected.extend(rows)
                    used = info
                    break

            if collected:
                break
            if round_index < ADAPTER_ROUNDS - 1:
                time.sleep(ROUND_GAP)
    finally:
        handle.close()

    rows = _sort_rows(_dedupe(collected))

    # 全是硬错误、且没有任何一张网卡"成功但为空" → 这是故障，不是"周围没 AP"
    if not rows and failures and not silent:
        return _failure(_failure_message(failures), detail)

    if not rows:
        warnings.append(
            "本次没有扫描到任何 BSS。请确认 Wi-Fi 已打开、周围确实有 AP，或稍后重试。"
        )

    primary = used or ordered[0]
    payload = {
        "ok": True,
        "scanned_at": _now_iso(),
        "interface": {
            "name": primary.description,
            "state": primary.state_name,
            "current_ssid": connection.ssid if connection else "",
            "current_bssid": connection.bssid if connection else "",
        },
        "warnings": warnings,
        "bss": rows,
    }
    if diagnose:
        payload["diagnostics"] = notes
    return payload


def _failure(message: str, notes: Optional[List[str]] = None) -> dict:
    payload = {
        "ok": False,
        "scanned_at": _now_iso(),
        "interface": {"name": "", "state": "", "current_ssid": "", "current_bssid": ""},
        "warnings": [],
        "error": message,
        "bss": [],
    }
    if notes:
        payload["diagnostics"] = notes
    return payload
