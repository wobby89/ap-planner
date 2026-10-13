"""Windows 原生 WLAN API（wlanapi.dll）的 ctypes 绑定。

只封装本项目用得到的五件事，刻意保持最小面积：

    WlanOpenHandle            打开 WLAN 客户端句柄
    WlanEnumInterfaces        枚举无线网卡
    WlanScan / 通知回调       触发一次扫描，并等待"扫描完成"通知
    WlanGetNetworkBssList     取回扫描到的 BSS 列表（含 IE 数据块）
    WlanQueryInterface        取回当前连接属性（SSID / BSSID）

其他 opcode、ACL、profile 管理一律不做。
"""

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import POINTER, Structure, byref, sizeof
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import List, Optional

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

WLAN_CLIENT_VERSION = 2            # Windows Vista 及以上
ERROR_SUCCESS = 0

WLAN_MAX_NAME_LENGTH = 256
DOT11_SSID_MAX_LENGTH = 32

# DOT11_BSS_TYPE
DOT11_BSS_TYPE_ANY = 3

# 接口状态 WLAN_INTERFACE_STATE
INTERFACE_STATE_NAMES = {
    0: "未就绪",
    1: "已连接",
    2: "已组建临时网络",
    3: "正在断开",
    4: "未连接",
    5: "正在关联",
    6: "正在发现",
    7: "正在认证",
}
INTERFACE_STATE_CONNECTED = 1

# WlanQueryInterface 的 opcode
WLAN_INTF_OPCODE_CURRENT_CONNECTION = 7

# 通知来源与通知码
WLAN_NOTIFICATION_SOURCE_ACM = 0x00000008
WLAN_NOTIFICATION_ACM_SCAN_COMPLETE = 7
WLAN_NOTIFICATION_ACM_SCAN_FAIL = 8

# DOT11_PHY_TYPE → 展示用名称
DOT11_PHY_NAMES = {
    1: "802.11FHSS",
    2: "802.11DSSS",
    4: "802.11a",
    5: "802.11b",
    6: "802.11g",
    7: "802.11n",
    8: "802.11ac",
    9: "802.11ad",
    10: "802.11ax",
    11: "802.11be",
}

# Windows 打开"定位服务"设置页的 URI，出错时直接告诉用户去哪开
LOCATION_SETTINGS_URI = "ms-settings:privacy-location"

# 常见 WLAN API 错误码，给出比 FormatError 更贴合本场景的解释。
#
# 错误码 5 的说明**很关键**：微软从 2023 年起（Win10 更新 / Win11）把 Wi-Fi 扫描
# 纳入了位置隐私管控 —— 用户没有授予"精确位置"权限时，下面这几个 API 一律返回
# ERROR_ACCESS_DENIED(5)：
#
#     WlanScan / WlanGetNetworkBssList / WlanGetAvailableNetworkList
#     WlanQueryInterface（opcode = wlan_intf_opcode_current_connection）
#
# 这是"换到别人电脑上就扫不到 Wi-Fi"最常见的原因，而且**提权完全没用**
# （连 netsh wlan show networks 都会直接提示需要位置权限）。所以这里绝对不能
# 再写"请以管理员身份运行"，那会把用户带偏。
# 参考：https://learn.microsoft.com/windows/win32/nativewifi/wi-fi-access-location-changes
WLAN_ERROR_HINTS = {
    5: "访问被拒绝。Windows 要求调用方拥有“精确位置”权限才允许扫描 Wi-Fi："
       "请打开“设置 → 隐私和安全性 → 位置”，先打开“定位服务”，"
       "再在下面的应用列表里允许本程序访问位置"
       f"（在地址栏输入 {LOCATION_SETTINGS_URI} 可直接跳转到该页面）。"
       "注意：以管理员身份运行并不能解决这个问题。",
    87: "参数无效。",
    1062: "WLAN AutoConfig（WlanSvc）服务未启动。请在“服务”中启动它。",
    1063: "WLAN 服务内部错误。",
    1168: "找不到指定的元素。",
    5023: "无线网卡已被禁用，请先在系统设置中打开 Wi-Fi。",
}

# 网卡驱动层（NDIS / dot11）的状态码，0x8034xxxx 这一段最常见的是
# "射频被关掉了"（飞行模式、硬件开关、Fn 快捷键）。
_NDIS_DOT11_PREFIX = 0x80340000

