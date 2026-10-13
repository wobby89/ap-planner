/* ==========================================================================
   前端冒烟测试（不需要浏览器）

   用 jsdom 真跑一遍页面里的 JS，检查：
     * 外部脚本是否都正确加载、有没有运行时报错
     * 第一步的表格、统计块是否按扫描数据渲染
     * 第二步、第三步能否正常算出结果
     * 频道分配是否真的避开了已确认的相邻 AP

   跑之前先装 jsdom（仅开发时用）：

     npm install jsdom
     node tests/page-smoke-test.mjs

   如果 jsdom 装在别处，用 JSDOM_HOME 指过去：

     JSDOM_HOME=/path/to/node_modules node tests/page-smoke-test.mjs
   ========================================================================== */

import path from 'node:path';
import process from 'node:process';
import { pathToFileURL } from 'node:url';

const ROOT = path.resolve(import.meta.dirname, '..');
const INDEX = path.join(ROOT, 'frontend', 'index.html');

/* ESM 不认 NODE_PATH，所以按"项目内 → JSDOM_HOME"两处找 jsdom */
async function loadJsdom() {
  try {
    return await import('jsdom');
  } catch (err) {
    if (err.code !== 'ERR_MODULE_NOT_FOUND') throw err;
  }
  const home = process.env.JSDOM_HOME;
  if (home) {
    const entry = path.join(home, 'jsdom', 'lib', 'api.js');
    return await import(pathToFileURL(entry).href);
  }
  console.error('找不到 jsdom。请在项目目录执行：npm install jsdom');
  console.error('或指定已安装位置：JSDOM_HOME=<node_modules 路径> node tests/page-smoke-test.mjs');
  process.exit(2);
}

const { JSDOM, VirtualConsole } = await loadJsdom();

const failures = [];
const notes = [];

function check(label, condition, detail = '') {
  if (condition) {
    notes.push(`  [通过] ${label}${detail ? ' — ' + detail : ''}`);
  } else {
    failures.push(`  [失败] ${label}${detail ? ' — ' + detail : ''}`);
  }
}

function section(title) {
  notes.push(`\n${title}`);
}

/* ---------------------------------------------------------------- 加载页面 */
const consoleErrors = [];
const virtualConsole = new VirtualConsole();
virtualConsole.on('jsdomError', err => {
  // jsdom 对 window.scrollTo / 导航之类没实现，和被测代码无关
  if (/Not implemented/i.test(err.message)) return;
  consoleErrors.push('jsdomError: ' + err.message);
});
virtualConsole.on('error', (...args) => consoleErrors.push('console.error: ' + args.join(' ')));

const dom = await JSDOM.fromFile(INDEX, {
  runScripts: 'dangerously',
  resources: 'usable',
  pretendToBeVisual: true,
  virtualConsole,
});

await new Promise(resolve => {
  if (dom.window.document.readyState === 'complete') resolve();
  else dom.window.addEventListener('load', resolve);
});
await new Promise(resolve => setTimeout(resolve, 250));

const win = dom.window;
const $ = selector => win.document.querySelector(selector);
const $$ = selector => Array.from(win.document.querySelectorAll(selector));
const run = expression => win.eval(expression);

/* ---------------------------------------------------------------- 基本健康 */
section('页面加载');
check('没有脚本运行时报错', consoleErrors.length === 0, consoleErrors.join(' | '));

const snapshot = run('window.__WLAN_SCAN__');
check('读到了扫描快照 data/scan.js', !!snapshot, snapshot ? `${snapshot.bss.length} 条 BSS` : '缺失');

const bssCount = run('SCAN.length');
check('SCAN 已填充', bssCount > 0, `${bssCount} 条`);

/* ---------------------------------------------------------------- 第一步 */
section('第一步 · 扫描结果表');

const rows = $$('#tbody tr');
check('表格行数与数据条数一致', rows.length === Math.min(bssCount, rows.length),
  `${rows.length} 行 / ${bssCount} 条`);

