/* ==========================================================================
   STEP 1 · 圈选 + 分组
   ========================================================================== */

function renderTable() {
  const band = $('#fBand').value;
  const rows = SCAN
    .filter(b => band === 'all' || b.band === band)
    .sort((a, b) => {
      const am = isMine(a) ? 0 : 1, bm = isMine(b) ? 0 : 1;
      if (am !== bm) return am - bm;
      const an = apKeyOf(a) || '\uffff', bn = apKeyOf(b) || '\uffff';
      const c = an.localeCompare(bn, undefined, { numeric: true });
      if (c) return c;
      if (a.band !== b.band) return BAND_ORDER.indexOf(a.band) - BAND_ORDER.indexOf(b.band);
      if (a.ch !== b.ch) return (a.ch || 9999) - (b.ch || 9999);
      return a.bssid.localeCompare(b.bssid);
    });

  if (!rows.length) {
    $('#tbody').innerHTML = `<tr><td colspan="11" style="color:var(--text3);text-align:center;padding:26px 0">
      ${SCAN.length ? '该频段下没有 BSS' : '还没有扫描数据'}</td></tr>`;
  } else {
    $('#tbody').innerHTML = rows.map(b => {
      const mine = isMine(b);
      const lvl = b.rssi >= -60 ? 'a' : b.rssi >= -75 ? 'b' : 'c';
      const hide = b.ssid === '';
      const apName = apKeyOf(b) || '';
      return `<tr class="${mine ? 'mine' : ''}">
        <td><input type="checkbox" data-bssid="${b.bssid}" ${mine ? 'checked' : ''}></td>
        <td class="${hide ? 'ssid-hide' : ''}">${escapeHtml(txt(b.ssid))}</td>
        <td class="mono">${b.bssid}</td>
        <td><span class="tag ${bandTagClass(b.band)}">${bandLabel(b.band)}</span></td>
        <td class="mono">${b.ch || '—'}</td>
        <td class="mono" style="color:var(--text2)">${b.freq ? b.freq + ' MHz' : '—'}</td>
        <td class="mono">${b.w ? b.w + ' MHz' : '—'}</td>
        <td><span class="rssi"><span class="bars"><i class="${lvl}"></i><i class="${lvl}"></i><i class="${lvl}"></i><i class="${lvl}"></i></span><b>${b.rssi} dBm</b></span></td>
        <td style="color:var(--text2)">${escapeHtml(b.phy)}</td>
        <td>${b.sec.indexOf('TKIP') >= 0
              ? `<span class="tag red">${escapeHtml(b.sec)}</span>`
              : `<span style="color:var(--text2)">${escapeHtml(b.sec)}</span>`}</td>
        <td>${mine
              ? `<input type="text" class="apname" data-bssid="${b.bssid}" value="${escapeHtml(apName)}" placeholder="填 AP 名">`
              : '<span style="color:var(--text3);font-size:12px">—</span>'}</td>
      </tr>`;
    }).join('');
  }

  $('#tbody').querySelectorAll('input[type=checkbox]').forEach(cb => {
    cb.addEventListener('change', () => {
      const b = SCAN.find(x => x.bssid === cb.dataset.bssid);
      if (!b) return;
      if (cb.checked) { state.selected.add(b.bssid); assignOne(b); }
      else { state.selected.delete(b.bssid); delete state.apName[b.bssid]; }
      refreshAps();
      resetAdjacency();
      renderTable();
    });
  });

  $('#tbody').querySelectorAll('input.apname').forEach(inp => {
    inp.addEventListener('change', () => {      // 用 change 而不是 input，避免每敲一下重绘丢焦点
      const bssid = inp.dataset.bssid;
      const old = state.apName[bssid];
      const v = inp.value.trim();
      if (!v || v === old) { inp.value = old || ''; return; }
      // 同名的 BSS 就是同一台 AP —— 改名会带着"兄弟射频"一起改
      Object.keys(state.apName).forEach(k => { if (state.apName[k] === old) state.apName[k] = v; });
      refreshAps();
      resetAdjacency();
      renderTable();
    });
  });

  updateStats();
}

function updateStats() {
  const total = SCAN.length;
  const mine = SCAN.filter(isMine).length;
  refreshAps();

  $('#statTotal').innerHTML = total + '<span class="u">个</span>';
  $('#statMine').innerHTML  = mine + '<span class="u">条 BSS</span>';
  $('#statAps').innerHTML   = MY_APS.length
    ? `归为 ${MY_APS.length} 台 AP`
    : '<span style="color:var(--amber)">还没有分组</span>';
  $('#statOther').innerHTML = (total - mine) + '<span class="u">个</span>';

  const rule = GROUP_RULES.find(r => r.id === $('#fGroup').value);
  if (rule) {
    $('#groupHint').innerHTML = '<b>分组规则：</b>' + rule.label + '。' + rule.hint
      + ' <b>改了名字就等于改分组</b> —— 把两条 BSS 填成同一个名字，它们就被算作同一台 AP 的两个射频。';
  }

  const picked = SCAN.filter(isMine);
  const c24 = picked.filter(b => b.band === '2.4').length;
  const c5  = picked.filter(b => b.band === '5').length;
  $('#selHint').innerHTML = picked.length
    ? `已勾选 <b>${picked.length}</b> 个 BSS（2.4G ${c24} 条 · 5G ${c5} 条），归为 <b>${MY_APS.length}</b> 台 AP`
    : '还没有勾选任何 BSS，请先在上表勾选本公司的 AP';
}

/* ---------- 筛选与批量操作 ---------- */

function applyFilterRule() {
  const prefix = $('#fSsid').value.trim();
  const oui = $('#fOui').value.trim().toUpperCase();
  state.selected.clear();
  SCAN.forEach(b => {
    const matched = (!prefix || b.ssid.startsWith(prefix))
                 && (!oui || b.bssid.toUpperCase().startsWith(oui));
    if (matched) state.selected.add(b.bssid);
  });
  regroup($('#fGroup').value);        // 整批重算，编号 AP1..APn
  refreshAps();
  resetAdjacency();
  renderTable();
}

function clearSelection() {
  state.selected.clear();
  state.apName = {};
  state.knownAps.clear();
  state.band24.clear();
  state.adj = {};
  state.touched.clear();
  state.done.clear();
  state.cur = 0;
  refreshAps();
  renderTable();
}

function initStep1() {
  $('#fGroup').innerHTML = GROUP_RULES
    .map(r => `<option value="${r.id}">${r.label}</option>`).join('');
  $('#fGroup').value = DEFAULT_GROUP_RULE;

  $('#btnApply').addEventListener('click', applyFilterRule);
  $('#btnClear').addEventListener('click', clearSelection);
  $('#fBand').addEventListener('change', renderTable);
  // 用户自己敲过 SSID，就视为"手动指定"，之后不再被自动预填覆盖
  $('#fSsid').addEventListener('input', () => { ssidAutoFilled = false; });
  $('#fGroup').addEventListener('change', () => {
    regroup($('#fGroup').value);
    refreshAps();
    resetAdjacency();
    renderTable();
  });
}
