/**
 * 執行視圖：同一張圖，節點依狀態上色，事件即時串流進時間軸。
 *
 * 用的是和編輯器一樣的 Drawflow 畫布與轉換層 —— 你在編輯器排的版，執行時
 * 看到的就是同一張圖，不用在腦中對應兩種不同的呈現。
 *
 * 斷線重連：EventSource 內建重連，並帶 Last-Event-ID，後端會從那個序號之後
 * 補送，所以中途關掉分頁再打開不會漏事件。
 */

import { toDrawflow } from './graph.js';

const $ = (s) => document.querySelector(s);
const runId = $('[data-run-id]').dataset.runId;

const state = {
  editor: null,
  nodes: new Map(),      // nid -> { dfId, status, events: [], out: {} }
  events: [],
  selected: null,
  finished: false,
  lastSeq: 0,
};

const STATUS_BADGE = {
  passed: 'bg-emerald-500/15 text-emerald-300',
  failed: 'bg-rose-500/15 text-rose-300',
  running: 'bg-sky-500/15 text-sky-300',
  cancelled: 'bg-slate-500/15 text-slate-300',
  queued: 'bg-amber-500/15 text-amber-300',
};

// 這些 kind 平常摺起來，勾「顯示原始輸出」才出現。
// agent 的原始 stdout 量很大，預設全開會把真正重要的訊息淹掉。
const NOISY = new Set(['stdout']);

const KIND_ICON = {
  status: '•', message: '💬', reasoning: '🤔', tool_call: '⚙',
  tool_result: '↩', file_edit: '✎', usage: '∑', error: '✕', stdout: '·',
};

// ------------------------------------------------------------------ 啟動

async function boot() {
  const run = await fetch(`/api/runs/${runId}`).then((r) => r.json());
  if (run.error) { $('#pane-timeline').textContent = run.error; return; }

  $('#run-name').textContent = run.workflow_name || '(未命名)';
  $('#run-requirement').textContent = run.requirement || '(沒有需求文字)';
  $('#run-branch').textContent = run.branch || '';
  applyRunStatus(run);

  initCanvas(run.graph);
  for (const node of run.nodes || []) {
    setNodeStatus(node.node_id, node.status);
    entry(node.node_id).out = node;
  }

  connect();
}

function initCanvas(graph) {
  const editor = new Drawflow($('#canvas'));
  editor.reroute = true;
  editor.curvature = 0.5;
  editor.editor_mode = 'fixed';   // 唯讀：可平移縮放，不能改結構
  editor.start();
  editor.import(toDrawflow(graph, nodeHtml));
  state.editor = editor;

  for (const [dfId, node] of Object.entries(editor.export().drawflow.Home.data)) {
    entry(node.data.nid).dfId = dfId;
  }

  // Drawflow 是從接點元素的實際座標算連線路徑的，import 當下版面還沒完成
  requestAnimationFrame(() => {
    for (const id of Object.keys(editor.export().drawflow.Home.data)) {
      editor.updateConnectionNodes(`node-${id}`);
    }
    fitView();
  });

  $('#canvas').addEventListener('mousedown', (e) => {
    const el = e.target.closest('.drawflow-node');
    if (!el) return;
    const dfId = el.id.replace('node-', '');
    const raw = editor.getNodeFromId(dfId);
    selectNode(raw.data.nid);
  });
}

function fitView() {
  const nodes = Object.values(state.editor.export().drawflow.Home.data);
  if (!nodes.length) return;
  const minX = Math.min(...nodes.map((n) => n.pos_x));
  const minY = Math.min(...nodes.map((n) => n.pos_y));
  const maxX = Math.max(...nodes.map((n) => n.pos_x + 210));
  const maxY = Math.max(...nodes.map((n) => n.pos_y + 110));
  const box = $('#canvas').getBoundingClientRect();
  const pad = 30;
  const zoom = Math.max(0.5, Math.min(1,
    (box.width - pad * 2) / Math.max(1, maxX - minX),
    (box.height - pad * 2) / Math.max(1, maxY - minY)));

  const ed = state.editor;
  ed.zoom = zoom;
  ed.canvas_x = pad - minX * zoom;
  ed.canvas_y = pad - minY * zoom;
  ed.precanvas.style.transformOrigin = '0 0';
  ed.precanvas.style.transform = `translate(${ed.canvas_x}px, ${ed.canvas_y}px) scale(${zoom})`;
  for (const id of Object.keys(ed.export().drawflow.Home.data)) {
    ed.updateConnectionNodes(`node-${id}`);
  }
}

