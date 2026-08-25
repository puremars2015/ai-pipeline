/**
 * 編輯器主程式：節點面板拖曳、Drawflow 畫布、節點設定表單、存檔 / 驗證 / 執行。
 *
 * 節點設定表單是由後端 /api/adapters 回傳的 fields 動態生成的 —— 新增一個
 * adapter 只要放 yaml，表單自動出現，前端不用改。
 */

import { fromDrawflow, toDrawflow, portsOf, hasInput, makeNodeId } from './graph.js';
import { activeProject, NO_PROJECT_HINT } from './project.js';

const $ = (sel) => document.querySelector(sel);

const state = {
  editor: null,
  specs: new Map(),   // type -> spec（adapters + builtins 合併）
  current: null,      // 目前選中的 Drawflow 節點 id
  workflowId: '',
  workflowName: '未命名工作流',
  settings: {},
  project: null,     // 工作流住在專案裡，沒有專案就沒有工作流可存取
  dirty: false,      // 畫布上有沒有還沒存回專案資料夾的變更
  leaving: false,    // 我們自己發動的跳轉，別再問一次「確定要離開嗎」
};

// 切換專案、關掉分頁、重新整理都會讓編到一半的東西消失。用 beforeunload 而不是
// 只攔選擇器 —— 這樣三種情況一次都顧到，也不必讓頁首知道編輯器的內部狀態。
window.addEventListener('beforeunload', (e) => {
  if (state.dirty && !state.leaving) e.preventDefault();
});

/** 這個專案的工作流端點。 */
const wfUrl = (path = '') =>
  `/api/projects/${encodeURIComponent(state.project.id)}/workflows${path}`;

const KIND_LABEL = { builtin: '內建', agent: 'Agent', shell: '指令', mock: '測試' };

// ------------------------------------------------------------------ 啟動

async function boot() {
  const { adapters, builtins } = await fetch('/api/adapters').then((r) => r.json());
  for (const spec of [...builtins, ...adapters]) state.specs.set(spec.id, spec);

  renderPalette([...builtins, ...adapters]);
  initEditor();

  // 專案的挑選與記憶由頁首的選擇器負責（base.html 掛的 mountPicker）
  state.project = await activeProject();
  if (!state.project) {
    showProblems([NO_PROJECT_HINT]);
    status('用右上角的「管理」加一個專案', 'bad');
    return;
  }

  const ids = await loadWorkflowList();
  const wanted = new URLSearchParams(location.search).get('wf');
  // 沒有指定就開第一個；專案是空的（剛註冊）就直接給一張空畫布
  if (wanted && ids.includes(wanted)) await loadWorkflow(wanted);
  else if (ids.length) await loadWorkflow(ids[0]);
  else newWorkflow();
}

function initEditor() {
  const container = $('#canvas');
  const editor = new Drawflow(container);
  editor.reroute = true;
  editor.curvature = 0.5;
  editor.start();

  editor.on('nodeSelected', (id) => selectNode(id));
  editor.on('nodeUnselected', () => selectNode(null));
  editor.on('nodeRemoved', () => { selectNode(null); markDirty(); });
  editor.on('nodeMoved', markDirty);
  editor.on('connectionCreated', markDirty);
  editor.on('connectionRemoved', markDirty);

  container.addEventListener('drop', onDrop);
  container.addEventListener('dragover', (e) => e.preventDefault());

  // 視窗大小改變後連線的幾何會失準（算的是元素的實際座標）
  window.addEventListener('resize', () => refreshConnections());

  state.editor = editor;
}

/**
 * 重算所有連線的幾何。
 *
 * Drawflow 是從接點元素的 getBoundingClientRect() 推算連線路徑的，所以 import()
 * 當下若版面還沒完成，算出來的全是 "M 5.5 5.5 C …" 這種退化路徑 —— 節點畫得出來
 * 但線completely看不見。等下一個 animation frame 版面穩定後再重算一次才會正確。
 */
