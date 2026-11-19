// "Channels": the Memory List (named frequencies in folders, each with its
// own modulation, priority weight and on/off) and the blacklist.
import { api, carrierHz, confirmDialog, errorToast, fmtChannel, fmtMHz, formDialog, h, icon, modal, mount, select, tabs, t, toast, typedAs833 } from '../core.js';
import { exportChannelsXml, MAX_CHANNEL_FILE_BYTES, parseChannelsXml, planChannelImport } from '../channel-transfer.js';
import { S, cfg, loadSettings, on, patchConfig, saveBlacklist, saveChannels } from '../state.js';
import { modLabel, modOptions } from './bands.js';
let tab = 'memory';

// Sortable column headers (click: sort by it, click again: reverse). `dir` is the first click's direction:
// 1 = ascending (A-Z, low-high), -1 = descending (highest / newest / switched on first).
const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });
const SORTS = {
  memory: { storage: 'channelsSort', cols: {
    on: { get: (c) => (c.enabled ? 1 : 0), dir: -1 },
    freq: { get: (c) => c.freq_hz, dir: 1 },
    name: { get: (c) => c.name || '', text: true, dir: 1 },
    mod: { get: (c) => modLabel(c.modulation), text: true, dir: 1 },
    weight: { get: (c) => c.weight || 0, dir: -1 },
  } },
  blacklist: { storage: 'blacklistSort', cols: {
    freq: { get: (b) => b.freq_hz, dir: 1 },
    margin: { get: (b) => b.margin_hz, dir: -1 },
    label: { get: (b) => (b.label === 'Auto-lockout (SKIP)' ? t('Skipped from the Scan page') : b.label || ''), text: true, dir: 1 },
    added: { get: (b) => b.date_added || '', text: true, dir: -1 },
  } },
};
const sortState = {};
for (const [name, def] of Object.entries(SORTS)) {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(def.storage)); } catch { /* private window: start from the default */ }
  sortState[name] = saved && def.cols[saved.key] && (saved.dir === 1 || saved.dir === -1) ? saved : { key: 'freq', dir: 1 };
}

function sorted(name, list) {
  const { key, dir } = sortState[name];
  const col = SORTS[name].cols[key];
  return [...list].sort((a, b) => {
    const x = col.get(a), y = col.get(b);
    // An empty text (no name, no description) goes last either way - reversing must not bury the real entries.
    if (col.text && (x === '') !== (y === '')) return x === '' ? 1 : -1;
    const c = col.text ? collator.compare(x, y) : x - y;
    return c ? c * dir : SORTS[name].cols.freq.get(a) - SORTS[name].cols.freq.get(b);
  });
}

function folders() {
  return [...new Set([...cfg().channel_folders, ...S.settings.channels.map((c) => c.folder).filter(Boolean)])].sort((a, b) => a.localeCompare(b));
}


