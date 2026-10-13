"""802.11 信息元素（Information Element, IE）解析。

WlanGetNetworkBssList 返回的每条 WLAN_BSS_ENTRY 尾部带一段 IE 数据块，
取自信标帧 / 探测响应帧。本项目从这段数据里取出页面上需要展示的几列：

    信道   ← DS Parameter Set(3) → HT Operation(61) → 中心频率换算
    频宽   ← VHT Operation(192) → HE PHY 能力(255/35) → HT Operation/Capabilities
    PHY    ← EHT(255/106) / HE(255/35,36) / VHT(191,192) / HT(45,61) 能力元素
    加密   ← RSN(48) / 厂商专有 WPA(221) / Capability.Privacy

信号强度不在这里取：它直接来自 WLAN_BSS_ENTRY.lRssi。
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Set, Tuple

# --------------------------------------------------------------------------
# 元素 ID
# --------------------------------------------------------------------------

EID_SSID = 0
EID_DS_PARAMETER_SET = 3
EID_RSN = 48
EID_HT_CAPABILITIES = 45
EID_HT_OPERATION = 61
EID_VHT_CAPABILITIES = 191
EID_VHT_OPERATION = 192
EID_VENDOR_SPECIFIC = 221
EID_EXTENSION = 255

# EID 255 的扩展 ID
EXT_HE_CAPABILITIES = 35
EXT_HE_OPERATION = 36
EXT_HE_6GHZ_BAND_CAPABILITIES = 59
EXT_EHT_OPERATION = 106

OUI_RSN = b"\x00\x0f\xac"
OUI_WPA = b"\x00\x50\xf2"
WPA_OUI_TYPE = 1

# IE 数据块理论上从 offset 0 开始，个别驱动会把固定字段一起带进来，
# 这里准备几个候选起点，按"解析得最完整"的那个为准。
_START_CANDIDATES = (0, 12, 8, 4)

_RECOGNIZED = (
    EID_DS_PARAMETER_SET, EID_RSN, EID_HT_CAPABILITIES,
    EID_HT_OPERATION, EID_VHT_CAPABILITIES, EID_VHT_OPERATION, EID_EXTENSION,
)

# --------------------------------------------------------------------------
# 加密
# --------------------------------------------------------------------------

_CIPHER_SUITES = {
    0: "USE-GROUP",
    1: "WEP-40",
    2: "TKIP",
    4: "CCMP",
    5: "WEP-104",
    8: "GCMP",
    9: "GCMP-256",
}

# AKM suite → (代际, 是否企业级)
_AKM_SUITES = {
    1: ("WPA2", True),      # 802.1X
    2: ("WPA2", False),     # PSK
    3: ("WPA2", True),      # FT-802.1X
    4: ("WPA2", False),     # FT-PSK
    5: ("WPA2", True),      # 802.1X-SHA256
    6: ("WPA2", False),     # PSK-SHA256
    8: ("WPA3", False),     # SAE
    9: ("WPA3", False),     # FT-SAE
    11: ("WPA3", True),     # 802.1X-SuiteB
    12: ("WPA3", True),     # 802.1X-SHA384
    13: ("WPA3", True),     # FT-802.1X-SHA384
    18: ("OWE", False),     # 增强开放
    19: ("OWE", False),
}

# 展示顺序：让 "WPA2/WPA3" 这种过渡态自然排成人们习惯的写法
_FAMILY_ORDER = {"WPA2": 0, "WPA3": 1, "WPA": 2, "OWE": 3}

# 加密套件的展示优先级：优先展示更强的那个
_CIPHER_ORDER = {"CCMP": 0, "GCMP": 1, "GCMP-256": 2, "TKIP": 3, "WEP-40": 4, "WEP-104": 5}

_RSN_CAP_MFPR = 0x0040      # Management Frame Protection Required


class IeSet:
    """一条 BSS 的所有信息元素，按 Element ID 归组。"""

    __slots__ = ("_items",)

    def __init__(self, items: Optional[Dict[int, List[bytes]]] = None) -> None:
        self._items = items or {}

    def all(self, eid: int) -> List[bytes]:
        return self._items.get(eid, [])

    def first(self, eid: int) -> Optional[bytes]:
        items = self._items.get(eid)
        return items[0] if items else None

    def last(self, eid: int) -> Optional[bytes]:
        items = self._items.get(eid)
        return items[-1] if items else None

    def has(self, eid: int) -> bool:
        return bool(self._items.get(eid))

    def extension_ids(self) -> Set[int]:
        """所有扩展元素（EID 255）的扩展 ID。"""
        return {p[0] for p in self._items.get(EID_EXTENSION, []) if p}

    def extension(self, ext_id: int) -> Optional[bytes]:
        for payload in self._items.get(EID_EXTENSION, []):
            if payload and payload[0] == ext_id:
                return payload
        return None

    def __bool__(self) -> bool:
        return bool(self._items)


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------


def _walk(blob: bytes, start: int) -> Tuple[Dict[int, List[bytes]], int]:
    """从 start 开始顺序走一遍 TLV，遇到越界就停。返回 (元素表, 消耗到的位置)。"""
    items: Dict[int, List[bytes]] = {}
    index = start
    total = len(blob)
    while index + 2 <= total:
        eid = blob[index]
        length = blob[index + 1]
        end = index + 2 + length
        if end > total:
            break
        items.setdefault(eid, []).append(blob[index + 2:end])
        index = end
    return items, index


def parse_ies(blob: bytes) -> IeSet:
    """解析 IE 数据块。容错优先：解析不动就返回空集合，绝不抛异常。"""
    if not blob:
        return IeSet()

    best_items: Dict[int, List[bytes]] = {}
    best_rank: Optional[Tuple[int, int, int]] = None
    for start in _START_CANDIDATES:
        if start >= len(blob):
            continue
        items, consumed = _walk(blob, start)
        if not items and start != 0:
            continue
        starts_with_ssid = 1 if blob[start] == EID_SSID else 0
        recognized = sum(1 for eid in _RECOGNIZED if eid in items)
        rank = (starts_with_ssid, consumed, recognized)
        if best_rank is None or rank > best_rank:
            best_items, best_rank = items, rank
    return IeSet(best_items)


# --------------------------------------------------------------------------
# 频段 / 信道 / 频宽
# --------------------------------------------------------------------------


def band_of(freq_mhz: int) -> str:
    """按中心频率判断频段。返回 '2.4' / '5' / '6'。"""
    if freq_mhz < 2500:
        return "2.4"
    if freq_mhz < 5925:
        return "5"
    return "6"


def channel_from_frequency(freq_mhz: int) -> int:
    """中心频率 → 信道号。宽信道下这是"中心信道"，仅作兜底。"""
    if freq_mhz < 2500:
        channel = (freq_mhz - 2407) // 5
        return channel if 1 <= channel <= 14 else 0
    if freq_mhz < 5925:
        channel = (freq_mhz - 5000) // 5
        return channel if 32 <= channel <= 196 else 0
    channel = (freq_mhz - 5950) // 5
    return channel if 1 <= channel <= 253 else 0


def channel_of(ies: IeSet, freq_mhz: int, band: str) -> int:
    """取主信道号。

    40/80/160 MHz 时中心频率与主信道不是一回事，所以优先信标里明写的
    主信道（DS Parameter Set / HT Operation），都没有才退回频率换算。
    """
    ds = ies.last(EID_DS_PARAMETER_SET)
    if ds and len(ds) >= 1 and 1 <= ds[0] <= 200:
        return ds[0]

    ht_op = ies.last(EID_HT_OPERATION)
    if ht_op and len(ht_op) >= 1 and 1 <= ht_op[0] <= 200:
        return ht_op[0]

    return channel_from_frequency(freq_mhz)


def width_of(ies: IeSet) -> int:
    """取工作频宽（MHz）：20 / 40 / 80 / 160。"""
    vht_op = ies.last(EID_VHT_OPERATION)
    if vht_op:
        code = vht_op[0]
        if code == 1:
            return 80
        if code == 2:
            return 160
        if code == 3:
            return 160             # 80+80，对外按 160 展示
        # code == 0 表示沿用 HT 的 20 / 40

    he_cap = ies.extension(EXT_HE_CAPABILITIES)
    # HE Capabilities 载荷：[0]=扩展ID, [1:7]=MAC 能力, [7:18]=PHY 能力
    if he_cap and len(he_cap) >= 9:
        code = (he_cap[7] >> 1) & 0x03
        if code == 1:
            return 40
        if code == 2:
            return 80
        if code == 3:
            return 160

    ht_op = ies.last(EID_HT_OPERATION)
    if ht_op and len(ht_op) >= 2 and (ht_op[1] & 0x03) in (1, 3):
        return 40

    ht_cap = ies.last(EID_HT_CAPABILITIES)
    if ht_cap and len(ht_cap) >= 2:
        info = ht_cap[0] | (ht_cap[1] << 8)
        if ((info >> 1) & 0x03) in (1, 3):
            return 40

    return 20


# --------------------------------------------------------------------------
# PHY
# --------------------------------------------------------------------------

_PHY_RANK = {
    "802.11FHSS": 0, "802.11DSSS": 0, "802.11b": 1, "802.11g": 2, "802.11a": 2,
    "802.11n": 3, "802.11ac": 4, "802.11ad": 4, "802.11ax": 5, "802.11be": 6,
}


def _phy_from_ies(ies: IeSet) -> Optional[str]:
    extensions = ies.extension_ids()
    if EXT_EHT_OPERATION in extensions:
        return "802.11be"
    if EXT_HE_CAPABILITIES in extensions or EXT_HE_OPERATION in extensions:
        return "802.11ax"
    if ies.has(EID_VHT_CAPABILITIES) or ies.has(EID_VHT_OPERATION):
        return "802.11ac"
    if ies.has(EID_HT_CAPABILITIES) or ies.has(EID_HT_OPERATION):
        return "802.11n"
    return None


def phy_of(ies: IeSet, bss_phy_type: int, freq_mhz: int, phy_names: Dict[int, str]) -> str:
    """判定 PHY 类型。

    信标里的能力元素最准；WLAN_BSS_ENTRY.dot11BssPhyType 有些驱动填、有些
    不填，所以两个来源都取，选代际更高的那个 —— 只会更准确，不会更保守。
    """
    candidates: List[str] = []
    from_ies = _phy_from_ies(ies)
    if from_ies:
        candidates.append(from_ies)
    from_entry = phy_names.get(bss_phy_type)
    if from_entry:
        candidates.append(from_entry)

    if candidates:
        return max(candidates, key=lambda name: _PHY_RANK.get(name, -1))
    return "802.11a" if freq_mhz >= 5000 else "802.11g"


# --------------------------------------------------------------------------
# 加密
# --------------------------------------------------------------------------


def _parse_cipher_list(payload: bytes, index: int) -> Tuple[List[str], int]:
    """解析 (count, suite-list)，返回 (套件名列表, 新下标)。"""
    if index + 2 > len(payload):
        return [], index
    count = payload[index] | (payload[index + 1] << 8)
    index += 2
    suites: List[str] = []
    for _ in range(count):
        if index + 4 > len(payload):
            break
        suites.append(_CIPHER_SUITES.get(payload[index + 3], "未知"))
        index += 4
    return suites, index


def _parse_akm_list(payload: bytes, index: int) -> Tuple[List[str], List[bool], int]:
    if index + 2 > len(payload):
        return [], [], index
    count = payload[index] | (payload[index + 1] << 8)
    index += 2
    families: List[str] = []
    enterprises: List[bool] = []
    for _ in range(count):
        if index + 4 > len(payload):
            break
        info = _AKM_SUITES.get(payload[index + 3])
        if info:
            families.append(info[0])
            enterprises.append(info[1])
        index += 4
    return families, enterprises, index


def _parse_rsn(payload: bytes) -> Optional[dict]:
    """解析 RSN 元素（WPA2 / WPA3 / OWE）。"""
    if len(payload) < 8:
        return None
    index = 2                                       # 跳过 version
    group_cipher = _CIPHER_SUITES.get(payload[index + 3], "未知")
    index += 4
    pairwise, index = _parse_cipher_list(payload, index)
    families, enterprises, index = _parse_akm_list(payload, index)

    mfp_required = False
    if index + 2 <= len(payload):
        capabilities = payload[index] | (payload[index + 1] << 8)
        mfp_required = bool(capabilities & _RSN_CAP_MFPR)

    return {
        "families": families,
        "enterprises": enterprises,
        "pairwise": pairwise,
        "group": group_cipher,
        "mfp_required": mfp_required,
    }


def _parse_wpa(payload: bytes) -> Optional[dict]:
    """解析厂商专有 WPA（WPA1）元素。载荷已跳过 OUI + type。"""
    if len(payload) < 8:
        return None
    index = 2
    group_cipher = _CIPHER_SUITES.get(payload[index + 3], "未知")
    index += 4
    pairwise, index = _parse_cipher_list(payload, index)
    families, enterprises, index = _parse_akm_list(payload, index)
    return {
        "families": families or ["WPA"],
        "enterprises": enterprises,
        "pairwise": pairwise,
        "group": group_cipher,
        "mfp_required": False,
    }


def _format_ciphers(parsed: dict) -> str:
    ciphers = [c for c in parsed["pairwise"] if c not in ("USE-GROUP", "未知")]
    if not ciphers and parsed["group"] not in ("USE-GROUP", "未知"):
        ciphers = [parsed["group"]]
    if not ciphers:
        return ""
    unique = sorted(set(ciphers), key=lambda c: _CIPHER_ORDER.get(c, 99))
    return "+".join(unique)


def security_of(ies: IeSet, privacy: bool) -> str:
    """把加密配置压成页面上那一列字符串，例如 WPA2/WPA3-CCMP。

    取值顺序：RSN(48) → 厂商 WPA(221) → Capability.Privacy 推断 WEP → 开放。
    """
    parsed = None
    rsn = ies.last(EID_RSN)
    if rsn:
        parsed = _parse_rsn(rsn)

    if parsed is None:
        for payload in ies.all(EID_VENDOR_SPECIFIC):
            if len(payload) >= 4 and payload[:3] == OUI_WPA and payload[3] == WPA_OUI_TYPE:
                parsed = _parse_wpa(payload[4:])
                break

    if parsed is None:
        return "WEP" if privacy else "开放"

    families = sorted(
        set(parsed["families"]), key=lambda f: _FAMILY_ORDER.get(f, 99)
    )
    name = "/".join(families) if families else "WPA2"
    if any(parsed["enterprises"]):
        name += "-Enterprise"
    ciphers = _format_ciphers(parsed)
    if ciphers:
        name += "-" + ciphers
    return name


# --------------------------------------------------------------------------
# 一次性提取
# --------------------------------------------------------------------------


def describe(blob: bytes, *, capability: int, freq_mhz: int,
             bss_phy_type: int, phy_names: Dict[int, str]) -> Dict[str, object]:
    """把一段 IE 数据块整理成页面上要用的几列。"""
    ies = parse_ies(blob)
    band = band_of(freq_mhz)
    return {
        "band": band,
        "channel": channel_of(ies, freq_mhz, band),
        "width": width_of(ies),
        "phy": phy_of(ies, bss_phy_type, freq_mhz, phy_names),
        "security": security_of(ies, bool(capability & 0x0010)),
        "ie_count": sum(len(v) for v in ies._items.values()),
    }