function refreshConnections() {
  requestAnimationFrame(() => {
    const data = state.editor.export().drawflow.Home.data;
    for (const id of Object.keys(data)) state.editor.updateConnectionNodes(`node-${id}`);
  });
}

/** 把畫布平移縮放到剛好裝得下所有節點。 */
function fitView() {
  const data = state.editor.export().drawflow.Home.data;
  const nodes = Object.values(data);
  if (!nodes.length) return;

  const NODE_W = 210;
  const NODE_H = 110;
  const minX = Math.min(...nodes.map((n) => n.pos_x));
  const minY = Math.min(...nodes.map((n) => n.pos_y));
  const maxX = Math.max(...nodes.map((n) => n.pos_x + NODE_W));
  const maxY = Math.max(...nodes.map((n) => n.pos_y + NODE_H));

  const box = $('#canvas').getBoundingClientRect();
  const pad = 40;
  const zoom = Math.min(
    1,
    (box.width - pad * 2) / Math.max(1, maxX - minX),
    (box.height - pad * 2) / Math.max(1, maxY - minY),
  );

  const editor = state.editor;
  // 不縮到看不清字。寧可留給使用者自己拖曳，也不要把節點縮成一團色塊。
  editor.zoom = Math.max(0.55, zoom);
  editor.canvas_x = pad - minX * editor.zoom;
  editor.canvas_y = pad - minY * editor.zoom;
  editor.precanvas.style.transform =
    `translate(${editor.canvas_x}px, ${editor.canvas_y}px) scale(${editor.zoom})`;
  editor.precanvas.style.transformOrigin = '0 0';
  refreshConnections();
}

// ------------------------------------------------------------- 節點面板

function renderPalette(specs) {
  const groups = new Map();
  for (const spec of specs) {
    const key = KIND_LABEL[spec.kind] || spec.kind;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(spec);
  }

  $('#palette').innerHTML = [...groups.entries()]
    .map(([group, items]) => `
      <div class="mb-4">
        <div class="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-slate-500">${group}</div>
        ${items.map((s) => `
          <div class="palette-item ${s.installed ? '' : 'opacity-40'}"
               draggable="${s.installed}" data-type="${s.id}"
               title="${esc(s.description || '')}${s.installed ? '' : '（未安裝）'}">
            <span class="dot dot-${s.id}"></span>
            <span class="flex-1 truncate">${esc(s.label)}</span>
            ${s.mutates ? '<span class="tag" title="會寫檔，與其他寫檔節點序列化執行">寫</span>' : ''}
          </div>`).join('')}
      </div>`).join('');

  $('#palette').querySelectorAll('[draggable=true]').forEach((el) => {
    el.addEventListener('dragstart', (e) => e.dataTransfer.setData('type', el.dataset.type));
  });
}

function onDrop(e) {
  e.preventDefault();
  const type = e.dataTransfer.getData('type');
  const spec = state.specs.get(type);
  if (!spec) return;

  const rect = $('#canvas').getBoundingClientRect();
  const zoom = state.editor.zoom;
  const x = (e.clientX - rect.left) / zoom - state.editor.precanvas.getBoundingClientRect().left / zoom;
  const y = (e.clientY - rect.top) / zoom - state.editor.precanvas.getBoundingClientRect().top / zoom;

  addNode(type, Math.max(20, x), Math.max(20, y));
  markDirty();
}

function existingNids() {
  const graph = fromDrawflow(state.editor.export());
  return new Set(graph.nodes.map((n) => n.id));
}

function addNode(type, x, y) {
  const spec = state.specs.get(type);
  const nid = makeNodeId(type, existingNids());
  const config = {};
  for (const field of spec.fields || []) config[field.name] = field.default;
  if (spec.kind === 'agent' || spec.kind === 'mock') config.prompt = config.prompt || '';

  const node = { id: nid, type, label: spec.label, config, pos: { x, y } };
  state.editor.addNode(
    type,
    hasInput(type) ? 1 : 0,
    portsOf(type).length,
    x, y,
    `nd nd-${type}`,
    { nid, label: spec.label, config },
    nodeHtml(node),
    false,
  );
}

