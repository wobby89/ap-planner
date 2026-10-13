/* ==========================================================================
   STEP 3 · 规划计算
   ========================================================================== */

/* 某信道的"干扰惩罚"：把该信道上所有外部 BSS 的功率线性相加 */
function penalty(band, ch) {
  return SCAN.filter(b => !isMine(b) && b.band === band && b.ch === ch)
             .reduce((sum, b) => sum + Math.pow(10, b.rssi / 10), 0);
}

/* 该信道上最强的外部 BSS */
function worstNeighbor(band, ch) {
  const list = SCAN.filter(b => !isMine(b) && b.band === band && b.ch === ch);
  if (!list.length) return null;
  return list.reduce((a, b) => b.rssi > a.rssi ? b : a);
}

const nameOf = b => !b ? '' : escapeHtml(b.ssid === '' ? '(隐藏 SSID)' : b.ssid);

/**
 * 贪心分配：
 *  1) 候选信道按干扰惩罚升序排
 *  2) 优先选"全公司还没用过"的信道（第二层：用得越少越靠前）
 *  3) 硬约束：不能与已分配的相邻 AP 同信道；实在无解才放宽
 */
function plan(band, apList) {
  const cands = (band === '2.4' ? CH24 : CH5).slice()
    .sort((a, b) => penalty(band, a) - penalty(band, b) || a - b);

  const assigned = {};                   // AP -> ch
  const used = {};                       // ch -> 次数
  cands.forEach(c => used[c] = 0);

  apList.forEach(ap => {
    const conflict = ch => apList.some(o => o !== ap && assigned[o] === ch && isAdj(ap, o));
    const pick =
      cands.find(c => used[c] === 0 && !conflict(c)) ||
      cands.find(c => used[c] > 0 && !conflict(c)) ||
      cands.slice().sort((a, b) =>
        (conflict(a) - conflict(b)) || (penalty(band, a) - penalty(band, b)))[0];
    assigned[ap] = pick;
    used[pick]++;
  });
  return { assigned, cands };
}

/* 在「相邻 AP 不能同信道」的硬约束下，求一个尽量大、且能塞进现有信道的 AP 子集。
   20 台以内直接穷举（2^n），精确；太大就退回原样不精简。 */
function slimFor(band, apList) {
  if (apList.length > 18) return apList.slice();
  const conflicts = list => {
    const result = plan(band, list);
    return list.filter(a =>
      list.some(o => o !== a && isAdj(a, o) && result.assigned[o] === result.assigned[a])
    ).length;
  };
  let best = null;
  for (let mask = 1; mask < (1 << apList.length); mask++) {
    const list = apList.filter((_, i) => mask & (1 << i));
    if (best && list.length < best.length) continue;
    if (conflicts(list) > 0) continue;
    // 同样大的集合里，优先保留信号更强的那批（覆盖更确定）
    const score = list.reduce((sum, a) => sum + rssiOf(a), 0);
    if (!best || list.length > best.length || score > best.score) {
      best = list;
      best.score = score;
    }
  }
  return best ? best.slice() : apList.slice();
}

function conflictCount(band, apList, result) {
  return apList.filter(a =>
    apList.some(o => o !== a && isAdj(a, o) && result.assigned[o] === result.assigned[a])
  ).length;
}

function reasonFor(band, ch, ap, apList, assigned) {
  const parts = [];
  const neighbor = worstNeighbor(band, ch);
  if (!neighbor) {
    parts.push(`ch${ch} 上没有任何外部 BSS，干扰最轻`);
  } else {
    const above = neighbor.rssi > CCA;
    parts.push(`同频邻居 ${nameOf(neighbor)} 为 ${neighbor.rssi} dBm，`
      + (above
        ? `<b style="color:var(--red);font-weight:500">高于</b> CCA 门限 ${CCA} dBm，两者会互相退避`
        : `<b style="color:var(--green);font-weight:500">低于</b> CCA 门限 ${CCA} dBm，影响可忽略`));
  }

  const clashes = apList.filter(o => o !== ap && isAdj(ap, o) && assigned[o] === ch);
  const neighbors = apList.filter(o => o !== ap && isAdj(ap, o));
  if (clashes.length) {
    parts.push(`与相邻的 ${clashes.join('、')} 撞频，属于放宽后的妥协方案`);
  } else if (neighbors.length) {
    parts.push(`相邻的 ${neighbors.map(o => `${o} 用 ch${assigned[o]}`).join('、')}，已避开`);
  } else {
    parts.push('该 AP 四周没有相邻 AP，可自由取用');
  }
  return parts.join('；') + '。';
}

