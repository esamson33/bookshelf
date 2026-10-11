/* The AI status bar, shown in the New books panel's upper right corner.
 *
 * The Manage view rebuilds its panels on every render, so the bar is built
 * once here and exposed as `AiStatus.element`; manage.js moves that same node
 * into each new panel, which keeps its state and idle timer across renders.
 *
 * The server sends an `ai` event as each OpenAI call is sent ("busy") and
 * when it settles ("ok" or "error"). The bar is gray when idle, animated while
 * a call is in flight, green or red once it settles, and fades back to gray
 * after IDLE_MS with nothing new. A call in flight never times out — lookups
 * routinely take longer than IDLE_MS.
 */
window.AiStatus = (() => {
  const IDLE_MS = 10_000;
  const bar = document.createElement('div');
  bar.setAttribute('role', 'status');

  const TITLES = {
    idle:  () => 'AI: idle',
    busy:  () => 'AI: request in progress…',
    ok:    () => 'AI: last call succeeded',
    error: e  => `AI: last call failed — ${e || 'unknown error'}`,
  };
  // The color each settled state glows in from, before it lands on its own.
  const GLOWS = { ok: '--ok-glow', error: '--danger-glow' };
  const calm = window.matchMedia('(prefers-reduced-motion: reduce)');
  let timer = null;

  function show(state, error) {
    bar.className = `ai-bar ${state}`;
    bar.title = TITLES[state](error);
    bar.setAttribute('aria-label', bar.title);
  }

  // Played through the Web Animations API rather than a CSS animation:
  // manage.js moves the bar into a freshly built panel on every render, and
  // a CSS animation restarts each time its element is reinserted, so the glow
  // would play twice. This one plays once per state change.
  function glow(state) {
    if (calm.matches || !GLOWS[state] || !bar.animate) return;
    const color = getComputedStyle(document.documentElement)
      .getPropertyValue(GLOWS[state]).trim();
    if (!color) return;
    bar.animate([
      { backgroundColor: color, boxShadow: `0 0 10px 2px ${color}` },
      { boxShadow: '0 0 0 0 transparent' },
    ], { duration: 900, easing: 'ease-out' });
  }

  Bus.on('ai', event => {
    if (!TITLES[event.state]) return;
    clearTimeout(timer);
    timer = null;
    show(event.state, event.error);
    glow(event.state);
    if (event.state === 'ok' || event.state === 'error') {
      timer = setTimeout(() => show('idle'), IDLE_MS);
    }
  });

  show('idle');
  return { element: bar };
})();
