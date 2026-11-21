'use strict';
/*
 * /live: seven charts, scrolling, fed by the stream (livechart.js).
 *
 * The order of events is the design:
 *   1. claim a slot and subscribe;
 *   2. once IoT acknowledges the subscription, fetch the seed -- the last five minutes;
 *   3. merge the two by `t`, so the overlap is drawn once.
 * Seeding first would leave a gap between the seed and the first streamed message. If the
 * seed came from CloudFront's two-second cache and predates the subscription, it is fetched
 * once more after the cache has turned over, which closes the last possible hole.
 *
 * The tab hidden stops the stream and frees the slot; visible again starts over, seed and all.
 */
(function () {
  const C = Charts;
  const SRC = window.SIGEN_LIVE || {};
  const SEED = SRC.seed || '/api/live/seed';
  const SESSION = SRC.session || '/api/live/session';
  const WINDOW_S = 300;

  const $ = (id) => document.getElementById(id);
  const clock = new Live.Clock();
  const store = new Live.Store(WINDOW_S);
  const reducedMotion = window.matchMedia &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  let charts = [];
  let tz = [];
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
    } else if (state === 'error' && detail && detail.error) {
      n.textContent = detail.error;
      n.hidden = false;
    } else {
      n.hidden = true;
    }
  }

  function build(series) {
    const root = $('charts');
    C.clear(root);
    charts = series.map((spec) => {
      const value = C.el('span', { class: 'now-val', text: '—' });
      const host = C.el('div', { class: 'chart' });
      const card = C.el('article', { class: 'card live-card', 'data-key': spec.key }, [
        C.el('div', { class: 'card-head' }, [C.el('h2', { text: spec.title }), value]),
        host,
      ]);
      root.appendChild(card);
      const chart = new Live.LiveChart(host, Object.assign({ windowS: WINDOW_S }, spec));
      return { spec, chart, value, host };
    });
    document.body.dataset.charts = String(charts.length);
  }

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

  const stream = new Live.Stream({
    session: SESSION, clock,
    onState: (s, d) => {
      status(s, d);
      // Full: still show the last five minutes, refreshed from the (cached) seed.
      if (s === 'full') seed(null);
    },
    onSubscribed: (at) => seed(at),
    onMessage: (m) => {
      // A duplicate is counted where it is caught, not at the next redraw, so a check of the
      // page (and a curious reader of the DOM) can see de-duplication working between frames.
      if (!store.add(m)) document.body.dataset.dupes = String(store.dupes);
    },
  });

  window.addEventListener('resize', () => {
    for (const c of charts) { c.chart.build(); }
    redraw();
  });

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      stream.stop(true);
    } else {
      stream.start();
      tick();
    }
  });

  stream.start();
  window.SIGEN_LIVE_PAGE = { stream, store, clock, seed, redraw };
  tick();
})();
