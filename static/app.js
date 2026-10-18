'use strict';
// midihub control panel. Plain JS, no build step, works offline.

const $ = id => document.getElementById(id);

// ============================================================
// Helpers
// ============================================================
const NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];
const noteName = n => NOTE_NAMES[n % 12] + (Math.floor(n / 12) - 1);

const TYPES = {
    cc:        { label: 'Control change (CC)', num: 'CC number', status: 0xB0 },
    note:      { label: 'Note',                num: 'Note',      status: 0x90 },
    pc:        { label: 'Program change',      num: 'Program',   status: 0xC0 },
    pressure:  { label: 'Channel pressure',    num: null,        status: 0xD0 },
    polyat:    { label: 'Poly aftertouch',     num: 'Note',      status: 0xA0 },
    pitchbend: { label: 'Pitch bend',          num: null,        status: 0xE0 },
    sysex:     { label: 'SysEx',               num: null,        status: 0xF0 },
};
const TYPE_OF_STATUS = { 0x80: 'note', 0x90: 'note', 0xA0: 'polyat', 0xB0: 'cc', 0xC0: 'pc', 0xD0: 'pressure', 0xE0: 'pitchbend' };
const SHORT = { cc: 'CC', note: 'Note', noteon: 'Note on', noteoff: 'Note off', pc: 'Program', pressure: 'Pressure', polyat: 'Aftertouch', pitchbend: 'Pitch bend', sysex: 'SysEx' };
const NUM_KEY = { cc: 'cc', note: 'note', noteon: 'note', noteoff: 'note', polyat: 'note', pc: 'pc' };

function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    Object.assign(node, props);
    for (const c of children) if (c != null) node.append(c);
    return node;
}
const hex2 = b => b.toString(16).padStart(2, '0').toUpperCase();
const bytesToHex = b => (b || []).map(hex2).join(' ');
function hexToBytes(s) {
    return String(s || '').split(/[\s,]+/).filter(Boolean)
        .map(t => parseInt(t.replace(/^0x/i, ''), 16)).filter(n => !isNaN(n) && n >= 0 && n <= 255);
}
const intOrNull = v => { const n = parseInt(v, 10); return isNaN(n) ? null : n; };
function storageGet(key) { try { return localStorage.getItem(key); } catch { return null; } }
function storageSet(key, v) { try { localStorage.setItem(key, v); } catch { /* private mode */ } }

// Index of the one byte that changes across same-length SysEx messages.
function detectValueIndex(msgs) {
    if (msgs.length < 2) return null;
    const first = msgs[0];
    if (msgs.some(m => m.length !== first.length)) return null;
    const varying = [];
    for (let i = 0; i < first.length; i++) if (msgs.some(m => m[i] !== first[i])) varying.push(i);
    return varying.length === 1 ? varying[0] : null;
}

// Human description of raw MIDI bytes.
function describe(b) {
    if (!b || !b.length) return '';
    const s = b[0];
    if (s === 0xF0) return `SysEx · ${b.length} bytes`;
    if (s >= 0xF0) return 'System message';
    const hi = s & 0xF0, ch = (s & 0x0F) + 1;
    switch (hi) {
        case 0x80: return `Note off ${noteName(b[1])} (${b[1]}) · ch ${ch}`;
        case 0x90: return b[2] === 0 ? `Note off ${noteName(b[1])} (${b[1]}) · ch ${ch}` : `Note ${noteName(b[1])} (${b[1]}) vel ${b[2]} · ch ${ch}`;
        case 0xA0: return `Aftertouch ${noteName(b[1])} = ${b[2]} · ch ${ch}`;
        case 0xB0: return `CC ${b[1]} = ${b[2]} · ch ${ch}`;
        case 0xC0: return `Program ${b[1]} · ch ${ch}`;
        case 0xD0: return `Pressure ${b[1]} · ch ${ch}`;
        case 0xE0: return `Pitch bend ${(b[1] | (b[2] << 7)) - 8192} · ch ${ch}`;
    }
    return bytesToHex(b);
}

// Short label of a mapping side, e.g. "CC 20 · ch 1" or "SysEx F0 7D …".
function sideLabel(spec, isOut) {
    const t = spec.type || (isOut ? 'sysex' : 'cc');
    if (t === 'sysex') {
        const tpl = spec.template || [];
        return tpl.length ? `SysEx ${bytesToHex(tpl.slice(0, 6))}${tpl.length > 6 ? ' …' : ''}` : 'Any SysEx';
    }
    let s = SHORT[t] || t;
    const n = spec[NUM_KEY[t]];
    if (n != null) s += ' ' + (t === 'note' || t === 'polyat' ? `${noteName(n)} (${n})` : n);
    else if (NUM_KEY[t] && isOut) s += ' (same)';
    else if (NUM_KEY[t]) s += ' (any)';
    if (spec.channel != null) s += ` · ch ${spec.channel + 1}`;
    return s;
}

// ============================================================
// State
// ============================================================
const state = {
    config: null,       // working copy (what Save sends)
    server: {},         // last /api/state
    dirty: false,
    editingDevice: null,
    editingMap: null,   // index of mapping in the form, or null for a new one
    learning: null,     // { side: 'in'|'out', seq, sysex: {dev: [...]}} while Learn is armed
    monitorOn: false,
    monitor: [],        // recent events, newest first
    seq: 0,
    lastActivity: {},
};

