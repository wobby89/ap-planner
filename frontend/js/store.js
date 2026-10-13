/* ==========================================================================
   全局状态与通用工具
   所有文件都是普通 script，共享同一个全局作用域，按 index.html 里的顺序加载。
   ========================================================================== */

const state = {
  selected: new Set(),      // 用户勾选为本公司的 BSSID
  apName: {},               // BSSID -> 归属 AP 的名字（名字相同 = 同一台 AP）
  knownAps: new Set(),      // 见过的 AP 名字（新 AP 默认开 2.4G）
  band24: new Set(),        // 哪些 AP 开 2.4G
  adj: {},                  // "AP1|AP2" -> true/false
  touched: new Set(),       // 用户手动核对过的配对
  cur: 0,                   // 当前正在询问第几台 AP
  done: new Set(),          // 已经确认过的 AP
  n: 0
};

const $  = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));

/* 「归属哪台 AP」完全由用户决定，不从 BSSID 尾字节去猜 */
const apKeyOf = b => state.apName[b.bssid] || null;
/* 「是不是本公司」= 用户勾没勾，不看 OUI */
const isMine  = b => state.selected.has(b.bssid);
const txt = s => s === '' ? '(隐藏 SSID)' : s;

/* dBm -> 条形高度（-95 dBm 为 0%，-30 dBm 为 100%） */
const hOf = rssi => Math.max(6, Math.min(100, Math.round((rssi + 95) / 65 * 100)));

let MY_APS = [];

/* 本公司 AP 名字列表，按 AP 编号自然排序（AP2 排在 AP10 前面） */
function refreshAps() {
  const out = [];
  SCAN.forEach(b => {
    if (!isMine(b)) return;
    const n = apKeyOf(b);
    if (n && !out.includes(n)) out.push(n);
  });
  out.sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
  MY_APS = out;
  state.n = out.length;

  // 新出现的 AP 默认开 2.4G（先反映"当前就是这样"），消失的清掉
  out.forEach(k => {
    if (!state.knownAps.has(k)) { state.knownAps.add(k); state.band24.add(k); }
  });
  Array.from(state.band24).forEach(k => { if (!out.includes(k)) state.band24.delete(k); });
  Array.from(state.done).forEach(k => { if (!out.includes(k)) state.done.delete(k); });
  Object.keys(state.adj).forEach(k => {
    if (!k.split('|').every(x => out.includes(x))) { delete state.adj[k]; state.touched.delete(k); }
  });
  if (state.cur > out.length - 1) state.cur = Math.max(0, out.length - 1);
}

/* ---------- BSSID 的"身份" ----------
   一台 AP 的 2.4G 与 5G 是两条不同的 BSSID，只有中间几个字节不一样，尾部才是
   它的身份（这就是"同一台 AP 被当成两台"的根源）。 */
const bssidParts = b => String(b.bssid).toUpperCase().split(':');
const bssidTail  = b => bssidParts(b).slice(-2).join(':');

/* 分组键。'single' 直接拿整个 BSSID —— 同一台 AP 两个频段共用一个 BSSID 时
   仍然会合并成一台。另外两条按"同 SSID + BSSID 的某一段"分组。
   SSID 为空（隐藏 BSS）时用 * 占位，不参与 SSID 比较 —— 它往往就是同一台 AP
   上没有名字的那条 BSS，尾部一样就当同一台。 */
function groupKeyOf(b, rule) {
  if (rule === 'single') return b.bssid;
  const part = rule === 'prefix5' ? bssidParts(b).slice(0, 5).join(':') : bssidTail(b);
  return (b.ssid ? b.ssid : '*') + '|' + part;
}

const keyPartOf = key => key.slice(key.lastIndexOf('|') + 1);

/* 两条 BSS 是不是同一台 AP */
function sameGroup(a, b, rule) {
  if (rule === 'single') return a.bssid === b.bssid;
  const ka = groupKeyOf(a, rule), kb = groupKeyOf(b, rule);
  if (ka === kb) return true;
  /* SSID 有一条是隐藏的：只看 BSSID 那一段 */
  return (!a.ssid || !b.ssid) && keyPartOf(ka) === keyPartOf(kb);
}