export async function editChannelDialog(ch, defaults = {}) {
  const isNew = !ch;
  const initial = ch || defaults;
  const knownFolders = folders();
  let newFolderValue = '__new_folder__';
  while (knownFolders.includes(newFolderValue)) newFolderValue += '_';
  const v = await formDialog(isNew ? t('Add channel') : t('Edit channel'), [
    { key: 'freq', label: t('Frequency (MHz)'), type: 'number', value: initial.freq_hz ? fmtChannel(initial.freq_hz) : '',
      hint: t('Airband: type the channel as published, e.g. 126.805') },
    { key: 'name', label: t('Name'), value: isNew ? '' : initial.name || '', placeholder: t('e.g. Downtown Tower') },
    { key: 'mod', label: t('Modulation'), type: 'select', value: initial.modulation || 'AM', options: modOptions() },
    { key: 'folder', label: t('Folder'), type: 'select', value: initial.folder || '',
      options: [['', t('(no folder)')], ...knownFolders.map((f) => [f, f]), [newFolderValue, t('New folder…')]] },
    { key: 'newFolder', label: t('Folder name') },
    { key: 'weight', label: t('Priority weight'), type: 'number', value: initial.weight || 0,
      hint: t('0 = normal. Higher = checked more often.') },
  ], t('Save'), { dismissible: false, autocomplete: 'off', onChange: ({ folder, newFolder }) => {
    const creating = folder.value === newFolderValue;
    newFolder.closest('label').style.display = creating ? '' : 'none';
    newFolder.disabled = !creating;
    newFolder.required = creating;
    newFolder.setCustomValidity(creating && !newFolder.value.trim() ? t('Enter a folder name.') : '');
    if (creating && document.activeElement === folder) newFolder.focus();
  } });
  if (!v || !(v.freq > 0)) return false;
  const hz = carrierHz(v.freq);
  const channels = S.settings.channels;
  if (channels.some((c) => c.freq_hz === hz && c !== ch)) {
    toast(t('{f} MHz is already in the list', { f: fmtChannel(hz) }), 'bad');
    return false;
  }
  const folder = v.folder === newFolderValue ? v.newFolder.trim() : v.folder;
  const data = { freq_hz: hz, ch833: typedAs833(v.freq), name: v.name.trim(), modulation: v.mod, folder, weight: v.weight || 0 };
  if (folder && !cfg().channel_folders.includes(folder)) patchConfig({ channel_folders: [...cfg().channel_folders, folder] });
  await saveChannels(isNew ? [...channels, { ...data, enabled: true }]
    : channels.map((c) => c === ch ? { ...c, ...data } : c));
  return true;
}

export function openChannelForFrequency(freq_hz, name = '', modulation = 'AM') {
  const exact = carrierHz(fmtChannel(freq_hz));
  const existing = S.settings.channels.find((c) => c.freq_hz === exact);
  return editChannelDialog(existing, { freq_hz: exact, name, modulation });
}