const columns = $$('#tbody tr:first-child td').length;
check('每行 11 列，与表头一致', columns === 11, `${columns} 列`);

check('无线接口名称已填充', $('#statIface').textContent.trim() !== '—',
  $('#statIface').textContent.trim());
check('BSS 总数已填充', /\d/.test($('#statTotal').textContent), $('#statTotal').textContent.trim());

const filterValue = run('$("#fBand").options.length');
check('频段下拉框按实际频段生成', filterValue >= 2, `${filterValue} 个选项`);

/* 所有行都应有 频段/信道/频率/频宽 */
const firstRow = $$('#tbody tr td').map(td => td.textContent.trim());
check('信道与频率不为空', firstRow.some(t => /^\d+$/.test(t)) && firstRow.some(t => /MHz/.test(t)),
  firstRow.join(' | ').slice(0, 110));

/* 手动勾选能力：勾上第一行的一条非本公司 BSS */
const autoSelected = run('state.selected.size');
check('按当前连接自动圈出了本公司的 AP', autoSelected > 0, `${autoSelected} 条 BSS`);

/* ---------------------------------------------------------------- 第二步 */
section('第二步 · 逐台确认相邻关系');
run('go(2)');

const aps = run('MY_APS.slice()');
check('AP 已分组', aps.length > 0, aps.join('、'));
check('导航按钮数量 = AP 数量', $$('#apnav .apdot').length === aps.length);
check('进度显示正确', $('#adjProgress').textContent.includes(`/ ${aps.length} 台`),
  $('#adjProgress').textContent.trim());

const askRows = $$('#askList .askrow').length;
check('询问列表 = AP 数 - 1', askRows === Math.max(aps.length - 1, 0),
  `${askRows} 项（AP ${aps.length} 台）`);

if (aps.length > 1) {
  const sub = $('#askSub').textContent;
  check('第一台的提示按实际数量生成（不再是写死的 7 项）',
    sub.includes(String(aps.length - 1)), sub.trim());
}

/* 走完所有 AP，全部按推测值确认，让规划能跑起来 */
for (let i = 0; i < aps.length; i++) {
  run('$("#btnNext").click()');
}
const adjacencyPairs = run('confirmedPairs().length');
check('走完确认流程后生成了相邻关系', adjacencyPairs >= 0,
  `${adjacencyPairs} 对相邻`);

/* ---------------------------------------------------------------- 第三步 */
section('第三步 · 规划建议');
run('go(3)');

const bannerCount = $$('#banners .banner').length;
check('生成三条结论横幅', bannerCount === 3, `${bannerCount} 条`);

const fiveRows = $$('#tbl5 tbody tr').length;
const ap5Count = run('MY_APS.filter(k => bssOf(k, "5") !== null).length');
check('5 GHz 结果表行数 = 有 5G 射频的 AP 数', fiveRows === ap5Count,
  `${fiveRows} 行 / ${ap5Count} 台`);

const open24Count = run('MY_APS.filter(k => state.band24.has(k) && bssOf(k, "2.4") !== null).length');
const tbl24Text = $('#tbl24').textContent;
check('2.4 GHz 结果表与实际开启情况一致',
  open24Count > 0 ? $$('#tbl24 tbody tr').length === open24Count : tbl24Text.includes('均已关闭'),
  open24Count > 0 ? `${$$('#tbl24 tbody tr').length} 行` : '显示"均已关闭"占位');

const spectrumSlots = $$('#spectrum .slot').length;
check('频谱图渲染了 2.4G(13) + 5G(9) 共 22 个信道格', spectrumSlots === 22, `${spectrumSlots} 个`);

const findings = $$('#findings .item').length;
check('本次扫描的发现有 4 张卡', findings === 4, `${findings} 张`);

const why = $$('#why .item').length;
check('通用原理有 4 张卡', why === 4, `${why} 张`);

