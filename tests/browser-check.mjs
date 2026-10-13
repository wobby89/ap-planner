/* ==========================================================================
   真实浏览器检查（用系统已装的 Chrome/Edge，不额外下载 Chromium）

   做两件事：
     * 把三个步骤各截一张图，方便肉眼确认样式没拆坏
     * 采集控制台报错与未捕获异常 —— jsdom 覆盖不到真实的排版和渲染

   通过 DevTools Protocol 驱动，只用 Node 自带的 WebSocket，无需安装依赖。

     node tests/browser-check.mjs                       检查 http://127.0.0.1:8765/
     node tests/browser-check.mjs --url http://127.0.0.1:8765/ --out shots
     node tests/browser-check.mjs --select              截图前先应用一次默认筛选
     CHROME_PATH="D:/chrome.exe" node tests/browser-check.mjs
   ========================================================================== */

import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import process from 'node:process';

/* ---------------------------------------------------------------- 参数 */
const argv = process.argv.slice(2);
const argOf = (name, fallback) => {
  const i = argv.indexOf('--' + name);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : fallback;
};

const BASE_URL = argOf('url', 'http://127.0.0.1:8765/').replace(/#.*$/, '');
const OUT_DIR = path.resolve(argOf('out', 'tests/screenshots'));
const WIDTH = Number(argOf('width', 1280));
const HEIGHT = Number(argOf('height', 900));
const PORT = Number(argOf('port', 9333));

/* 截图前先跑一次 applyFilterRule()。不这么做的话第一步是「一条都没勾」的状态，
   第二 / 第三步因为没有分好组的 AP 而只剩一句提示，截图完全没法用。 */
const SELECT_FIRST = argv.includes('--select');

const CHROME_CANDIDATES = [
  process.env.CHROME_PATH,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
  '/usr/bin/google-chrome',
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
].filter(Boolean);

const chromePath = CHROME_CANDIDATES.find(p => {
  try { return fs.statSync(p).isFile(); } catch { return false; }
});

if (!chromePath) {
  console.error('找不到 Chrome / Edge，可以用 CHROME_PATH 指定路径。');
  process.exit(2);
}

/* ---------------------------------------------------------------- 工具 */
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function waitForJson(url, timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(url);
      if (res.ok) return await res.json();
    } catch { /* 还没起来 */ }
    await sleep(150);
  }
  throw new Error('等待 Chrome 调试端口超时：' + url);
}

/* 极简 CDP 客户端 */
class Cdp {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    this.listeners = new Map();
    ws.addEventListener('message', event => {
      const msg = JSON.parse(event.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        msg.error ? reject(new Error(msg.error.message)) : resolve(msg.result);
      } else if (msg.method) {
        (this.listeners.get(msg.method) || []).forEach(fn => fn(msg.params));
      }
    });
  }

  send(method, params = {}) {
    const id = ++this.id;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => this.pending.set(id, { resolve, reject }));
  }

  on(method, fn) {
    if (!this.listeners.has(method)) this.listeners.set(method, []);
    this.listeners.get(method).push(fn);
  }

  onceEvent(method, timeoutMs = 15000) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('等待事件超时：' + method)), timeoutMs);
      this.on(method, params => { clearTimeout(timer); resolve(params); });
    });
  }
}

/* ---------------------------------------------------------------- 主流程 */
const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ap-planner-chrome-'));
const chrome = spawn(chromePath, [
  '--headless=new',
  '--disable-gpu',
  '--no-sandbox',
  '--hide-scrollbars',
  '--disable-extensions',
  '--no-first-run',
  `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${userDataDir}`,
  `--window-size=${WIDTH},${HEIGHT}`,
  'about:blank'
], { stdio: 'ignore' });