// ------------------------------------------------------------- 節點外觀

function nodeHtml(node) {
  const spec = state.specs.get(node.type) || {};
  const ports = portsOf(node.type);
  const badges = ports.length > 1
    ? `<div class="ports">${ports.map((p) => `<span class="port port-${p}">${p}</span>`).join('')}</div>`
    : '';
  return `
    <div class="nd-head">
      <span class="dot dot-${node.type}"></span>
      <span class="nd-title">${esc(node.label || node.id)}</span>
    </div>
    <div class="nd-type">${esc(spec.label || node.type)}</div>
    <div class="nd-sum">${esc(summarise(node))}</div>
    ${badges}`;
}

function summarise(node) {
  const cfg = node.config || {};
  if (node.type === 'condition') return cfg.expr || '(沒有運算式)';
  if (node.type === 'shell') {
    // 挑第一行「看得出在做什麼」的內容。多行腳本的第一行常常是 set -u 或註解，
    // 拿那個當摘要等於什麼都沒說。
    const line = (cfg.command || '').split('\n')
      .map((l) => l.trim())
      .find((l) => l && !l.startsWith('#') && !/^set\s/.test(l) && !/^[A-Z_]+=/.test(l));
    return line ? line.slice(0, 60) : '(沒有指令)';
  }
  if (node.type === 'git') return `${cfg.action || 'commit'}: ${cfg.message || ''}`.slice(0, 60);
  if (node.type === 'requirement') return cfg.text ? cfg.text.slice(0, 60) : '(用啟動時輸入的需求)';
  const prompt = (cfg.prompt || '').trim();
  return prompt ? prompt.split('\n')[0].slice(0, 60) : '(沒有 prompt)';
}

function refreshNode(dfId) {
  const raw = state.editor.getNodeFromId(dfId);
  const node = { id: raw.data.nid, type: raw.name, label: raw.data.label, config: raw.data.config };
  const el = document.querySelector(`#node-${dfId} .drawflow_content_node`);
  if (el) el.innerHTML = nodeHtml(node);
}

// ------------------------------------------------------------- 設定面板

