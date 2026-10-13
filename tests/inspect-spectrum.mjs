/* 一次性查询脚本：把频谱图每个格子的真实状态打出来，用来核对渲染结果。
   用法：node tests/inspect-spectrum.mjs [url] */

import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const BASE_URL = (process.argv[2] || 'http://127.0.0.1:8765/').replace(/#.*$/, '');
const PORT = 9344;

const CHROME = [
  process.env.CHROME_PATH,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'
].filter(Boolean).find(p => { try { return fs.statSync(p).isFile(); } catch { return false; } });

const sleep = ms => new Promise(r => setTimeout(r, ms));

const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ap-inspect-'));
const chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-sandbox',
  `--remote-debugging-port=${PORT}`, `--user-data-dir=${userDataDir}`, 'about:blank'], { stdio: 'ignore' });

let out;
try {
  let list = null;
  for (let i = 0; i < 80 && !list; i++) {
    await sleep(200);
    try { list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json(); } catch { /* retry */ }
  }
  const ws = new WebSocket(list.find(t => t.type === 'page').webSocketDebuggerUrl);
  await new Promise(r => ws.addEventListener('open', r, { once: true }));

  let id = 0;
  const pending = new Map();
  ws.addEventListener('message', e => {
    const m = JSON.parse(e.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result); pending.delete(m.id); }
  });
  const send = (method, params = {}) => new Promise(res => {
    const myId = ++id;
    pending.set(myId, res);
    ws.send(JSON.stringify({ id: myId, method, params }));
  });
  const evaluate = async expr => (await send('Runtime.evaluate', { expression: expr, returnByValue: true })).result.value;

  await send('Page.enable');
  await send('Runtime.enable');
  await send('Page.navigate', { url: BASE_URL });
  for (let i = 0; i < 60; i++) {
    if (await evaluate('document.readyState') === 'complete') break;
    await sleep(150);
  }
  await sleep(600);
  await evaluate('go(3)');
  await sleep(800);

  out = await evaluate(`(function () {
    const slots = Array.from(document.querySelectorAll('#spectrum .slot'));
    return slots.map(el => ({
      ch: el.querySelector('.ch').textContent.trim(),
      fq: el.querySelector('.fq').textContent.trim(),
      ours: el.querySelector('.ours span') ? el.querySelector('.ours span').textContent.trim() : '',
      label: el.querySelector('.nm').textContent.trim(),
      bar: el.querySelector('.bar') ? el.querySelector('.bar').className.replace('bar ', '') : '无',
      height: el.querySelector('.bar') ? el.querySelector('.bar').style.height : '',
      noise: el.classList.contains('noise')
    }));
  })()`);

  const five = out.filter(s => Number(s.ch) >= 36);
  const two = out.filter(s => Number(s.ch) < 36);

  const fmt = s => `  ch${String(s.ch).padEnd(4)} ${s.fq.padEnd(10)} 临时分配=${(s.ours || '—').padEnd(9)}`
    + ` 邻居=${(s.label || '—').replace(/\\n/g, ',').padEnd(18)} 柱=${s.bar.padEnd(4)} ${s.height.padEnd(5)}`
    + (s.noise ? ' (重叠区)' : '');

  console.log('===== 2.4 GHz =====');
  two.forEach(s => console.log(fmt(s)));
  console.log('\n===== 5 GHz =====');
  five.forEach(s => console.log(fmt(s)));
} finally {
  try { chrome.kill(); } catch { /* ignore */ }
  await sleep(300);
  try { fs.rmSync(userDataDir, { recursive: true, force: true }); } catch { /* ignore */ }
}