let cdp;
try {
  await waitForJson(`http://127.0.0.1:${PORT}/json/version`);
  const targets = await waitForJson(`http://127.0.0.1:${PORT}/json/list`);
  const page = targets.find(t => t.type === 'page');
  if (!page) throw new Error('没有可用的页面目标');

  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener('open', resolve, { once: true });
    ws.addEventListener('error', () => reject(new Error('调试 WebSocket 连接失败')), { once: true });
  });
  cdp = new Cdp(ws);

  const problems = [];
  await cdp.send('Page.enable');
  await cdp.send('Runtime.enable');
  await cdp.send('Log.enable');

  cdp.on('Runtime.exceptionThrown', p => {
    const d = p.exceptionDetails;
    problems.push(`未捕获异常：${d.exception?.description || d.text}`);
  });
  cdp.on('Runtime.consoleAPICalled', p => {
    if (p.type === 'error' || p.type === 'warning') {
      problems.push(`console.${p.type}：${p.args.map(a => a.value ?? a.description).join(' ')}`);
    }
  });
  cdp.on('Log.entryAdded', p => {
    if (p.entry.level === 'error') {
      problems.push(`页面日志：${p.entry.text}${p.entry.url ? '  <- ' + p.entry.url : ''}`);
    }
  });

  fs.mkdirSync(OUT_DIR, { recursive: true });

  /* 三个步骤各截几张：步骤内定位到指定元素，让关键区域落在视口里 */
  const shots = [
    { step: 1, scrollTo: null, name: 'step1-scan' },
    { step: 2, scrollTo: null, name: 'step2-adjacency' },
    { step: 3, scrollTo: null, name: 'step3-plan-top' },
    { step: 3, scrollTo: '#spectrum', name: 'step3-spectrum' },
    { step: 3, scrollTo: '#findings', name: 'step3-findings' }
  ];

  /* 等页面加载完：直接轮询 readyState，比监听 Page.loadEventFired 稳 */
  async function navigateAndWait(url) {
    await cdp.send('Page.navigate', { url });
    const deadline = Date.now() + 15000;
    while (Date.now() < deadline) {
      const { result } = await cdp.send('Runtime.evaluate', {
        expression: 'document.readyState',
        returnByValue: true
      });
      if (result.value === 'complete') return;
      await sleep(120);
    }
    throw new Error('页面加载超时：' + url);
  }

  const evaluate = expression => cdp.send('Runtime.evaluate', { expression, returnByValue: true })
    .then(r => r.result.value);

  /* 只加载一次页面；换步骤走页面自己的 go()，顺便把切步骤这条路径也测了 */
  await navigateAndWait(BASE_URL);
  await sleep(700);

  if (SELECT_FIRST) {
    const picked = await evaluate(
      'typeof applyFilterRule === "function"'
      + ' ? (applyFilterRule(), document.querySelectorAll("#tbody tr.mine").length)'
      + ' : -1'
    );
    console.log(`已应用默认筛选：圈中 ${picked} 条 BSS\n`);
    await sleep(400);
  }

  const results = [];
  for (const shot of shots) {
    await evaluate(`go(${shot.step})`);
    await sleep(450);

    if (shot.scrollTo) {
      // 等 go() 的平滑滚动停下来，再自己定位过去
      await evaluate(`(() => {
        const el = document.querySelector('${shot.scrollTo}');
        if (el) window.scrollTo({ top: el.getBoundingClientRect().top + window.scrollY - 60, behavior: 'instant' });
        return !!el;
      })()`);
      await sleep(300);
    }

    const { data } = await cdp.send('Page.captureScreenshot', { format: 'png' });
    const file = path.join(OUT_DIR, shot.name + '.png');
    fs.writeFileSync(file, Buffer.from(data, 'base64'));

    const meta = await evaluate('({ rows: document.querySelectorAll("#tbody tr").length, '
      + 'slots: document.querySelectorAll("#spectrum .slot").length, '
      + 'banners: document.querySelectorAll("#banners .banner").length, '
      + 'findings: document.querySelectorAll("#findings .item").length, '
      + 'scrollH: document.documentElement.scrollHeight })');
    results.push({ name: shot.name, file, ...meta });
  }

  console.log('浏览器：' + chromePath);
  console.log(`页面高度：${results[0].scrollH}px　输出目录：${OUT_DIR}\n`);
  for (const r of results) {
    console.log(`  ${r.name.padEnd(20)} 表格 ${String(r.rows).padStart(3)} 行 · `
      + `信道格 ${String(r.slots).padStart(2)} 个 · 横幅 ${r.banners} 条 · 发现卡 ${r.findings} 张`);
  }

  console.log();
  if (problems.length) {
    console.log(`控制台问题 ${problems.length} 条：`);
    problems.forEach(p => console.log('  - ' + p));
    process.exitCode = 1;
  } else {
    console.log('控制台干净：没有报错，也没有未捕获异常。');
  }
} finally {
  try { await cdp?.send('Browser.close'); } catch { /* 已经关了 */ }
  await sleep(300);
  if (!chrome.killed) chrome.kill();
  await sleep(300);
  try { fs.rmSync(userDataDir, { recursive: true, force: true }); } catch { /* 占用中就算了 */ }
}