_WLANAPI = ctypes.WinDLL("wlanapi.dll")


class WlanApiError(RuntimeError):
    """WLAN API 调用失败。"""

    def __init__(self, api: str, code: int):
        self.api = api
        self.code = code
        self.hint = WLAN_ERROR_HINTS.get(code)
        message = f"{api} 调用失败（错误码 {code}）"
        if self.hint:
            message += f"：{self.hint}"
        super().__init__(message)


def format_error(code: int) -> str:
    """把 Win32 错误码整理成人能读的字符串。"""
    if code in WLAN_ERROR_HINTS:
        return WLAN_ERROR_HINTS[code]
    if code & 0xFFFF0000 == _NDIS_DOT11_PREFIX:
        return (f"网卡驱动状态码 0x{code:08X}，通常是 Wi-Fi 开关 / 飞行模式没打开，"
                "或网卡尚未就绪。等几秒重试；仍不行就关掉再打开一次 Wi-Fi。")
    try:
        return ctypes.FormatError(code).strip() or f"错误码 {code}"
    except Exception:                                   # pragma: no cover
        return f"错误码 {code}"


# SSID 按规定是 UTF-8，但国内不少路由器 / 智能家电用的是 GBK 字节。
# 按可靠性从高到低试，最后一个 latin-1 保证任何字节序列都能解出来。
_SSID_ENCODINGS = ("utf-8", "gb18030", "latin-1")


def decode_ssid(raw: bytes) -> str:
    for encoding in _SSID_ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")         # pragma: no cover


# --------------------------------------------------------------------------
# 结构体
# --------------------------------------------------------------------------


class GUID(Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __str__(self) -> str:
        d4 = "".join(f"{b:02X}" for b in self.Data4)
        return f"{{{self.Data1:08X}-{self.Data2:04X}-{self.Data3:04X}-{d4[:4]}-{d4[4:]}}}"


class DOT11_SSID(Structure):
    _fields_ = [
        ("uSSIDLength", wintypes.ULONG),
        ("ucSSID", ctypes.c_ubyte * DOT11_SSID_MAX_LENGTH),
    ]

    def text(self) -> str:
        raw = bytes(self.ucSSID[: max(0, min(self.uSSIDLength, DOT11_SSID_MAX_LENGTH))])
        return decode_ssid(raw)


class WLAN_RATE_SET(Structure):
    _fields_ = [
        ("uRateSetLength", wintypes.ULONG),
        ("usRateSet", ctypes.c_ushort * 126),
    ]


class WLAN_BSS_ENTRY(Structure):
    """一条 BSS 记录。布局与 wlanapi.h 一致，64 位下 sizeof == 360。"""

    _fields_ = [
        ("dot11Ssid", DOT11_SSID),
        ("uPhyId", wintypes.ULONG),
        ("dot11Bssid", ctypes.c_ubyte * 6),
        ("dot11BssType", ctypes.c_int),
        ("dot11BssPhyType", ctypes.c_int),
        ("lRssi", wintypes.LONG),
        ("uLinkQuality", wintypes.ULONG),
        ("bInRegDomain", ctypes.c_ubyte),
        ("usBeaconPeriod", ctypes.c_ushort),
        ("ullTimestamp", ctypes.c_ulonglong),
        ("ullHostTimestamp", ctypes.c_ulonglong),
        ("usCapabilityInformation", ctypes.c_ushort),
        ("ulChCenterFrequency", wintypes.ULONG),
        ("wlanRateSet", WLAN_RATE_SET),
        ("ulIeOffset", wintypes.ULONG),
        ("ulIeSize", wintypes.ULONG),
    ]


class WLAN_INTERFACE_INFO(Structure):
    _fields_ = [
        ("InterfaceGuid", GUID),
        ("strInterfaceDescription", ctypes.c_wchar * WLAN_MAX_NAME_LENGTH),
        ("isState", ctypes.c_int),
    ]


class WLAN_INTERFACE_INFO_LIST(Structure):
    _fields_ = [
        ("dwNumberOfItems", wintypes.DWORD),
        ("dwIndex", wintypes.DWORD),
        ("InterfaceInfo", WLAN_INTERFACE_INFO * 1),
    ]


class WLAN_ASSOCIATION_ATTRIBUTES(Structure):
    _fields_ = [
        ("dot11Ssid", DOT11_SSID),
        ("dot11BssType", ctypes.c_int),
        ("dot11Bssid", ctypes.c_ubyte * 6),
        ("dot11PhyType", ctypes.c_int),
        ("uDot11PhyIndex", wintypes.ULONG),
        ("wlanSignalQuality", wintypes.ULONG),
        ("ulRxRate", wintypes.ULONG),
        ("ulTxRate", wintypes.ULONG),
    ]


class WLAN_SECURITY_ATTRIBUTES(Structure):
    _fields_ = [
        ("bSecurityEnabled", wintypes.BOOL),
        ("bOneXEnabled", wintypes.BOOL),
        ("dot11AuthAlgorithm", ctypes.c_int),
        ("dot11CipherAlgorithm", ctypes.c_int),
    ]


class WLAN_CONNECTION_ATTRIBUTES(Structure):
    _fields_ = [
        ("isState", ctypes.c_int),
        ("wlanConnectionMode", ctypes.c_int),
        ("strProfileName", ctypes.c_wchar * WLAN_MAX_NAME_LENGTH),
        ("wlanAssociationAttributes", WLAN_ASSOCIATION_ATTRIBUTES),
        ("wlanSecurityAttributes", WLAN_SECURITY_ATTRIBUTES),
    ]


class WLAN_NOTIFICATION_DATA(Structure):
    _fields_ = [
        ("NotificationSource", wintypes.DWORD),
        ("NotificationCode", wintypes.DWORD),
        ("InterfaceGuid", GUID),
        ("dwDataSize", wintypes.DWORD),
        ("pData", ctypes.c_void_p),
    ]


NOTIFICATION_CALLBACK = ctypes.WINFUNCTYPE(
    None, POINTER(WLAN_NOTIFICATION_DATA), ctypes.c_void_p
)

# --------------------------------------------------------------------------
# 函数原型
# --------------------------------------------------------------------------

_WLANAPI.WlanOpenHandle.argtypes = [
    wintypes.DWORD, ctypes.c_void_p, POINTER(wintypes.DWORD), POINTER(wintypes.HANDLE)
]
_WLANAPI.WlanOpenHandle.restype = wintypes.DWORD

_WLANAPI.WlanCloseHandle.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
_WLANAPI.WlanCloseHandle.restype = wintypes.DWORD

_WLANAPI.WlanFreeMemory.argtypes = [ctypes.c_void_p]
_WLANAPI.WlanFreeMemory.restype = None

_WLANAPI.WlanEnumInterfaces.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, POINTER(POINTER(WLAN_INTERFACE_INFO_LIST))
]
_WLANAPI.WlanEnumInterfaces.restype = wintypes.DWORD

