/* ==========================================================================
   STEP 2 · 逐台确认相邻关系
   从 AP1 开始一台一台问「旁边还有哪些 AP」。已经确认过的配对自动锁住带到
   后面的页面 —— 既不用重复回答，也不会自相矛盾。
   ========================================================================== */

function adjKey(a, b) { return a < b ? a + '|' + b : b + '|' + a; }
function isAdj(a, b) { return state.adj[adjKey(a, b)] === true; }

/* 第 index 台 AP 在推测摆放里的坐标 */
function posHint(index) {
  return [Math.floor(index / POS_COLS), index % POS_COLS];
}

/* 推测值：按 POS_COLS 列的网格摆放，上下左右及对角算相邻。
   这只是**预填**，不是事实。名字不在网格假设范围内的 AP（超过前
   POS_ASSUMED_COUNT 台）一律保守地猜成"互相靠近" —— 相邻报多了只会让
   规划更保守，不会产生错误建议，所以这个方向是安全的。 */
function guessAdjacency() {
  const g = {};
  for (let i = 0; i < MY_APS.length; i++) {
    for (let j = i + 1; j < MY_APS.length; j++) {
      let adjacent = true;
      if (i < POS_ASSUMED_COUNT && j < POS_ASSUMED_COUNT) {
        const pa = posHint(i), pb = posHint(j);
        adjacent = Math.abs(pa[0] - pb[0]) <= 1 && Math.abs(pa[1] - pb[1]) <= 1;
      }
      g[adjKey(MY_APS[i], MY_APS[j])] = adjacent;
    }
  }
  return g;
}

function renderAsk() {
  const n = MY_APS.length;

  if (!n) {
    $('#apnav').innerHTML = '';
    $('#adjProgress').textContent = '第 0 / 0 台';
    $('#askTitle').textContent = '还没有可确认的 AP';
    $('#askSub').textContent = '请回到第一步，勾选本公司的 BSS 并把它们归到各自的 AP 名下。';
    $('#askList').innerHTML = '';
    $('#askHint').textContent = '';
    $('#btnPrev').disabled = true;
    $('#btnNext').disabled = true;
    renderAdjSummary();
    return;
  }

  $('#btnNext').disabled = false;
  const cur = state.cur;
  const me = MY_APS[cur];

  $('#apnav').innerHTML = MY_APS.map((k, i) => {
    const cls = state.done.has(k) ? 'done' : (i === cur ? 'cur' : '');
    const tick = state.done.has(k)
      ? '<svg class="tick" viewBox="0 0 16 16" fill="#067647"><path d="M6.2 11.4L3 8.2l1-1 2.2 2.2L12 3.6l1 1z"/></svg>'
      : '';
    return `<div class="apdot ${cls}" data-goto="${i}">${tick}${escapeHtml(k)}</div>`;
  }).join('');
  $('#apnav').querySelectorAll('[data-goto]').forEach(el => {
    el.addEventListener('click', () => { state.cur = +el.dataset.goto; renderAsk(); });
  });

  $('#adjProgress').textContent = '第 ' + (cur + 1) + ' / ' + n + ' 台';
  $('#askTitle').textContent = me + ' 旁边还有哪些 AP？';

  if (n === 1) {
    $('#askSub').textContent = '只圈出了 1 台 AP，没有需要确认的相邻关系，直接进入下一步即可。';
  } else if (cur === 0) {
    $('#askSub').textContent = `点一下切换「靠近 / 不靠近」。这是第一台，下面 ${n - 1} 项都要你亲自判断。`;
  } else {
    $('#askSub').textContent = '灰色带锁的项，前面已经确认过了，不用重复选 —— 锁住是为了避免和前面的回答自相矛盾。'
      + `要改就点上面的 ${MY_APS[0]} 回去改。`;
  }

  $('#askList').innerHTML = MY_APS.map((other, j) => {
    if (other === me) return '';
    const key = adjKey(me, other);
    const locked = j < cur;                     // 前面那台的回答已经定死了这一对
    const val = state.adj[key] === true;
    const guessed = !state.touched.has(key);
    const tag = locked
      ? `<span class="mini lock">已由 ${escapeHtml(other)} 确认</span>`
      : guessed
        ? '<span class="mini guess">推测值，请核对</span>'
        : '<span class="mini edited">你已核对</span>';
    const ch = chOf(other);
    return `<div class="askrow ${locked ? 'locked' : ''}" data-pair="${key}" data-locked="${locked ? 1 : 0}">
      <div class="l">
        <div class="nm">${escapeHtml(other)} ${tag}</div>
        <div class="mt">5G ${ch === null ? '未开' : 'ch' + ch} · ${rssiOf(other)} dBm · ${deltaTag(me, other)}</div>
      </div>
      <div class="toggle2">
        <span class="y ${val ? 'on' : ''}">靠近</span>
        <span class="n ${!val ? 'on' : ''}">不靠近</span>
      </div>
    </div>`;
  }).join('');

  $('#askList').querySelectorAll('.askrow').forEach(el => {
    if (el.dataset.locked === '1') return;      // 锁住的行不可点
    el.addEventListener('click', () => {
      const key = el.dataset.pair;
      state.adj[key] = !(state.adj[key] === true);
      state.touched.add(key);
      renderAsk();
    });
  });

  renderAdjSummary();
  $('#askHint').textContent = '还剩 ' + (n - state.done.size) + ' 台待确认';
  $('#btnPrev').disabled = cur === 0;
  $('#btnNext').textContent = cur === n - 1 ? '完成，生成规划建议' : '这台确认了，下一台';
}