function currentCh(ap, band) {
  return chOf(ap, band);
}

function chanOwners(band, apList) {
  const map = new Map();
  apList.forEach(k => {
    const ch = chOf(k, band);
    if (ch === null) return;
    if (!map.has(ch)) map.set(ch, []);
    map.get(ch).push(k);
  });
  return map;
}

const listCh = set => Array.from(set).sort((a, b) => a - b).map(c => 'ch' + c).join('、');

/* ---------- 2.4G 开关卡片 ---------- */
function renderPlanGrid() {
  $('#planGrid').innerHTML = MY_APS.map(k => {
    const b5  = bssOf(k, '5');
    const b24 = bssOf(k, '2.4');
    const on = state.band24.has(k) && !!b24;
    const sw24 = b24
      ? `<div class="sw ${on ? 'on' : ''}" data-band24="${escapeHtml(k)}">2.4G · ${on ? 'ch' + b24.ch : '关闭'}</div>`
      : '<div class="sw disabled">2.4G · 无射频</div>';
    return `<div class="apcell">
      <div class="n">${escapeHtml(k)}${b24 && !on ? '<span class="tag gray" style="font-weight:400">2.4G 关闭</span>' : ''}</div>
      <div class="sub" title="${escapeHtml(b5 ? b5.bssid : (b24 ? b24.bssid : ''))}">${b5 ? b5.bssid : (b24 ? b24.bssid : '—')}</div>
      <div class="switchrow">
        ${sw24}
        <div class="sw static ${b5 ? 'on' : 'disabled'}">5G · ${b5 ? 'ch' + b5.ch : '无射频'}</div>
      </div>
    </div>`;
  }).join('');

  $('#planGrid').querySelectorAll('[data-band24]').forEach(el => {
    el.addEventListener('click', () => {
      const k = el.dataset.band24;
      state.band24.has(k) ? state.band24.delete(k) : state.band24.add(k);
      render();
    });
  });
}

