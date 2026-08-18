/**
 * 專案選擇器：切換專案、新增專案、從範本起步、解除註冊。
 *
 * 掛在 base.html 的頁首，所以編輯器與執行紀錄兩頁共用同一套。
 *
 * 決定「現在是哪個專案」的順序：網址的 ?project= → localStorage 記住的 →
 * 清單的第一個。網址優先是為了讓連結貼給同事能指到同一個專案；localStorage
 * 是為了讓你重開分頁時還在原本那個，不用每次重選。
 */

const KEY = 'ai-workflow:project';

export const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// 同一頁裡頁首與內容都要用到專案清單。快取起來只取一次 —— 取兩次除了多一趟
// 往返，還可能拿到不一致的結果（中間剛好有人改了註冊表）。
let _projects = null;

export function fetchProjects({ fresh = false } = {}) {
  if (!_projects || fresh) {
    _projects = fetch('/api/projects')
      .then((r) => r.json())
      .then(({ projects }) => projects);
  }
  return _projects;
}

/** 目前作用中的專案，沒有任何專案時為 null。 */
export async function activeProject() {
  return pickActive(await fetchProjects());
}

export function pickActive(projects) {
  if (!projects.length) return null;
  const wanted = new URLSearchParams(location.search).get('project')
    || localStorage.getItem(KEY);
  return projects.find((p) => p.id === wanted) || projects[0];
}

export function remember(id) {
  localStorage.setItem(KEY, id);
}

/** 把 ?project= 同步進網址列，但不新增一筆瀏覽歷史。 */
export function syncUrl(id) {
  const url = new URL(location.href);
  if (url.searchParams.get('project') === id) return;
  url.searchParams.set('project', id);
  history.replaceState(null, '', url);
}

export const NO_PROJECT_HINT =
  '還沒有註冊任何專案。工作流是存在專案資料夾裡的，所以要先加一個專案。';

/**
 * 切換專案後要去哪。
 *
 * 一律整頁重新載入而不是原地換資料 —— 編輯器的畫布、節點面板、工作流清單
 * 全都是開頁時建起來的，原地抽換等於要再寫一套重置邏輯，而那套邏輯漏掉
 * 任何一項就會讓你在 A 專案的畫布上編輯 B 專案的工作流。
 *
 * run 詳情頁綁的是特定一個 run，切專案之後那個 run 不屬於新專案，
 * 所以回到執行紀錄列表。
 */
function destinationFor(id) {
  const q = `?project=${encodeURIComponent(id)}`;
  return location.pathname === '/' ? `/${q}` : `/runs${q}`;
}

// ------------------------------------------------------------------ 頁首

/**
 * 在頁首掛上選擇器。回傳目前作用中的專案（沒有任何專案時為 null）。
 * 呼叫端拿到 null 就該顯示自己的引導訊息。
 */
export async function mountPicker() {
  const host = document.getElementById('project-bar');
  if (!host) return null;

  let active = null;

  // 可以重畫：在管理對話框裡增刪之後，頁首要跟著更新。少了這一步，
  // 已經移除的專案還會留在下拉選單裡，選下去就跳到一個不存在的專案。
  async function refresh({ fresh = false } = {}) {
    const projects = await fetchProjects({ fresh });
    active = pickActive(projects);
    if (active) { remember(active.id); syncUrl(active.id); }
    draw(projects);
  }

  function draw(projects) {
    // 路徑放在最左邊並且截斷：它是最長的一段，但也是最不需要看清楚的
    // （要看完整的有 title，管理對話框裡也有）。選擇器與按鈕不能被壓縮。
    host.innerHTML = `
      <span id="project-detail" class="min-w-0 truncate text-xs"></span>
      <select id="project-select" class="fi w-44 flex-none text-xs"
              title="切換專案">${projects.map((p) => `
        <option value="${esc(p.id)}" ${p.id === active?.id ? 'selected' : ''}
          >${p.ok ? '' : '⚠ '}${esc(p.name)}</option>`).join('')}
        ${projects.length ? '' : '<option value="">（還沒有專案）</option>'}
      </select>
      <button id="project-manage"
              class="flex-none whitespace-nowrap rounded bg-slate-700 px-2.5 py-1 text-xs hover:bg-slate-600"
              title="新增或移除專案">管理</button>`;

    const detail = host.querySelector('#project-detail');
    detail.className = 'min-w-0 truncate text-xs';
    if (!active) {
      detail.textContent = '⚠ 還沒有專案，按「管理」新增';
      detail.classList.add('text-amber-400');
    } else if (active.ok) {
      detail.textContent = `${active.path} (${active.main_branch})`;
      detail.classList.add('text-slate-500');
      detail.title = `${active.path}\n基準分支 ${active.main_branch}`;
    } else {
      // 只取第一行：驗證失敗的訊息有多行說明，塞進頁首會把版面撐爆。
      // 完整內容留在 title 與管理對話框裡。
      detail.textContent = `⚠ ${active.error.split('\n')[0]}`;
      detail.classList.add('text-amber-400');
      detail.title = active.error;
    }

    host.querySelector('#project-select').addEventListener('change', (e) => {
      if (e.target.value) location.href = destinationFor(e.target.value);
    });
    host.querySelector('#project-manage').addEventListener(
      'click', () => openManager(active, () => refresh({ fresh: true })),
    );
  }

  await refresh();
  return active;
}

// ------------------------------------------------------------ 管理對話框