function selectNode(dfId) {
  state.current = dfId;
  const panel = $('#inspector');
  if (!dfId) {
    panel.innerHTML = '<p class="p-3 text-sm text-slate-500">選一個節點來編輯設定。</p>';
    return;
  }

  const raw = state.editor.getNodeFromId(dfId);
  const spec = state.specs.get(raw.name) || { fields: [] };
  const data = raw.data || {};
  const cfg = data.config || {};

  const fields = [...(spec.fields || [])];
  if (spec.kind === 'agent' || spec.kind === 'mock') {
    fields.unshift({
      name: 'prompt', type: 'textarea', label: 'Prompt（支援 Jinja 模板）',
      default: '', options: [],
      help: '可用 {{ requirement }}、{{ nodes.<id>.last_message }}、{{ nodes.<id>.structured }}、{{ run.diff }}、{{ loop.iteration }}',
    });
  }
  if (spec.supports_schema) {
    fields.push({
      name: 'schema', type: 'textarea', label: '輸出 JSON Schema（選填）',
      default: '', options: [],
      help: '填了就要求 agent 回傳符合 schema 的 JSON，下游可用 nodes.<id>.structured.<欄位> 判斷 —— 比讓它回自由文字再比對字串可靠得多。',
    });
  }

  panel.innerHTML = `
    <div class="p-3">
      <div class="mb-3 flex items-center gap-2">
        <span class="dot dot-${raw.name}"></span>
        <span class="text-sm font-semibold">${esc(spec.label || raw.name)}</span>
        <code class="ml-auto text-[11px] text-slate-500">${esc(data.nid)}</code>
      </div>

      ${textField('__label', '節點名稱', data.label || '', '顯示在畫布上的名字')}
      ${fields.map((f) => renderField(f, cfg[f.name])).join('')}

      <details class="mt-3 rounded border border-slate-700">
        <summary class="cursor-pointer px-2 py-1.5 text-xs text-slate-400">進階</summary>
        <div class="p-2">
          ${selectField('__mutates', '會寫檔案', String(data.mutates ?? spec.mutates),
                        [['true', '是（與其他寫檔節點序列化）'], ['false', '否（可真正平行）']],
                        '唯讀的審查 / 規劃節點設成「否」才能真的平行執行。')}
          ${selectField('__on_error', '失敗時', data.on_error || 'fail',
                        [['fail', '中止整個 run'], ['continue', '繼續，交給下游條件節點判斷']],
                        '跑測試的節點應該用「繼續」—— 測試失敗是訊號，不是意外。')}
          ${selectField('__join', '多入邊時', data.join || 'all',
                        [['all', '等全部上游完成'], ['any', '任一上游到達就跑']], '')}
          ${textField('__max_visits', '最多造訪次數', data.max_visits ?? '', '迴圈保護，留空用全域預設值')}
          ${textField('__timeout_sec', 'Timeout（秒）', data.timeout_sec ?? '', '留空用全域預設值')}
        </div>
      </details>

      <button id="del-node" class="mt-3 w-full rounded border border-rose-800 px-2 py-1.5 text-xs text-rose-300 hover:bg-rose-950">刪除節點</button>
    </div>`;

  panel.querySelectorAll('[data-field]').forEach((input) => {
    input.addEventListener('change', () => applyField(dfId, input));
    if (input.tagName === 'TEXTAREA') {
      input.addEventListener('blur', () => applyField(dfId, input));
    }
  });
  $('#del-node').addEventListener('click', () => {
    state.editor.removeNodeId(`node-${dfId}`);
  });
}

function applyField(dfId, input) {
  const raw = state.editor.getNodeFromId(dfId);
  const data = JSON.parse(JSON.stringify(raw.data || {}));
  data.config = data.config || {};
  const key = input.dataset.field;
  const value = input.value;

  if (key === '__label') {
    data.label = value || data.nid;
  } else if (key === '__mutates') {
    data.mutates = value === 'true';
  } else if (key === '__on_error' || key === '__join') {
    data[key.slice(2)] = value;
  } else if (key === '__max_visits' || key === '__timeout_sec') {
    const n = parseInt(value, 10);
    if (Number.isFinite(n) && n > 0) data[key.slice(2)] = n;
    else delete data[key.slice(2)];
  } else {
    data.config[key] = value;
  }

  state.editor.updateNodeDataFromId(dfId, data);
  refreshNode(dfId);
  markDirty();
}

// -------------------------------------------------------------- 表單元件

function renderField(field, value) {
  const current = value ?? field.default ?? '';
  if (field.type === 'select') {
    return selectField(field.name, field.label, current,
                       field.options.map((o) => [o, o || '(預設)']), field.help);
  }
  if (field.type === 'textarea') {
    return `
      <label class="mb-2 block">
        <span class="fl">${esc(field.label)}</span>
        <textarea data-field="${field.name}" rows="6" class="fi font-mono">${esc(current)}</textarea>
        ${field.help ? `<span class="fh">${esc(field.help)}</span>` : ''}
      </label>`;
  }
  return textField(field.name, field.label, current, field.help,
                   field.type === 'number' ? 'number' : 'text');
}

function textField(name, label, value, help, type = 'text') {
  return `
    <label class="mb-2 block">
      <span class="fl">${esc(label)}</span>
      <input data-field="${name}" type="${type}" value="${esc(String(value ?? ''))}" class="fi">
      ${help ? `<span class="fh">${esc(help)}</span>` : ''}
    </label>`;
}

function selectField(name, label, value, options, help) {
  return `
    <label class="mb-2 block">
      <span class="fl">${esc(label)}</span>
      <select data-field="${name}" class="fi">
        ${options.map(([v, t]) => `<option value="${esc(v)}" ${String(v) === String(value) ? 'selected' : ''}>${esc(t)}</option>`).join('')}
      </select>
      ${help ? `<span class="fh">${esc(help)}</span>` : ''}
    </label>`;
}