function nodeHtml(node) {
  return `
    <div class="nd-head">
      <span class="dot dot-${node.type}"></span>
      <span class="nd-title">${esc(node.label || node.id)}</span>
      <span class="nd-badge" data-badge="${node.id}"></span>
    </div>
    <div class="nd-type">${esc(node.type)}</div>
    <div class="nd-sum" data-live="${node.id}"></div>`;
}

// ------------------------------------------------------------------ SSE

function connect() {
  const url = `/api/runs/${runId}/events` + (state.lastSeq ? `?after=${state.lastSeq}` : '');
  const es = new EventSource(url);

  // 所有事件都走預設型別，讀 data.kind 分流。後端刻意不設 event: <kind>，
  // 否則 onmessage 只收得到 kind === "message" 的那些。
  es.onmessage = (e) => {
    const event = JSON.parse(e.data);
    state.lastSeq = Math.max(state.lastSeq, event.seq);
    ingest(event);
  };

  es.addEventListener('done', async (e) => {
    es.close();
    state.finished = true;
    const final = JSON.parse(e.data);
    const run = await fetch(`/api/runs/${runId}`).then((r) => r.json());
    applyRunStatus(run);
    for (const node of run.nodes || []) {
      setNodeStatus(node.node_id, node.status);
      entry(node.node_id).out = node;
    }
    if (state.selected) selectNode(state.selected);
    appendTimeline({ seq: 1e9, node_id: '', kind: final.status === 'passed' ? 'status' : 'error',
                     text: `${final.status}${final.reason ? '：' + final.reason : ''}`,
                     data: { phase: 'run_end' } });
  });

  es.onerror = () => { if (state.finished) es.close(); };  // 未結束就交給瀏覽器自動重連
}

function ingest(event) {
  state.events.push(event);
  const phase = event.data?.phase;

  if (event.node_id) {
    const e = entry(event.node_id);
    e.events.push(event);
    if (phase === 'node_start') setNodeStatus(event.node_id, 'running');
    else if (phase === 'node_end') setNodeStatus(event.node_id, event.data.result);
    else if (event.kind === 'error') setNodeStatus(event.node_id, 'failed');

    if (['message', 'tool_call', 'file_edit', 'status'].includes(event.kind)) {
      const live = document.querySelector(`[data-live="${event.node_id}"]`);
      if (live && event.text) live.textContent = event.text.split('\n')[0].slice(0, 60);
    }
    if (state.selected === event.node_id) renderNodePane(event.node_id);
  }

  if (phase === 'run_end') {
    // 直接用事件裡帶的狀態更新標頭，不再多發一個 request。
    // 原本是靠 run_end 觸發 fetch 才更新，那個 fetch 一慢（例如伺服器執行緒被
    // 沒關掉的 SSE 連線佔住），標頭就會一直卡在 running，而且不會自己恢復。
    applyRunStatus({
      status: event.data.status || 'passed',
      reason: event.data.reason || '',
      branch: event.data.branch || $('#run-branch').textContent,
    });
  }
  appendTimeline(event);
}

// ------------------------------------------------------------- 節點狀態

function entry(nid) {
  if (!state.nodes.has(nid)) state.nodes.set(nid, { dfId: null, status: 'pending', events: [], out: {} });
  return state.nodes.get(nid);
}