/* ---------- 主渲染 ---------- */
function render() {
  refreshAps();
  renderPlanGrid();

  if (!MY_APS.length) {
    $('#planGridNote').innerHTML = '还没有分好组的 AP，请回到第一步。';
    $('#banners').innerHTML = banner('warn',
      '<b>没有可规划的 AP。</b>请回到第一步勾选本公司的 BSS，并把属于同一台 AP 的两个射频填成同一个名字。');
    $('#tbl5').innerHTML = '';
    $('#tbl24').innerHTML = '';
    $('#h5').textContent = '';
    $('#n5').textContent = '';
    $('#h24').textContent = '';
    $('#n24').textContent = '';
    $('#spectrum').innerHTML = '';
    $('#findings').innerHTML = '<div class="item"><b>还没有数据</b><p>请先在第一步圈出本公司的 AP。</p></div>';
    $('#why').innerHTML = whyCards()
      .map(([t, d]) => `<div class="item"><b>${t}</b><p>${d}</p></div>`).join('');
    return;
  }

  /* 只在有对应射频的 AP 上做规划：真实环境里常出现只有 2.4G 的 IoT AP */
  const ap5 = MY_APS.filter(k => bssOf(k, '5') !== null);
  const open24 = MY_APS.filter(k => state.band24.has(k) && bssOf(k, '2.4') !== null);

  const p5 = plan('5', ap5);
  const p24 = plan('2.4', open24);

  const cur5m = chanOwners('5', ap5);
  const cur24m = chanOwners('2.4', open24);
  const reuse5 = Array.from(cur5m.entries()).filter(([, list]) => list.length > 1);
  const new5 = new Set(Object.values(p5.assigned));
  const new24 = new Set(open24.map(k => p24.assigned[k]));
  const c5 = conflictCount('5', ap5, p5);
  const c24 = conflictCount('2.4', open24, p24);
  const slim = slimFor('2.4', open24);
  const blocked5 = CH5.filter(c => { const w = worstNeighbor('5', c); return w && w.rssi > CCA; });
  const blocked24 = CH24.filter(c => { const w = worstNeighbor('2.4', c); return w && w.rssi > CCA; });
  const noRadio24 = MY_APS.length - MY_APS.filter(k => bssOf(k, '2.4') !== null).length;

  /* ---- 结论横幅（全部由数据算出）---- */
  const banners = [];
  banners.push(banner('warn',
    '<b>当前问题：</b>'
    + `5 GHz 下 <b>${ap5.length}</b> 台 AP 只用了 <b>${cur5m.size}</b> 条信道`
    + (reuse5.length
        ? `（${reuse5.map(([c, list]) => 'ch' + c + ' 上挤了 ' + list.join(' / ')).join('；')}）`
        : '，没有复用')
    + `；2.4 GHz 下 <b>${open24.length}</b> 台 AP 占用 <b>${cur24m.size}</b> 条信道，`
    + `而 2.4 GHz 只有 <b>${CH24.length}</b> 条互不重叠的可用信道。`
    + '同频 AP 会被迫互相退避，把空口时间切掉。'));

  banners.push(banner('info',
    '<b>外部占用：</b>'
    + ((blocked5.length || blocked24.length)
      ? (blocked5.length
          ? `5 GHz 的 ${blocked5.map(c => `ch${c}（${nameOf(worstNeighbor('5', c))}，${worstNeighbor('5', c).rssi} dBm）`).join('、')} 超过 CCA 门限，规划时已优先避开；`
          : '5 GHz 没有邻近 AP 超过 CCA 门限；')
        + (blocked24.length
          ? `2.4 GHz 的 ${blocked24.map(c => `ch${c}`).join('、')} 被占用（最强 ${blocked24.map(c => worstNeighbor('2.4', c).rssi).join(' / ')} dBm）`
            + (blocked24.length === CH24.length ? '，三条互不重叠信道全部沦陷' : '') + '。'
          : '2.4 GHz 也没有外部占用。')
      : `没有检测到信号高于 CCA 门限（${CCA} dBm）的邻近 AP —— 两个频段都有干净信道可选。`)));

  banners.push(banner('ok',
    '<b>建议结果：</b>'
    + `5 GHz 扩展到 <b>${new5.size}</b> 条互不重叠的信道`
    + (c5 ? `（仍有 ${c5} 台无法完全避开相邻冲突）` : '')
    + `；2.4 GHz 保留 <b>${open24.length}</b> 台 AP、占用 <b>${new24.size}</b> 条信道`
    + (c24 ? `，其中 <b>${c24}</b> 台与相邻 AP 撞频 —— 建议精简到 <b>${slim.length}</b> 台` : '，无相邻冲突')
    + '；两个频段频宽一律建议固定 20 MHz。'
    + (noRadio24 ? `另有 ${noRadio24} 台 AP 没有 2.4 GHz 射频，不参与 2.4G 规划。` : '')));

  $('#banners').innerHTML = banners.join('');

  /* ---- 结果表 ---- */
  function rows(band, apList, result) {
    if (!apList.length) return '';
    return apList.map(ap => {
      const cur = currentCh(ap, band);
      const next = result.assigned[ap];
      const changed = cur !== next;
      return `<tr>
        <td style="font-weight:500;white-space:nowrap">${escapeHtml(ap)}</td>
        <td style="white-space:nowrap"><span class="chip keep">${cur === null ? '未开' : 'ch' + cur}</span><span class="arrow">→</span><span class="chip new">ch${next}</span> ${changed ? '<span class="tag green" style="margin-left:2px">调整</span>' : '<span class="tag gray" style="margin-left:2px">保持</span>'}</td>
        <td style="font-family:var(--mono);font-size:11.5px;color:var(--text2);white-space:nowrap">${freqOf(next, band)} MHz<br>20 MHz</td>
        <td><div class="reason">${reasonFor(band, next, ap, apList, result.assigned)}</div></td>
      </tr>`;
    }).join('');
  }

  const head = '<thead><tr><th>AP</th><th>信道</th><th>频率 / 频宽</th><th>依据</th></tr></thead><tbody>';
  $('#tbl5').innerHTML = head + (ap5.length
    ? rows('5', ap5, p5) + '</tbody>'
    : '<tbody><tr><td colspan="4" style="color:var(--text3)">所有 AP 都没有 5 GHz 射频</td></tr></tbody>');
  $('#tbl24').innerHTML = head + (open24.length
    ? rows('2.4', open24, p24) + '</tbody>'
    : '<tbody><tr><td colspan="4" style="color:var(--text3)">所有 AP 的 2.4G 均已关闭</td></tr></tbody>');

  /* ---- 表格下方的说明（同样由数据算出）---- */
  $('#h5').textContent = `${ap5.length} 台 · 20 MHz`;
  $('#n5').innerHTML = `当前 ${ap5.length} 台 AP 分布在 ${cur5m.size} 条信道上`
    + (cur5m.size < ap5.length ? '，存在同频复用；建议扩到互不重叠的信道' : '，已经没有复用空间可挤')
    + (blocked5.length ? `。已主动避开被邻近 AP 压过 CCA 门限的 ${listCh(blocked5)}` : '') + '。';

  $('#h24').textContent = `${open24.length} 台 · 20 MHz`;
  $('#n24').innerHTML = `2.4 GHz 只有 ${CH24.length} 条互不重叠的信道，当前开了 ${open24.length} 台`
    + (c24
      ? `，在当前相邻关系下最少需要精简到 <b>${slim.length}</b> 台才能做到零相邻冲突（点上面的按钮可以一键应用）。`
      : `，规划后占用 ${new24.size} 条信道且没有相邻冲突，这个数量是合适的。`);

  $('#planGridNote').innerHTML = '点卡片上的 2.4G 开关可以随时改，下面的建议会跟着重算。'
    + `当前开 ${open24.length} 台，其中 ${c24} 台与相邻 AP 撞频`
    + (c24 ? `；在当前相邻关系下，最多 <b>${slim.length}</b> 台可以做到零冲突。` : '。');

  /* ---- 频谱占用图 ---- */
  function slotMarkup(channel, band, ours, neighbors, useful) {
    const maxR = neighbors.length ? Math.max(...neighbors.map(x => x.rssi)) : null;
    const hot = maxR !== null && maxR > CCA;
    const names = neighbors.map(x => (x.ssid === '' ? '隐藏' : escapeHtml(x.ssid))).join('<br>');
    const ownLabel = escapeHtml(ours.join(' '));
    return `<div class="slot ${useful ? '' : 'noise'}">
      <div class="ours">${ours.length ? `<span title="${ownLabel}">${ownLabel}</span>` : ''}</div>
      <div class="barzone">${maxR !== null ? `<div class="bar ${hot ? 'hot' : 'dim'}" style="height:${hOf(maxR)}%"></div>` : ''}</div>
      <div class="nm">${names || (useful ? '空闲' : '重叠')}</div>
      <div class="ch">${channel}</div>
      <div class="fq">${freqOf(channel, band)} MHz</div>
    </div>`;
  }

  const slots24 = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13].map(c =>
    slotMarkup(c, '2.4', open24.filter(a => p24.assigned[a] === c),
      SCAN.filter(b => !isMine(b) && b.band === '2.4' && b.ch === c), CH24.includes(c))
  ).join('');

  const slots5 = CH5.map(c =>
    slotMarkup(c, '5', ap5.filter(a => p5.assigned[a] === c),
      SCAN.filter(b => !isMine(b) && b.band === '5' && b.ch === c), true)
  ).join('');

  $('#spectrum').innerHTML = `
    <div class="spec-row">
      <div class="spec-label">2.4 GHz<br><span style="font-size:11px;color:var(--text3)">仅 1 / 6 / 11</span></div>
      <div class="spec-track">${slots24}</div>
    </div>
    <div class="spec-row">
      <div class="spec-label">5 GHz<br><span style="font-size:11px;color:var(--text3)">非 DFS 段</span></div>
      <div class="spec-track">${slots5}</div>
    </div>
    <p class="note">方块高度 = 该信道上最强的外部 BSS 信号强度，越矮越好。橙色 = 高于 CCA 门限 ${CCA} dBm，会实际抢走空口时间。</p>`;

  /* ---- 本次扫描的发现（全部动态生成）---- */
  const findings = [];

  findings.push(['外部占用：' + (blocked5.length + blocked24.length) + ' 条信道压过 CCA 门限',
    (blocked5.length
      ? `5 GHz 有 ${blocked5.map(c => `ch${c}（${nameOf(worstNeighbor('5', c))}，${worstNeighbor('5', c).rssi} dBm）`).join('、')}。`
      : '5 GHz 没有邻近 AP 超过 CCA 门限。')
    + (blocked24.length
      ? `2.4 GHz 的 ${blocked24.map(c => `ch${c}`).join('、')} 被外部占用${blocked24.length === CH24.length ? '，1 / 6 / 11 三条全部沦陷，没有一条干净信道' : ''}。`
      : '2.4 GHz 也没有外部占用。')]);

  findings.push(['本公司当前的复用情况',
    `5 GHz：${ap5.length} 台 AP 落在 ${cur5m.size} 条信道上`
    + (reuse5.length
        ? `，${reuse5.map(([c, l]) => `ch${c} 上有 <b>${l.join(' 和 ')}</b>`).join('，')}。`
        : '，没有两台挤在同一条信道。')
    + `2.4 GHz：${open24.length} 台 AP 落在 ${cur24m.size} 条信道上`
    + (open24.length === 0
        ? '。'
        : (cur24m.size < CH24.length ? '。' : `，已经把 ${CH24.length} 条互不重叠信道用满。`))]);

  findings.push(['规划后的遗留冲突：' + (c5 + c24) + ' 台',
    (c5 + c24 === 0
      ? '按上面的方案调整后，5 GHz 与 2.4 GHz 都不存在「相邻 AP 同信道」的情况。'
      : (c5 ? `5 GHz 有 <b>${c5}</b> 台无法完全避开；` : '')
        + (c24 ? `2.4 GHz 有 <b>${c24}</b> 台无法完全避开（只有 ${CH24.length} 条互不重叠信道，这是数学上的必然）；` : '')
        + '这些 AP 的依据里会明确写出是妥协方案，不会假装没问题。')]);

  findings.push(['2.4 GHz 该开几台：' + slim.length + ' 台',
    (open24.length === 0
      ? '当前没有 AP 开启 2.4 GHz。'
      : (c24 === 0
          ? `当前开启的 ${open24.length} 台在相邻约束下不冲突，可以全部保留。`
          : `当前开 ${open24.length} 台、其中 ${c24} 台与相邻 AP 撞频；在当前相邻关系下，最多 <b>${slim.length}</b> 台能做到零冲突`
            + `（建议保留 ${slim.join('、')}）。留着的这几台只服务 IoT、打印机、门禁这类老设备，笔记本和手机应全部引导到 5 GHz。`))]);

  $('#findings').innerHTML = findings
    .map(([t, d]) => `<div class="item"><b>${t}</b><p>${d}</p></div>`).join('');

  /* ---- 通用原理（固定文案，与本次数据无关）---- */
  $('#why').innerHTML = whyCards()
    .map(([t, d]) => `<div class="item"><b>${t}</b><p>${d}</p></div>`).join('');
}