// ------------------------------------------------------------ 存 / 讀 / 跑

function currentGraph() {
  return fromDrawflow(state.editor.export(), {
    id: state.workflowId,
    name: $('#wf-name').value || '未命名工作流',
    settings: { ...state.settings, isolation: $('#wf-isolation').value },
  });
}

/** 共用模式下會寫檔的節點是排隊跑的，把這件事直接寫在畫面上。 */
function updateIsolationHint() {
  const perNode = $('#wf-isolation').value === 'per_node';
  const writers = currentGraph().nodes.filter((n) => {
    const spec = state.specs.get(n.type);
    const mutates = n.mutates === undefined ? spec?.mutates : n.mutates;
    return mutates && !['requirement', 'condition'].includes(n.type);
  }).length;

  $('#isolation-hint').innerHTML = perNode
    ? `<span class="text-sky-400">${writers} 個會寫檔的節點各自一個 worktree，可真平行；`
      + `fan-in 時會合併，可能衝突</span>`
    : `<span class="text-slate-500">${writers} 個會寫檔的節點共用一個 worktree，`
      + `會排隊執行</span>`;
}

async function loadWorkflowList() {
  const { workflows } = await fetch(wfUrl()).then((r) => r.json());
  $('#wf-list').innerHTML = workflows
    .map((w) => `<option value="${esc(w.id)}">${esc(w.name)}${w.broken ? ' ⚠ 壞掉' : ''}</option>`)
    .join('');
  return workflows.map((w) => w.id);
}

async function loadWorkflow(id) {
  const resp = await fetch(wfUrl(`/${encodeURIComponent(id)}`));
  if (!resp.ok) { status(`找不到工作流 ${id}`, 'warn'); return; }
  const graph = await resp.json();

  state.workflowId = graph.id || '';
  state.workflowName = graph.name || '未命名工作流';
  state.settings = graph.settings || {};
  $('#wf-name').value = state.workflowName;
  $('#wf-list').value = state.workflowId;
  $('#wf-isolation').value = state.settings.isolation || 'shared';

  state.editor.import(toDrawflow(graph, nodeHtml));
  selectNode(null);
  fitView();
  state.dirty = false;
  updateIsolationHint();
  status(`已載入「${state.workflowName}」`, 'ok');
  validate();
}

async function save({ asNew = false } = {}) {
  const graph = currentGraph();

  // 另存新檔就把 id 拿掉 —— 後端看到沒有 id 才會配一個新的。
  // 沒有這個區分的話，按儲存永遠是覆蓋目前開著的那個（包括隨附的範本），
  // 而且範本被蓋掉之後不會自動還原（seeder 只在 db 裡沒有該 id 時才匯入）。
  if (asNew) delete graph.id;

  const resp = await fetch(wfUrl(), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(graph),
  });
  const body = await resp.json();
  if (!resp.ok) { status(body.error, 'bad'); return; }

  state.workflowId = body.id;
  state.dirty = false;
  await loadWorkflowList();
  $('#wf-list').value = body.id;
  const what = asNew ? '已另存為新的工作流' : '已儲存';
  status(body.problems.length ? `${what}，但有 ${body.problems.length} 個問題` : what,
         body.problems.length ? 'warn' : 'ok');
  showProblems(body.problems);
  return body.id;
}

/** 清空畫布，開一個新的工作流。 */
function newWorkflow() {
  state.workflowId = '';
  state.workflowName = '新工作流';
  state.settings = {};
  state.editor.clear();
  $('#wf-name').value = state.workflowName;
  $('#wf-list').value = '';
  $('#wf-isolation').value = 'shared';
  state.dirty = false;
  selectNode(null);
  updateIsolationHint();
  showProblems(['空的工作流：從左邊拖一個「需求」節點開始。']);
  status('新的工作流（還沒儲存）', 'warn');
}