async function api(path, body) {
    const opts = { headers: { 'Content-Type': 'application/json', 'X-Midihub': '1' } };
    if (body !== undefined) { opts.method = 'POST'; opts.body = JSON.stringify(body); }
    const r = await fetch(path, opts);
    if (r.status === 401) { location.href = '/login'; throw new Error('login required'); }
    let data = {};
    try { data = await r.json(); } catch { /* empty */ }
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    return data;
}

// ============================================================
// Toasts & dialogs
// ============================================================
function toast(message, kind = 'info') {
    const t = el('div', { className: 'toast' });
    if (kind !== 'info') t.append(el('span', { className: 'dot ' + (kind === 'ok' ? 'ok' : kind === 'warn' ? 'warn' : 'err') }));
    t.append(el('span', { textContent: message }));
    $('toasts').append(t);
    setTimeout(() => { t.classList.add('out'); setTimeout(() => t.remove(), 200); }, kind === 'err' ? 5000 : 2600);
}

function confirmBox(title, text, okLabel, danger = false) {
    const d = $('confirmDialog');
    $('confirmTitle').textContent = title;
    $('confirmText').textContent = text;
    $('confirmOk').textContent = okLabel;
    $('confirmOk').className = 'btn ' + (danger ? 'danger armed' : 'primary');
    d.returnValue = '';
    d.showModal();
    return new Promise(resolve => d.addEventListener('close', () => resolve(d.returnValue === 'ok'), { once: true }));
}

// ============================================================
// Config helpers
// ============================================================
const deviceIds = () => Object.keys((state.config && state.config.devices) || {});
const isConnected = id => ((state.server.ports || {})[id] || []).length > 0;

function setDirty(v) {
    state.dirty = v;
    $('dirtyDot').hidden = !v;
    $('saveBtn').classList.toggle('nudge', v);
}
function changed() { setDirty(true); renderAll(); }

async function save() {
    try {
        await api('/api/save', state.config);
        setDirty(false);
        toast('Saved. Changes are live.', 'ok');
        await refresh(true);
    } catch (e) {
        toast('Not saved: ' + e.message, 'err');
    }
}

// ============================================================
// Tabs & menu
// ============================================================
function selectTab(name) {
    document.querySelectorAll('.tab').forEach(t => t.setAttribute('aria-selected', String(t.dataset.tab === name)));
    document.querySelectorAll('.panel').forEach(p => { p.hidden = p.dataset.panel !== name; });
    storageSet('midihub.tab', name);
}
function toggleMenu(open) {
    const m = $('menu');
    m.hidden = open === undefined ? !m.hidden : !open;
    $('menuBtn').setAttribute('aria-expanded', String(!m.hidden));
}

// ============================================================
// Status pills & alerts
// ============================================================
function setPill(which, dotClass, text) {
    $(which + 'Dot').className = 'dot ' + dotClass;
    $(which + 'PillText').textContent = text;
}

function renderStatus() {
    const ids = deviceIds();
    const on = ids.filter(isConnected).length;
    setPill('dev', !ids.length ? '' : on === ids.length ? 'ok' : on ? 'warn' : 'err',
        ids.length ? `${on} of ${ids.length} devices` : 'No devices');

    const g = state.server.guard || {};
    const plan = state.server.plan || {};
    const muted = Object.keys(g.muted || {});
    const errs = (plan.warnings || []).filter(w => w.level === 'err').length;
    const warns = (plan.warnings || []).length - errs;
    const enabled = (state.config.loop_guard || {}).echo_guard !== false;
    if (muted.length) setPill('guard', 'err', `Muted: ${muted.join(', ')}`);
    else if (errs) setPill('guard', 'warn', errs === 1 ? '1 route blocked' : `${errs} routes blocked`);
    else if (warns) setPill('guard', 'warn', warns === 1 ? '1 loop warning' : `${warns} loop warnings`);
    else setPill('guard', enabled ? 'ok' : '', enabled ? 'No loops' : 'Loop guard off');

    // Alerts card: live alerts first, then routing warnings
    const items = [];
    for (const a of (g.alerts || []).slice().reverse()) {
        items.push(el('div', { className: 'alert' },
            el('span', { className: 'dot ' + (a.level === 'err' ? 'err' : 'warn') }),
            el('span', { textContent: a.text }),
            el('time', { textContent: new Date(a.t * 1000).toLocaleTimeString() })));
    }
    for (const w of plan.warnings || []) {
        items.push(el('div', { className: 'alert' },
            el('span', { className: 'dot ' + (w.level === 'err' ? 'err' : 'warn') }),
            el('span', { textContent: w.text })));
    }
    $('alertsCard').hidden = !items.length;
    $('alertList').replaceChildren(...items);
    $('alertsHint').textContent = muted.length
        ? 'Outputs of a looping device are muted. Fix the cause, then press Reset.'
        : 'Things that could send MIDI in circles.';
    $('guardResetBtn').hidden = !(muted.length || (g.alerts || []).length);
}

