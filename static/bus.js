/* One event stream for the whole page, and the pub/sub the views read it through.
 *
 * The server pushes rather than the page polling: a title lookup takes tens of
 * seconds and its progress lines are worth showing as they happen. EventSource
 * reconnects on its own after a drop, so there is no retry logic here — only
 * the connected/disconnected notices, so a view can say when it is out of touch.
 *
 * Event types: `job` (a background job started, progressed, or finished),
 * `books` (the library or the archive changed), `landing` (the __landing/
 * count changed).
 */
window.Bus = (() => {
  const handlers = new Map();          // type -> Set<fn>

  function on(type, fn) {
    if (!handlers.has(type)) handlers.set(type, new Set());
    handlers.get(type).add(fn);
    return () => handlers.get(type).delete(fn);
  }

  function emit(type, data) {
    const set = handlers.get(type);
    if (!set) return;
    // Copy first: a handler may unsubscribe itself while we iterate.
    for (const fn of [...set]) {
      try { fn(data); } catch (e) { console.error('bus handler failed', type, e); }
    }
  }

  const source = new EventSource('/api/events');
  source.onmessage = e => {
    let event;
    try { event = JSON.parse(e.data); } catch (err) { return; }
    if (event && event.type) emit(event.type, event);
  };
  source.onopen  = () => emit('connected', null);
  source.onerror = () => emit('disconnected', null);   // EventSource retries itself

  return { on, emit };
})();