/* 这四条是任何办公室都成立的原理，所以是固定文案 */
function whyCards() {
  return [
    ['为什么相邻的 AP 不能同信道',
      '同频干扰成立的条件是「两台 AP 互相靠近」。相邻关系就是把这个条件显式告诉程序：相邻的必须错开，隔了几间房、离得远的可以复用同一条信道 —— 这正是 2.4 GHz 只有 3 条非重叠信道却能覆盖多于 3 台 AP 的原理。'],
    ['为什么两个频段都固定 20 MHz',
      '频宽翻倍等于一次绑走两条信道：2.4 GHz 会直接吃掉 1/6/11 里的一大半，5 GHz 非 DFS 段也从 8 条可用掉到 4 条。而频宽只提升单用户峰值，办公室的流量是「很多台终端各干一点事」，20 MHz 反而能塞下更多终端。Wi-Fi 6 的 OFDMA 在小频宽下调度粒度更细，多用户并发效率更高。'],
    ['为什么 2.4 GHz 靠「少开几台 AP」来降干扰',
      '2.4 GHz 只有 1 / 6 / 11 三条互不重叠的信道，这是物理事实，无法靠规划绕过。AP 数量超过 3 台之后，唯一的办法就是减少开启的数量 —— 只留物理上分散的几台做兼容层，其余关掉。'],
    ['为什么优先用 5 GHz 的非 DFS 段（149–165）',
      '52–64、100–140 这段受雷达检测约束，一旦触发就会整段跳频，表现为「莫名其妙卡一下」。而 36–48 是各家公司的默认段，撞车概率最高。非 DFS 段一共能凑出 8 条互不重叠的 20 MHz 信道（36/40/44/48 + 149/153/157/161），只要 AP 数量不超过 8 台就能全部错开，且完全不受雷达影响。']
  ];
}

function initStep3() {
  $('#btnSlim').addEventListener('click', () => {
    state.band24 = new Set(
      slimFor('2.4', MY_APS.filter(k => bssOf(k, '2.4') !== null))
    );
    render();
  });
  $('#btnAll24').addEventListener('click', () => {
    state.band24 = new Set(MY_APS.filter(k => bssOf(k, '2.4') !== null));
    render();
  });
}