_WLANAPI.WlanScan.argtypes = [
    wintypes.HANDLE, POINTER(GUID), POINTER(DOT11_SSID), ctypes.c_void_p, ctypes.c_void_p
]
_WLANAPI.WlanScan.restype = wintypes.DWORD

_WLANAPI.WlanGetNetworkBssList.argtypes = [
    wintypes.HANDLE, POINTER(GUID), POINTER(DOT11_SSID), ctypes.c_int,
    wintypes.BOOL, ctypes.c_void_p, POINTER(ctypes.c_void_p),
]
_WLANAPI.WlanGetNetworkBssList.restype = wintypes.DWORD

_WLANAPI.WlanQueryInterface.argtypes = [
    wintypes.HANDLE, POINTER(GUID), ctypes.c_int, ctypes.c_void_p,
    POINTER(wintypes.DWORD), POINTER(ctypes.c_void_p), POINTER(wintypes.DWORD),
]
_WLANAPI.WlanQueryInterface.restype = wintypes.DWORD

_WLANAPI.WlanRegisterNotification.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, NOTIFICATION_CALLBACK,
    ctypes.c_void_p, ctypes.c_void_p, POINTER(wintypes.DWORD),
]
_WLANAPI.WlanRegisterNotification.restype = wintypes.DWORD


# --------------------------------------------------------------------------
# 数据类
# --------------------------------------------------------------------------


@dataclass
class InterfaceInfo:
    guid: GUID
    description: str
    state: int

    @property
    def state_name(self) -> str:
        return INTERFACE_STATE_NAMES.get(self.state, f"未知({self.state})")

    @property
    def is_connected(self) -> bool:
        return self.state == INTERFACE_STATE_CONNECTED