function setNodeStatus(nid, status) {
  const e = entry(nid);
  e.status = status || 'pending';
  if (!e.dfId) return;
  const el = document.getElementById(`node-${e.dfId}`);
  if (!el) return;
  el.classList.remove('st-running', 'st-passed', 'st-failed', 'st-skipped', 'st-cancelled');
  if (e.status !== 'pending') el.classList.add(`st-${e.status}`);
  const badge = el.querySelector(`[data-badge="${nid}"]`);
  if (badge) badge.textContent = { running: '⏳', passed: '✓', failed: '✕',
                                   skipped: '–', cancelled: '⏹' }[e.status] || '';
}

// -------------------------------------------------------------- 時間軸

function appendTimeline(event) {
  const host = $('#pane-timeline');
  const atBottom = host.scrollHeight - host.scrollTop - host.clientHeight < 60;

  const row = document.createElement('div');
  row.className = `tl tl-${event.kind}` + (NOISY.has(event.kind) ? ' tl-noise' : '');
  const long = (event.text || '').length > 160 || (event.text || '').includes('\n');

  row.innerHTML = `
    <span class="tl-icon">${KIND_ICON[event.kind] || '·'}</span>
    <span class="tl-node">${esc(event.node_id || '—')}</span>
    <div class="tl-body">${
      long
        ? `<details><summary>${esc((event.text || '').split('\n')[0].slice(0, 120))}</summary><pre>${esc(event.text)}</pre></details>`
        : esc(event.text || '')
    }</div>`;
  host.appendChild(row);

  if (atBottom) host.scrollTop = host.scrollHeight;
}

$('#show-noise').addEventListener('change', (e) => {
  $('#pane-timeline').classList.toggle('show-noise', e.target.checked);
});

// -------------------------------------------------------------- 節點面板

function selectNode(nid) {
  state.selected = nid;
  switchTab('node');
  renderNodePane(nid);
}

function renderNodePane(nid) {
  const e = entry(nid);
  const out = e.out || {};
  const usage = out.usage || {};
  const usageBits = Object.entries(usage)
    .filter(([, v]) => typeof v === 'number' && v)
    .map(([k, v]) => `${k}=${typeof v === 'number' && k.includes('cost') ? '$' + v.toFixed(4) : v}`);

  $('#pane-node').innerHTML = `
    <div class="mb-2 flex items-center gap-2">
      <span class="text-sm font-semibold">${esc(out.label || nid)}</span>
      <span class="rounded px-1.5 py-0.5 text-[10px] ${STATUS_BADGE[e.status] || 'bg-slate-700'}">${e.status}</span>
      <code class="ml-auto text-[10px] text-slate-500">${esc(nid)}</code>
    </div>
    ${meta('造訪次數', out.visits)}
    ${meta('exit code', out.exit_code)}
    ${meta('session', out.session_id)}
    ${usageBits.length ? meta('用量', usageBits.join('  ')) : ''}
    ${out.files?.length ? `<div class="sec"><div class="sec-t">改動檔案</div><ul class="text-xs">${
        out.files.map((f) => `<li class="font-mono text-emerald-300">${esc(f)}</li>`).join('')}</ul></div>` : ''}
    ${out.structured !== null && out.structured !== undefined
        ? `<div class="sec"><div class="sec-t">結構化輸出</div><pre class="blk">${esc(JSON.stringify(out.structured, null, 2))}</pre></div>` : ''}
    ${out.last_message ? `<div class="sec"><div class="sec-t">最終回覆</div><pre class="blk">${esc(out.last_message)}</pre></div>` : ''}
    ${/* shell 節點沒有 normalizer，輸出走 stdout 不是 message，所以要另外顯示 */''}
    ${out.stdout && out.stdout !== out.last_message
        ? `<div class="sec"><div class="sec-t">stdout</div><pre class="blk">${esc(out.stdout)}</pre></div>` : ''}
    <div class="sec"><div class="sec-t">這個節點的事件（${e.events.length}）</div>
      ${e.events.filter((x) => !NOISY.has(x.kind)).slice(-40).map((x) =>
        `<div class="text-[11px] text-slate-400"><span class="text-slate-600">${KIND_ICON[x.kind] || '·'}</span> ${esc((x.text || '').slice(0, 200))}</div>`).join('')}
    </div>`;
}