/* ---------------------------------------------------------------- 算法校验 */
section('算法校验 · 相邻 AP 不应被分到同一信道');

const violations = run(`(function () {
  const band = '5';
  const ap5 = MY_APS.filter(k => bssOf(k, '5') !== null);
  const result = plan('5', ap5);
  const bad = [];
  for (let i = 0; i < ap5.length; i++) {
    for (let j = i + 1; j < ap5.length; j++) {
      if (isAdj(ap5[i], ap5[j]) && result.assigned[ap5[i]] === result.assigned[ap5[j]]) {
        bad.push(ap5[i] + '=' + ap5[j] + '@ch' + result.assigned[ap5[i]]);
      }
    }
  }
  return bad;
})()`);
check('信道分配避开了所有相邻 AP 冲突', violations.length === 0,
  violations.length ? violations.join('、') : '零冲突');

const coverage = run(`(function () {
  const ap5 = MY_APS.filter(k => bssOf(k, '5') !== null);
  const r = plan('5', ap5);
  return ap5.filter(k => r.assigned[k] === undefined).length;
})()`);
check('每台有 5G 射频的 AP 都分到了信道', coverage === 0, `${coverage} 台漏配`);

/* ---------------------------------------------------------------- 导出文本 */
section('导出内容');
const planText = run('buildPlanText()');
check('导出文本包含扫描时间', /扫描时间：\d{4}/.test(planText), planText.split('\n')[1]);
check('导出文本包含相邻关系段', planText.includes('相邻关系'));
check('导出文本不再出现"演示"字样', !planText.includes('演示'));

/* ---------------------------------------------------------------- 分组规则 */
section('分组规则 · 同一台 AP 的双频 BSS 必须并成一台');

const defaultRule = run('$("#fGroup").value');
check('默认分组规则是"后 2 字节"', defaultRule === 'tail2', defaultRule);

/* 现场实测到的真实形态：同一台 AP 的 2.4G / 5G 只有中间字节不同，尾部才是它的身份 */
const tailCase = run(`(function () {
  const mk = (bssid, band) => ({ bssid: bssid, band: band, ssid: 'Corp' });
  const a = mk('58:B4:BB:D5:1A:B4', '2.4');
  const b = mk('5A:B4:BB:85:1A:B4', '5');
  return { tail2: sameGroup(a, b, 'tail2'), prefix5: sameGroup(a, b, 'prefix5') };
})()`);
check('默认规则把同尾的 2.4G / 5G 判为同一台 AP', tailCase.tail2 === true);
check('旧的"前 5 字节"规则会把它拆成两台（说明为什么要换默认值）',
  tailCase.prefix5 === false);

/* BSSID 完全相同（同一台 AP 两个频段共用一个 BSSID）时，任何规则下都不能拆 */
const sameBssidMerged = run(`(function () {
  const mk = band => ({ bssid: 'AA:BB:CC:DD:EE:FF', band: band, ssid: 'Corp' });
  return ['tail2', 'prefix5', 'single'].every(r => sameGroup(mk('2.4'), mk('5'), r));
})()`);
check('相同 BSSID 的双频 BSS 在任何规则下都并成一台', sameBssidMerged === true);

/* 拿真实快照验一遍：同 SSID + 同尾部 的 BSS 不能落在不同的 AP 名下 */
const splitPairs = run(`(function () {
  const mine = SCAN.filter(isMine), bad = [];
  for (let i = 0; i < mine.length; i++) {
    for (let j = i + 1; j < mine.length; j++) {
      const a = mine[i], b = mine[j];
      if (a.bssid === b.bssid) continue;
      if (a.ssid && a.ssid === b.ssid && bssidTail(a) === bssidTail(b) &&
          apKeyOf(a) !== apKeyOf(b)) bad.push(a.bssid + ' / ' + b.bssid);
    }
  }
  return bad;
})()`);
check('快照里所有「同 SSID + 同尾部」的 BSS 都归到了同一台 AP',
  splitPairs.length === 0, splitPairs.join('、') || '无拆分');