@dataclass
class BssEntry:
    """一条原始 BSS 记录。IE 解析留给 ie_parser 处理。"""

    ssid: str
    bssid: str
    rssi: int
    link_quality: int
    center_freq_khz: int
    phy_type: int
    bss_type: int
    capability: int
    beacon_period: int
    ie_offset: int
    ie_size: int
    ie_blob: bytes = b""

    @property
    def center_freq_mhz(self) -> int:
        return int(round(self.center_freq_khz / 1000.0))

    @property
    def privacy(self) -> bool:
        """Capability Information 的 Privacy 位。"""
        return bool(self.capability & 0x0010)


@dataclass
class CurrentConnection:
    ssid: str = ""
    bssid: str = ""
    signal_quality: int = 0
    fields: dict = field(default_factory=dict)


@dataclass
class ScanOutcome:
    """一次 WlanScan 调用的结果。

    以前这里只返回 bool，把 WlanScan 的错误码直接丢掉了 —— 于是"位置权限没开"
    和"周围确实没有 AP"在用户眼里长得一模一样（都是"扫不到"）。现在把错误码
    带出来，页面和诊断脚本才能说清到底卡在哪。
    """

    code: int = ERROR_SUCCESS            # WlanScan 的返回码，0 表示调用成功
    completed: bool = False              # 是否等到了"扫描完成"通知
    notification_registered: bool = True

    @property
    def ok(self) -> bool:
        """WlanScan 这个调用本身是否成功。"""
        return self.code == ERROR_SUCCESS

    @property
    def error(self) -> str:
        if self.ok:
            return ""
        return f"触发扫描失败（错误码 {self.code}）：{format_error(self.code)}"


# --------------------------------------------------------------------------
# 句柄
# --------------------------------------------------------------------------


class WlanHandle:
    """WlanOpenHandle 的上下文管理器。"""

    def __init__(self) -> None:
        self._handle = wintypes.HANDLE()
        self._negotiated = wintypes.DWORD()
        code = _WLANAPI.WlanOpenHandle(
            WLAN_CLIENT_VERSION, None, byref(self._negotiated), byref(self._handle)
        )
        if code != ERROR_SUCCESS:                       # 退回 Windows XP 的版本号再试一次
            code = _WLANAPI.WlanOpenHandle(
                1, None, byref(self._negotiated), byref(self._handle)
            )
        if code != ERROR_SUCCESS or not self._handle.value:
            raise WlanApiError("WlanOpenHandle", code)

    @property
    def raw(self) -> wintypes.HANDLE:
        return self._handle

    def close(self) -> None:
        if self._handle and self._handle.value:
            _WLANAPI.WlanCloseHandle(self._handle, None)
            self._handle = wintypes.HANDLE()

    def __enter__(self) -> "WlanHandle":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


# --------------------------------------------------------------------------
# 接口枚举
# --------------------------------------------------------------------------


def enum_interfaces(handle: WlanHandle) -> List[InterfaceInfo]:
    """枚举本机所有无线网卡。"""
    p_list = POINTER(WLAN_INTERFACE_INFO_LIST)()
    code = _WLANAPI.WlanEnumInterfaces(handle.raw, None, byref(p_list))
    if code != ERROR_SUCCESS:
        raise WlanApiError("WlanEnumInterfaces", code)

    try:
        count = p_list.contents.dwNumberOfItems
        if not count:
            return []
        base = ctypes.addressof(p_list.contents)
        offset = WLAN_INTERFACE_INFO_LIST.InterfaceInfo.offset
        array = (WLAN_INTERFACE_INFO * count).from_address(base + offset)
        return [
            InterfaceInfo(
                guid=item.InterfaceGuid,
                description=item.strInterfaceDescription.strip(),
                state=item.isState,
            )
            for item in array
        ]
    finally:
        _WLANAPI.WlanFreeMemory(p_list)


def pick_interfaces(interfaces: List[InterfaceInfo]) -> List[InterfaceInfo]:
    """按"最可能扫得出东西"的顺序排列网卡：已连接的排前面，其余按枚举顺序跟在后面。

    不少机器上 WlanEnumInterfaces 会返回不止一张网卡（物理网卡 + 虚拟网卡 /
    USB 网卡 / 被禁用的那张），以前只扫第一张，碰上第一张是虚拟网卡时就会
    "一张 AP 也扫不到"。所以这里返回完整顺序，由调用方逐张尝试。
    """
    connected = [info for info in interfaces if info.is_connected]
    rest = [info for info in interfaces if not info.is_connected]
    return connected + rest


