/* crawlyard panel. Every value that reaches innerHTML goes through esc(): queue items, file
   paths and container names all come from places an attacker can write to. */

const QUEUES = [
    { key: 'scrape_queue', hint: 'jobs waiting', clear: true },
    { key: 'dead_jobs', hint: 'failed every retry', clear: true, alert: true },
    { key: 'proxy_queue', hint: 'proxies in rotation', clear: true },
    { key: 'good_proxies', hint: 'proxies that worked' },
    { key: 'banned_proxies', hint: 'proxies dropped', clear: true },
];

const state = {
    activeTab: 'fleet',
    fleets: [],
    containers: [],
    editor: null,
    openFiles: new Map(),       // path -> { name, model, modified }
    activePath: null,
    selected: [],               // container ids, in display order
    sockets: new Map(),
    buffers: new Map(),         // id -> [{ text, type, at }]
    logsMode: 'split',
    logQuery: '',
    follow: true,
    wrap: true,
    columns: 'auto',
    timers: {},
};

const $ = (id) => document.getElementById(id);

function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}

async function api(path, opts = {}) {
    const res = await fetch(path, { ...opts, headers: { 'Content-Type': 'application/json', ...opts.headers } });
    const isJson = (res.headers.get('content-type') || '').includes('application/json');
    const data = isJson ? await res.json() : { detail: await res.text() };
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    return data;
}

const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body ?? {}) });

function toast(message, type = 'info', ms = 4000) {
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.textContent = message;
    $('toast-container').appendChild(el);
    setTimeout(() => { el.classList.add('leaving'); setTimeout(() => el.remove(), 200); }, ms);
}

async function withBusy(button, fn) {
    button.disabled = true;
    button.classList.add('loading');
    try { await fn(); } finally { button.disabled = false; button.classList.remove('loading'); }
}

