'use strict';
/*
 * /live: seven charts, scrolling, fed by the stream (livechart.js).
 *
 * The order of events is the design:
 *   1. fetch the seed -- the last five minutes -- straight away, and claim a slot and subscribe
 *      at the same time;
 *   2. once IoT acknowledges the subscription, fetch the seed AGAIN;
 *   3. merge everything by `t`, so the overlap is drawn once.
 * The second seed is what closes the gap between the first seed and the first streamed
 * message; if it came from CloudFront's two-second cache and predates the subscription, it is
 * fetched once more after the cache has turned over.
 *
 * The first seed does NOT wait for the stream. It used to, and a browser that could not open
 * the WebSocket at all -- a content blocker, a VPN, a work network refusing *.amazonaws.com --
 * then drew nothing whatever, behind a pill saying "reconnecting". The data was there the whole
 * time. Now the page always shows the last five minutes, polls the (cached) seed every ten
 * seconds while the stream is not live, and says why the stream is not.
 *
 * The tab hidden stops the stream and frees the slot; visible again starts over, seed and all.
 */
(function () {
  const C = Charts;
  const SRC = window.SIGEN_LIVE || {};
  const SEED = SRC.seed || '/api/live/seed';
  const SESSION = SRC.session || '/api/live/session';
  const WINDOW_S = 300;
  const POLL_MS = 10000;             // the seed, while the stream is not live

  const $ = (id) => document.getElementById(id);
  const clock = new Live.Clock();
  const store = new Live.Store(WINDOW_S);
  const reducedMotion = window.matchMedia &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  let charts = [];
  let tz = [];
  // 'grid' (the default: every chart at a glance) or 'column' (one per row, each as large as
  // the window allows). ?layout= overrides, so a link can ask for either; the choice is kept.
  const LAYOUT_KEY = 'sigen.live.layout';
  let layout = (new URLSearchParams(location.search).get('layout')
                || localStorage.getItem(LAYOUT_KEY) || 'grid') === 'column' ? 'column' : 'grid';
  let lastDraw = 0;
  let drawnVersion = -1;

  function status(state, detail) {
    document.body.dataset.state = state;
    const pill = $('status');
    pill.className = 'pill ' + ({ live: 'ok', connecting: 'warn', reconnecting: 'warn',
                                  full: 'serious', error: 'bad', paused: '' }[state] || '');
    const secs = detail && detail.retryInMs ? Math.round(detail.retryInMs / 1000) : null;
    $('status-text').textContent = ({
      live: 'live', connecting: 'connecting…', paused: 'paused while hidden',
      reconnecting: 'reconnecting' + (secs ? ' in ' + secs + ' s' : ''),
      full: 'full — retrying in ' + secs + ' s',
      error: 'error' + (secs ? ' — retrying in ' + secs + ' s' : ''),
    }[state] || state);
    const n = $('notice');
    if (state === 'full') {
      n.textContent = 'The live view is full: every viewer slot is taken. Showing the last ' +
        'five minutes; it will join the stream when a slot frees up.';
      n.hidden = false;
    } else if (detail && !detail.everLive && detail.failures >= 2) {
      // Never connected, more than once: almost always something between this browser and
      // AWS IoT refusing the WebSocket, not the stream being down. Say which host, so it can be
      // allowed.
      n.textContent = 'The live stream cannot connect from this browser: the WebSocket to ' +
        'AWS IoT (*.iot.ap-southeast-2.amazonaws.com) is being refused or blocked -- often by a ' +
        'content blocker, a VPN or a work network. Showing the last five minutes instead, ' +
        'refreshed every ten seconds.' + (detail.error ? ' (' + detail.error + ')' : '');
      n.hidden = false;
    } else if (state === 'error' && detail && detail.error) {
      n.textContent = detail.error;
      n.hidden = false;
    } else {
      n.hidden = true;
    }
  }

  /**
   * How tall a chart should be. In the grid, a tile. In a column, as large as the window allows:
   * the smaller of "the viewport, less the sticky header and the card's own heading" and the
   * chart's width -- so on a wide screen each chart is one screen tall, and on a phone held
   * upright it is square rather than a strip.
   */
  function heightFor(c) {
    if (layout !== 'column') return 150;
    const top = document.querySelector('.topbar');
    const chrome = (c.card.offsetHeight - c.host.offsetHeight) + (top ? top.offsetHeight : 0) + 28;
    const width = c.host.clientWidth || window.innerWidth;
    return Math.max(180, Math.min(window.innerHeight - chrome, width));
  }

  function build(series) {
    const root = $('charts');
    C.clear(root);
    // Every card first, and only then the charts. A chart takes its width from its host when
    // it is built, and while the grid held only the first card, auto-fit gave that card the
    // whole row: the first chart was built ~1900 px wide and then squeezed into a third of
    // that, with its text shrunk to match. The second was built at half width.
    const made = series.map((spec) => {
      const value = C.el('span', { class: 'now-val', text: '—' });
      const host = C.el('div', { class: 'chart' });
      const card = C.el('article', { class: 'card live-card', 'data-key': spec.key }, [
        C.el('div', { class: 'card-head' }, [C.el('h2', { text: spec.title }), value]),
        host,
      ]);
      root.appendChild(card);
      return { spec, value, host, card };
    });
    charts = made.map((c) => Object.assign(c, {
      chart: new Live.LiveChart(c.host, Object.assign({ windowS: WINDOW_S }, c.spec,
                                                      { height: heightFor(c) })),
    }));
    for (const c of charts) observer.observe(c.host);
    document.body.dataset.charts = String(charts.length);
  }

  // Any chart whose host changed width, or whose height the layout now wants different, is
  // rebuilt -- one pass per frame however many resize events arrive.
  let relayoutQueued = false;
  function relayout() {
    if (relayoutQueued) return;
    relayoutQueued = true;
    requestAnimationFrame(() => {
      relayoutQueued = false;
      let changed = false;
      for (const c of charts) {
        const h = heightFor(c);
        if (c.chart.stale() || h !== c.chart.height) { c.chart.resize(h); changed = true; }
      }
      if (changed) redraw();
    });
  }
  const observer = window.ResizeObserver ? new ResizeObserver(relayout) : { observe() {} };

  function setLayout(mode) {
    layout = mode === 'column' ? 'column' : 'grid';
    localStorage.setItem(LAYOUT_KEY, layout);
    document.body.dataset.layout = layout;
    $('charts').classList.toggle('column', layout === 'column');
    for (const b of document.querySelectorAll('#layout button')) {
      b.setAttribute('aria-pressed', String(b.dataset.layout === layout));
    }
    // Now, not on the next frame: the column changes every card's width at once.
    for (const c of charts) c.chart.resize(heightFor(c));
    if (charts.length) redraw();
  }
  for (const b of document.querySelectorAll('#layout button')) {
    b.addEventListener('click', () => setLayout(b.dataset.layout));
  }

  // Where a sticky chart heading stops: just under the page header, whose height changes when
  // it wraps on a narrow screen. Kept current rather than guessed in CSS.
  const topbar = document.querySelector('.topbar');
  function syncTopbar() {
    if (topbar) document.documentElement.style.setProperty('--topbar-h', topbar.offsetHeight + 'px');
  }
  syncTopbar();
  if (window.ResizeObserver && topbar) new ResizeObserver(syncTopbar).observe(topbar);
  setLayout(layout);

  function redraw() {
    const now = clock.now();
    store.prune(now);
    const latest = store.latest();
    for (const c of charts) {
      c.chart.update(store, now, tz);
      c.value.textContent = latest ? Live.fmtNow(c.spec, latest[c.spec.key]) : '—';
    }
    drawnVersion = store.version;
    lastDraw = now;
    const regions = store.regions(now);
    const b = document.body.dataset;
    b.points = String(store.data.size);
    b.markers = String(store.markers.size);
    b.dupes = String(store.dupes);
    b.lastT = latest ? String(latest.t) : '';
    b.nodata = String(regions.filter((r) => r.pattern === 'hatch-nodata').length);
    b.norecords = String(regions.filter((r) => r.pattern === 'hatch-norecords').length);
    const ci = clock.info();
    b.offset = String(ci.offset);
    note(latest, now, ci);
  }

  function note(latest, now, ci) {
    const parts = [];
    if (latest) {
      const seen = latest._seen || null;
      parts.push('newest point ' + Live.fmtAge(now - latest.t) + ' old');
      // Pi clock vs the server's: how long after the Pi stamped a point it reached this page,
      // in server time. Includes transit, so a clock that is right shows a second or less.
      if (seen) parts.push('Pi→page ' + ((seen - latest.t) / 1000).toFixed(1) + ' s');
    }
    if (ci.rtt !== null) parts.push('clock ±' + Math.round(ci.rtt / 2) + ' ms vs server');
    $('clock-note').textContent = parts.join(' · ');
  }

  // Every animation frame: slide. Every second, or on new data: rebuild.
  function tick() {
    const now = clock.now();
    if (charts.length) {
      if (store.version !== drawnVersion || now - lastDraw > 1000) redraw();
      else if (!reducedMotion) for (const c of charts) c.chart.frame(now);
    }
    if (!document.hidden) {
      if (reducedMotion) setTimeout(tick, 1000);
      else requestAnimationFrame(tick);
    }
  }

  async function seed(subscribedAt) {
    const r = await Live.api(SEED);
    if (r.status === 401 && Live.recoverSession()) return;
    if (!r.body.ok) {
      status('error', { error: 'could not load the last five minutes: ' + r.body.error });
      return;
    }
    tz = r.body.tz || tz;
    if (!charts.length) build(r.body.series || []);
    for (const m of r.body.msgs || []) store.add(m);
    redraw();
    // A seed CloudFront cached before we subscribed can end before the first streamed point.
    if (subscribedAt && r.body.gen < subscribedAt) setTimeout(() => seed(null), 2500);
  }

  // While the stream is not live -- connecting, refused, full, failing -- the seed is the data.
  // CloudFront caches it for two seconds, so a page polling it costs the table nothing extra.
  let pollTimer = null;
  function polling(on) {
    if (on && !pollTimer) pollTimer = setInterval(() => seed(null), POLL_MS);
    if (!on && pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  const stream = new Live.Stream({
    session: SESSION, clock,
    onState: (s, d) => {
      status(s, d);
      polling(s !== 'live' && s !== 'paused');
    },
    onSubscribed: (at) => seed(at),
    onMessage: (m) => {
      // A duplicate is counted where it is caught, not at the next redraw, so a check of the
      // page (and a curious reader of the DOM) can see de-duplication working between frames.
      if (!store.add(m)) document.body.dataset.dupes = String(store.dupes);
    },
  });

  // A window resize changes the column's heights even where no card's width moved.
  window.addEventListener('resize', relayout);

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      stream.stop(true);
    } else {
      seed(null);
      stream.start();
      tick();
    }
  });

  seed(null);                          // first, and whatever the stream does
  stream.start();
  window.SIGEN_LIVE_PAGE = { stream, store, clock, seed, redraw };
  tick();
})();