def pick_interface(interfaces: List[InterfaceInfo]) -> Optional[InterfaceInfo]:
    """优选正在连接的那张网卡，否则用第一张。"""
    ordered = pick_interfaces(interfaces)
    return ordered[0] if ordered else None


def describe_interfaces(handle: WlanHandle) -> List[dict]:
    """给诊断输出用的网卡清单。"""
    return [
        {
            "index": index,
            "description": info.description,
            "state": info.state_name,
            "connected": info.is_connected,
            "guid": str(info.guid),
        }
        for index, info in enumerate(enum_interfaces(handle), 1)
    ]


# --------------------------------------------------------------------------
# 扫描
# --------------------------------------------------------------------------


class _ScanNotificationWatcher:
    """注册 ACM 通知，用于等"扫描完成"，避免死等固定秒数。

    注册失败（个别驱动 / 精简版系统不支持）时自动退化为定时等待。
    """

    def __init__(self, handle: WlanHandle, timeout: float = 12.0) -> None:
        self._handle = handle
        self._timeout = timeout
        self._event = threading.Event()
        self._callback = NOTIFICATION_CALLBACK(self._on_notification)
        self._registered = False

    def _on_notification(self, data, _context) -> None:
        try:
            if not data:
                return
            note = data.contents
            if note.NotificationSource != WLAN_NOTIFICATION_SOURCE_ACM:
                return
            if note.NotificationCode in (
                WLAN_NOTIFICATION_ACM_SCAN_COMPLETE,
                WLAN_NOTIFICATION_ACM_SCAN_FAIL,
            ):
                self._event.set()
        except Exception:                               # pragma: no cover
            pass

    def __enter__(self) -> "_ScanNotificationWatcher":
        previous = wintypes.DWORD()
        code = _WLANAPI.WlanRegisterNotification(
            self._handle.raw, WLAN_NOTIFICATION_SOURCE_ACM, True,
            self._callback, None, None, byref(previous),
        )
        self._registered = code == ERROR_SUCCESS
        return self

    @property
    def registered(self) -> bool:
        """通知是否注册成功。注册失败时只能退化为定时等待。"""
        return self._registered

    def wait(self) -> bool:
        if self._registered:
            return self._event.wait(self._timeout)
        return False

    def __exit__(self, *exc_info) -> None:
        if self._registered:
            try:
                _WLANAPI.WlanRegisterNotification(
                    self._handle.raw, 0, True, None, None, None, None
                )
            except Exception:                           # pragma: no cover
                pass


def request_scan(handle: WlanHandle, interface: InterfaceInfo,
                 *, wait: bool = True, timeout: float = 12.0) -> ScanOutcome:
    """让指定网卡扫描一次。

    WlanScan 是异步的：调用返回并不代表结果已经就绪，所以要么等通知，
    要么自己 sleep。Windows 侧对扫描频率有限流，调用失败不视为致命错误，
    后续仍可以读到上一次的缓存结果 —— 但**失败原因要带回去**（例如位置权限
    不足会返回 ERROR_ACCESS_DENIED），不能吞掉。
    """
    with _ScanNotificationWatcher(handle, timeout) as watcher:
        registered = watcher.registered
        code = _WLANAPI.WlanScan(
            handle.raw, byref(interface.guid), None, None, None
        )
        if code != ERROR_SUCCESS:
            # 权限不足 / 扫描被限流 / 网卡未就绪：把错误码原样带出去
            return ScanOutcome(code=code, completed=False,
                               notification_registered=registered)
        if not wait:
            return ScanOutcome(code=ERROR_SUCCESS, completed=True,
                               notification_registered=registered)
        if watcher.wait():
            return ScanOutcome(code=ERROR_SUCCESS, completed=True,
                               notification_registered=registered)

    # 通知没等到，给驱动一点时间把结果写进缓存
    time.sleep(2.0)
    return ScanOutcome(code=ERROR_SUCCESS, completed=False,
                       notification_registered=registered)


# --------------------------------------------------------------------------
# BSS 列表
# --------------------------------------------------------------------------

_IE_SIZE_SANITY_LIMIT = 8 * 1024


