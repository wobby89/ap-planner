/* ==========================================================================
   步骤切换 / 重新扫描 / 导出 / 启动
   ========================================================================== */

/* 当前停在第几步。go() 之外没人改它 */
let CURRENT_STEP = 1;

function go(step) {
  CURRENT_STEP = step;
  [1, 2, 3].forEach(i => $('#s' + i).classList.toggle('hidden', i !== step));
  $$('.step').forEach(el => {
    const i = +el.dataset.step;
    el.classList.toggle('on', i === step);
    el.classList.toggle('done', i < step);
  });
  if (step === 3) render();
  else if (step === 2) renderAsk();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

/* 扫描结果换了一批之后，把界面状态重新对齐 */
function refreshAfterScan() {
  const present = new Set(SCAN.map(b => b.bssid));
  Array.from(state.selected).forEach(id => { if (!present.has(id)) state.selected.delete(id); });
  Object.keys(state.apName).forEach(id => { if (!present.has(id)) delete state.apName[id]; });

  if (!state.selected.size) {
    // 没得可留（第一次打开，或换到了别的网络），按当前过滤条件重新圈一遍
    applyFilterRule();
  } else {
    refreshAps();
    resetAdjacency();
    renderTable();
  }
  renderAsk();
}

/* ---------- 重新扫描 ---------- */
async function rescan() {
  const button = $('#btnRescan');
  const label = button.textContent;

  button.disabled = true;
  button.textContent = '扫描中…';
  SCAN_WARNINGS = [];
  SCAN_ERROR = '';
  renderScanNotice();

  let payload = null;
  try {
    payload = await fetchLiveScan(true);
  } catch (err) {
    payload = null;
  }

  if (payload) {
    applyScanPayload(payload, 'live');
    resyncSsidFilter();            // 换了一批数据，过滤条件跟着当前网络走
    renderIfaceStat();
    // 扫描失败时也照样重画一遍，页面会变成空表 + 错误横幅
    refreshAfterScan();
  } else {
    SCAN_ERROR = '连不上本地扫描服务。请先运行 run_server.bat，或在项目目录执行 python backend/server.py。';
  }

  renderScanNotice();
  renderDataTag();

  /* 数据整个换了一批，第二、三步的相邻关系和规划结论都作废了 ——
     不管用户当时停在哪一步，都拉回第一步重新看扫描结果。 */
  if (CURRENT_STEP !== 1) go(1);

  button.disabled = false;
  button.textContent = label;
}

/* ---------- 导出建议 ---------- */
function buildPlanText() {
  const ap5 = MY_APS.filter(k => bssOf(k, '5') !== null);
  const open24 = MY_APS.filter(k => state.band24.has(k) && bssOf(k, '2.4') !== null);
  const p5 = plan('5', ap5);
  const p24 = plan('2.4', open24);

  const line = (ap, band, target) => {
    const cur = chOf(ap, band);
    return `  ${ap}  ${cur === null ? '(未开)' : 'ch' + cur}  ->  ch${target}  (${freqOf(target, band)} MHz)`;
  };

  const lines = [
    '本公司 AP 信道规划建议',
    `扫描时间：${SCANNED_AT || '未知'}`,
    `无线接口：${INTERFACE.name || '未知'}`,
    '频宽建议：2.4G / 5G 一律 20 MHz',
    ''
  ];

  lines.push(`【5 GHz】共 ${ap5.length} 台`);
  ap5.forEach(a => lines.push(line(a, '5', p5.assigned[a])));

  lines.push('', `【2.4 GHz】仅以下 ${open24.length} 台开启`);
  if (open24.length) {
    open24.forEach(a => lines.push(line(a, '2.4', p24.assigned[a])));
  } else {
    lines.push('  （无）');
  }

  const c24 = conflictCount('2.4', open24, p24);
  if (c24) {
    const slim = slimFor('2.4', MY_APS.filter(k => bssOf(k, '2.4') !== null));
    lines.push('',
      `注意：2.4 GHz 仍有 ${c24} 台与相邻 AP 撞频。`,
      `在当前相邻关系下，最多 ${slim.length} 台可做到零冲突：${slim.join('、')}`);
  }

  lines.push('',
    '相邻关系（这次规划依据的「谁靠近谁」）：',
    '  ' + (confirmedPairs().join('  ') || '（无）'));

  return lines.join('\n');
}

function downloadText(text, filename) {
  const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function exportPlan() {
  if (!MY_APS.length) {
    alert('还没有圈选本公司的 AP。');
    return;
  }
  const stamp = (SCANNED_AT || '').replace(/[:T]/g, '-').slice(0, 19) || 'latest';
  downloadText(buildPlanText(), `ap-channel-plan-${stamp}.txt`);
}

/* 页面由本地服务打开时，页面里的快照可能已经过时，甚至是打包时从开发机带过来的
   （那台机器的 Wi-Fi 和现场完全不是一回事）。这里再向服务要一次数据自我纠正：
   拿到就换成真实数据，拿不到（服务没起 / 扫描失败）就保持原样 —— 页面上本来就有
   对应的提示横幅，不去覆盖它。 */
async function refreshFromServer() {
  let payload = null;
  try {
    payload = await fetchLiveScan(false);
  } catch (err) {
    payload = null;
  }
  if (!payload || !payload.ok) return;

  applyScanPayload(payload, 'live');
  resyncSsidFilter();            // 新数据可能是另一个网络，过滤条件要跟着走
  renderIfaceStat();
  refreshAfterScan();
  renderScanNotice();
  renderDataTag();
  if (CURRENT_STEP === 3) render();
}

/* ---------- 启动 ---------- */
function init() {
  initStep1();
  initStep2();
  initStep3();

  $$('[data-go]').forEach(b => b.addEventListener('click', () => go(+b.dataset.go)));
  $$('.step').forEach(el => el.addEventListener('click', () => go(+el.dataset.step)));
  $('#btnExport').addEventListener('click', exportPlan);
  $('#btnRescan').addEventListener('click', rescan);

  // 快照读不到也继续，下面会渲染成空表 + 提示横幅
  loadSnapshot();
  prefillFilters();
  renderIfaceStat();
  renderScanNotice();
  renderDataTag();
  refreshAfterScan();

  // 支持 #step2 / #step3 直接跳到某一步（方便截图与分享）
  const jump = +(location.hash.replace('#step', '') || 1);
  if (jump >= 2 && jump <= 3) go(jump);

  // 由本地服务打开时，再拉一次实时数据（file:// 直开就跳过，保证双击也能看快照）
  if (/^https?:$/.test(location.protocol)) refreshFromServer();
}

init();
