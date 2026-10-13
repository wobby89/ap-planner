/* ==========================================================================
   数据来源
   页面上每一行 = 一条 BSS，字段表与 index.html 的表格列一一对应。
   数据有两个来源：

     1. data/scan.js   命令行 `python backend/cli.py` 导出的快照，
                        直接双击 index.html 也能看到真实数据。
     2. /api/scan      backend/server.py 提供的实时接口，可随时重扫。

   两边字段名一致，这里统一转成页面内部用的短名字（ch / freq / w / sec）。
   ========================================================================== */

const SCAN = [];                       // 全量 BSS，页面所有计算都基于它

let INTERFACE = { name: '', state: '', current_ssid: '', current_bssid: '' };
let SCANNED_AT = '';
let SCAN_WARNINGS = [];
let SCAN_ERROR = '';
let SCAN_SOURCE = 'snapshot';          // 'live' | 'snapshot'

/* 后端地址。页面由本地服务打开时走相对路径，
   直接双击打开时退回固定端口（服务端开了 CORS）。 */
const API_ENDPOINTS = ['api/scan', 'http://127.0.0.1:8765/api/scan'];

const EMPTY_INTERFACE = { name: '', state: '', current_ssid: '', current_bssid: '' };

/* 后端的字段名 -> 页面内部字段名 */
function normalizeRow(row) {
  return {
    ssid:  row.ssid || '',
    bssid: row.bssid || '',
    band:  row.band || '',
    ch:    row.channel || 0,
    freq:  row.frequency || 0,
    w:     row.width || 0,
    rssi:  typeof row.rssi === 'number' ? row.rssi : 0,
    phy:   row.phy || '',
    sec:   row.security || ''
  };
}

function applyScanPayload(payload, source) {
  SCAN.length = 0;
  (payload && payload.bss ? payload.bss : []).forEach(row => SCAN.push(normalizeRow(row)));

  INTERFACE    = (payload && payload.interface) || Object.assign({}, EMPTY_INTERFACE);
  SCANNED_AT   = (payload && payload.scanned_at) || '';
  SCAN_WARNINGS = (payload && payload.warnings) || [];
  SCAN_ERROR   = (payload && payload.error) || '';
  SCAN_SOURCE  = source;

  refreshBandFilter();
}

/* 读取快照：由 index.html 里的 <script src="data/scan.js"> 提前塞好 */
function loadSnapshot() {
  try {
    if (window.__WLAN_SCAN__) {
      applyScanPayload(window.__WLAN_SCAN__, 'snapshot');
      return true;
    }
  } catch (err) {
    SCAN_ERROR = '快照数据解析失败：' + err.message;
  }
  SCAN_ERROR = '读不到扫描数据。请先运行 python backend/cli.py 生成快照，'
             + '或启动 python backend/server.py 后刷新页面。';
  return false;
}

/* 请求实时扫描。fresh=true 表示重新扫描一次（会慢几秒） */
async function fetchLiveScan(fresh) {
  const query = fresh ? '?fresh=1&write=1' : '?fresh=0';
  for (const endpoint of API_ENDPOINTS) {
    try {
      const response = await fetch(endpoint + query, { cache: 'no-store' });
      if (!response.ok) continue;
      const payload = await response.json();
      if (payload && typeof payload.ok !== 'undefined') return payload;
    } catch (err) {
      /* 这个地址不通，试下一个 */
    }
  }
  return null;
}

/* ---------- 频段筛选下拉框：按实际扫到的频段生成 ---------- */
const BAND_ORDER = ['2.4', '5', '6'];

function refreshBandFilter() {
  const select = $('#fBand');
  if (!select) return;

  const bands = [];
  SCAN.forEach(b => { if (b.band && !bands.includes(b.band)) bands.push(b.band); });
  bands.sort((a, b) => BAND_ORDER.indexOf(a) - BAND_ORDER.indexOf(b));

  const previous = select.value || 'all';
  select.innerHTML = '<option value="all">全部频段</option>'
    + bands.map(b => `<option value="${b}">仅 ${bandLabel(b)}</option>`).join('');

  const stillThere = Array.from(select.options).some(o => o.value === previous);
  select.value = stillThere ? previous : 'all';
}