def get_bss_list(handle: WlanHandle, interface: InterfaceInfo) -> List[BssEntry]:
    """读取该网卡当前可见的全部 BSS（等价于 netsh wlan show networks 的底层数据）。"""
    p_list = ctypes.c_void_p()
    code = _WLANAPI.WlanGetNetworkBssList(
        handle.raw, byref(interface.guid),
        None,                       # 不过滤 SSID
        DOT11_BSS_TYPE_ANY,
        False,                      # 不限制加密类型
        None,
        byref(p_list),
    )
    if code != ERROR_SUCCESS:
        raise WlanApiError("WlanGetNetworkBssList", code)
    if not p_list.value:
        return []

    try:
        header = ctypes.cast(p_list, POINTER(wintypes.DWORD))
        total_size = int(header[0])
        count = int(header[1])
        if not count:
            return []

        entry_offset = ctypes.sizeof(wintypes.DWORD) * 2
        array = (WLAN_BSS_ENTRY * count).from_address(p_list.value + entry_offset)

        entries: List[BssEntry] = []
        for item in array:
            ssid = item.dot11Ssid.text()
            bssid = ":".join(f"{b:02X}" for b in item.dot11Bssid)
            blob = _read_ie_blob(p_list.value, total_size, item, entry_offset)
            entries.append(
                BssEntry(
                    ssid=ssid,
                    bssid=bssid,
                    rssi=int(item.lRssi),
                    link_quality=int(item.uLinkQuality),
                    center_freq_khz=int(item.ulChCenterFrequency),
                    phy_type=int(item.dot11BssPhyType),
                    bss_type=int(item.dot11BssType),
                    capability=int(item.usCapabilityInformation),
                    beacon_period=int(item.usBeaconPeriod),
                    ie_offset=int(item.ulIeOffset),
                    ie_size=int(item.ulIeSize),
                    ie_blob=blob,
                )
            )
        return entries
    finally:
        _WLANAPI.WlanFreeMemory(p_list)


def _read_ie_blob(list_address: int, total_size: int,
                  entry: WLAN_BSS_ENTRY, entry_offset: int) -> bytes:
    """把 WLAN_BSS_ENTRY 尾部的 IE 数据块读出来。

    ulIeOffset 是相对本条 entry 起点的偏移；读之前做一次边界校验，
    越界就退回"紧跟在结构体之后"这一经验位置，避免踩到非法内存。
    """
    size = int(entry.ulIeSize)
    if size <= 0 or size > _IE_SIZE_SANITY_LIMIT:
        return b""

    entry_address = ctypes.addressof(entry)
    candidates = [int(entry.ulIeOffset), ctypes.sizeof(WLAN_BSS_ENTRY)]
    list_end = list_address + max(total_size, entry_offset)

    for offset in candidates:
        start = entry_address + offset
        if offset < 0 or start + size > list_end:
            continue
        try:
            blob = ctypes.string_at(start, size)
        except (OSError, ValueError):                   # pragma: no cover
            continue
        if blob:
            return blob
    return b""


# --------------------------------------------------------------------------
# 当前连接
# --------------------------------------------------------------------------


def query_current_connection(handle: WlanHandle,
                             interface: InterfaceInfo) -> Optional[CurrentConnection]:
    """读取当前连接属性；未连接时返回 None。"""
    size = wintypes.DWORD()
    p_data = ctypes.c_void_p()
    opcode_type = wintypes.DWORD()
    code = _WLANAPI.WlanQueryInterface(
        handle.raw, byref(interface.guid), WLAN_INTF_OPCODE_CURRENT_CONNECTION,
        None, byref(size), byref(p_data), byref(opcode_type),
    )
    if code != ERROR_SUCCESS or not p_data.value:
        return None
    if size.value < ctypes.sizeof(WLAN_CONNECTION_ATTRIBUTES):
        _WLANAPI.WlanFreeMemory(p_data)
        return None

    try:
        attrs = ctypes.cast(
            p_data, POINTER(WLAN_CONNECTION_ATTRIBUTES)
        ).contents
        assoc = attrs.wlanAssociationAttributes
        return CurrentConnection(
            ssid=assoc.dot11Ssid.text(),
            bssid=":".join(f"{b:02X}" for b in assoc.dot11Bssid),
            signal_quality=int(assoc.wlanSignalQuality),
        )
    finally:
        _WLANAPI.WlanFreeMemory(p_data)
