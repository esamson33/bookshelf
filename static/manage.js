/* The Manage view: everything the CLI used to do, in the page.
 *
 * Filing a book takes three moves — put PDFs in the landing pad, look their
 * titles up, review what came back — and only the middle one is slow. It runs
 * as a background job whose progress arrives over the event stream; the review
 * that follows reads staged rows out of the catalog, so it survives a reload
 * and an edit made here is what apply() later acts on.
 */
(() => {
  const $ = sel => document.querySelector(sel);
  const esc = s => String(s).replace(/[&<>"]/g,
    c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));

  function el(tag, props = {}, kids = []) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      // `list` and anything hyphenated (aria-*) are attributes only — assigning
      // them as properties silently does nothing.
      if (key === 'list' || key.includes('-')) node.setAttribute(key, value);
      else node[key] = value;
    }
    for (const kid of [].concat(kids)) if (kid) node.append(kid);
    return node;
  }

  let STATUS = null;
  let CATEGORIES = [];
  let WAITING = [];
  let ROWS = [];
  let JOB = null;
  let OFFLINE = false;
  let FLASH = null;
  //: The scan controls are rebuilt on every render, so their values live here
  //: rather than in the DOM — a job event mid-typing would otherwise wipe them.
  const FORM = {publisher: '', category: '', fresh: false};
  //: The rename panel's own controls, kept out of the DOM for the same reason.
  const RENAME = {directory: '', recursive: false, publisher: '', groups: [],
                  picker: null};
  let DEDUPE = null;
  let ARCHIVE = [];              // books taken off the shelf, from /api/archive
  //: Duplicate groups whose tick the user flipped from its default. Groups
  //: start ticked, except those matched on title alone — different files —
  //: which start unticked, so nothing in them goes without a deliberate tick.
  let FLIPPED = new Set();
  let KEEP = new Map();          // duplicate filename -> path of the copy to keep
  let KEEP_STALE = true;

  // What Reset puts everything back to. Held apart from the state itself so
  // the button can also tell whether anything is worth resetting.
  const FORM_DEFAULTS = {publisher: '', category: '', fresh: false};
  const RENAME_DEFAULTS = {directory: '', recursive: false, publisher: ''};

  const dirty = () =>
    Object.keys(FORM_DEFAULTS).some(k => FORM[k] !== FORM_DEFAULTS[k]) ||
    Object.keys(RENAME_DEFAULTS).some(k => RENAME[k] !== RENAME_DEFAULTS[k]) ||
    RENAME.picker !== null || FLIPPED.size > 0 || KEEP.size > 0 || !KEEP_STALE || FLASH !== null;

  // Clears what was typed and ticked on this page, and nothing else. Staged
  // proposals are catalog rows, not form data: they stay, and a rename batch
  // reappears the moment its folder is named again. Discarding those is what
  // the "Discard earlier proposals" box does, on the next scan.
  function resetForms() {
    Object.assign(FORM, FORM_DEFAULTS);
    Object.assign(RENAME, RENAME_DEFAULTS, {groups: [], picker: null});
    FLIPPED = new Set();
    KEEP = new Map();
    KEEP_STALE = true;
    FLASH = null;
    render();
  }

  const pane = $('#manage');
  const toggle = $('#modeToggle');

  // ── server calls ───────────────────────────────────────────────────────
  //
  // Every one returns parsed JSON or throws with the server's own message, so
  // a failure shows the reason rather than a generic "something went wrong".
  async function api(path, options) {
    const response = await fetch(path, options);
    const text = await response.text();
    let body = null;
    try { body = text ? JSON.parse(text) : null; } catch (e) { /* not JSON */ }
    if (!response.ok) throw new Error((body && body.error) || text || response.statusText);
    return body;
  }

  const postJSON = (path, payload) => api(path, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload || {}),
  });

  async function refreshStatus() {
    STATUS = await api('/api/status');
    JOB = STATUS.job;
    paintBadge();
    render();
  }

  async function refreshLanding() {
    WAITING = (await api('/api/landing')).files;
    render();
  }

  async function refreshProposals() {
    ROWS = (await api('/api/shelve/pending')).rows;
    render();
  }

  const refreshCategories = () =>
    api('/api/categories').then(d => { CATEGORIES = d.categories; });

  const refreshDedupe = () =>
    api('/api/dedupe').then(d => { DEDUPE = d; });

  const refreshArchive = () =>
    api('/api/archive').then(d => { ARCHIVE = d.archive; });

  async function refreshRenames() {
    if (!RENAME.directory) { RENAME.groups = []; return; }
    try {
      const payload = await api(
        `/api/rename/pending?directory=${encodeURIComponent(RENAME.directory)}`);
      RENAME.groups = payload.groups;
    } catch (e) { RENAME.groups = []; }
  }

  const refreshAll = () => Promise.all([
    refreshStatus(), refreshLanding(), refreshProposals(),
    refreshCategories(), refreshDedupe(), refreshRenames(), refreshArchive(),
  ]).then(render);

  function fail(e) {
    FLASH = {text: String(e.message || e), bad: true};
    render();
  }

  // ── actions ────────────────────────────────────────────────────────────

  async function upload(files) {
    const pdfs = [...files].filter(f => /\.pdf$/i.test(f.name));
    if (!pdfs.length) return fail(new Error('Only PDF files can be added to the shelf.'));
    let added = 0;
    for (const file of pdfs) {
      try {
        await api(`/api/upload?name=${encodeURIComponent(file.name)}`,
                  {method: 'POST', body: file});
        added++;
      } catch (e) {
        fail(new Error(`${file.name}: ${e.message}`));
      }
    }
    if (added) FLASH = {text: `Added ${added} file${added === 1 ? '' : 's'} to the landing pad.`};
    await Promise.all([refreshStatus(), refreshLanding()]);
  }

  async function scan() {
    FLASH = null;
    try {
      await postJSON('/api/shelve/scan', {
        category: FORM.category, publisher: FORM.publisher, fresh: FORM.fresh,
      });
    } catch (e) { fail(e); }
  }

  async function applyShelve() {
    FLASH = null;
    try {
      const result = await postJSON('/api/shelve/apply');
      FLASH = {text: result.summary + (result.lines.length ? '\n' + result.lines.join('\n') : ''),
               bad: result.errors > 0};
      await Promise.all([refreshStatus(), refreshLanding(), refreshProposals()]);
    } catch (e) { fail(e); }
  }

  // Saved on blur rather than per keystroke: one request per edit, and the
  // input keeps whatever the server accepted.
  async function saveField(input, row, field) {
    const value = input.value.trim();
    const before = field === 'title' ? row.title : row.category;
    if (value === before) return;
    input.classList.add('saving');
    try {
      const updated = await postJSON(`/api/proposal/${row.id}`, {[field]: value});
      Object.assign(row, updated.row);
      input.value = field === 'title' ? row.title : row.category;
      input.classList.remove('saving', 'failed');
    } catch (e) {
      input.classList.remove('saving');
      input.classList.add('failed');
      input.value = before;               // the server refused it; show what stands
      fail(e);
    }
  }

  async function dropRow(row) {
    try {
      await postJSON(`/api/proposal/${row.id}/drop`);
      ROWS = ROWS.filter(r => r.id !== row.id);
      render();
      refreshStatus().catch(() => {});
    } catch (e) { fail(e); }
  }

  async function scanRenames() {
    FLASH = null;
    try {
      await postJSON('/api/rename/scan', {
        directory: RENAME.directory,
        recursive: RENAME.recursive,
        publisher: RENAME.publisher,
      });
    } catch (e) { fail(e); }
  }

  async function applyRenames() {
    FLASH = null;
    try {
      const result = await postJSON('/api/rename/apply', {directory: RENAME.directory});
      FLASH = {text: result.summary + (result.lines.length ? '\n' + result.lines.join('\n') : ''),
               bad: result.errors > 0};
      await refreshRenames();
      render();
    } catch (e) { fail(e); }
  }

  async function browse(path) {
    try {
      RENAME.picker = await api(`/api/browse?path=${encodeURIComponent(path || '')}`);
      render();
    } catch (e) { fail(e); }
  }

  // The copy of a duplicate group the user picked to keep, or the server's
  // default (the earliest filed). A pick that no longer names one of the
  // group's copies — the files changed underneath — falls back to the default.
  function keptPath(group) {
    const picked = KEEP.get(group.filename);
    return group.copies.some(c => c.path === picked) ? picked : group.keep;
  }

  const isIncluded = group => !group.review !== FLIPPED.has(group.filename);

  const megabytes = bytes => bytes == null ? '' : `${(bytes / 1e6).toFixed(1)} MB`;

  const categoryOf = (group, path) =>
    (group.copies.find(c => c.path === path) || {category: path}).category;

  async function applyDedupe() {
    const chosen = DEDUPE.duplicates.filter(isIncluded);
    // Name every file before it goes: the deletion is permanent, and this is
    // the last point at which a wrong pick can be caught.
    const lines = chosen.map(d => {
      const kept = keptPath(d);
      const gone = d.copies.filter(c => c.path !== kept).map(c => `  ✗ ${c.path}`);
      return `${d.filename}\n  ✓ keep ${kept}\n${gone.join('\n')}`;
    });
    if (KEEP_STALE && DEDUPE.stale.length) {
      lines.push(`${DEDUPE.stale.length} catalog row(s) with no file on disk`);
    }
    if (!confirm(`Delete for good?\n\n${lines.join('\n\n')}\n\nThis cannot be undone.`)) return;
    FLASH = null;
    const filenames = chosen.map(d => d.filename);
    // Always name the kept copy, even the default, so what is deleted is
    // exactly what the page showed.
    const keep = Object.fromEntries(chosen.map(d => [d.filename, keptPath(d)]));
    try {
      const result = await postJSON('/api/dedupe/apply',
                                    {filenames, keep, stale: KEEP_STALE});
      FLASH = {text: result.summary + (result.lines.length ? '\n' + result.lines.join('\n') : ''),
               bad: result.errors > 0};
      FLIPPED = new Set();
      KEEP = new Map();
      await Promise.all([refreshDedupe(), refreshStatus()]);
      render();
    } catch (e) { fail(e); }
  }

  async function runSummaries() {
    FLASH = null;
    try { await postJSON('/api/summaries/run'); } catch (e) { fail(e); }
  }

  const stemOf = book => book.filename.replace(/\.pdf$/i, '');

  // Both archive actions answer with the fresh list, so the panel is redrawn
  // from the server's answer rather than from a guess about what it did.
  async function restoreBook(book) {
    FLASH = null;
    try {
      ARCHIVE = (await postJSON(`/api/archive/${book.id}/restore`)).archive;
      FLASH = {text: `Put “${stemOf(book)}” back in ${book.category}.`};
      render();
    } catch (e) { fail(e); }
  }

  async function deleteBook(book) {
    const ok = confirm(`Delete “${stemOf(book)}” for good?\n\n` +
                       `The file and its catalog entry are removed. This cannot be undone.`);
    if (!ok) return;
    FLASH = null;
    try {
      ARCHIVE = (await postJSON(`/api/archive/${book.id}/delete`)).archive;
      FLASH = {text: `Deleted “${stemOf(book)}”.`};
      render();
    } catch (e) { fail(e); }
  }

  // ── rendering ──────────────────────────────────────────────────────────

  function paintBadge() {
    const waiting = STATUS ? STATUS.unclassified + STATUS.pending_shelve : 0;
    $('#modePill').textContent = waiting ? String(waiting) : '';
    toggle.querySelector('.label').textContent =
      document.body.classList.contains('managing') ? 'Library' : 'Manage';
  }

  function jobStrip() {
    if (!JOB) return null;
    const strip = el('div', {className: `job ${JOB.state}`});
    const head = el('div', {className: 'head'});
    head.append(el('span', {className: 'spinner'}));
    head.append(el('span', {textContent: {
      shelve: 'Looking up titles and categories',
      rename: 'Looking up titles',
      summaries: 'Fetching summaries',
    }[JOB.kind] || JOB.kind}));
    head.append(el('span', {className: 'state', textContent:
      JOB.state === 'running' ? 'working…' : JOB.state === 'done' ? 'finished' : 'failed'}));
    strip.append(head);
    const log = JOB.lines.concat(JOB.error ? [JOB.error] : []).join('\n');
    if (log) strip.append(el('pre', {className: 'log', textContent: log}));
    return strip;
  }

  function dropzone() {
    const zone = el('div', {className: 'dropzone', id: 'dropzone'});
    const picker = el('input', {type: 'file', accept: 'application/pdf,.pdf',
                                multiple: true, hidden: true});
    const link = el('button', {className: 'link', type: 'button',
                               textContent: 'choose files'});
    link.addEventListener('click', () => picker.click());
    picker.addEventListener('change', () => {
      upload(picker.files).catch(fail);
      picker.value = '';
    });

    zone.append(el('div', {}, [document.createTextNode('Drop PDFs here, or '), link]));
    const hint = el('div', {className: 'hint'});
    hint.innerHTML = `or copy them into <code>${esc(STATUS.landing_dir)}</code>`;
    zone.append(hint);
    zone.append(picker);

    // dragover must be cancelled or the browser navigates to the dropped file.
    const stop = e => { e.preventDefault(); e.stopPropagation(); };
    zone.addEventListener('dragover', e => { stop(e); zone.classList.add('over'); });
    zone.addEventListener('dragleave', e => { stop(e); zone.classList.remove('over'); });
    zone.addEventListener('drop', e => {
      stop(e);
      zone.classList.remove('over');
      upload(e.dataTransfer.files).catch(fail);
    });
    return zone;
  }

  // The model's category suggestions for one book, best first, each with how
  // well it thinks the category fits. Picking one saves it as the category —
  // the same path as typing it into the field.
  function candidateChips(row, input) {
    const known = new Set(CATEGORIES.map(c => c.toLowerCase()));
    const box = el('div', {className: 'candidates', role: 'group',
                           'aria-label': 'Suggested categories'});
    box.append(el('span', {className: 'label', textContent: 'Suggested'}));

    row.candidates.forEach(c => {
      const scored = typeof c.score === 'number';
      const isNew = !known.has(c.category.toLowerCase());
      const chosen = c.category === row.category;
      const chip = el('button', {type: 'button',
        className: `chip${chosen ? ' chosen' : ''}`,
        title: `${c.category}${isNew ? ' (new category)' : ''}` +
               (scored ? ` — the model rates this ${c.score}% appropriate` : ''),
        'aria-pressed': String(chosen)});
      chip.append(el('span', {className: 'name', textContent: c.category}));
      if (isNew) chip.append(el('span', {className: 'new', textContent: 'new'}));
      if (scored) {
        const meter = el('span', {className: 'meter'});
        meter.append(el('span', {className: 'fill'}));
        meter.firstChild.style.width = `${c.score}%`;
        chip.append(meter, el('span', {className: 'pct', textContent: `${c.score}%`}));
      }
      chip.addEventListener('click', async () => {
        if (chosen) return;
        input.value = c.category;
        await saveField(input, row, 'category');
        box.replaceWith(candidateChips(row, input));
      });
      box.append(chip);
    });
    return box;
  }

  function proposalRow(row) {
    const node = el('div', {className: 'row'});
    node.append(el('div', {className: 'from', textContent: `from ${row.original}`}));

    const title = el('input', {type: 'text', value: row.title,
                               'aria-label': 'Title'});
    title.addEventListener('change', () => saveField(title, row, 'title'));
    node.append(title);

    if (row.category !== null) {
      const category = el('input', {type: 'text', value: row.category,
                                    list: 'categoryList', 'aria-label': 'Category'});
      category.addEventListener('change', async () => {
        await saveField(category, row, 'category');
        const chips = node.querySelector('.candidates');
        if (chips) chips.replaceWith(candidateChips(row, category));
      });
      node.append(category);
      if (row.candidates && row.candidates.length) node.append(candidateChips(row, category));
    } else {
      node.append(el('div'));
    }

    if (row.summary) node.append(el('div', {className: 'summary', textContent: row.summary}));

    const drop = el('button', {className: 'drop', type: 'button', textContent: '×',
                               title: 'Forget this proposal',
                               'aria-label': `Forget the proposal for ${row.original}`});
    drop.addEventListener('click', () => dropRow(row));
    node.append(drop);
    return node;
  }

  function landingPanel() {
    const panel = el('section', {className: 'panel'});
    const heading = el('h2', {}, [document.createTextNode('New books')]);
    if (STATUS.unclassified) {
      heading.append(el('span', {className: 'tag',
        textContent: `${STATUS.unclassified} waiting`}));
    }
    if (window.AiStatus) heading.append(AiStatus.element);
    panel.append(heading);
    panel.append(el('p', {className: 'lede',
      textContent: 'Books land here first, then get a looked-up title and a category before being filed onto the shelf.'}));
    panel.append(dropzone());

    if (WAITING.length) {
      const list = el('ul', {className: 'waiting'});
      WAITING.forEach(name => list.append(el('li', {textContent: name})));
      panel.append(list);
    }

    // controls
    const controls = el('div', {className: 'controls'});
    const bind = (input, key) => {
      input.addEventListener('input', () => {
        FORM[key] = input.type === 'checkbox' ? input.checked : input.value.trim();
      });
      return input;
    };
    controls.append(el('label', {}, [
      el('span', {textContent: 'Publisher hint (optional)'}),
      bind(el('input', {type: 'text', value: FORM.publisher,
                        placeholder: 'Manning, Packt, …'}), 'publisher'),
    ]));
    controls.append(el('label', {}, [
      el('span', {textContent: 'File everything into (optional)'}),
      bind(el('input', {type: 'text', value: FORM.category, list: 'categoryList',
                        placeholder: 'let the model choose'}), 'category'),
    ]));
    controls.append(el('label', {className: 'inline'}, [
      bind(el('input', {type: 'checkbox', checked: FORM.fresh}), 'fresh'),
      el('span', {textContent: 'Discard earlier proposals'})]));

    const busy = JOB && JOB.state === 'running';
    const scanBtn = el('button', {className: 'primary', type: 'button',
                                  textContent: 'Look up titles'});
    scanBtn.disabled = busy || !STATUS.unclassified || !STATUS.lookup.available;
    scanBtn.addEventListener('click', () => scan());
    controls.append(scanBtn);
    panel.append(controls);

    if (!STATUS.lookup.available) {
      panel.append(el('div', {className: 'flash bad', textContent: STATUS.lookup.reason}));
    }

    // review
    if (ROWS.length) {
      panel.append(el('div', {className: 'row-labels'}, [
        el('span', {textContent: 'Title'}), el('span', {textContent: 'Category'})]));
      const rows = el('div', {className: 'rows'});
      ROWS.forEach(row => rows.append(proposalRow(row)));
      panel.append(rows);

      const bar = el('div', {className: 'apply-bar'});
      const applyBtn = el('button', {className: 'primary', type: 'button',
        textContent: `File ${ROWS.length} book${ROWS.length === 1 ? '' : 's'}`});
      applyBtn.disabled = busy;
      applyBtn.addEventListener('click', () => applyShelve());
      bar.append(applyBtn);
      bar.append(el('span', {className: 'note',
        textContent: 'Edits are saved as you make them — you can come back to this later.'}));
      panel.append(bar);
    }
    return panel;
  }

  function renamePanel() {
    const busy = JOB && JOB.state === 'running';
    const panel = el('section', {className: 'panel'});
    panel.append(el('h2', {textContent: 'Rename a folder'}));
    panel.append(el('p', {className: 'lede',
      textContent: 'Give a folder of badly-named PDFs their proper published titles. The files stay where they are.'}));

    const controls = el('div', {className: 'controls'});
    const dirInput = el('input', {type: 'text', value: RENAME.directory,
                                  placeholder: '/Users/you/Downloads',
                                  'aria-label': 'Folder to scan'});
    dirInput.style.minWidth = '320px';
    dirInput.addEventListener('input', () => { RENAME.directory = dirInput.value.trim(); });
    controls.append(el('label', {}, [el('span', {textContent: 'Folder'}), dirInput]));

    const browseBtn = el('button', {className: 'ghost', type: 'button', textContent: 'Browse…'});
    browseBtn.addEventListener('click', () => {
      if (RENAME.picker) { RENAME.picker = null; render(); }
      else browse(RENAME.directory);
    });
    controls.append(browseBtn);

    const pubInput = el('input', {type: 'text', value: RENAME.publisher,
                                  placeholder: 'Manning, Packt, …'});
    pubInput.addEventListener('input', () => { RENAME.publisher = pubInput.value.trim(); });
    controls.append(el('label', {}, [
      el('span', {textContent: 'Publisher hint (optional)'}), pubInput]));

    const recursive = el('input', {type: 'checkbox', checked: RENAME.recursive});
    recursive.addEventListener('input', () => { RENAME.recursive = recursive.checked; });
    controls.append(el('label', {className: 'inline'}, [
      recursive, el('span', {textContent: 'Include subfolders'})]));

    const scanBtn = el('button', {className: 'primary', type: 'button',
                                  textContent: 'Look up titles'});
    scanBtn.disabled = busy || !RENAME.directory || !STATUS.lookup.available;
    scanBtn.addEventListener('click', () => scanRenames());
    controls.append(scanBtn);
    panel.append(controls);

    if (RENAME.picker) panel.append(picker(RENAME.picker));

    const rows = RENAME.groups.reduce((n, g) => n + g.rows.length, 0);
    if (rows) {
      RENAME.groups.forEach(group => {
        if (RENAME.groups.length > 1 || group.directory !== '.') {
          panel.append(el('div', {className: 'group-head', textContent: `${group.directory}/`}));
        }
        const list = el('div', {className: 'rows'});
        group.rows.forEach(row => list.append(proposalRow(row)));
        panel.append(list);
      });
      const bar = el('div', {className: 'apply-bar'});
      const applyBtn = el('button', {className: 'primary', type: 'button',
        textContent: `Rename ${rows} file${rows === 1 ? '' : 's'}`});
      applyBtn.disabled = busy;
      applyBtn.addEventListener('click', () => applyRenames());
      bar.append(applyBtn);
      panel.append(bar);
    }
    return panel;
  }

  function picker(state) {
    const box = el('div', {className: 'picker'});
    box.append(el('div', {className: 'here', textContent: state.path}));
    const list = el('div', {className: 'list'});
    if (state.parent) {
      const up = el('button', {type: 'button', textContent: '↰  ..'});
      up.addEventListener('click', () => browse(state.parent));
      list.append(up);
    }
    state.dirs.forEach(name => {
      const item = el('button', {type: 'button', textContent: `📁  ${name}`});
      item.addEventListener('click', () => browse(`${state.path}/${name}`));
      list.append(item);
    });
    if (!state.dirs.length && !state.parent) {
      list.append(el('div', {className: 'here', textContent: 'No subfolders here.'}));
    }
    box.append(list);

    const foot = el('div', {className: 'foot'});
    foot.append(el('span', {textContent:
      `${state.pdf_count} PDF${state.pdf_count === 1 ? '' : 's'} in this folder`}));
    const use = el('button', {className: 'ghost', type: 'button', textContent: 'Use this folder'});
    use.addEventListener('click', () => {
      RENAME.directory = state.path;
      RENAME.picker = null;
      refreshRenames().then(render);
    });
    foot.append(use);
    box.append(foot);
    return box;
  }

  function dedupePanel() {
    const panel = el('section', {className: 'panel'});
    const heading = el('h2', {}, [document.createTextNode('Duplicates')]);
    const total = DEDUPE ? DEDUPE.file_count : 0;
    if (total) heading.append(el('span', {className: 'tag', textContent: `${total} to remove`}));
    panel.append(heading);
    panel.append(el('p', {className: 'lede',
      textContent: 'The same book filed under more than one category, and catalog rows whose file is gone. ' +
                   'Choose the copy to keep; the others are deleted. The earliest filed copy is chosen to start with.'}));

    if (!DEDUPE || (!DEDUPE.duplicates.length && !DEDUPE.stale.length)) {
      panel.append(el('div', {className: 'lede', textContent: 'Nothing duplicated, nothing stale.'}));
      return panel;
    }

    DEDUPE.duplicates.forEach((group, index) => {
      const node = el('div', {className: 'dupe'});
      const included = isIncluded(group);
      const box = el('input', {type: 'checkbox', checked: included,
                               'aria-label': `Remove duplicates of ${group.filename}`});
      box.addEventListener('change', () => {
        if (FLIPPED.has(group.filename)) FLIPPED.delete(group.filename);
        else FLIPPED.add(group.filename);
        render();
      });
      node.append(box);
      const body = el('div', {className: 'body'});
      // Copies grouped by identical contents can carry different names, so the
      // heading says why they are one book and each copy shows its own name.
      const named = new Set(group.copies.map(c => c.name)).size > 1;
      body.append(el('div', {className: 'name',
        textContent: named ? group.copies[group.copies.length - 1].name.replace(/\.pdf$/i, '')
                           : group.filename}));
      body.append(el('div', {className: 'why', textContent: {
        'name': 'Same filename',
        'content': 'Identical files under different names',
        'name and content': 'Same filename or identical file',
      }[group.match] || (group.review
        ? 'Same title, but different files — maybe another edition or scan. Open each to compare.'
        : 'Same filename')}));

      // One button per copy, each saying what tapping it does — "Keep this
      // copy". The verdict is spelled out below as plain text, so nothing
      // that reads like a status can be tapped and silently mean the opposite.
      const kept = keptPath(group);
      const copies = el('div', {className: 'copies', role: 'group',
                                'aria-label': `Which copy of ${group.filename} to keep`});
      group.copies.forEach((copy, i) => {
        const keeping = copy.path === kept;
        // A card, not one big button: it holds two controls — keep this copy,
        // open it to compare — and a link cannot live inside a button.
        const choice = el('div', {className: `copy${keeping ? ' chosen' : ''}${included ? '' : ' off'}`,
                                  title: copy.path});
        const pick = el('button', {type: 'button', className: 'action', disabled: !included,
          'aria-pressed': String(keeping),
          'aria-label': `Keep the copy in ${copy.category}`,
          textContent: keeping ? '✓ Keeping this copy' : 'Keep this copy'});
        pick.addEventListener('click', () => {
          if (keeping) return;
          KEEP.set(group.filename, copy.path);
          render();
        });
        choice.append(pick);
        choice.append(el('span', {className: 'category', textContent: copy.category}));
        if (named) choice.append(el('span', {className: 'file', textContent: copy.name}));
        const meta = el('span', {className: 'meta', textContent:
          [copy.added ? `filed ${copy.added.slice(0, 10)}` : 'not in the catalog',
           megabytes(copy.size)].filter(Boolean).join(' · ')});
        if (copy.added && i === 0) meta.append(el('span', {className: 'age', textContent: 'oldest'}));
        else if (copy.added && i === group.copies.length - 1) {
          meta.append(el('span', {className: 'age', textContent: 'newest'}));
        }
        choice.append(meta);
        if (copy.id != null) {
          choice.append(el('a', {className: 'open', href: `/pdf/${copy.id}`, target: '_blank',
                                 rel: 'noopener', textContent: 'Open ↗'}));
        }
        copies.append(choice);
      });
      body.append(copies);

      const goners = group.copies.filter(c => c.path !== kept);
      body.append(el('div', {className: 'outcome', textContent: included
        ? `Keeps ${categoryOf(group, kept)} · deletes ${goners.map(c => c.category).join(', ')}` +
          (goners.some(c => c.tracked) ? ' (and its catalog row)' : '')
        : group.review ? 'Left alone until you tick it — these are different files.'
                       : 'Left alone — tick the box to deal with this one.'}));
      body.append(copies);
      node.append(body);
      panel.append(node);
    });

    if (DEDUPE.stale.length) {
      const node = el('div', {className: 'dupe'});
      const box = el('input', {type: 'checkbox', checked: KEEP_STALE,
                               'aria-label': 'Remove stale catalog rows'});
      box.addEventListener('change', () => { KEEP_STALE = box.checked; render(); });
      node.append(box);
      const body = el('div', {className: 'body'});
      body.append(el('div', {className: 'name',
        textContent: `${DEDUPE.stale.length} catalog row(s) with no file on disk`}));
      const paths = el('div', {className: 'paths'});
      DEDUPE.stale.forEach(entry => paths.append(el('div', {textContent: entry.path})));
      body.append(paths);
      node.append(body);
      panel.append(node);
    }

    // Files and catalog rows are removed independently — a duplicate can have
    // no row at all, and a stale row has no file — so the button counts both
    // or it reads "Delete 0 files" while still about to drop rows.
    const chosen = DEDUPE.duplicates.filter(isIncluded);
    const files = chosen.reduce((n, d) => n + d.copies.length - 1, 0);
    const records = chosen.reduce((n, d) =>
                      n + d.copies.filter(c => c.tracked && c.path !== keptPath(d)).length, 0)
                    + (KEEP_STALE ? DEDUPE.stale.length : 0);
    const parts = [];
    if (files) parts.push(`${files} file${files === 1 ? '' : 's'}`);
    if (records) parts.push(`${records} catalog row${records === 1 ? '' : 's'}`);

    const bar = el('div', {className: 'apply-bar'});
    const button = el('button', {className: 'danger', type: 'button',
      textContent: parts.length ? `Delete ${parts.join(' and ')}` : 'Nothing selected'});
    button.disabled = !parts.length;
    button.addEventListener('click', () => applyDedupe());
    bar.append(button);
    bar.append(el('span', {className: 'note', textContent: 'Deleting is permanent.'}));
    panel.append(bar);
    return panel;
  }

  function archivePanel() {
    const panel = el('section', {className: 'panel'});
    const heading = el('h2', {}, [document.createTextNode('Archive')]);
    if (ARCHIVE.length) {
      heading.append(el('span', {className: 'tag',
        textContent: `${ARCHIVE.length} archived`}));
    }
    panel.append(heading);
    const lede = el('p', {className: 'lede'});
    lede.innerHTML = 'Books taken off the shelf from their info card. They sit in ' +
      `<code>${esc(STATUS.shelf_dir)}/__archive/</code> until you put them back or delete them.`;
    panel.append(lede);

    if (!ARCHIVE.length) {
      panel.append(el('div', {className: 'lede', textContent: 'Nothing archived.'}));
      return panel;
    }

    ARCHIVE.forEach(book => {
      const node = el('div', {className: 'archived'});
      const body = el('div', {className: 'body'});
      // The PDF is still served, so the title opens it in a new tab — worth
      // a look before deciding to delete.
      const name = book.missing
        ? el('span', {textContent: stemOf(book)})
        : el('a', {href: `/pdf/${book.id}`, target: '_blank', rel: 'noopener',
                   textContent: stemOf(book)});
      body.append(el('div', {className: 'name'}, [name]));
      const hint = el('div', {className: 'hint'});
      hint.append(document.createTextNode(`from ${book.category}`));
      if (book.missing) {
        hint.append(el('span', {className: 'missing',
          textContent: ' · file missing from __archive/'}));
      }
      body.append(hint);
      node.append(body);

      const restore = el('button', {className: 'ghost', type: 'button',
        textContent: 'Put back', title: `Move it back to ${book.category}`});
      restore.disabled = book.missing;
      restore.addEventListener('click', () => restoreBook(book));
      node.append(restore);

      const remove = el('button', {className: 'danger', type: 'button',
        textContent: 'Delete', title: 'Delete the file and its catalog entry'});
      remove.addEventListener('click', () => deleteBook(book));
      node.append(remove);
      panel.append(node);
    });
    return panel;
  }

  function summariesPanel() {
    const busy = JOB && JOB.state === 'running';
    const missing = STATUS.missing_summaries;
    const panel = el('section', {className: 'panel'});
    const heading = el('h2', {}, [document.createTextNode('Summaries')]);
    if (missing) heading.append(el('span', {className: 'tag', textContent: `${missing} missing`}));
    panel.append(heading);
    panel.append(el('p', {className: 'lede', textContent: missing
      ? `${missing} shelved book${missing === 1 ? ' has' : 's have'} never had a summary looked up.`
      : 'Every shelved book has been through a summary lookup.'}));

    const button = el('button', {className: 'primary', type: 'button',
                                 textContent: 'Fetch missing summaries'});
    button.disabled = busy || !missing || !STATUS.lookup.available;
    button.addEventListener('click', () => runSummaries());
    panel.append(button);
    return panel;
  }

  function toolbar() {
    const bar = el('div', {className: 'toolbar'});
    bar.append(el('span', {className: 'title', textContent: 'Manage'}));
    const reset = el('button', {className: 'ghost', type: 'button',
      textContent: 'Reset',
      title: 'Clear the options, folder and selections on this page. '
           + 'Nothing on the shelf or in the catalog changes.'});
    reset.disabled = !dirty();
    reset.addEventListener('click', resetForms);
    bar.append(reset);
    return bar;
  }

  let deferred = false;

  function render() {
    if (!STATUS) return;
    // A push can land while a title is half-typed; redrawing then would throw
    // the edit away. Wait for the field to be left instead.
    const editing = pane.contains(document.activeElement)
                    && document.activeElement.tagName === 'INPUT';
    if (editing) {
      if (!deferred) {
        deferred = true;
        document.activeElement.addEventListener('blur', () => {
          deferred = false;
          render();
        }, {once: true});
      }
      return;
    }
    const inner = el('div', {className: 'manage-inner'});
    inner.append(toolbar());

    if (OFFLINE) {
      inner.append(el('div', {className: 'offline',
        textContent: 'Lost contact with the server — progress will resume when it returns.'}));
    }
    const strip = jobStrip();
    if (strip) inner.append(strip);
    if (FLASH) {
      inner.append(el('div', {className: 'flash' + (FLASH.bad ? ' bad' : ''),
                              textContent: FLASH.text}));
    }
    inner.append(landingPanel());
    inner.append(renamePanel());
    inner.append(dedupePanel());
    inner.append(archivePanel());
    inner.append(summariesPanel());

    // One datalist for every category input on the page.
    const list = el('datalist', {id: 'categoryList'});
    CATEGORIES.forEach(name => list.append(el('option', {value: name})));
    inner.append(list);

    pane.replaceChildren(inner);
  }

  // ── wiring ─────────────────────────────────────────────────────────────

  toggle.addEventListener('click', () => {
    const managing = document.body.classList.toggle('managing');
    paintBadge();
    if (managing) {
      FLASH = null;
      refreshAll().catch(fail);
    }
  });

  Bus.on('job', event => {
    JOB = event;
    render();
    if (event.state !== 'running') {
      // The scan staged rows; the review reads them back rather than trusting
      // the job's own result, so an edit made meanwhile is never clobbered.
      refreshAll().catch(() => {});
    }
  });
  Bus.on('landing', () => { refreshStatus().catch(() => {}); refreshLanding().catch(() => {}); });
  Bus.on('books', () => {
    refreshStatus().catch(() => {});
    refreshCategories().catch(() => {});
    Promise.all([refreshDedupe(), refreshArchive()]).then(render).catch(() => {});
  });
  Bus.on('connected', () => { OFFLINE = false; render(); });
  Bus.on('disconnected', () => { OFFLINE = true; render(); });

  // The badge has to be right before the view is ever opened.
  refreshStatus().catch(() => {});
  refreshCategories().catch(() => {});
})();