function meta(label, value) {
  if (value === null || value === undefined || value === '') return '';
  return `<div class="text-xs"><span class="text-slate-500">${label}：</span><span class="font-mono">${esc(String(value))}</span></div>`;
}

// ------------------------------------------------------------ 變更 / 產物

async function loadDiff() {
  const host = $('#pane-diff');
  host.innerHTML = '<span class="text-xs text-slate-500">載入中…</span>';
  const d = await fetch(`/api/runs/${runId}/diff`).then((r) => r.json());
  if (d.error) { host.textContent = d.error; return; }
  if (!d.diff) { host.innerHTML = '<span class="text-xs text-slate-500">這個 run 沒有產生任何變更。</span>'; return; }

  host.innerHTML = `
    <div class="sec">
      <div class="sec-t">合併指令</div>
      <pre class="blk select-all">${esc(d.merge_command)}</pre>
    </div>
    <div class="sec"><div class="sec-t">commits</div><pre class="blk">${esc(d.log)}</pre></div>
    <div class="sec"><div class="sec-t">統計</div><pre class="blk">${esc(d.stat)}</pre></div>
    <div class="sec"><div class="sec-t">diff</div><pre class="blk diff">${colourDiff(d.diff)}</pre></div>`;
}

function colourDiff(text) {
  return esc(text).split('\n').map((line) => {
    if (line.startsWith('+++') || line.startsWith('---')) return `<span class="d-meta">${line}</span>`;
    if (line.startsWith('@@')) return `<span class="d-hunk">${line}</span>`;
    if (line.startsWith('+')) return `<span class="d-add">${line}</span>`;
    if (line.startsWith('-')) return `<span class="d-del">${line}</span>`;
    if (line.startsWith('diff --git')) return `<span class="d-file">${line}</span>`;
    return line;
  }).join('\n');
}

async function loadArtifacts() {
  const host = $('#pane-artifacts');
  const { artifacts } = await fetch(`/api/runs/${runId}/artifacts`).then((r) => r.json());
  if (!artifacts.length) {
    host.innerHTML = '<span class="text-xs text-slate-500">沒有產物。（QA 節點設了 schema 才會產生）</span>';
    return;
  }
  host.innerHTML = artifacts.map((a) => `
    <div class="sec">
      <div class="sec-t">${esc(a.name)} <span class="text-slate-600">${a.size} bytes</span></div>
      <pre class="blk">${esc(a.content)}</pre>
    </div>`).join('');
}

// ------------------------------------------------------------------ 其他

function applyRunStatus(run) {
  const badge = $('#run-status');
  badge.textContent = run.status;
  badge.className = `rounded px-2 py-0.5 text-xs ${STATUS_BADGE[run.status] || 'bg-slate-700'}`;
  $('#btn-cancel').classList.toggle('hidden', !['running', 'queued'].includes(run.status));
  if (run.branch) $('#run-branch').textContent = run.branch;

  const reason = $('#run-reason');
  reason.classList.toggle('hidden', !run.reason);
  reason.textContent = run.reason || '';
  reason.className = run.reason
    ? 'border-b border-slate-700 px-3 py-2 text-xs text-amber-400'
    : 'hidden';
}

$('#btn-cancel').addEventListener('click', async () => {
  await fetch(`/api/runs/${runId}/cancel`, { method: 'POST' });
});

function switchTab(name) {
  document.querySelectorAll('.tab').forEach((t) =>
    t.classList.toggle('tab-on', t.dataset.tab === name));
  document.querySelectorAll('.pane').forEach((p) =>
    p.classList.toggle('hidden', p.id !== `pane-${name}`));
  if (name === 'diff') loadDiff();
  if (name === 'artifacts') loadArtifacts();
}
document.querySelectorAll('.tab').forEach((t) =>
  t.addEventListener('click', () => switchTab(t.dataset.tab)));

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

window.__runDebug = { state, selectNode, switchTab };

boot();