/* 按当前规则重算分组，并把名字重编号为 AP1..APn（用户之前手改的名字会被重置） */
function regroup(rule) {
  const groups = [];
  SCAN.filter(isMine).forEach(b => {
    const g = groups.find(members => sameGroup(members[0], b, rule));
    if (g) g.push(b); else groups.push([b]);
  });
  const next = {};
  groups.forEach((members, i) => members.forEach(b => next[b.bssid] = 'AP' + (i + 1)));
  state.apName = next;
}

/* 分组 / 勾选变化后重置相邻关系：不是清空，而是重新给一版保守的推测值。
   清空会变成"默认全部不相邻"，那是危险方向 —— 相邻报少了会让规划错误地
   复用同信道；报多了只会让规划更保守。所以要"宁多勿少"。 */
function resetAdjacency() {
  state.adj = guessAdjacency();
  state.touched.clear();
  state.done.clear();
  state.cur = 0;
}

function nextApName() {
  const used = new Set(Object.values(state.apName));
  let i = 1;
  while (used.has('AP' + i)) i++;
  return 'AP' + i;
}

/* 勾上一条 BSS 时：按当前规则找它的"兄弟射频"，找到就归同一台 AP，否则新开一台 */
function assignOne(b) {
  const rule = $('#fGroup').value;
  const sib = SCAN.find(o => o !== b && isMine(o) &&
    state.apName[o.bssid] && sameGroup(o, b, rule));
  state.apName[b.bssid] = sib ? state.apName[sib.bssid] : nextApName();
}

/* 取这台 AP 在某频段上的那条 BSS（没有该射频时返回 null）。
   同一频段有好几条 BSS 时（隐藏 SSID 那条、多 SSID 那条）取信号最强的 ——
   那才是终端实际关联的主 BSS。 */
function bssOf(ap, band) {
  const list = SCAN.filter(x => isMine(x) && apKeyOf(x) === ap && x.band === band);
  return list.length ? list.reduce((a, b) => (b.rssi > a.rssi ? b : a)) : null;
}

/* 用最强的一条 BSS 近似「这台 AP 离扫描点多近」 */
function rssiOf(ap) {
  const list = SCAN.filter(b => isMine(b) && apKeyOf(b) === ap);
  return list.length ? Math.max(...list.map(x => x.rssi)) : -100;
}

function chOf(ap, band) {
  const b = bssOf(ap, band || '5');
  return b ? b.ch : null;
}

function deltaTag(a, b) {
  const d = Math.abs(rssiOf(a) - rssiOf(b));
  return d <= 6 ? '两者信号几乎一样强' : '信号相差 ' + d + ' dB';
}

/* ---------- 共用的小组件 ---------- */

/* 横幅里那个小圆图标 */
function icon(kind) {
  const color = kind === 'ok' ? '#067647'
              : kind === 'warn' ? '#b45309'
              : kind === 'bad' ? '#b42318'
              : '#2563eb';
  const path = kind === 'ok'
    ? 'M8 1.5a6.5 6.5 0 100 13 6.5 6.5 0 000-13zm3.2 4.9l-4 4.2-2.2-2.2.9-.9 1.3 1.3 3.1-3.3.9.9z'
    : kind === 'warn' || kind === 'bad'
    ? 'M8 1.5a6.5 6.5 0 100 13 6.5 6.5 0 000-13zM7.3 4.5h1.4v5H7.3v-5zm0 6.2h1.4v1.4H7.3v-1.4z'
    : 'M8 1.5a6.5 6.5 0 100 13 6.5 6.5 0 000-13zM7.3 7h1.4v5H7.3V7zm0-2.5h1.4v1.4H7.3V4.5z';
  return `<svg class="ico" viewBox="0 0 16 16" fill="${color}"><path d="${path}"/></svg>`;
}

function banner(kind, html) {
  return `<div class="banner ${kind}">${icon(kind)}<div>${html}</div></div>`;
}

/* 用户输入被拼进 innerHTML 的地方统一转义，避免 SSID 里的特殊字符搞坏页面 */
function escapeHtml(value) {
  return String(value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