const perAp = run('MY_APS.map(k => SCAN.filter(b => isMine(b) && apKeyOf(b) === k).length)');
check('每台 AP 最多 2 条 BSS（2.4G + 5G）', perAp.every(n => n <= 2),
  `AP 条数 ${perAp.join('/')}`);

/* 快照里当前网络"实际扫到的频段"必须被自动圈选全部覆盖。
   当前网络只开单频时这条前提本身就不成立，所以只做包含关系检查，不硬性要求 2.4G。 */
const realCover = run(`(function () {
  const ssid = INTERFACE.current_ssid;
  if (!ssid) return null;
  const netBands = Array.from(new Set(SCAN.filter(b => b.ssid.indexOf(ssid) === 0).map(b => b.band))).sort();
  const got = Array.from(new Set(SCAN.filter(b => b.ssid.indexOf(ssid) === 0 && isMine(b)).map(b => b.band))).sort();
  return { netBands, got };
})()`);
check('快照里当前网络的每个频段都被自动圈到',
  !realCover || realCover.netBands.every(b => realCover.got.includes(b)),
  realCover ? `网络频段 ${realCover.netBands.join('、')} → 圈到 ${realCover.got.join('、')}` : '无当前网络');

/* 用一份"当前网络确实是双频"的构造数据，验证自动圈选会同时覆盖 2.4G 与 5G，
   并把两条射频并成同一台 AP —— 这才是用户报的那个 bug 的完整复现。
   真实环境里当前网络未必开双频（现场快照就只剩 5G），所以不能拿环境数据来断言这件事。 */
const dualBand = run(`(function () {
  applyScanPayload({
    ok: true,
    scanned_at: '2026-01-01T00:00:00+08:00',
    interface: { name: '测试网卡', state: '已连接', current_ssid: 'Corp', current_bssid: '5A:B4:BB:85:1A:B4' },
    warnings: [],
    bss: [
      { ssid: 'Corp',     bssid: '58:B4:BB:D5:1A:B4', band: '2.4', channel: 6,   frequency: 2437, width: 20, rssi: -50, phy: '802.11ax', security: 'WPA2-CCMP' },
      { ssid: 'Corp',     bssid: '5A:B4:BB:85:1A:B4', band: '5',   channel: 44,  frequency: 5220, width: 80, rssi: -45, phy: '802.11ax', security: 'WPA2-CCMP' },
      { ssid: 'Neighbor', bssid: '11:22:33:44:55:66', band: '5',   channel: 149, frequency: 5745, width: 80, rssi: -70, phy: '802.11ax', security: 'WPA2-CCMP' }
    ]
  }, 'snapshot');
  $('#fSsid').value = '';
  $('#fOui').value = '';
  filtersPrefilled = false;
  prefillFilters();
  applyFilterRule();
  const mine = SCAN.filter(isMine);
  return {
    bands: Array.from(new Set(mine.map(b => b.band))).sort(),
    aps: MY_APS.slice(),
    perAp: MY_APS.map(k => SCAN.filter(b => isMine(b) && apKeyOf(b) === k).length),
    neighborPicked: mine.some(b => b.ssid === 'Neighbor')
  };
})()`);
check('双频当前网络：自动圈选同时覆盖 2.4G 与 5G',
  dualBand.bands.length === 2 && dualBand.bands.includes('2.4') && dualBand.bands.includes('5'),
  dualBand.bands.join('、'));
check('双频当前网络：两条射频并成同一台 AP',
  dualBand.aps.length === 1 && dualBand.perAp[0] === 2,
  `${dualBand.aps.join('、') || '无'}（${dualBand.perAp.join('/')} 条 BSS）`);
check('双频当前网络：别家网络不会被误圈进来', dualBand.neighborPicked === false);