// ============================================================
// Devices
// ============================================================
function renderDevices() {
    const list = $('deviceList');
    const ids = deviceIds();
    const act = state.server.activity || {};
    if (!ids.length) {
        list.replaceChildren(el('div', { className: 'empty', textContent: 'No devices yet. Plug in an instrument and pick its port below.' }));
    } else {
        list.replaceChildren(...ids.map(id => {
            const d = state.config.devices[id];
            const ports = (state.server.ports || {})[id] || [];
            const dot = el('span', { className: 'dot ' + (ports.length ? 'ok' : ''), id: 'act-' + id });
            const title = el('div', { className: 'mapping-title' },
                el('strong', { textContent: id }),
                el('span', { className: 'badge ' + (ports.length ? 'ok' : 'muted'), textContent: ports.length ? 'connected' : 'not connected' }));
            const meta = el('div', { className: 'mapping-meta',
                textContent: `matches “${d.match}”` + (ports.length ? ` · ${ports.join(', ')}` : '') + ` · ${act[id] || 0} messages` });
            const editBtn = el('button', { className: 'btn', type: 'button', textContent: 'Edit' });
            const delBtn = el('button', { className: 'btn icon danger', type: 'button', title: 'Remove device', textContent: '×' });
            delBtn.setAttribute('aria-label', 'Remove ' + id);
            editBtn.addEventListener('click', () => editDevice(id));
            delBtn.addEventListener('click', () => removeDevice(id));
            return el('div', { className: 'mapping static' + (state.editingDevice === id ? ' active' : '') },
                el('span', { className: 'swatch dev' }, dot), title,
                el('div', { className: 'mapping-actions' }, editBtn, delBtn), meta);
        }));
    }
    renderPortChoices();
}

const portMatch = p => p.replace(/\s*\d+:\d+\s*$/, '');
function matchedBy(port) {
    return deviceIds().find(id => { const m = state.config.devices[id].match; return m && port.includes(m); });
}
function suggestName(port) {
    const toks = port.split(':')[0].split(/[\s_-]+/).filter(t => /[a-zA-Z]/.test(t));
    let base = (toks[toks.length - 1] || 'device').toLowerCase().replace(/[^a-z0-9]/g, '') || 'device';
    let name = base, i = 2;
    while (state.config.devices[name]) name = base + i++;
    return name;
}

function renderPortChoices() {
    const ports = (state.server.available || []).filter(p => !/Midi Through/i.test(p) && !/^midihub/i.test(p));
    const sel = $('devPort');
    const cur = sel.value;
    sel.replaceChildren(el('option', { value: '', textContent: ports.length ? '— pick a port —' : 'No ports found' }),
        ...ports.map(p => el('option', { value: p, textContent: p + (matchedBy(p) ? `  (${matchedBy(p)})` : '') })));
    sel.value = ports.includes(cur) ? cur : '';
    $('portChips').replaceChildren(...ports.map(p => {
        const m = matchedBy(p);
        const chip = el('button', { type: 'button', className: 'chip' + (m ? ' matched' : '') });
        if (m) chip.append(el('span', { textContent: p + ' → ' }), el('b', { textContent: m }));
        else { chip.append(el('span', { textContent: '+ ' + p })); chip.addEventListener('click', () => pickPort(p)); }
        return chip;
    }));
}

function pickPort(port) {
    $('devPort').value = port;
    if (!state.editingDevice) $('devName').value = suggestName(port);
    $('devMatch').value = portMatch(port);
    $('devName').focus();
}

function editDevice(id) {
    state.editingDevice = id;
    $('devName').value = id;
    $('devMatch').value = state.config.devices[id].match;
    $('devAddBtn').textContent = 'Save device';
    $('devCancelBtn').hidden = false;
    $('deviceForm').classList.add('editing');
    renderDevices();
}
function cancelDevice() {
    state.editingDevice = null;
    $('devName').value = $('devMatch').value = $('devPort').value = '';
    $('devAddBtn').textContent = 'Add device';
    $('devCancelBtn').hidden = true;
    $('deviceForm').classList.remove('editing');
    renderDevices();
}

function renameDevice(from, to) {
    const c = state.config;
    const devs = {};
    for (const [k, v] of Object.entries(c.devices)) devs[k === from ? to : k] = v;
    c.devices = devs;
    for (const r of c.routes || []) {
        if (r.from === from) r.from = to;
        r.to = (r.to || []).map(t => t === from ? to : t);
    }
    for (const m of c.mappings || []) {
        if (m.input && m.input.device === from) m.input.device = to;
        if (m.output && m.output.device === from) m.output.device = to;
    }
    if (c.init && c.init[from]) { c.init[to] = c.init[from]; delete c.init[from]; }
    const ex = (c.network || {}).export || [];
    c.network.export = ex.map(d => d === from ? to : d);
}

function saveDevice() {
    const name = $('devName').value.trim().toLowerCase().replace(/[^a-z0-9_-]/g, '');
    const match = $('devMatch').value.trim();
    if (!name || !match) { toast('Pick a port, or fill in a name and match text.', 'warn'); return; }
    const editing = state.editingDevice;
    if (name !== editing && state.config.devices[name]) { toast(`There is already a device called ${name}.`, 'warn'); return; }
    if (editing && editing !== name) renameDevice(editing, name);
    state.config.devices[name] = { ...(state.config.devices[name] || {}), match };
    toast(editing ? `Updated ${name}` : `Added ${name}. Save to start using it.`, 'ok');
    cancelDevice();
    changed();
}

async function removeDevice(id) {
    const used = (state.config.routes || []).some(r => r.from === id || (r.to || []).includes(id)) ||
        (state.config.mappings || []).some(m => (m.input || {}).device === id || (m.output || {}).device === id);
    if (used && !await confirmBox(`Remove ${id}?`, 'Its routes and mappings are removed too.', 'Remove', true)) return;
    const c = state.config;
    delete c.devices[id];
    c.routes = (c.routes || []).filter(r => r.from !== id).map(r => ({ ...r, to: (r.to || []).filter(t => t !== id) })).filter(r => r.to.length);
    c.mappings = (c.mappings || []).filter(m => (m.input || {}).device !== id && (m.output || {}).device !== id);
    if (c.init) delete c.init[id];
    if (c.network && c.network.export) c.network.export = c.network.export.filter(d => d !== id);
    if (state.editingDevice === id) cancelDevice();
    changed();
}