function fmtDate(s) {
    if (!s || s.startsWith('0001')) return '–';
    return new Date(s).toLocaleString(undefined, { month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}

function every(name, ms, fn) {
    clearInterval(state.timers[name]);
    fn();
    state.timers[name] = setInterval(fn, ms);
}

function stop(name) {
    clearInterval(state.timers[name]);
    delete state.timers[name];
}

// ── Tabs ──
const TAB_ENTER = {
    ide: () => initEditor(),
    redis: () => every('redis', 5000, loadQueues),
    logs: () => every('logs', 8000, loadLogContainers),
    stats: () => every('stats', 10000, loadStats),
};

function showTab(name) {
    state.activeTab = name;
    document.querySelectorAll('.tab-btn').forEach((btn) => {
        const active = btn.dataset.tab === name;
        btn.setAttribute('aria-selected', active);
        btn.tabIndex = active ? 0 : -1;
        $(`tab-${btn.dataset.tab}`).hidden = !active;
    });
    ['redis', 'logs', 'stats'].forEach((t) => t !== name && stop(t));
    TAB_ENTER[name]?.();
}

function initTabs() {
    const tabs = [...document.querySelectorAll('.tab-btn')];
    tabs.forEach((btn, i) => {
        btn.addEventListener('click', () => showTab(btn.dataset.tab));
        btn.addEventListener('keydown', (e) => {
            const step = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
            if (!step) return;
            const next = tabs[(i + step + tabs.length) % tabs.length];
            next.focus();
            showTab(next.dataset.tab);
        });
    });
}

// ── Header ──
function setPill(el, ok, label) {
    el.className = `pill ${ok ? 'ok' : 'down'}`;
    el.textContent = label;
}

async function checkHealth() {
    try {
        const d = await api('/api/health');
        setPill($('docker-status'), d.docker_connected, d.docker_connected ? `Docker ${d.docker_version}` : 'Docker down');
        setPill($('redis-status'), d.redis_connected, d.redis_connected ? 'Redis' : 'Redis down');
    } catch {
        setPill($('docker-status'), false, 'Panel API down');
        setPill($('redis-status'), false, 'Redis unknown');
    }
}

function setMeter(name, pct, title) {
    const bar = $(`${name}-bar`);
    bar.style.width = `${pct}%`;
    bar.className = 'meter-fill' + (pct > 90 ? ' danger' : pct > 70 ? ' warning' : '');
    $(`${name}-text`).textContent = `${Math.round(pct)}%`;
    if (title) bar.closest('.meter').title = title;
}

async function loadHostStats() {
    try {
        const d = await api('/api/host/stats');
        setMeter('cpu', d.cpu);
        setMeter('ram', d.ram, `${d.ram_used} of ${d.ram_total} GB used`);
    } catch { /* header meters are best-effort */ }
}

// ── Fleet ──
async function loadFleets() {
    try {
        state.fleets = (await api('/api/fleet/services')).fleets;
    } catch (e) {
        toast(`Could not list compose files: ${e.message}`, 'error');
        return;
    }
    const select = $('fleet-compose-select');
    const main = state.fleets.find((f) => f.services.includes('worker'));
    select.innerHTML = state.fleets.length
        ? state.fleets.map((f) => `<option value="${esc(f.file)}" ${f === main ? 'selected' : ''}>${esc(f.file)}</option>`).join('')
        : '<option value="">No compose files found</option>';
    updateServiceSelect();
    renderServices();
}

function updateServiceSelect() {
    const fleet = state.fleets.find((f) => f.file === $('fleet-compose-select').value);
    $('fleet-service-select').innerHTML = (fleet?.services || [])
        .map((s) => `<option ${s === 'worker' ? 'selected' : ''}>${esc(s)}</option>`).join('');
}

function serviceState(fleet, service) {
    // Other compose projects on the host may reuse service names like "postgres".
    const mine = state.containers.filter((c) => c.service === service && (!fleet.project || c.project === fleet.project));
    const running = mine.filter((c) => c.status === 'running').length;
    if (running) return { cls: 'running', label: `${running} running` };
    if (mine.some((c) => c.status === 'restarting')) return { cls: 'restarting', label: 'restarting' };
    return { cls: '', label: mine.length ? 'stopped' : 'not created' };
}

function renderServices() {
    const grid = $('compose-services-grid');
    if (!state.fleets.length) {
        grid.innerHTML = '<p class="empty">No compose files found under the code directory.</p>';
        return;
    }
    grid.innerHTML = state.fleets.map((f) => `
        <article class="compose-card">
            <h3>${esc(f.file)}</h3>
            ${f.services.map((s) => {
                const st = serviceState(f, s);
                const attrs = `data-file="${esc(f.file)}" data-service="${esc(s)}"`;
                return `<div class="service-row">
                    <span class="service-name">${esc(s)}</span>
                    <span class="state ${st.cls}">${esc(st.label)}</span>
                    <div class="service-actions">
                        <button class="btn btn-xs" data-action="service" data-op="start" ${attrs}>Start</button>
                        <button class="btn btn-xs" data-action="service" data-op="stop" ${attrs}>Stop</button>
                        <button class="btn btn-xs btn-danger" data-action="service" data-op="remove" ${attrs}>Remove</button>
                    </div>
                </div>`;
            }).join('')}
        </article>`).join('');
}

async function loadContainers() {
    const tbody = $('worker-table-body');
    try {
        state.containers = (await api('/api/containers?all=true')).containers;
    } catch (e) {
        tbody.innerHTML = `<tr class="empty-row"><td colspan="6">Could not reach Docker: ${esc(e.message)}</td></tr>`;
        return;
    }
    $('active-workers').textContent = state.containers.filter((c) => c.status === 'running').length;
    tbody.innerHTML = state.containers.length ? state.containers.map((c) => `<tr>
        <td><span class="state ${c.status === 'running' ? 'running' : ''}">${esc(c.status)}</span></td>
        <td class="mono">${esc(c.name)}</td>
        <td class="mono">${esc(c.service || '–')}</td>
        <td class="mono">${esc(c.image)}</td>
        <td>${esc(fmtDate(c.started_at))}</td>
        <td class="num">${esc(c.restart_count)}</td>
    </tr>`).join('') : '<tr class="empty-row"><td colspan="6">No containers yet. Start a service above.</td></tr>';
    renderServices();
}

async function controlService(button) {
    const { file, service, op } = button.dataset;
    if (op === 'remove' && !confirm(`Remove all ${service} containers?`)) return;
    await withBusy(button, async () => {
        try {
            await post(`/api/fleet/service/${op}`, { compose_file: file, service });
            toast(`${service}: ${op} done`, 'success');
        } catch (e) {
            toast(`${service}: ${op} failed. ${e.message}`, 'error', 8000);
        }
        loadContainers();
    });
}

function initFleet() {
    const input = $('scale-input');
    $('fleet-compose-select').addEventListener('change', updateServiceSelect);
    $('scale-up-btn').addEventListener('click', () => { input.value = Math.min(50, (+input.value || 0) + 1); });
    $('scale-down-btn').addEventListener('click', () => { input.value = Math.max(0, (+input.value || 0) - 1); });

    $('scale-form').addEventListener('submit', (e) => {
        e.preventDefault();
        const count = Number.parseInt(input.value, 10);
        if (!(count >= 0 && count <= 50)) { toast('Enter a number from 0 to 50.', 'warning'); return; }
        const service = $('fleet-service-select').value;
        withBusy($('scale-btn'), async () => {
            try {
                await post('/api/fleet/scale', { compose_file: $('fleet-compose-select').value, service, count });
                toast(`${service} scaled to ${count}`, 'success');
            } catch (err) {
                toast(`Scaling failed. ${err.message}`, 'error', 8000);
            }
            loadContainers();
        });
    });

    $('hard-stop-btn').addEventListener('click', async () => {
        const file = $('fleet-compose-select').value;
        if (!confirm(`Stop every container in ${file}?`)) return;
        try {
            await post('/api/fleet/stop', { compose_file: file });
            toast(`Stopped ${file}`, 'warning');
        } catch (e) { toast(`Stop failed. ${e.message}`, 'error'); }
        loadContainers();
    });

    $('prune-exited-btn').addEventListener('click', async () => {
        if (!confirm('Delete all exited containers on this host?')) return;
        try {
            const d = await post('/api/containers/prune-exited');
            toast(`Removed ${d.deleted_count} exited containers`, 'success');
        } catch (e) { toast(`Prune failed. ${e.message}`, 'error'); }
        loadContainers();
    });

    $('refresh-workers-btn').addEventListener('click', loadContainers);
    $('refresh-compose-btn').addEventListener('click', loadFleets);
    $('compose-services-grid').addEventListener('click', (e) => {
        const btn = e.target.closest('[data-action="service"]');
        if (btn) controlService(btn);
    });
}

// ── Code editor ──
const ICON_DIR = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';
const ICON_FILE = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>';

function renderTree(items, depth = 0) {
    return items.map((item) => {
        const style = `style="--depth:${depth}"`;
        if (item.type === 'dir') {
            return `<button class="tree-item" role="treeitem" aria-expanded="${depth === 0}" data-action="toggle-dir" ${style}>
                    ${ICON_DIR}<span class="tree-name">${esc(item.name)}</span></button>
                <div class="tree-group" role="group">${renderTree(item.children || [], depth + 1)}</div>`;
        }
        return `<button class="tree-item" role="treeitem" data-action="open-file" data-path="${esc(item.path)}" ${style}>
            ${ICON_FILE}<span class="tree-name">${esc(item.name)}</span></button>`;
    }).join('');
}

async function loadFileTree() {
    try {
        $('file-tree').innerHTML = renderTree((await api('/api/files')).tree);
    } catch (e) {
        $('file-tree').innerHTML = `<p class="empty">Could not list files: ${esc(e.message)}</p>`;
    }
}

function setEditorStatus(text, cls = '') {
    const el = $('ide-status');
    el.textContent = text;
    el.className = `tag ${cls}`;
}

async function openFile(path) {
    if (!state.editor) return;
    document.querySelectorAll('.tree-item.active').forEach((i) => i.classList.remove('active'));
    document.querySelector(`.tree-item[data-path="${CSS.escape(path)}"]`)?.classList.add('active');
    if (!state.openFiles.has(path)) {
        try {
            const d = await api(`/api/files/read?path=${encodeURIComponent(path)}`);
            state.openFiles.set(path, { name: d.name, model: monaco.editor.createModel(d.content, d.language), modified: false });
        } catch (e) {
            toast(`Could not open ${path}. ${e.message}`, 'error');
            return;
        }
    }
    switchToFile(path);
}

function switchToFile(path) {
    const file = state.openFiles.get(path);
    state.activePath = path;
    state.editor.setModel(file.model);
    $('ide-filename').textContent = path;
    setEditorStatus(file.modified ? 'Unsaved' : 'Saved', file.modified ? 'dirty' : '');
    $('ide-toolbar').hidden = false;
    renderEditorTabs();
}

function closeFile(path) {
    state.openFiles.get(path)?.model.dispose();
    state.openFiles.delete(path);
    if (state.activePath === path) {
        const rest = [...state.openFiles.keys()];
        if (rest.length) switchToFile(rest.at(-1));
        else { state.activePath = null; state.editor.setModel(null); $('ide-toolbar').hidden = true; }
    }
    renderEditorTabs();
}

function renderEditorTabs() {
    const el = $('editor-tabs');
    if (!state.openFiles.size) {
        el.innerHTML = '<p class="editor-tabs-empty">Open a file from the list.</p>';
        return;
    }
    el.innerHTML = [...state.openFiles].map(([path, f]) => `
        <div class="editor-tab ${path === state.activePath ? 'active' : ''}">
            <button class="editor-tab-open" data-action="switch-file" data-path="${esc(path)}" title="${esc(path)}">${f.modified ? '<span class="dot" aria-label="unsaved">●</span>' : ''}${esc(f.name)}</button>
            <button class="editor-tab-close" data-action="close-file" data-path="${esc(path)}" aria-label="Close ${esc(f.name)}">✕</button>
        </div>`).join('');
}

function tokenColor(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function initEditor() {
    if (state.editor || !window.require) return;
    require.config({ paths: { vs: 'https://cdn.jsdelivr.net/npm/monaco-editor@0.45.0/min/vs' } });
    require(['vs/editor/editor.main'], () => {
        monaco.editor.defineTheme('crawlyard', {
            base: 'vs-dark', inherit: true, rules: [],
            colors: {
                'editor.background': tokenColor('--card'),
                'editor.lineHighlightBackground': tokenColor('--raised'),
                'editorCursor.foreground': tokenColor('--accent'),
                'editorLineNumber.foreground': tokenColor('--muted'),
            },
        });
        state.editor = monaco.editor.create($('monaco-editor-container'), {
            theme: 'crawlyard', fontFamily: 'Fira Code, monospace', fontSize: 13,
            minimap: { enabled: false }, scrollBeyondLastLine: false, automaticLayout: true, padding: { top: 12 },
        });
        state.editor.onDidChangeModelContent((e) => {
            const file = state.openFiles.get(state.activePath);
            if (!file || e.isFlush || file.modified) return;
            file.modified = true;
            setEditorStatus('Unsaved', 'dirty');
            renderEditorTabs();
        });
        state.editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, saveFile);
        loadFileTree();
    });
}

async function saveFile() {
    const file = state.openFiles.get(state.activePath);
    if (!file) return;
    await withBusy($('deploy-btn'), async () => {
        setEditorStatus('Saving…', 'dirty');
        try {
            const d = await post('/api/files/deploy', {
                path: state.activePath, content: file.model.getValue(), service: $('deploy-service-select').value || null,
            });
            file.modified = false;
            setEditorStatus('Saved');
            renderEditorTabs();
            toast(d.restarted.length ? `Saved; restarted ${d.restarted.length} container(s)` : 'Saved', 'success');
        } catch (e) {
            setEditorStatus('Save failed', 'error');
            toast(`Save failed. ${e.message}`, 'error', 8000);
        }
    });
}

async function checkSyntax() {
    if (!state.activePath) return;
    await withBusy($('test-code-btn'), async () => {
        try {
            const d = await post('/api/ide/validate', {
                filename: state.activePath.split('/').pop(), content: state.editor.getValue(),
            });
            const where = d.line ? ` at line ${d.line}, column ${d.offset}` : '';
            toast(`${d.message}${where}`, d.valid ? 'success' : 'error', d.valid ? 3000 : 8000);
            if (d.line) state.editor.revealLineInCenter(d.line);
        } catch (e) { toast(`Check failed. ${e.message}`, 'error'); }
    });
}

function askPath(onConfirm) {
    const dialog = $('modal');
    $('modal-input').value = '';
    dialog.returnValue = '';
    dialog.showModal();
    dialog.addEventListener('close', () => {
        const value = $('modal-input').value.trim();
        if (dialog.returnValue === 'ok' && value) onConfirm(value);
    }, { once: true });
}

function initEditorActions() {
    $('file-tree').addEventListener('click', (e) => {
        const item = e.target.closest('.tree-item');
        if (!item) return;
        if (item.dataset.action === 'toggle-dir') {
            item.setAttribute('aria-expanded', item.getAttribute('aria-expanded') !== 'true');
        } else {
            openFile(item.dataset.path);
        }
    });
    $('editor-tabs').addEventListener('click', (e) => {
        const btn = e.target.closest('[data-action]');
        if (btn?.dataset.action === 'switch-file') switchToFile(btn.dataset.path);
        if (btn?.dataset.action === 'close-file') closeFile(btn.dataset.path);
    });
    $('deploy-btn').addEventListener('click', saveFile);
    $('test-code-btn').addEventListener('click', checkSyntax);
    $('refresh-tree-btn').addEventListener('click', loadFileTree);
    $('modal-cancel-btn').addEventListener('click', () => $('modal').close());
    $('new-file-btn').addEventListener('click', () => askPath(async (path) => {
        try {
            await post('/api/files/create', { path, content: '' });
            await loadFileTree();
            openFile(path);
        } catch (e) { toast(`Could not create ${path}. ${e.message}`, 'error'); }
    }));
}

// ── Queues ──
async function loadQueues() {
    try {
        const { queues } = await api('/api/redis/queues');
        $('queue-grid').innerHTML = QUEUES.map((q) => {
            const size = queues[q.key]?.size ?? 0;
            return `<article class="queue-card ${q.alert && size ? 'alert' : ''}">
                <h3>${q.key}</h3>
                <span class="count">${size.toLocaleString()}</span>
                <span class="hint">${q.hint}</span>
                <div class="row">
                    <button class="btn btn-xs" data-action="peek" data-key="${q.key}">Show</button>
                    ${q.clear ? `<button class="btn btn-xs btn-danger" data-action="flush" data-key="${q.key}">Clear</button>` : ''}
                </div>
            </article>`;
        }).join('');
    } catch (e) {
        $('queue-grid').innerHTML = `<p class="empty">Redis unavailable: ${esc(e.message)}</p>`;
    }
    try {
        const info = await api('/api/redis/info');
        $('redis-mem').textContent = info.used_memory_human || '–';
        $('redis-clients').textContent = info.connected_clients ?? '–';
        $('redis-uptime').textContent = info.uptime_seconds != null ? `${Math.floor(info.uptime_seconds / 3600)} h` : '–';
    } catch { /* the queue grid already shows the error */ }
}

async function peekQueue(key) {
    try {
        const d = await api(`/api/redis/queue/peek?queue=${encodeURIComponent(key)}&count=30`);
        $('peek-queue-name').textContent = `${key}: first ${d.count}`;
        $('peek-list').innerHTML = d.items.length
            ? d.items.map((item) => `<li>${esc(item)}</li>`).join('')
            : '<li class="empty">Empty.</li>';
    } catch (e) { toast(`Could not read ${key}. ${e.message}`, 'error'); }
}

async function flushKey(key) {
    if (!confirm(`Delete everything in ${key}? This cannot be undone.`)) return;
    try {
        await post('/api/redis/queue/flush', { key });
        toast(`${key} cleared`, 'warning');
        loadQueues();
    } catch (e) { toast(`Could not clear ${key}. ${e.message}`, 'error'); }
}

function initQueues() {
    $('queue-grid').addEventListener('click', (e) => {
        const btn = e.target.closest('[data-action]');
        if (btn?.dataset.action === 'peek') peekQueue(btn.dataset.key);
        if (btn?.dataset.action === 'flush') flushKey(btn.dataset.key);
    });
    $('push-form').addEventListener('submit', async (e) => {
        e.preventDefault();
        const values = $('url-bulk-input').value.split('\n').map((l) => l.trim()).filter(Boolean);
        if (!values.length) { toast('Nothing to add: the box is empty.', 'warning'); return; }
        const queue = $('push-queue-select').value;
        try {
            const d = await post('/api/redis/queue/push', { queue, values });
            toast(`Added ${d.pushed} to ${queue} (now ${d.new_size})`, 'success');
            $('url-bulk-input').value = '';
            loadQueues();
        } catch (err) { toast(`Could not add jobs. ${err.message}`, 'error'); }
    });
}

// ── Logs ──
async function loadLogContainers() {
    try {
        state.containers = (await api('/api/containers?all=true')).containers;
    } catch (e) {
        $('logs-container-list').innerHTML = `<p class="empty">Could not reach Docker: ${esc(e.message)}</p>`;
        return;
    }
    $('logs-container-list').innerHTML = state.containers.length ? state.containers.map((c) => `
        <label class="log-item">
            <input type="checkbox" data-id="${esc(c.id)}" ${state.selected.includes(c.id) ? 'checked' : ''}>
            <span class="log-item-name" title="${esc(c.name)}">${esc(c.name)}</span>
            <span class="state ${c.status === 'running' ? 'running' : ''}"><span class="visually-hidden">${esc(c.status)}</span></span>
        </label>`).join('') : '<p class="empty">No containers.</p>';
}

const nameOf = (id) => state.containers.find((c) => c.id === id)?.name || id;

function setSelected(id, on) {
    if (on && !state.selected.includes(id)) {
        state.selected.push(id);
        connectLog(id);
    } else if (!on) {
        state.selected = state.selected.filter((x) => x !== id);
        state.sockets.get(id)?.close();
        state.sockets.delete(id);
    }
}

function connectLog(id) {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${proto}//${location.host}/ws/logs/${encodeURIComponent(id)}`);
    ws.onmessage = (ev) => {
        const d = JSON.parse(ev.data);
        addLogLine(id, d.log ?? `[panel] ${d.error}`);
    };
    ws.onclose = () => state.sockets.delete(id);
    state.sockets.set(id, ws);
}

function logType(line) {
    if (/\b(ERROR|CRITICAL|Traceback|exception)\b/i.test(line)) return 'error';
    if (/\bWARN(ING)?\b/.test(line)) return 'warning';
    return '';
}

function queryRegex() {
    if (!state.logQuery) return null;
    try { return new RegExp(state.logQuery, 'gi'); } catch { return new RegExp(state.logQuery.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi'); }
}

function highlight(text, re) {
    if (!re) return esc(text);
    let out = '';
    let last = 0;
    for (const m of text.matchAll(re)) {
        if (!m[0]) break;
        out += esc(text.slice(last, m.index)) + `<mark>${esc(m[0])}</mark>`;
        last = m.index + m[0].length;
    }
    return out + esc(text.slice(last));
}

function lineHtml(entry, re, source) {
    const [, ts, rest] = entry.text.match(/^(\d{4}-\d\d-\d\dT[\d:.]+Z)\s?(.*)$/s) || [null, '', entry.text];
    const time = ts ? `<span class="ts">${esc(ts.slice(11, 19))}</span>` : '';
    const tag = source ? `<span class="log-source" style="background:${sourceColor(source)}">${esc(source)}</span>` : '';
    return `<div class="log-line ${entry.type}">${tag}${time}<span class="text">${highlight(rest, re)}</span></div>`;
}

function sourceColor(name) {
    let h = 0;
    for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) % 360;
    return `hsl(${h} 70% 72%)`;
}

const matches = (text, re) => !re || (re.lastIndex = 0, re.test(text));

function addLogLine(id, text) {
    const entry = { text, type: logType(text), at: Date.now() };
    const buf = state.buffers.get(id) || [];
    buf.push(entry);
    if (buf.length > 500) buf.shift();
    state.buffers.set(id, buf);
    if (state.activeTab !== 'logs') return;

    const re = queryRegex();
    if (!matches(text, re)) return;
    const body = state.logsMode === 'split' ? $(`term-${id}`) : $('log-display-container');
    if (!body) return;
    body.insertAdjacentHTML('beforeend', lineHtml(entry, re, state.logsMode === 'merge' ? nameOf(id) : ''));
    while (body.childElementCount > 1000) body.firstElementChild.remove();
    if (state.follow) body.scrollTop = body.scrollHeight;
}

function renderLogs() {
    const box = $('log-display-container');
    box.className = `log-display-container ${state.logsMode}-mode ${state.wrap ? 'wrap' : ''} ${state.columns !== 'auto' ? `cols-${state.columns}` : ''}`;
    if (!state.selected.length) {
        box.innerHTML = '<p class="log-empty">Pick containers on the left to follow their logs.</p>';
        return;
    }
    const re = queryRegex();
    if (state.logsMode === 'split') {
        box.innerHTML = state.selected.map((id) => `
            <section class="terminal-window" draggable="true" data-id="${esc(id)}" aria-label="Logs for ${esc(nameOf(id))}">
                <div class="terminal-header"><span>${esc(nameOf(id))}</span>
                    <button class="icon-btn" data-action="unfollow" data-id="${esc(id)}" aria-label="Stop following ${esc(nameOf(id))}">✕</button></div>
                <div class="terminal-body" id="term-${esc(id)}">${(state.buffers.get(id) || [])
                    .filter((e) => matches(e.text, re)).map((e) => lineHtml(e, re)).join('')}</div>
            </section>`).join('');
        box.querySelectorAll('.terminal-body').forEach((b) => { b.scrollTop = b.scrollHeight; });
    } else {
        const all = state.selected.flatMap((id) => (state.buffers.get(id) || []).map((e) => ({ ...e, source: nameOf(id) })));
        box.innerHTML = all.sort((a, b) => a.at - b.at).filter((e) => matches(e.text, re)).map((e) => lineHtml(e, re, e.source)).join('');
        box.scrollTop = box.scrollHeight;
    }
}

function initLogs() {
    $('logs-container-list').addEventListener('change', (e) => {
        if (e.target.dataset.id) { setSelected(e.target.dataset.id, e.target.checked); renderLogs(); }
    });
    $('logs-select-all').addEventListener('click', () => {
        state.containers.filter((c) => c.status === 'running').forEach((c) => setSelected(c.id, true));
        loadLogContainers(); renderLogs();
    });
    $('logs-clear-selection').addEventListener('click', () => {
        [...state.selected].forEach((id) => setSelected(id, false));
        loadLogContainers(); renderLogs();
    });
    $('log-display-container').addEventListener('click', (e) => {
        const btn = e.target.closest('[data-action="unfollow"]');
        if (btn) { setSelected(btn.dataset.id, false); loadLogContainers(); renderLogs(); }
    });
    for (const [id, mode] of [['mode-split-btn', 'split'], ['mode-merge-btn', 'merge']]) {
        $(id).addEventListener('click', () => {
            state.logsMode = mode;
            $('mode-split-btn').setAttribute('aria-pressed', mode === 'split');
            $('mode-merge-btn').setAttribute('aria-pressed', mode === 'merge');
            renderLogs();
        });
    }
    let debounce;
    $('log-search-input').addEventListener('input', (e) => {
        clearTimeout(debounce);
        debounce = setTimeout(() => { state.logQuery = e.target.value; renderLogs(); }, 150);
    });
    $('split-cols-select').addEventListener('change', (e) => { state.columns = e.target.value; renderLogs(); });
    $('auto-scroll-toggle').addEventListener('change', (e) => { state.follow = e.target.checked; });
    $('wrap-lines-toggle').addEventListener('change', (e) => { state.wrap = e.target.checked; renderLogs(); });
    $('clear-terminal-btn').addEventListener('click', () => { state.selected.forEach((id) => state.buffers.set(id, [])); renderLogs(); });

    // Reorder split-view windows by dragging their header (mouse only).
    let dragged = null;
    const box = $('log-display-container');
    box.addEventListener('dragstart', (e) => { dragged = e.target.closest('.terminal-window')?.dataset.id; e.target.classList?.add('dragging'); });
    box.addEventListener('dragover', (e) => { e.preventDefault(); e.target.closest('.terminal-window')?.classList.add('drag-over'); });
    box.addEventListener('dragleave', (e) => e.target.closest('.terminal-window')?.classList.remove('drag-over'));
    box.addEventListener('drop', (e) => {
        e.preventDefault();
        const target = e.target.closest('.terminal-window')?.dataset.id;
        if (dragged && target && dragged !== target) {
            state.selected = state.selected.filter((x) => x !== dragged);
            state.selected.splice(state.selected.indexOf(target), 0, dragged);
        }
        dragged = null;
        renderLogs();
    });
    renderLogs();
}

// ── Stats ──
let chart = null;
const samples = [];

async function loadStats() {
    let d;
    try {
        d = await api('/api/stats/summary');
    } catch (e) {
        $('stats-updated').textContent = `not updating: ${e.message}`;
        return;
    }
    if (!d.connected) {
        $('stats-updated').textContent = `database unreachable: ${d.error}`;
        return;
    }
    const c = d.counters || {};
    $('stat-items').textContent = d.items.toLocaleString();
    $('stat-new').textContent = d.new_last_hour.toLocaleString();
    $('stat-jobs').textContent = [c.jobs_ok, c.job_errors, c.jobs_dead].map((n) => Number(n || 0).toLocaleString()).join(' / ');
    $('stat-db-size').textContent = `${(d.db_bytes / 1048576).toFixed(1)} MB`;
    $('stats-updated').textContent = `updated ${new Date().toLocaleTimeString()}`;

    samples.push({ t: new Date().toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' }), v: d.new_last_hour });
    if (samples.length > 30) samples.shift();
    drawChart();
}

function drawChart() {
    if (!window.Chart) return;
    const labels = samples.map((h) => h.t);
    const values = samples.map((h) => h.v);
    if (chart) {
        chart.data.labels = labels;
        chart.data.datasets[0].data = values;
        chart.update('none');
        return;
    }
    const grid = tokenColor('--line');
    const text = tokenColor('--muted');
    const font = { family: 'Fira Sans' };
    chart = new Chart($('harvestChart'), {
        type: 'line',
        data: { labels, datasets: [{ data: values, borderColor: tokenColor('--accent'), borderWidth: 2, pointRadius: 0, tension: 0.25 }] },
        options: {
            responsive: true, maintainAspectRatio: false, animation: false,
            plugins: { legend: { display: false } },
            scales: {
                x: { grid: { color: grid }, ticks: { color: text, font, maxTicksLimit: 6 } },
                y: { grid: { color: grid }, ticks: { color: text, font, precision: 0 }, beginAtZero: true },
            },
        },
    });
}

// ── Boot ──
document.addEventListener('DOMContentLoaded', () => {
    initTabs();
    initFleet();
    initEditorActions();
    initQueues();
    initLogs();
    every('health', 15000, checkHealth);
    every('host', 3000, loadHostStats);
    every('containers', 10000, loadContainers);
    loadFleets();
});