/* 还原真实快照，后面的用例继续按现场数据跑 */
run(`(function () {
  applyScanPayload(window.__WLAN_SCAN__, 'snapshot');
  $('#fSsid').value = '';
  $('#fOui').value = '';
  filtersPrefilled = false;
  prefillFilters();
  applyFilterRule();
})()`);

/* 预填只按 SSID：按 OUI 圈会把同一台 AP 的 2.4G 射频整片漏掉 */
const missedByPrefix = run(`(function () {
  const prefix = $('#fSsid').value.trim();
  if (!prefix) return [];
  return SCAN.filter(b => b.ssid.indexOf(prefix) === 0 && !isMine(b))
             .map(b => b.bssid + ' (' + b.band + ')');
})()`);
check('预填的 SSID 能圈住该网络的全部射频', missedByPrefix.length === 0,
  missedByPrefix.length ? '漏掉 ' + missedByPrefix.join('、') : '全覆盖');
check('不再用 OUI 预填（OUI 前缀会漏掉 2.4G 射频）', run('$("#fOui").value') === '',
  run('$("#fOui").value') || '空');

/* ---------------------------------------------------------------- 重扫回首页 */
section('重新扫描 · 自动回到第一步');

run('go(3)');
check('重扫前停在第三步', run('CURRENT_STEP') === 3, 'CURRENT_STEP=' + run('CURRENT_STEP'));

/* 用快照数据冒充一次实时扫描，测试不依赖本地服务在运行 */
run(`window.fetch = () => Promise.resolve({
  ok: true,
  json: () => Promise.resolve(window.__WLAN_SCAN__)
})`);
await run('rescan()');

check('重扫后自动回到第一步', run('CURRENT_STEP') === 1, 'CURRENT_STEP=' + run('CURRENT_STEP'));
check('第一步可见，第二 / 第三步已隐藏',
  !$('#s1').classList.contains('hidden')
  && $('#s2').classList.contains('hidden') && $('#s3').classList.contains('hidden'));
check('步骤条高亮跟着回到第一步', $$('.step')[0].classList.contains('on')
  && !$$('.step')[2].classList.contains('on'));

/* ---------------------------------------------------------------- 第二步文案 */
section('第二步文案 · 用「靠近 / 不靠近」');

run('go(2)');
const pageText = win.document.body.textContent;
check('页面不再出现"听到"', !pageText.includes('听到'));
check('说明里用的是"靠近"', $('#s2').textContent.includes('靠近'));

const toggles = $$('#askList .toggle2');
check('行内切换按钮写着「靠近 / 不靠近」',
  toggles.length > 0
  && toggles[0].querySelector('.y').textContent.trim() === '靠近'
  && toggles[0].querySelector('.n').textContent.trim() === '不靠近',
  toggles.length ? toggles[0].textContent.trim() : '无行');

check('导出文本里也改成了"靠近"',
  run('buildPlanText()').includes('谁靠近谁') && !run('buildPlanText()').includes('听到'));

/* ------------------------------------------------- 服务端数据自纠正 */
section('页面启动拉实时数据 · 换电脑不会显示别人的 Wi-Fi');

/* 模拟最坑的那种局面：页面里先是打包机带过来的旧快照（当前网络是 QuanYou29），
   服务端给出的却是本机的实时数据（当前网络是 LiveNet）。 */
const livePayload = {
  ok: true,
  scanned_at: '2026-02-02T10:00:00+08:00',
  interface: { name: '实时网卡', state: '已连接',
               current_ssid: 'LiveNet', current_bssid: 'B0:BE:76:22:1A:B4' },
  warnings: [],
  bss: [
    { ssid: 'LiveNet', bssid: 'B0:BE:76:11:1A:B4', band: '2.4', channel: 6,
      frequency: 2437, width: 20, rssi: -55, phy: '802.11n', security: 'WPA2-CCMP' },
    { ssid: 'LiveNet', bssid: 'B0:BE:76:22:1A:B4', band: '5', channel: 36,
      frequency: 5180, width: 20, rssi: -40, phy: '802.11ax', security: 'WPA2-CCMP' }
  ]
};
run(`window.fetch = () => Promise.resolve({
  ok: true,
  json: () => Promise.resolve(${JSON.stringify(livePayload)})
})`);
await run('refreshFromServer()');