function renderAdjSummary() {
  const pairs = [];
  for (let i = 0; i < MY_APS.length; i++) {
    for (let j = i + 1; j < MY_APS.length; j++) {
      if (state.adj[adjKey(MY_APS[i], MY_APS[j])] === true) {
        pairs.push(MY_APS[i] + '–' + MY_APS[j]);
      }
    }
  }
  $('#adjSummary').innerHTML = pairs.length
    ? pairs.map(p => `<span class="p">${escapeHtml(p)}</span>`).join('')
    : '<span class="empty">还没有确认任何相邻关系</span>';
}

/* 「全部靠近 / 全部不靠近」只作用于还没锁的项 */
function setFromCur(val) {
  MY_APS.forEach((other, j) => {
    if (j > state.cur) {
      const key = adjKey(MY_APS[state.cur], other);
      state.adj[key] = val;
      state.touched.add(key);
    }
  });
  renderAsk();
}

/* ---------- 已确认的相邻关系，供导出和规划使用 ---------- */
function confirmedPairs() {
  const pairs = [];
  for (let i = 0; i < MY_APS.length; i++) {
    for (let j = i + 1; j < MY_APS.length; j++) {
      if (isAdj(MY_APS[i], MY_APS[j])) pairs.push(MY_APS[i] + '–' + MY_APS[j]);
    }
  }
  return pairs;
}

function initStep2() {
  $('#btnAllY').addEventListener('click', () => setFromCur(true));
  $('#btnAllN').addEventListener('click', () => setFromCur(false));
  $('#btnReGuess').addEventListener('click', () => {
    const guessed = guessAdjacency();
    MY_APS.forEach((other, j) => {
      if (j > state.cur) {
        const key = adjKey(MY_APS[state.cur], other);
        state.adj[key] = guessed[key];
        state.touched.delete(key);
      }
    });
    renderAsk();
  });
  $('#btnPrev').addEventListener('click', () => {
    if (state.cur > 0) { state.cur--; renderAsk(); }
  });
  $('#btnNext').addEventListener('click', () => {
    if (!MY_APS.length) return;
    state.done.add(MY_APS[state.cur]);
    if (state.cur < MY_APS.length - 1) {
      state.cur++;
      renderAsk();
    } else {
      renderAsk();
      go(3);
    }
  });
}