// ============================================================
// Routes
// ============================================================
function routeState(from, to) {
    const plan = state.server.plan || {};
    const has = list => (list || []).some(e => e[0] === from && e[1] === to);
    if ((plan.blocked || []).some(b => b.from === from && b.to === to)) return 'blocked';
    if (has(plan.filtered)) return 'filtered';
    if (has(plan.direct)) return 'direct';
    return null;
}

function renderRoutes() {
    const ids = deviceIds();
    const table = $('routeMatrix');
    if (ids.length < 2) {
        table.replaceChildren(el('tr', {}, el('td', { className: 'empty', textContent: 'Add at least two devices to route between them.' })));
        return;
    }
    const routes = state.config.routes || [];
    const head = el('tr', {}, el('th', { className: 'from', textContent: 'from ↓ / to →' }), ...ids.map(i => el('th', { textContent: i })));
    const rows = ids.map(from => el('tr', {}, el('td', { className: 'from', textContent: from }), ...ids.map(to => {
        if (from === to) return el('td', { className: 'self', textContent: '—' });
        const on = routes.some(r => r.from === from && (r.to || []).includes(to));
        const cb = el('input', { type: 'checkbox', checked: on });
        cb.setAttribute('aria-label', `Route ${from} to ${to}`);
        cb.addEventListener('change', () => toggleRoute(from, to, cb.checked));
        const cell = el('span', { className: 'cell' }, cb);
        const st = on && !state.dirty ? routeState(from, to) : null;
        if (st) {
            const badge = el('span', { className: 'badge ' + ({ direct: 'ok', filtered: 'warn', blocked: 'err' })[st], textContent: st });
            if (st === 'blocked') {
                const b = state.server.plan.blocked.find(x => x.from === from && x.to === to);
                badge.title = 'Loop: ' + b.cycle.join(' → ');
            }
            cell.append(badge);
        }
        return el('td', {}, cell);
    })));
    table.replaceChildren(head, ...rows);
}

function toggleRoute(from, to, on) {
    const routes = state.config.routes = state.config.routes || [];
    let r = routes.find(x => x.from === from);
    if (on) {
        if (!r) routes.push(r = { from, to: [] });
        if (!r.to.includes(to)) r.to.push(to);
        if (routes.some(x => x.from === to && (x.to || []).includes(from)))
            toast(`${to} already sends to ${from}. Routing both ways makes a loop, so one of them will be blocked.`, 'warn');
    } else if (r) {
        r.to = r.to.filter(t => t !== to);
        if (!r.to.length) routes.splice(routes.indexOf(r), 1);
    }
    changed();
}

// ============================================================
// Mappings: form
// ============================================================
function fillSelect(sel, options, value) {
    sel.replaceChildren(...options.map(([v, label]) => el('option', { value: v, textContent: label })));
    if (value !== undefined) sel.value = value;
}

function channelOptions(anyLabel) {
    return [['', anyLabel], ...Array.from({ length: 16 }, (_, i) => [String(i), `Channel ${i + 1}`])];
}

function fillMapSelects() {
    const ids = deviceIds();
    const keep = id => $(id).value;
    const iv = keep('inDev'), ov = keep('outDev');
    fillSelect($('inDev'), ids.map(i => [i, i]), ids.includes(iv) ? iv : ids[0]);
    fillSelect($('outDev'), [['', 'Where its routes go'], ...ids.map(i => [i, i])], ids.includes(ov) ? ov : '');
    if (!$('inType').options.length) {
        const types = Object.entries(TYPES).map(([k, v]) => [k, v.label]);
        fillSelect($('inType'), types, 'cc');
        fillSelect($('outType'), types, 'cc');
        fillSelect($('inCh'), channelOptions('Any channel'), '');
        fillSelect($('outCh'), channelOptions('Same as incoming'), '');
    }
    updateMapFields();
}

function updateMapFields() {
    for (const side of ['in', 'out']) {
        const t = $(side + 'Type').value;
        const box = $(side + 'Side');
        const info = TYPES[t] || TYPES.cc;
        box.querySelectorAll('[data-show=channel]').forEach(n => { n.hidden = t === 'sysex'; });
        box.querySelectorAll('[data-show=number]').forEach(n => { n.hidden = !info.num; });
        box.querySelectorAll('[data-show=sysex]').forEach(n => { n.hidden = t !== 'sysex'; });
        $(side + 'NumLabel').textContent = info.num || 'Number';
        $(side + 'Num').placeholder = side === 'in' ? 'any' : (t === 'pc' ? 'from value' : 'same');
    }
    $('testBtn').disabled = !$('outDev').value && !routeTargets($('inDev').value).length;
}

function routeTargets(dev) {
    return ((state.config.routes || []).find(r => r.from === dev) || {}).to || [];
}