async function openManager(active, onChanged) {
  const dlg = document.getElementById('project-dialog');
  const body = document.getElementById('project-dialog-body');

  async function render() {
    // 對話框裡剛增刪過，一定要重新取
    const projects = await fetchProjects({ fresh: true });
    body.innerHTML = `
      <form id="add-form" class="mb-4 rounded border border-slate-700 p-3">
        <div class="mb-2 text-xs font-semibold text-slate-300">新增專案</div>
        <label class="mb-2 block">
          <span class="fl">專案路徑</span>
          <input id="add-path" class="fi" required
                 placeholder="~/code/my-app 或 /Users/you/code/my-app">
          <span class="fh">會在這個資料夾建立 .ai-workflow-proj/。
            工作流定義要進 git，執行紀錄會被 .gitignore 擋掉。</span>
        </label>
        <label class="mb-2 block">
          <span class="fl">顯示名稱（選填）</span>
          <input id="add-name" class="fi" placeholder="預設用資料夾名稱">
        </label>
        <div id="add-error" class="mb-2 hidden whitespace-pre-wrap text-xs text-rose-400"></div>
        <button class="rounded bg-sky-700 px-3 py-1 text-xs font-medium hover:bg-sky-600">
          新增</button>
      </form>

      <div class="mb-2 text-xs font-semibold text-slate-300">已註冊的專案</div>
      ${projects.length ? '' : '<p class="text-xs text-slate-500">還沒有任何專案。</p>'}
      ${projects.map((p) => `
        <div class="mb-1.5 flex items-start gap-2 rounded border border-slate-700 p-2">
          <div class="min-w-0 flex-1">
            <div class="flex items-center gap-2">
              <span class="text-sm">${esc(p.name)}</span>
              ${p.id === active?.id
                ? '<span class="rounded bg-sky-500/15 px-1.5 text-[10px] text-sky-300">使用中</span>'
                : ''}
            </div>
            <div class="truncate font-mono text-[11px] text-slate-500">${esc(p.path)}</div>
            ${p.ok
              ? `<div class="text-[11px] text-slate-500">基準分支 ${esc(p.main_branch)}</div>`
              : `<div class="text-[11px] text-amber-400">⚠ ${esc(p.error.split('\n')[0])}</div>`}
          </div>
          <button data-remove="${esc(p.id)}"
                  class="flex-none rounded border border-slate-600 px-2 py-0.5 text-[11px] text-slate-300 hover:border-rose-700 hover:text-rose-300"
                  title="只從清單移除，不刪任何檔案">移除</button>
        </div>`).join('')}`;

    body.querySelector('#add-form').addEventListener('submit', onAdd);
    body.querySelectorAll('[data-remove]').forEach((btn) => {
      btn.addEventListener('click', () => onRemove(btn.dataset.remove, projects));
    });
  }

  async function onAdd(e) {
    e.preventDefault();
    const error = body.querySelector('#add-error');
    error.classList.add('hidden');

    const resp = await fetch('/api/projects', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        path: body.querySelector('#add-path').value.trim(),
        name: body.querySelector('#add-name').value.trim(),
      }),
    });
    const added = await resp.json();
    if (!resp.ok) {
      // 驗證失敗的訊息是多行的、而且是寫給人看的（例如「這通常是因為
      // ~/.git 意外存在」），整段顯示出來才有用。
      error.textContent = added.error;
      error.classList.remove('hidden');
      return;
    }
    await offerTemplates(added);
  }

  async function onRemove(id, projects) {
    const target = projects.find((p) => p.id === id);
    if (!confirm(`把「${target.name}」從清單移除？\n\n`
                 + `${target.path} 底下的檔案都不會被刪掉，\n`
                 + '包括工作流定義與執行紀錄。之後可以再加回來。')) return;

    await fetch(`/api/projects/${encodeURIComponent(id)}`, { method: 'DELETE' });
    // 移除的是目前使用中的專案，就得換一個 —— 留在原地的話整頁都是壞的
    if (id === active?.id) { localStorage.removeItem(KEY); location.href = '/'; return; }
    await render();
    await onChanged?.();
  }

  /** 剛註冊的專案 workflows/ 是空的，這是給起點的時機。 */
  async function offerTemplates(project) {
    const { templates } = await fetch('/api/templates').then((r) => r.json());
    body.innerHTML = `
      <div class="mb-3">
        <div class="mb-1 text-sm">已新增「${esc(project.name)}」</div>
        <div class="font-mono text-[11px] text-slate-500">${esc(project.path)}</div>
      </div>
      <div class="mb-2 text-xs text-slate-400">
        這個專案還沒有工作流。要從範本開始嗎？（之後也可以用
        <code>tools.templates</code> 匯入）
      </div>
      ${templates.map((t) => `
        <label class="mb-1 flex items-center gap-2 text-xs">
          <input type="checkbox" class="accent-sky-600" value="${esc(t.id)}">
          <span class="flex-1">${esc(t.name)}</span>
          <span class="text-slate-500">${t.nodes} 個節點</span>
        </label>`).join('')}
      <div class="mt-3 flex gap-2">
        <button id="do-import" class="rounded bg-sky-700 px-3 py-1 text-xs font-medium hover:bg-sky-600">
          匯入選中的並切換過去</button>
        <button id="skip-import" class="rounded bg-slate-700 px-3 py-1 text-xs hover:bg-slate-600">
          略過，直接切換</button>
      </div>`;

    const go = () => { remember(project.id); location.href = destinationFor(project.id); };
    body.querySelector('#skip-import').addEventListener('click', go);
    body.querySelector('#do-import').addEventListener('click', async (e) => {
      e.target.disabled = true;
      const wanted = [...body.querySelectorAll('input:checked')].map((i) => i.value);
      for (const id of wanted) {
        await fetch(
          `/api/projects/${encodeURIComponent(project.id)}/workflows/import/${encodeURIComponent(id)}`,
          { method: 'POST' },
        );
      }
      go();
    });
  }

  await render();
  dlg.showModal();
}