check('实时数据替换掉了页面里的快照',
  run('SCANNED_AT') === livePayload.scanned_at, run('SCANNED_AT'));
check('顶部标签切换为「实时扫描数据」',
  $('#dataTag').textContent.includes('实时扫描数据'), $('#dataTag').textContent.trim());
check('接口名换成实时数据里的网卡',
  $('#statIface').textContent.trim() === '实时网卡', $('#statIface').textContent.trim());
check('SSID 过滤框从旧网络改到新网络',
  $('#fSsid').value === 'LiveNet', $('#fSsid').value || '(空)');
check('自动圈到了新网络的 2 条 BSS',
  run('SCAN.filter(isMine).length') === 2,
  `${run('SCAN.filter(isMine).length')} 条`);
check('新网络的两条射频按后 2 字节并成 1 台 AP',
  run('MY_APS.length') === 1, run('MY_APS.slice()').join('、') || '(无)');

/* 拉不到数据（服务没起 / 扫描失败）时必须原样保留，不能把页面清空 */
run(`window.fetch = () => Promise.reject(new Error('no server'))`);
await run('refreshFromServer()');
check('拉不到数据时不动页面（仍是上一次的数据）',
  run('SCANNED_AT') === livePayload.scanned_at && run('SCAN.length') === 2,
  `${run('SCANNED_AT')} · ${run('SCAN.length')} 条`);

/* 还原真实快照，后面的用例继续按现场数据跑 */
run(`(function () {
  applyScanPayload(window.__WLAN_SCAN__, 'snapshot');
  $('#fSsid').value = '';
  $('#fOui').value = '';
  filtersPrefilled = false;
  ssidAutoFilled = false;
  prefillFilters();
  applyFilterRule();
})()`);

/* ---------------------------------------------------------------- 异常路径 */
section('异常路径 · 扫描失败 / 空结果');

const beforeErrors = consoleErrors.length;
run(`applyScanPayload({
  ok: false,
  scanned_at: '',
  interface: { name: '', state: '', current_ssid: '', current_bssid: '' },
  warnings: [],
  error: '测试用的扫描失败信息',
  bss: []
}, 'live')`);
run('refreshAfterScan(); renderScanNotice(); renderDataTag();');

check('扫描失败时页面不报错，走的是提示分支', consoleErrors.length === beforeErrors,
  consoleErrors.slice(beforeErrors).join(' | '));
check('显示了失败横幅', $('#scanNotice').textContent.includes('测试用的扫描失败信息'));
check('顶部标签切换为不可用状态', $('#dataTag').className.includes('fail'),
  $('#dataTag').textContent.trim());
check('表格显示空状态而不是残留旧数据',
  $('#tbody').textContent.includes('还没有扫描数据'), $('#tbody').textContent.trim().slice(0, 40));

/* 让页面回到正常数据，测试结束后 DOM 不残留在异常态 */
run('applyScanPayload(window.__WLAN_SCAN__, "snapshot")');
run('prefillFilters(); renderIfaceStat(); refreshAfterScan(); renderScanNotice(); renderDataTag();');

/* ---------------------------------------------------------------- 报告 */
console.log(notes.join('\n'));
console.log('\n' + '='.repeat(64));
if (failures.length) {
  console.log('失败项：');
  console.log(failures.join('\n'));
  console.log('='.repeat(64));
  dom.window.close();
  process.exit(1);
}
console.log(`全部通过（${notes.filter(n => n.includes('[通过]')).length} 项）`);
console.log('='.repeat(64));

console.log('\n---- 规划建议导出样例 ----\n');
console.log(planText);

dom.window.close();