function specFromForm(side) {
    const t = $(side + 'Type').value;
    const spec = { type: t };
    const dev = $(side + 'Dev').value;
    if (dev) spec.device = dev;
    if (t === 'sysex') {
        const tpl = hexToBytes($(side + 'Tpl').value);
        if (tpl.length) spec.template = tpl;
        const vi = intOrNull($(side + 'Vi').value);
        if (vi != null) spec.value_index = vi;
        return spec;
    }
    const ch = intOrNull($(side + 'Ch').value);
    if (ch != null) spec.channel = ch;
    const n = intOrNull($(side + 'Num').value);
    if (n != null && NUM_KEY[t] && TYPES[t].num) spec[NUM_KEY[t]] = Math.max(0, Math.min(127, n));
    return spec;
}

function mappingFromForm() {
    const input = specFromForm('in');
    const output = specFromForm('out');
    const sc = ['sc0', 'sc1', 'sc2', 'sc3'].map(id => intOrNull($(id).value));
    if (sc.every(v => v != null)) output.scale = sc;
    if ($('replaceOrig').checked) output.block_original = true;
    return { input, output };
}

function specToForm(side, spec) {
    let t = spec.type || (side === 'out' ? 'sysex' : 'cc');
    if (t === 'noteon' || t === 'noteoff') t = 'note';
    $(side + 'Type').value = t;
    if (side === 'in' && spec.device) $('inDev').value = spec.device;
    if (side === 'out') $('outDev').value = spec.device || '';
    $(side + 'Ch').value = spec.channel == null ? '' : String(spec.channel);
    const n = spec[NUM_KEY[t]] ?? (t === 'note' ? spec.noteon : undefined);
    $(side + 'Num').value = n == null ? '' : n;
    $(side + 'Tpl').value = bytesToHex(spec.template || []);
    $(side + 'Vi').value = spec.value_index == null ? '' : spec.value_index;
    updateMapFields();
}

function resetMapForm() {
    state.editingMap = null;
    stopLearn();
    specToForm('in', { type: 'cc', device: $('inDev').value });
    specToForm('out', { type: 'cc' });
    ['sc0', 'sc1', 'sc2', 'sc3'].forEach(id => { $(id).value = ''; });
    $('replaceOrig').checked = false;
    $('mapAddBtn').textContent = 'Add mapping';
    $('mapCancelBtn').hidden = true;
    $('mapForm').classList.remove('editing');
    $('mapNote').textContent = '';
    renderMappings();
}

