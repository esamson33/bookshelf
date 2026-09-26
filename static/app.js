(() => {
  let BOOKS = [];
  let RECENT = [];
  let UNCLASSIFIED = 0;
  let activeId = null;
  const $ = sel => document.querySelector(sel);
  const esc = s => s.replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

  // ── persistence (localStorage, resilient to private-mode failures) ─────────
  const KEY_W = 'bookshelf.sidebarWidth';
  const KEY_E = 'bookshelf.expanded';
  const KEY_LAST = 'bookshelf.lastBook';
  const store = {
    get(k)   { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k,v) { try { localStorage.setItem(k, v); } catch (e) {} },
  };
  // category names the user has opened (collapsed by default), restored from storage
  const expanded = new Set((() => {
    try { return JSON.parse(store.get(KEY_E)) || []; } catch (e) { return []; }
  })());
  const saveExpanded = () => store.set(KEY_E, JSON.stringify([...expanded]));

  // `initial` reopens the last book read; a push-driven refresh must not, or
  // the viewer would jump away from whatever the user is reading.
  async function load({initial = false} = {}) {
    const r = await fetch('/api/books');
    const data = await r.json();
    BOOKS = data.books;
    RECENT = data.recent;
    UNCLASSIFIED = data.unclassified || 0;
    $('#count').textContent = `· ${BOOKS.length}`;
    render();
    if (initial) restoreLast();
  }

  // The library used to be fetched once and never again, so a shelve or a
  // dedupe left the page stale until a manual reload.
  const reload = () => load().catch(() => {});
  Bus.on('books', reload);
  Bus.on('landing', reload);

  async function refreshRecent() {
    try {
      const r = await fetch('/api/recent');
      RECENT = (await r.json()).recent;
      if (!$('#q').value.trim()) render();
    } catch (e) { /* ignore */ }
  }

  // Show a book in the viewer + highlight it. Does NOT record an access.
  // Uses the PDF.js viewer, which remembers per-document page/scroll/zoom in
  // localStorage and restores it automatically (no #page hash → don't override).
  function showBook(book) {
    activeId = book.id;
    const frame = $('#pdf');
    frame.src = `/vendor/pdfjs/web/viewer.html?file=${encodeURIComponent('/pdf/' + book.id)}`;
    frame.style.display = 'block';
    $('#placeholder').style.display = 'none';
    // update active highlight without a full re-render flicker
    document.querySelectorAll('.book.active').forEach(el => el.classList.remove('active'));
    document.querySelectorAll(`.book[data-id="${book.id}"]`).forEach(el => el.classList.add('active'));
  }

  // User-initiated open: show it, remember it, and record the access.
  function open(book) {
    showBook(book);
    store.set(KEY_LAST, String(book.id));
    fetch(`/api/open/${book.id}`, { method: 'POST' })
      .then(() => refreshRecent())
      .catch(() => {});
  }

  // Drop a book off the Recent list — clears its access stamp, nothing else.
  function forget(book) {
    fetch(`/api/forget/${book.id}`, { method: 'POST' })
      .then(r => r.json())
      .then(data => {
        if (!data.recent) return;
        RECENT = data.recent;
        if (!$('#q').value.trim()) render();
      })
      .catch(() => {});
  }

  // Take the viewer back to its empty state — the book it was showing is gone.
  function clearViewer() {
    activeId = null;
    const frame = $('#pdf');
    frame.src = 'about:blank';
    frame.style.display = 'none';
    $('#placeholder').style.display = '';
  }

  // Move a book off the shelf into __archive/. Nothing is deleted here: that
  // is a second decision, made from the Manage view's Archive panel.
  async function archive(book, showError) {
    const stem = stemOf(book);
    const ok = confirm(`Archive “${stem}”?\n\nIt leaves the library and goes to ` +
                       `__archive/. You can put it back, or delete it for good, from Manage.`);
    if (!ok) return;
    try {
      const r = await fetch(`/api/archive/${book.id}`, { method: 'POST' });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.error || r.statusText);
    } catch (e) {
      showError(`Could not archive: ${e.message}`);
      return;
    }
    closeModal();
    if (activeId === book.id) clearViewer();
    reload();                                    // the push will say so too
  }

  // Reopen the last book read (restores view only; no access stamp).
  function restoreLast() {
    const id = parseInt(store.get(KEY_LAST), 10);
    if (!Number.isFinite(id)) return;
    const book = BOOKS.find(b => b.id === id);
    if (book) showBook(book);
  }

  const stemOf = book => book.filename.replace(/\.pdf$/i, '');

  // A row = the book button plus the affordances overlaid at its right edge
  // (a button can't nest inside a button, hence the wrapper).
  function bookButton(book, {showCat = false, showForget = false} = {}) {
    const row = document.createElement('div');
    row.className = 'book-row';

    const b = document.createElement('button');
    b.className = 'book' + (book.id === activeId ? ' active' : '');
    b.dataset.id = book.id;
    const stem = stemOf(book);
    let html = `<div class="title">${esc(stem)}</div>`;
    if (showCat) html += `<div class="cat-hint">${esc(book.category)}</div>`;
    else if (book.summary) html += `<div class="summary">${esc(book.summary)}</div>`;
    b.innerHTML = html;
    b.addEventListener('click', () => open(book));

    const btns = document.createElement('div');
    btns.className = 'row-btns';

    const info = document.createElement('button');
    info.className = 'row-btn info-btn';
    info.type = 'button';
    info.textContent = '\u24d8';
    info.title = 'Book info';
    info.setAttribute('aria-label', `Info about ${stem}`);
    info.addEventListener('click', e => { e.stopPropagation(); showInfo(book); });
    btns.appendChild(info);

    if (showForget) {
      row.classList.add('has-forget');
      const x = document.createElement('button');
      x.className = 'row-btn forget-btn';
      x.type = 'button';
      x.textContent = '\u00d7';
      x.title = 'Remove from Recent';
      x.setAttribute('aria-label', `Remove ${stem} from Recent`);
      x.addEventListener('click', e => { e.stopPropagation(); forget(book); });
      btns.appendChild(x);
    }

    row.appendChild(b);
    row.appendChild(btns);
    return row;
  }

  // ── info modal ─────────────────────────────────────────────────────────────
  const covers = new Map();   // book id → {url, pages} for page 1, rendered once
  let pdfLibPromise = null;
  let lastFocus = null;

  function pdfLib() {
    if (!pdfLibPromise) {
      pdfLibPromise = import('/vendor/pdfjs/build/pdf.mjs').then(lib => {
        lib.GlobalWorkerOptions.workerSrc = '/vendor/pdfjs/build/pdf.worker.mjs';
        return lib;
      });
    }
    return pdfLibPromise;
  }

  // Rasterise page 1 as the cover. Cached per book — a shelf's covers never change.
  async function coverFor(id) {
    if (covers.has(id)) return covers.get(id);
    const lib = await pdfLib();
    // disableAutoFetch: only pull the byte ranges page 1 needs, not the whole book.
    const doc = await lib.getDocument({ url: `/pdf/${id}`, disableAutoFetch: true }).promise;
    try {
      const page = await doc.getPage(1);
      const unit = page.getViewport({ scale: 1 });
      const viewport = page.getViewport({ scale: Math.min(1000 / unit.width, 3) });
      const canvas = document.createElement('canvas');
      canvas.width = Math.ceil(viewport.width);
      canvas.height = Math.ceil(viewport.height);
      await page.render({ canvasContext: canvas.getContext('2d'), viewport }).promise;
      const out = { url: canvas.toDataURL('image/png'), pages: doc.numPages };
      covers.set(id, out);
      return out;
    } finally {
      doc.destroy();
    }
  }

  const fmtSize = n => n == null ? null
    : n >= 1 << 20 ? `${(n / (1 << 20)).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`;
  const fmtDate = s => {
    const d = s ? new Date(s.replace(' ', 'T')) : null;
    return d && !isNaN(d) ? d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }) : null;
  };

  function closeModal() {
    const back = $('#backdrop');
    if (!back) return;
    back.remove();
    document.removeEventListener('keydown', onModalKey);
    if (lastFocus) lastFocus.focus();
    lastFocus = null;
  }
  const onModalKey = e => { if (e.key === 'Escape') closeModal(); };

  function showInfo(book) {
    closeModal();
    lastFocus = document.activeElement;

    const back = document.createElement('div');
    back.className = 'backdrop';
    back.id = 'backdrop';
    back.innerHTML =
      '<div class="modal" role="dialog" aria-modal="true" aria-label="Book info">' +
        '<div class="cover"><div class="cover-msg">Rendering cover…</div></div>' +
        '<div class="details">' +
          `<h2>${esc(stemOf(book))}</h2>` +
          '<div class="meta"></div>' +
          '<div class="modal-summary"></div>' +
          '<div class="modal-error" role="alert"></div>' +
          '<div class="actions">' +
            '<button type="button" class="read">Read this book</button>' +
            '<button type="button" class="archive">Archive</button>' +
          '</div>' +
        '</div>' +
        '<button type="button" class="close" title="Close" aria-label="Close">\u00d7</button>' +
      '</div>';
    back.addEventListener('click', e => { if (e.target === back) closeModal(); });
    back.querySelector('.close').addEventListener('click', closeModal);
    back.querySelector('.read').addEventListener('click', () => { closeModal(); open(book); });
    back.querySelector('.archive').addEventListener('click', () =>
      archive(book, msg => { back.querySelector('.modal-error').textContent = msg; }));
    document.body.appendChild(back);
    document.addEventListener('keydown', onModalKey);
    back.querySelector('.close').focus();

    const alive = () => document.getElementById('backdrop') === back;
    const summary = back.querySelector('.modal-summary');
    if (book.summary) summary.textContent = book.summary;
    else { summary.textContent = 'No summary recorded for this book.'; summary.classList.add('none'); }

    // Named slots so the two async fills below can't duplicate or reorder facts.
    const meta = back.querySelector('.meta');
    const facts = { category: book.category, pages: null, size: null, last: null, from: null };
    const paint = () => {
      meta.replaceChildren(...['category', 'pages', 'size', 'last', 'from']
        .map(k => facts[k]).filter(Boolean)
        .map(t => Object.assign(document.createElement('span'), { textContent: t })));
    };
    paint();

    // Metadata and cover load independently — neither blocks the other.
    fetch(`/api/book/${book.id}`).then(r => r.json()).then(info => {
      if (!alive() || info.error) return;
      if (info.summary && !book.summary) { summary.textContent = info.summary; summary.classList.remove('none'); }
      const last = fmtDate(info.accessed_at);
      facts.size = fmtSize(info.size);
      facts.last = last ? `Last read ${last}` : 'Never opened';
      if (info.original_name !== info.filename) facts.from = `Filed from “${info.original_name}”`;
      paint();
    }).catch(() => {});

    coverFor(book.id).then(({ url, pages }) => {
      if (!alive()) return;
      const img = document.createElement('img');
      img.src = url;
      img.alt = `Cover of ${stemOf(book)}`;
      back.querySelector('.cover').replaceChildren(img);
      facts.pages = `${pages} page${pages === 1 ? '' : 's'}`;
      paint();
    }).catch(() => {
      if (!alive()) return;
      back.querySelector('.cover').replaceChildren(
        Object.assign(document.createElement('div'),
          { className: 'cover-msg', textContent: 'Cover unavailable — the PDF could not be rendered.' }));
    });
  }

  function render() {
    const q = $('#q').value.trim().toLowerCase();
    $('#clear').style.display = q ? 'block' : 'none';
    const side = $('#sidebar');
    side.innerHTML = '';

    if (UNCLASSIFIED > 0) {
      const n = document.createElement('div');
      n.className = 'notice';
      const plural = UNCLASSIFIED === 1 ? 'file' : 'files';
      n.innerHTML = `<b>${UNCLASSIFIED}</b> unclassified ${plural} waiting in ` +
                    `<code>__landing/</code>. Open <b>Manage</b> to file ${UNCLASSIFIED === 1 ? 'it' : 'them'}.`;
      side.appendChild(n);
    }

    if (q) {
      const hits = BOOKS.filter(b =>
        b.filename.toLowerCase().includes(q) || b.summary.toLowerCase().includes(q));
      const t = document.createElement('div');
      t.className = 'section-title';
      t.textContent = `Results · ${hits.length}`;
      side.appendChild(t);
      if (!hits.length) {
        side.insertAdjacentHTML('beforeend', `<div class="empty">No matches for “${esc(q)}”.</div>`);
        return;
      }
      hits.forEach(b => side.appendChild(bookButton(b, {showCat: true})));
      return;
    }

    // Recent
    if (RECENT.length) {
      const t = document.createElement('div');
      t.className = 'section-title';
      t.textContent = 'Recent';
      side.appendChild(t);
      RECENT.forEach(rb => {
        const full = BOOKS.find(b => b.id === rb.id) || rb;
        side.appendChild(bookButton({...full, summary: full.summary || ''},
                                    {showCat: true, showForget: true}));
      });
    }

    // Categories
    const cats = {};
    for (const b of BOOKS) (cats[b.category] ||= []).push(b);
    const names = Object.keys(cats).sort((a, b) => a.localeCompare(b));

    const allTitle = document.createElement('div');
    allTitle.className = 'section-title';
    allTitle.innerHTML = '<span>Categories</span>' +
      '<span class="actions">' +
        '<button type="button" id="expandAll" title="Expand all">\u25bc</button>' +
        '<button type="button" id="collapseAll" title="Collapse all">\u25b2</button>' +
      '</span>';
    // toggle every category in place — avoids a re-render (and the scroll jump)
    const setAll = open => {
      if (open) names.forEach(n => expanded.add(n)); else expanded.clear();
      saveExpanded();
      side.querySelectorAll('.cat-head').forEach(h => h.classList.toggle('collapsed', !open));
      side.querySelectorAll('.cat-body').forEach(b => b.classList.toggle('hidden', !open));
    };
    allTitle.querySelector('#expandAll').addEventListener('click', () => setAll(true));
    allTitle.querySelector('#collapseAll').addEventListener('click', () => setAll(false));
    side.appendChild(allTitle);

    for (const name of names) {
      const wrap = document.createElement('div');
      wrap.className = 'cat';
      const isOpen = expanded.has(name);
      const head = document.createElement('button');
      head.className = 'cat-head' + (isOpen ? '' : ' collapsed');
      head.innerHTML = `<span class="chev">▼</span><span>${esc(name)}</span>` +
                       `<span class="badge">${cats[name].length}</span>`;
      const body = document.createElement('div');
      body.className = 'cat-body' + (isOpen ? '' : ' hidden');
      cats[name].forEach(b => body.appendChild(bookButton(b)));
      head.addEventListener('click', () => {
        if (expanded.has(name)) expanded.delete(name); else expanded.add(name);
        saveExpanded();
        head.classList.toggle('collapsed');
        body.classList.toggle('hidden');
      });
      wrap.appendChild(head);
      wrap.appendChild(body);
      side.appendChild(wrap);
    }
  }

  $('#q').addEventListener('input', render);
  $('#clear').addEventListener('click', () => { $('#q').value = ''; render(); $('#q').focus(); });

  // ── resizable sidebar (draggable splitter, max 20% of screen) ──────────────
  const sidebar = $('#sidebar');
  const minW = () => Math.round(window.innerWidth * 0.15);
  const maxW = () => Math.round(window.innerWidth * 0.50);
  function setWidth(px, persist) {
    px = Math.min(Math.max(px, minW()), maxW());
    sidebar.style.width = px + 'px';
    if (persist) store.set(KEY_W, String(px));
  }
  const savedW = parseInt(store.get(KEY_W), 10);
  setWidth(Number.isFinite(savedW) ? savedW : maxW());   // restore, else 20% cap
  window.addEventListener('resize', () => setWidth(sidebar.getBoundingClientRect().width, true));

  const splitter = $('#splitter');
  splitter.addEventListener('mousedown', e => {
    e.preventDefault();
    document.body.classList.add('resizing');
    splitter.classList.add('dragging');
    const left = sidebar.getBoundingClientRect().left;
    const onMove = ev => setWidth(ev.clientX - left);
    const onUp = () => {
      setWidth(sidebar.getBoundingClientRect().width, true);  // persist final width
      document.body.classList.remove('resizing');
      splitter.classList.remove('dragging');
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  });

  load({initial: true}).catch(e => { $('#sidebar').innerHTML = `<div class="empty">Failed to load: ${esc(String(e))}</div>`; });
})();