export function channelsView(root) {
  const offs = [];
  const body = h('div.col');
  const tabsBox = h('div');
  const page = h('div.page', h('div.page-head', h('h1', t('Channels'))), tabsBox, body);
  mount(root, page);
  const renderTabs = () => mount(tabsBox, tabs([['memory', t('Memory List'), S.settings.channels.length], ['blacklist', t('Blacklist'), S.settings.blacklist.length]], tab,
    (v) => { tab = v; sel.clear(); render(); }));
  const collapsed = new Set(JSON.parse(localStorage.getItem('collapsedFolders') || '[]'));
  let query = '';

  // Row selection like Recordings: click, Ctrl/⌘+click, Shift+click; Delete
  // removes, Esc clears, Ctrl+A selects everything shown.
  const sel = new Set();
  const rowOf = new Map();
  const boxOf = new Map();            // item -> its checkbox
  let anchor = null;
  let bulkBox = h('div.bulk.hidden');
  let headBox = null;
  const shown = () => [...rowOf.keys()];
  function syncSel() {
    rowOf.forEach((tr, item) => tr.classList.toggle('selected', sel.has(item)));
    boxOf.forEach((cb, item) => { cb.checked = sel.has(item); });
    const all = shown();
    const n = all.filter((x) => sel.has(x)).length;
    if (headBox) { headBox.checked = !!all.length && n === all.length; headBox.indeterminate = n > 0 && n < all.length; }
    renderBulk();
  }
  // The checkbox column: tick one, shift+tick a range; the header box ticks everything shown.
  function checkbox(item) {
    const cb = h('input', { type: 'checkbox', 'aria-label': t('Select'), onclick: (e) => {
      e.stopPropagation();
      const all = shown();
      if (e.shiftKey && all.includes(anchor)) {
        const a = all.indexOf(anchor), b = all.indexOf(item);
        all.slice(Math.min(a, b), Math.max(a, b) + 1).forEach((x) => sel.add(x));
      } else { cb.checked ? sel.add(item) : sel.delete(item); }
      anchor = item;
      syncSel();
    } });
    boxOf.set(item, cb);
    return h('td.pick', cb);
  }
  function selectAllHead() {
    headBox = h('input', { type: 'checkbox', 'aria-label': t('Select all shown'), onclick: () => {
      const all = shown();
      if (all.every((x) => sel.has(x))) all.forEach((x) => sel.delete(x)); else all.forEach((x) => sel.add(x));
      syncSel();
    } });
    return h('th.pick', headBox);
  }
  function renderBulk() {
    bulkBox.classList.toggle('hidden', !sel.size);
    if (!sel.size) return;
    const items = [...sel];
    const all = shown();
    const actions = tab === 'memory' ? [
      h('button', { onclick: () => setEnabled(items, true) }, t('Scan')),
      h('button', { onclick: () => setEnabled(items, false) }, t("Don't scan")),
      select([['', t('Move to folder…')], ['__none__', t('(no folder)')], ...folders().map((f) => [f, f])], '', (v) => { if (v) moveTo(items, v === '__none__' ? '' : v); }, { style: { width: 'auto' }, 'aria-label': t('Move to folder…') }),
    ] : [];
    mount(bulkBox, h('div.row.wrap',
      h('b', t('{n} selected', { n: items.length.toLocaleString() })),
      items.length < all.length ? h('button.ghost', { onclick: () => { all.forEach((x) => sel.add(x)); syncSel(); } }, t('Select all {n}', { n: all.length.toLocaleString() })) : null,
      h('span.spacer'), ...actions,
      h('button.danger', { onclick: removeSelected }, icon('trash'), t('Delete')),
      h('button.ghost', { onclick: () => { sel.clear(); syncSel(); } }, t('Clear'))));
  }
  async function setEnabled(items, on) {
    items.forEach((c) => { c.enabled = on; });
    await saveChannels(S.settings.channels);
    render();
  }
  async function moveTo(items, folder) {
    items.forEach((c) => { c.folder = folder; });
    await saveChannels(S.settings.channels);
    render();
  }
  async function deleteAll() {
    const n = tab === 'memory' ? S.settings.channels.length : S.settings.blacklist.length;
    if (!n) return;
    const msg = tab === 'memory' ? t('Delete all {n} channels from the Memory List?', { n }) : t('Remove all {n} entries from the blacklist?', { n });
    if (!(await confirmDialog(tab === 'memory' ? t('Delete all channels') : t('Empty the blacklist'), msg, t('Delete all'), true))) return;
    if (tab === 'memory') await saveChannels([]); else await saveBlacklist([]);
    sel.clear();
    render();
  }
  function selectable(item, tr) {
    rowOf.set(item, tr);
    tr.addEventListener('click', (e) => {
      if (e.target.closest('input, button, label, a')) return;
      const all = shown();
      if (e.shiftKey && all.includes(anchor)) {
        const a = all.indexOf(anchor), b = all.indexOf(item);
        all.slice(Math.min(a, b), Math.max(a, b) + 1).forEach((x) => sel.add(x));
      } else if (e.ctrlKey || e.metaKey) { sel.has(item) ? sel.delete(item) : sel.add(item); anchor = item; }
      else { sel.clear(); sel.add(item); anchor = item; }
      syncSel();
    });
    return tr;
  }

  async function removeSelected() {
    const items = [...sel];
    if (tab === 'memory') {
      const msg = items.length === 1
        ? t('Delete {f} MHz {name}?', { f: fmtChannel(items[0].freq_hz), name: items[0].name })
        : t('Delete {n} selected channels?', { n: items.length });
      if (!(await confirmDialog(t('Delete channel'), msg, t('Delete'), true))) return;
      await saveChannels(S.settings.channels.filter((c) => !sel.has(c)));
    } else await saveBlacklist(S.settings.blacklist.filter((b) => !sel.has(b)));
    sel.clear();
    render();
  }

  const onKey = (e) => {
    if (document.activeElement.matches('textarea, select, input:not([type=checkbox])') || document.querySelector('.modal-back')) return;
    if ((e.key === 'Delete' || e.key === 'Backspace') && sel.size) { e.preventDefault(); removeSelected(); }
    else if (e.key === 'Escape' && sel.size) { sel.clear(); syncSel(); }
    else if ((e.ctrlKey || e.metaKey) && e.key === 'a') { e.preventDefault(); shown().forEach((x) => sel.add(x)); syncSel(); }
  };
  document.addEventListener('keydown', onKey);

  function sortTh(name, key, label, title) {
    const st = sortState[name], active = st.key === key;
    const flip = () => {
      sortState[name] = active ? { key, dir: -st.dir } : { key, dir: SORTS[name].cols[key].dir };
      try { localStorage.setItem(SORTS[name].storage, JSON.stringify(sortState[name])); } catch { /* not remembered, still sorted */ }
      render();
    };
    return h('th', { 'aria-sort': active ? (st.dir === 1 ? 'ascending' : 'descending') : 'none' },
      h('button.sort', { class: active ? 'on' : '', title: title || t('Sort by {col}', { col: label }), onclick: flip },
        label, h('span.arrow', active ? (st.dir === 1 ? '▲' : '▼') : '↕')));
  }

  function render() {
    rowOf.clear();
    boxOf.clear();
    headBox = null;
    bulkBox = h('div.bulk.hidden');
    renderTabs();
    mount(body, tab === 'memory' ? memory() : blacklist());
    sel.forEach((x) => { if (!rowOf.has(x)) sel.delete(x); });  // gone, filtered out or folded away
    syncSel();
  }

  // ---------------------------------------------------------- memory list
  async function editChannel(ch) {
    if (await editChannelDialog(ch)) {
      sel.clear();
      render();
    }
  }

  async function newFolder() {
    const v = await formDialog(t('New folder'), [{ key: 'name', label: t('Folder name') }], t('Create'));
    const name = v && v.name.trim();
    if (!name) return;
    if (folders().includes(name)) { toast(t('Folder “{name}” already exists.', { name }), 'bad'); return; }
    patchConfig({ channel_folders: [...cfg().channel_folders, name] });
    render();
  }

  async function renameFolder(old) {
    const v = await formDialog(t('Rename folder'), [{ key: 'name', label: t('Folder name'), value: old }], t('Rename'));
    const name = v && v.name.trim();
    if (!name || name === old) return;
    if (folders().includes(name)) { toast(t('Folder “{name}” already exists.', { name }), 'bad'); return; }
    S.settings.channels.forEach((c) => { if (c.folder === old) c.folder = name; });
    patchConfig({ channel_folders: cfg().channel_folders.map((f) => (f === old ? name : f)) });
    await saveChannels(S.settings.channels);
    render();
  }

  async function deleteFolder(name) {
    const inside = S.settings.channels.filter((c) => c.folder === name);
    const ok = await confirmDialog(t('Delete folder'), inside.length ? t('Delete “{name}” and its {n} channels?', { name, n: inside.length }) : t('Delete the empty folder “{name}”?', { name }), t('Delete'), true);
    if (!ok) return;
    patchConfig({ channel_folders: cfg().channel_folders.filter((f) => f !== name) });
    await saveChannels(S.settings.channels.filter((c) => c.folder !== name));
    render();
  }

  let transferBusy = false;
  const channelFile = h('input', { type: 'file', accept: '.xml,application/xml,text/xml', class: 'hidden',
    'aria-label': t('Import channels'), onchange: async () => {
      const file = channelFile.files[0];
      channelFile.value = '';
      if (!file || transferBusy) return;
      transferBusy = true;
      try {
        if (file.size > MAX_CHANNEL_FILE_BYTES) throw new Error(t('The channel XML file must be smaller than 5 MB.'));
        const imported = parseChannelsXml(await file.text());
        await loadSettings();
        const baseline = JSON.stringify(S.settings.channels);
        const plan = planChannelImport(S.settings.channels, imported.channels);
        const replacements = new Set();
        const choices = plan.conflicts.map(({ current, incoming }) => select([
          ['keep', t('Keep existing')], ['replace', t('Replace with imported')],
        ], 'keep', value => value === 'replace' ? replacements.add(current.freq_hz) : replacements.delete(current.freq_hz),
        { style: { width: 'auto', maxWidth: '100%' }, 'aria-label': t('Import choice for {f} MHz', { f: fmtChannel(current.freq_hz) }) }));
        const description = channel => [modLabel(channel.modulation), channel.folder || t('(no folder)'),
          channel.enabled ? t('Scan') : t("Don't scan"), `${t('Priority weight')}: ${channel.weight}`,
          channel.ch833 ? t('8.33 channel {c}', { c: fmtChannel(channel.freq_hz) }) : ''].filter(Boolean).join(' · ');
        const summary = h('div.col',
          h('p', t('{added} new, {conflicts} changed, {unchanged} unchanged channels.', {
            added: plan.added.length, conflicts: plan.conflicts.length, unchanged: plan.unchanged })),
          plan.conflicts.length ? h('p.small.muted', t('Choose which existing channels to replace. Names and all other channel settings are kept unless you choose Replace with imported.')) : null,
          ...plan.conflicts.map(({ current, incoming }, i) => h('div.col',
            { style: { borderTop: '1px solid var(--separator)', paddingTop: '12px' } },
            h('div.row.wrap', h('b.mono', `${fmtChannel(current.freq_hz)} MHz`), h('span.spacer'), choices[i]),
            h('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: '12px', overflowWrap: 'anywhere' } },
              ...[[t('Existing'), current], [t('Imported'), incoming]].map(([label, channel]) => h('div.col', { style: { gap: '4px' } },
                h('span.small.muted', label), h('b', channel.name || t('(unnamed)')), h('span.small.muted', description(channel))))))));
        if (!(await modal(t('Import channels'), summary,
          [[t('Cancel'), false, 'ghost'], [t('Import'), true, 'primary']], { dismissible: false }))) return;
        // Recheck after the review dialog so changes made by another client are not discarded.
        await loadSettings();
        if (JSON.stringify(S.settings.channels) !== baseline) throw new Error(t('Channels changed during import. Please import the file again to review the latest channels.'));
        const byFrequency = new Map(S.settings.channels.map(channel => [channel.freq_hz, channel]));
        for (const channel of plan.added) byFrequency.set(channel.freq_hz, channel);
        for (const { incoming } of plan.conflicts) if (replacements.has(incoming.freq_hz)) byFrequency.set(incoming.freq_hz, incoming);
        await api.put('/api/channels', [...byFrequency.values()]);
        const mergedFolders = [...new Set([...cfg().channel_folders, ...imported.folders])];
        await api.post('/api/config', { channel_folders: mergedFolders });
        await loadSettings();
        sel.clear(); render();
        toast(t('Imported {added} new channels and replaced {replaced} existing channels.', { added: plan.added.length, replaced: replacements.size }), 'good');
      } catch (error) { errorToast(error); }
      finally { transferBusy = false; }
    } });

  function exportChannels() {
    const xml = exportChannelsXml(S.settings.channels, cfg().channel_folders);
    const url = URL.createObjectURL(new Blob([xml], { type: 'application/xml;charset=utf-8' }));
    const link = h('a', { href: url, download: 'alisdr-channels.xml' });
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function channelRow(ch) {
    const save = () => saveChannels(S.settings.channels);
    return selectable(ch, h('tr', { ondblclick: (e) => { if (!e.target.closest('input, button, label')) editChannel(ch); } },
      checkbox(ch),
      h('td', h('label.switch', h('input', { type: 'checkbox', checked: ch.enabled, title: t('Scan this channel'), onchange: (e) => { ch.enabled = e.target.checked; save(); } }))),
      h('td.mono', { title: `${fmtMHz(ch.freq_hz, 4)} MHz` }, fmtChannel(ch.freq_hz)),
      h('td', ch.name || h('span.faint', t('(unnamed)'))),
      h('td', h('span.chip', modLabel(ch.modulation))),
      h('td', h('input', { type: 'number', min: 0, max: 20, step: 0.5, value: ch.weight, style: { width: '74px' }, title: t('Priority weight'), onchange: (e) => { ch.weight = +e.target.value || 0; save(); } })),
      h('td', h('div.actions',
        h('button.icon.ghost', { title: t('Edit'), onclick: () => editChannel(ch) }, icon('edit')),
        h('button.icon.ghost', {
          title: t('Delete'), onclick: async () => {
            if (!(await confirmDialog(t('Delete channel'), t('Delete {f} MHz {name}?', { f: fmtChannel(ch.freq_hz), name: ch.name }), t('Delete'), true))) return;
            await saveChannels(S.settings.channels.filter((c) => c !== ch));
            render();
          },
        }, icon('trash'))))));
  }

  function memory() {
    const q = query.trim().toLowerCase();
    const match = (c) => !q || `${fmtChannel(c.freq_hz)} ${fmtMHz(c.freq_hz, 4)} ${c.name} ${c.modulation} ${c.folder}`.toLowerCase().includes(q);
    const channels = sorted('memory', S.settings.channels.filter(match));
    const groups = new Map([['', []], ...folders().map((f) => [f, []])]);
    for (const channel of channels) groups.get(channel.folder || '').push(channel);
    const search = h('input', { type: 'search', placeholder: t('Search frequency, name, folder…'), value: query, oninput: (e) => { query = e.target.value; const pos = e.target.selectionStart; render(); const el = body.querySelector('input[type=search]'); el.focus(); el.setSelectionRange(pos, pos); } });
    const rows = [];
    for (const [folder, list] of groups) {
      if (folder) {
        if (q && !list.length) continue;
        const isCollapsed = collapsed.has(folder) && !q;
        rows.push(h('tr.folder-row', {
          onclick: () => { isCollapsed ? collapsed.delete(folder) : collapsed.add(folder); localStorage.setItem('collapsedFolders', JSON.stringify([...collapsed])); render(); },
        }, h('td', { colSpan: 6 }, h('div.row', icon('folder'), folder, h('span.faint.small', `${list.length}`))),
        h('td', h('div.actions', { onclick: (e) => e.stopPropagation() },
          h('button.icon.ghost', { title: t('Rename folder'), onclick: () => renameFolder(folder) }, icon('edit')),
          h('button.icon.ghost', { title: t('Delete folder'), onclick: () => deleteFolder(folder) }, icon('trash'))))));
        if (isCollapsed) continue;
      }
      rows.push(...list.map(channelRow));
    }
    const enabled = S.settings.channels.filter((c) => c.enabled).length;
    return h('section.card',
      h('div.row.wrap',
        h('div.grow', search),
        h('button.primary', { onclick: () => editChannel(null) }, icon('plus'), t('Add channel')),
        h('button', { onclick: newFolder }, icon('folder'), t('New folder')),
        h('button', { onclick: () => channelFile.click() }, t('Import')),
        h('button', { onclick: exportChannels }, icon('download'), t('Export')),
        S.settings.channels.length ? h('button.ghost.danger', { onclick: deleteAll }, icon('trash'), t('Delete all')) : null),
      channelFile,
      h('div.row.wrap.small.muted',
        t('{n} channels, {e} scanned', { n: S.settings.channels.length, e: enabled }),
        cfg().memory_list_enabled ? null : h('span.chip.warn', t('The Memory List is switched off on the Scan page'))),
      bulkBox,
      S.settings.channels.length
        ? h('div.table-wrap', h('table',
          h('thead', h('tr', selectAllHead(), sortTh('memory', 'on', '✓', t('Sort by scanned / not scanned')), sortTh('memory', 'freq', t('MHz')), sortTh('memory', 'name', t('Name')), sortTh('memory', 'mod', t('Mode')), sortTh('memory', 'weight', t('Weight')), h('th', ''))),
          h('tbody', rows)))
        : h('div.empty', icon('channels'), h('div', t('No saved channels yet. Add one, or save a frequency straight from a recording.'))));
  }

  // ---------------------------------------------------------- blacklist
  let bq = '';
  async function addBlacklist() {
    const v = await formDialog(t('Add to blacklist'), [
      { key: 'freq', label: t('Frequency (MHz)'), type: 'number' },
      { key: 'margin', label: t('Margin ± (kHz)'), type: 'number', value: (cfg().default_blacklist_margin_hz / 1000) },
      { key: 'label', label: t('Description'), value: '' },
    ], t('Add'));
    if (!v || !(v.freq > 0)) return;
    await saveBlacklist([...S.settings.blacklist, { freq_hz: carrierHz(v.freq), margin_hz: Math.round((v.margin || 0) * 1000), label: v.label.trim(), date_added: new Date().toISOString().slice(0, 10) }]);
    render();
  }

  function blacklist() {
    const q = bq.trim().toLowerCase();
    const list = sorted('blacklist', S.settings.blacklist.filter((b) => !q || `${fmtChannel(b.freq_hz)} ${fmtMHz(b.freq_hz, 4)} ${b.label} ${b.date_added}`.toLowerCase().includes(q)));
    const search = h('input', { type: 'search', placeholder: t('Search blacklist…'), value: bq, oninput: (e) => { bq = e.target.value; render(); const el = body.querySelector('input[type=search]'); el.focus(); } });
    return h('section.card',
      h('div.row.wrap', h('div.grow', search), h('button.primary', { onclick: addBlacklist }, icon('plus'), t('Add')),
        S.settings.blacklist.length ? h('button.ghost.danger', { onclick: deleteAll }, icon('trash'), t('Delete all')) : null),
      h('p.small.muted', { style: { margin: 0 } }, t('The scanner never stops on these frequencies. “Skip + blacklist” on the Scan page adds them here.')),
      h('div.setting', h('div', h('b', t('Default blacklist margin')), h('p', t('± kHz around a frequency added with “Skip + blacklist”.'))),
        h('input', { type: 'number', value: cfg().default_blacklist_margin_hz / 1000, min: 0, step: 0.5, style: { width: '110px' }, onchange: (e) => patchConfig({ default_blacklist_margin_hz: Math.round(Math.max(0, +e.target.value) * 1000) }) })),
      bulkBox,
      list.length
        ? h('div.table-wrap', h('table',
          h('thead', h('tr', selectAllHead(), sortTh('blacklist', 'freq', t('MHz')), sortTh('blacklist', 'margin', t('Margin')), sortTh('blacklist', 'label', t('Description')), sortTh('blacklist', 'added', t('Added')), h('th', ''))),
          h('tbody', list.map((b) => selectable(b, h('tr',
            checkbox(b),
            h('td.mono', fmtChannel(b.freq_hz)),
            h('td.mono', `±${(b.margin_hz / 1000).toFixed(1)} kHz`),
            h('td', b.label === 'Auto-lockout (SKIP)' ? t('Skipped from the Scan page') : b.label),
            h('td.mono.small', b.date_added),
            h('td', h('div.actions', h('button.icon.ghost', {
              title: t('Remove from blacklist'), onclick: async () => { await saveBlacklist(S.settings.blacklist.filter((x) => x !== b)); render(); },
            }, icon('trash'))))))))))
        : h('div.empty', t('The blacklist is empty.')));
  }

  render();
  offs.push(on('settings', render));
  loadSettings();  // pick up blacklist entries the scanner added meanwhile
  return { destroy() { offs.forEach((f) => f()); document.removeEventListener('keydown', onKey); } };
}