function editMapping(i) {
    const m = state.config.mappings[i];
    state.editingMap = i;
    specToForm('in', m.input || {});
    specToForm('out', m.output || {});
    const sc = (m.output || {}).scale || [];
    ['sc0', 'sc1', 'sc2', 'sc3'].forEach((id, k) => { $(id).value = sc[k] ?? ''; });
    $('replaceOrig').checked = !!(m.output || {}).block_original;
    $('mapAddBtn').textContent = 'Save mapping';
    $('mapCancelBtn').hidden = false;
    $('mapForm').classList.add('editing');
    $('mapNote').textContent = `Editing mapping ${i + 1}`;
    renderMappings();
    $('mapForm').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function saveMapping() {
    const m = mappingFromForm();
    if (!m.input.device) { toast('Add a device first.', 'warn'); return; }
    if (m.output.type === 'sysex' && !(m.output.template || []).length) { toast('Enter the SysEx message to send, or press Learn on the right.', 'warn'); return; }
    if (m.input.type === 'sysex' && !(m.input.template || []).length) { toast('Enter the SysEx message to listen for, or press Learn on the left.', 'warn'); return; }
    if (!m.output.device && !routeTargets(m.input.device).length)
        toast(`${m.input.device} has no routes yet, so this mapping goes nowhere until you pick a device.`, 'warn');
    state.config.mappings = state.config.mappings || [];
    if (state.editingMap != null) state.config.mappings[state.editingMap] = { ...state.config.mappings[state.editingMap], ...m };
    else state.config.mappings.push(m);
    toast(state.editingMap != null ? 'Mapping updated' : 'Mapping added. Save to make it live.', 'ok');
    setDirty(true);
    resetMapForm();
    renderAll();
}

async function removeMapping(i) {
    state.config.mappings.splice(i, 1);
    if (state.editingMap === i) resetMapForm();
    else if (state.editingMap != null && state.editingMap > i) state.editingMap--;
    changed();
}

async function testOutput() {
    const m = mappingFromForm();
    const devs = m.output.device ? [m.output.device] : routeTargets(m.input.device);
    try {
        let sent;
        for (const d of devs) sent = await api('/api/test', { device: d, output: m.output, value: m.input.type === 'pitchbend' ? 16383 : 127, input_type: m.input.type });
        if (sent) toast(`Sent ${bytesToHex(sent.bytes)} to ${devs.join(', ')}`, 'ok');
    } catch (e) { toast(e.message, 'err'); }
}

// ============================================================
// Mappings: list
// ============================================================
function renderMappings() {
    const list = $('mapList');
    const maps = state.config.mappings || [];
    const warned = new Set(((state.server.plan || {}).warnings || []).filter(w => w.mapping != null).map(w => w.mapping));
    if (!maps.length) {
        list.replaceChildren(el('div', { className: 'empty', textContent: 'No mappings yet. Press Learn, move a control, and add your first one.' }));
        return;
    }
    list.replaceChildren(...maps.map((m, i) => {
        const inp = m.input || {}, out = m.output || {};
        const to = out.device || (routeTargets(inp.device).join(', ') || 'nowhere (no routes)');
        const title = el('div', { className: 'mapping-title' },
            el('strong', { textContent: `${inp.device || '?'} · ${sideLabel(inp, false)}` }),
            el('span', { className: 'arrow', textContent: '→' }),
            el('strong', { textContent: `${to} · ${sideLabel(out, true)}` }));
        const bits = [];
        if (out.scale) bits.push(`range ${out.scale[0]}–${out.scale[1]} → ${out.scale[2]}–${out.scale[3]}`);
        if (out.block_original) bits.push('replaces the original');
        if (out.type === 'sysex' && out.value_index == null) bits.push('fixed message');
        if (warned.has(i) && !state.dirty) bits.push('can feed back, see Loop guard');
        const meta = el('div', { className: 'mapping-meta', textContent: bits.join(' · ') || `Mapping ${i + 1}` });
        const badge = el('span', { className: 'badge', textContent: String(i + 1) });
        const editBtn = el('button', { className: 'btn', type: 'button', textContent: 'Edit' });
        const delBtn = el('button', { className: 'btn icon danger', type: 'button', title: 'Delete mapping', textContent: '×' });
        delBtn.setAttribute('aria-label', `Delete mapping ${i + 1}`);
        editBtn.addEventListener('click', e => { e.stopPropagation(); editMapping(i); });
        delBtn.addEventListener('click', e => { e.stopPropagation(); removeMapping(i); });
        const row = el('div', { className: 'mapping' + (state.editingMap === i ? ' active' : '') + (warned.has(i) && !state.dirty ? ' warn' : '') },
            el('span', { className: 'swatch dev' }, badge), title, el('div', { className: 'mapping-actions' }, editBtn, delBtn), meta);
        row.addEventListener('click', () => editMapping(i));
        return row;
    }));
}

// ============================================================
// Learn (press to learn) and monitor share one event stream
// ============================================================
let pollTimer = null;
async function ensureListening() {
    const want = !!state.learning || state.monitorOn;
    if (want && !pollTimer) {
        const r = await api('/api/learn', { on: true }).then(() => api('/api/learn?after=999999999'));
        state.seq = r.seq;
        pollTimer = setInterval(pollEvents, 120);
    } else if (!want && pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
        api('/api/learn', { on: false }).catch(() => {});
    }
}

async function pollEvents() {
    let r;
    try { r = await api('/api/learn?after=' + state.seq); } catch { return; }
    if (!r.active) { api('/api/learn', { on: true }).catch(() => {}); return; }
    for (const e of r.events) {
        state.seq = Math.max(state.seq, e.seq);
        if (state.monitorOn) addMonitor(e);
        if (state.learning) learnEvent(e);
    }
}

function setLearnUI() {
    for (const side of ['in', 'out']) {
        const on = state.learning && state.learning.side === side;
        const b = $(side + 'LearnBtn');
        b.classList.toggle('learning', !!on);
        b.textContent = on ? 'Listening…' : 'Learn';
        const hint = $(side + 'Hint');
        hint.classList.toggle('on', !!on);
        if (on) hint.textContent = side === 'in'
            ? 'Move a control, press a key or pad…'
            : 'Move the control on the device that should receive it…';
        else if (hint.classList.contains('on') || !state.learning) hint.textContent = hint.dataset.done || '';
    }
}

async function startLearn(side) {
    if (state.learning && state.learning.side === side) {
        // Second press: keep a fixed SysEx message if that's all we saw
        const l = state.learning;
        const any = Object.entries(l.sysex).find(([, v]) => v.length);
        if (any) applySysex(side, any[0], any[1][any[1].length - 1], null);
        stopLearn();
        return;
    }
    if (!deviceIds().length) { toast('Add a device first.', 'warn'); return; }
    state.learning = { side, sysex: {} };
    $(side + 'Hint').dataset.done = '';
    setLearnUI();
    try { await ensureListening(); } catch (e) { toast(e.message, 'err'); stopLearn(); }
}

function stopLearn() {
    if (!state.learning) return;
    state.learning = null;
    setLearnUI();
    ensureListening().catch(() => {});
}

function learnEvent(e) {
    const l = state.learning;
    const b = e.bytes;
    const side = l.side;
    if (b[0] === 0xF0) {
        const list = (l.sysex[e.device] = l.sysex[e.device] || []);
        list.push(b);
        const same = list.filter(m => m.length === b.length);
        const vi = detectValueIndex(same.slice(-8));
        if (vi != null) {
            applySysex(side, e.device, b, vi);
            stopLearn();
        } else {
            $(side + 'Hint').textContent = `Got ${list.length} SysEx message${list.length > 1 ? 's' : ''}. Keep moving the control to find the value, or press again to keep it as a fixed message.`;
        }
        return;
    }
    const hi = b[0] & 0xF0;
    if (hi === 0x80 || (hi === 0x90 && b[2] === 0)) return;  // wait for the note-on
    const t = TYPE_OF_STATUS[hi];
    if (!t) return;
    const spec = { type: t, channel: b[0] & 0x0F };
    if (NUM_KEY[t]) spec[NUM_KEY[t]] = b[1];
    if (side === 'out') {
        if (t === 'pc') delete spec.pc;  // program follows the incoming value
        spec.device = e.device;
    } else spec.device = e.device;
    specToForm(side, spec);
    const msg = `${e.device}: ${describe(b)}`;
    $(side + 'Hint').dataset.done = 'Learned ' + msg;
    stopLearn();
    toast('Got it: ' + msg, 'ok');
}

function applySysex(side, dev, bytes, vi) {
    specToForm(side, { type: 'sysex', device: dev, template: bytes, value_index: vi });
    const msg = vi == null ? `${dev}: fixed SysEx message` : `${dev}: SysEx, value in byte ${vi}`;
    $(side + 'Hint').dataset.done = 'Learned ' + msg;
    toast('Got it: ' + msg, 'ok');
}

// ---------- Monitor ----------
function addMonitor(e) {
    state.monitor.unshift(e);
    if (state.monitor.length > 150) state.monitor.length = 150;
    renderMonitorSoon();
}
let monitorQueued = false;
function renderMonitorSoon() {
    if (monitorQueued) return;
    monitorQueued = true;
    requestAnimationFrame(() => { monitorQueued = false; renderMonitor(); });
}

function renderMonitor() {
    $('monBtn').textContent = state.monitorOn ? 'Stop listening' : 'Start listening';
    $('monBtn').classList.toggle('learning', state.monitorOn);
    $('monHint').textContent = state.monitorOn ? 'Play something…' : '';
    const body = $('monBody');
    if (!state.monitor.length) {
        body.replaceChildren(el('tr', {}, Object.assign(el('td', { className: 'hint', textContent: state.monitorOn ? 'Nothing received yet.' : 'Press Start listening to see incoming MIDI.' }), { colSpan: 4 })));
        return;
    }
    body.replaceChildren(...state.monitor.map(e => {
        const useIn = el('button', { className: 'btn', type: 'button', textContent: 'Use as input' });
        const useOut = el('button', { className: 'btn', type: 'button', textContent: 'Use as output' });
        useIn.addEventListener('click', () => useEvent('in', e));
        useOut.addEventListener('click', () => useEvent('out', e));
        return el('tr', {},
            el('td', { textContent: e.device }),
            el('td', { textContent: describe(e.bytes) }),
            el('td', { className: 'hex', textContent: bytesToHex(e.bytes) }),
            el('td', { className: 'acts' }, useIn, ' ', useOut));
    }));
}

function useEvent(side, e) {
    if (state.editingMap == null && !$('mapCancelBtn').hidden) resetMapForm();
    if (e.bytes[0] === 0xF0) {
        const same = state.monitor.filter(x => x.device === e.device && x.bytes[0] === 0xF0 && x.bytes.length === e.bytes.length).map(x => x.bytes);
        applySysex(side, e.device, e.bytes, detectValueIndex(same.slice(0, 8)));
    } else {
        state.learning = { side, sysex: {} };
        learnEvent(e);
    }
    selectTab('mappings');
}

async function toggleMonitor() {
    state.monitorOn = !state.monitorOn;
    renderMonitor();
    try { await ensureListening(); } catch (e) { toast(e.message, 'err'); }
}

async function sendRaw() {
    const bytes = hexToBytes($('sendHex').value);
    const device = $('sendDev').value;
    if (!device || !bytes.length) { toast('Pick a device and type some hex bytes.', 'warn'); return; }
    try { await api('/api/test', { device, bytes }); toast(`Sent ${bytesToHex(bytes)} to ${device}`, 'ok'); }
    catch (e) { toast(e.message, 'err'); }
}

// ============================================================
// Settings
// ============================================================
function renderSettings() {
    const c = state.config;
    const ids = deviceIds();
    // Startup messages
    $('initList').replaceChildren(...(ids.length ? ids.map(id => {
        const input = el('input', { type: 'text', className: 'mono', placeholder: 'F0 7D 00 F7 | B0 79 00', autocomplete: 'off',
            value: ((c.init || {})[id] || []).map(bytesToHex).join(' | ') });
        input.addEventListener('change', () => {
            const msgs = input.value.split('|').map(hexToBytes).filter(b => b.length);
            c.init = c.init || {};
            if (msgs.length) c.init[id] = msgs; else delete c.init[id];
            setDirty(true);
        });
        return el('label', { className: 'field span-all' }, el('span', { textContent: id }), input);
    }) : [el('div', { className: 'empty span-all', textContent: 'No devices yet.' })]));

    // Loop guard
    const lg = c.loop_guard || {};
    $('lgBlock').checked = lg.block_cycles !== false;
    $('lgEcho').checked = lg.echo_guard !== false;
    $('lgFloodMute').checked = !!lg.flood_mute;
    $('lgFlood').value = lg.flood_limit ?? 2000;
    $('lgMute').value = lg.mute_seconds ?? 5;

    // Network
    const net = c.network || {};
    const exported = new Set(net.export || []);
    $('netList').replaceChildren(...(ids.length ? ids.map(id => {
        const cb = el('input', { type: 'checkbox', checked: exported.has(id) });
        cb.addEventListener('change', () => {
            c.network = c.network || {};
            const set = new Set(c.network.export || []);
            cb.checked ? set.add(id) : set.delete(id);
            c.network.export = [...set];
            setDirty(true);
        });
        return el('label', { className: 'check' }, cb, ` Share ${id}`);
    }) : [el('span', { className: 'hint', textContent: 'No devices yet.' })]));
    $('netPort').value = net.port || 21928;
    $('netMcast').value = net.mcast || '225.0.0.37';

    // Web
    const w = state.server.web || {};
    $('webInfo').textContent = w.password
        ? `Password protected · listening on ${w.host}:${w.port}. Change it on the hub with: python3 web.py --set-password`
        : `No password · listening on ${w.host}:${w.port} (this machine only). To open the web page to your network, set a password on the hub with: python3 web.py --set-password`;
    $('logoutBtn').hidden = !w.password;

    // Device pickers elsewhere
    const sd = $('sendDev').value;
    fillSelect($('sendDev'), ids.map(i => [i, i]), ids.includes(sd) ? sd : ids[0]);
}

function lgChanged() {
    const c = state.config;
    c.loop_guard = {
        ...(c.loop_guard || {}),
        block_cycles: $('lgBlock').checked,
        echo_guard: $('lgEcho').checked,
        flood_mute: $('lgFloodMute').checked,
        flood_limit: intOrNull($('lgFlood').value) || 2000,
        mute_seconds: intOrNull($('lgMute').value) || 5,
    };
    setDirty(true);
}

// ============================================================
// Refresh from the hub
// ============================================================
function renderAll() {
    renderStatus();
    renderDevices();
    renderRoutes();
    fillMapSelects();
    renderMappings();
    renderSettings();
}

async function refresh(full = false) {
    let s;
    if (!state.config) full = true;
    try { s = await api('/api/state'); } catch { setPill('dev', 'err', 'Hub not reachable'); return; }
    const before = JSON.stringify(state.server.ports || {}) + JSON.stringify(state.server.available || []) +
        JSON.stringify(state.server.plan || {}) + JSON.stringify(state.server.guard ? [state.server.guard.muted, state.server.guard.alerts.length] : []);
    const prevAct = state.server.activity || {};
    state.server = s;
    if (!state.dirty && (full || !state.config || JSON.stringify(s.config) !== JSON.stringify(state.config))) {
        state.config = structuredClone(s.config);
        renderAll();
        return;
    }
    const after = JSON.stringify(s.ports) + JSON.stringify(s.available) + JSON.stringify(s.plan) + JSON.stringify([s.guard.muted, s.guard.alerts.length]);
    if (before !== after) renderAll();
    else renderStatus();
    // Activity: flash a device's dot when it sent something
    for (const [dev, n] of Object.entries(s.activity || {})) {
        if (prevAct[dev] !== undefined && n !== prevAct[dev]) {
            const d = $('act-' + dev);
            if (d) { d.classList.add('flash'); setTimeout(() => d.classList.remove('flash'), 150); }
        }
    }
}

// ============================================================
// Wire up
// ============================================================
function wire() {
    $('saveBtn').addEventListener('click', save);
    $('menuBtn').addEventListener('click', e => { e.stopPropagation(); toggleMenu(); });
    document.addEventListener('click', e => { if (!$('menu').contains(e.target)) toggleMenu(false); });
    $('revertBtn').addEventListener('click', async () => { toggleMenu(false); setDirty(false); await refresh(true); toast('Changes discarded'); });
    $('logoutBtn').addEventListener('click', async () => { await api('/api/logout', {}); location.href = '/login'; });
    $('devPill').addEventListener('click', () => selectTab('devices'));
    $('guardPill').addEventListener('click', () => {
        if (!$('alertsCard').hidden) $('alertsCard').scrollIntoView({ behavior: 'smooth' });
        else selectTab('settings');
    });
    $('guardResetBtn').addEventListener('click', async () => {
        try { await api('/api/guard/reset', {}); toast('Loop guard reset', 'ok'); refresh(); } catch (e) { toast(e.message, 'err'); }
    });
    document.querySelectorAll('.tab').forEach(t => t.addEventListener('click', () => selectTab(t.dataset.tab)));

    // Devices
    $('devPort').addEventListener('change', () => { if ($('devPort').value) pickPort($('devPort').value); });
    $('devAddBtn').addEventListener('click', saveDevice);
    $('devCancelBtn').addEventListener('click', cancelDevice);

    // Mappings
    ['inType', 'outType', 'inDev', 'outDev'].forEach(id => $(id).addEventListener('change', updateMapFields));
    $('inLearnBtn').addEventListener('click', () => startLearn('in'));
    $('outLearnBtn').addEventListener('click', () => startLearn('out'));
    $('testBtn').addEventListener('click', testOutput);
    $('mapAddBtn').addEventListener('click', saveMapping);
    $('mapCancelBtn').addEventListener('click', resetMapForm);

    // Monitor
    $('monBtn').addEventListener('click', toggleMonitor);
    $('monClearBtn').addEventListener('click', () => { state.monitor = []; renderMonitor(); });
    $('sendBtn').addEventListener('click', sendRaw);
    $('sendHex').addEventListener('keydown', e => { if (e.key === 'Enter') sendRaw(); });

    // Settings
    ['lgBlock', 'lgEcho', 'lgFloodMute', 'lgFlood', 'lgMute'].forEach(id => $(id).addEventListener('change', lgChanged));
    $('netPort').addEventListener('change', () => { state.config.network = { ...(state.config.network || {}), port: intOrNull($('netPort').value) || 21928 }; setDirty(true); });
    $('netMcast').addEventListener('change', () => { state.config.network = { ...(state.config.network || {}), mcast: $('netMcast').value.trim() || '225.0.0.37' }; setDirty(true); });

    window.addEventListener('beforeunload', e => { if (state.dirty) { e.preventDefault(); e.returnValue = ''; } });
    document.addEventListener('keydown', e => {
        if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); save(); }
        if (e.key === 'Escape' && state.learning) stopLearn();
    });
}

wire();
selectTab(storageGet('midihub.tab') || 'devices');
renderMonitor();
refresh(true).then(() => resetMapForm());
setInterval(refresh, 1500);