/* ---------- 顶部数据来源标签 ---------- */
function renderDataTag() {
  const el = $('#dataTag');
  if (!el) return;

  if (SCAN_ERROR) {
    el.className = 'src-tag fail';
    el.textContent = '扫描不可用';
    return;
  }
  const stamp = SCANNED_AT ? SCANNED_AT.replace('T', ' ').slice(5, 16) : '';
  el.className = 'src-tag ' + (SCAN_SOURCE === 'live' ? 'live' : 'snapshot');
  el.textContent = (SCAN_SOURCE === 'live' ? '实时扫描数据' : '扫描快照')
                 + (stamp ? ' · ' + stamp : '');
}

/* ---------- 扫描层面的提示横幅 ---------- */
function renderScanNotice() {
  const box = $('#scanNotice');
  if (!box) return;

  const blocks = [];
  if (SCAN_ERROR) {
    blocks.push(banner('bad', '扫描不可用：' + SCAN_ERROR));
  }
  SCAN_WARNINGS.forEach(text => blocks.push(banner('warn', text)));

  if (!SCAN_ERROR && SCAN_SOURCE === 'snapshot') {
    blocks.push(banner('info',
      '当前展示的是命令行导出的扫描快照。点右上角「重新扫描」可以立刻采集实时数据。'));
  }
  if (!SCAN_ERROR && !SCAN.length) {
    blocks.push(banner('warn',
      '本次没有扫描到任何 BSS。请确认 Wi-Fi 已打开，然后重新扫描。'));
  }
  box.innerHTML = blocks.join('');
}

/* ---------- 第一步的统计块 ---------- */
function renderIfaceStat() {
  const el = $('#statIface');
  if (!el) return;
  el.textContent = INTERFACE.name || '未知无线接口';
  el.title = [INTERFACE.name, INTERFACE.state ? '状态：' + INTERFACE.state : '']
    .filter(Boolean).join(' · ');
}

/* 头一次拿到数据时，用"当前连着的网络"预填过滤条件 —— 那通常就是本公司的 AP。
   只预填 SSID：同一台 AP 的 2.4G / 5G 用的是同一个 SSID，一条 SSID 前缀就能把
   两个频段的射频一起圈进来。
   这里**不**预填 OUI —— 厂商给双频 BSSID 分配的前 3 字节经常是不同的
   （实测 2.4G 是 58:B4:BB:…，5G 是 5A:B4:BB:…），按 OUI 圈会把 2.4G 射频整片漏掉，
   结果就是"每台 AP 只剩 5G"。需要连隐藏 SSID 的 BSS 一起圈时，再手动填 OUI。 */
let filtersPrefilled = false;

/* #fSsid 里的值是不是"自动预填的"。用户一旦自己敲过，就再也不覆盖它。 */
let ssidAutoFilled = false;

function prefillFilters() {
  if (filtersPrefilled) return;
  filtersPrefilled = true;
  resyncSsidFilter();
}

/* 让 SSID 过滤框跟着"当前连着的网络"走。

   两种情况下才改写输入框：本来就是空的；或者里面是上一次自动填进去的**另一个**
   网络。后者是给"换到别人电脑上"准备的 —— 页面里可能先加载了打包机带过来的
   快照（当前网络是甲），随后实时数据回来（当前网络是乙），这时必须把过滤条件
   从甲改到乙，否则一条都圈不上。用户自己敲的内容永远不覆盖。 */
function resyncSsidFilter() {
  const ssidInput = $('#fSsid');
  if (!ssidInput) return;

  const live = INTERFACE.current_ssid;
  if (!live) return;

  const empty = !ssidInput.value;
  const staleAuto = ssidAutoFilled && ssidInput.value !== live;
  if (empty || staleAuto) {
    ssidInput.value = live;
    ssidAutoFilled = true;
  }
}