async function deleteWorkflow() {
  if (!state.workflowId) { status('這個工作流還沒儲存過', 'warn'); return; }
  const name = $('#wf-name').value || state.workflowId;
  if (!confirm(`確定要刪除「${name}」？\n\n（隨附的範本刪掉之後，下次啟動服務會從 workflows/ 目錄重新匯入。）`)) return;

  const resp = await fetch(wfUrl(`/${encodeURIComponent(state.workflowId)}`),
                           { method: 'DELETE' });
  if (!resp.ok) { status('刪除失敗', 'bad'); return; }
  const ids = await loadWorkflowList();
  if (ids.length) await loadWorkflow(ids[0]);
  else newWorkflow();
  status(`已刪除「${name}」`, 'ok');
}

async function validate() {
  const body = await fetch('/api/workflows/validate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(currentGraph()),
  }).then((r) => r.json());
  showProblems(body.problems);
  return body.ok;
}

function showProblems(problems) {
  const host = $('#problems');
  if (!problems?.length) {
    host.innerHTML = '<span class="text-emerald-400">✓ 沒有問題</span>';
    return;
  }
  host.innerHTML = problems.map((p) => `<div class="text-amber-400">• ${esc(p)}</div>`).join('');
}

async function run() {
  if (!(await validate())) { status('先修掉上面的問題再執行', 'bad'); return; }

  // 執行前先存檔。run 自己會存一份快照，但快照在執行紀錄裡、不在
  // .ai-workflow-proj/workflows/ —— 只按執行的人畫完一張圖跑完就沒了，
  // 下次進編輯器是空白畫布。存檔失敗就不要跑：使用者會以為已經存好了。
  if (state.dirty || !state.workflowId) {
    if (!(await save())) return;
  }

  const requirement = $('#requirement').value.trim();
  const resp = await fetch(`/api/projects/${encodeURIComponent(state.project.id)}/runs`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      graph: currentGraph(),
      requirement,
      workflow_id: state.workflowId,
    }),
  });
  const body = await resp.json();
  if (!resp.ok) { status(body.error, 'bad'); return; }
  // 這次執行用的是畫布上的即時內容，run 已經把它整份存成快照了
  state.leaving = true;
  location.href = `/projects/${encodeURIComponent(state.project.id)}/runs/${body.run_id}`;
}

function markDirty() {
  state.dirty = true;
  status('未儲存的變更', 'warn');
  updateIsolationHint();
  clearTimeout(markDirty._t);
  markDirty._t = setTimeout(validate, 400);
}

// ------------------------------------------------------------------ 工具

function status(text, tone = '') {
  const el = $('#status');
  el.textContent = text;
  el.className = { ok: 'text-emerald-400', warn: 'text-amber-400', bad: 'text-rose-400' }[tone] || 'text-slate-400';
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// ------------------------------------------------------------------ 綁定

$('#btn-new').addEventListener('click', newWorkflow);
$('#btn-save-as').addEventListener('click', () => save({ asNew: true }));
$('#btn-delete').addEventListener('click', deleteWorkflow);
$('#btn-save').addEventListener('click', () => save());
$('#btn-run').addEventListener('click', run);
$('#btn-validate').addEventListener('click', validate);
$('#wf-list').addEventListener('change', (e) => loadWorkflow(e.target.value));
$('#wf-isolation').addEventListener('change', markDirty);
$('#btn-zoom-in').addEventListener('click', () => state.editor.zoom_in());
$('#btn-zoom-out').addEventListener('click', () => state.editor.zoom_out());
$('#btn-zoom-reset').addEventListener('click', fitView);

// 讓瀏覽器測試可以直接驗證轉換的往返一致性
window.__wfDebug = { state, currentGraph, toDrawflow, fromDrawflow, nodeHtml, fitView, refreshConnections };

boot().catch((err) => status(`啟動失敗: ${err.message}`, 'bad'));
