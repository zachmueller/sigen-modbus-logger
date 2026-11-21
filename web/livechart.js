'use strict';
/*
 * The live stream in the browser: the clock, the data, the connection and the chart.
 * No build step and no framework, like the rest of web/. Needs charts.js (for the palette,
 * the hatches, the margins and the gap rule, so a live chart looks like an archive chart) and
 * vendor/mqtt.min.js (the MQTT client; see THIRD_PARTY_NOTICES.md).
 *
 * Used by live.js (the public live view) and devices.js (the device tracker).
 *
 * Where data comes from:
 *
 *   POST <session>  claim one of ten viewer slots -> a presigned wss:// URL for IoT Core
 *   MQTT subscribe  one message per two-second tick, straight from the Pi via IoT
 *   GET  <seed>     the last five minutes, fetched AFTER the subscription is acknowledged,
 *                   so nothing can fall between the seed and the first streamed message
 *
 * Every message carries `t`, the archive record's own timestamp in ms. That is the identity a
 * point is de-duplicated on: the seed and the stream overlap by design, and the same point
 * arriving twice is drawn once.
 */
const Live = (function () {
  const C = Charts;

  // ------------------------------------------------------------------- clock

  /**
   * Server time, estimated NTP-style from the session API's round trips.
   *
   * Each sample is four timestamps: t0 (this device sent), recv (API Gateway received), send
   * (the Lambda answered), t3 (this device received). The offset is the mean of the two legs and
   * the uncertainty is the round trip minus the server's own processing -- so a cold start's
   * second of init is not mistaken for clock offset. The sample with the smallest round trip is
   * the one believed, which is what NTP does and why it is robust to a slow mobile network.
   */
  class Clock {
    constructor() { this.samples = []; this.best = null; this.base = this.wallMinusMono(); }

    wallMinusMono() { return Date.now() - performance.now(); }

    add(t0, recv, send, t3) {
      if (![t0, recv, send, t3].every(Number.isFinite)) return;
      const rtt = Math.max(0, (t3 - t0) - (send - recv));
      const offset = ((recv - t0) + (send - t3)) / 2;
      this.samples.push({ rtt, offset, at: Date.now() });
      if (this.samples.length > 8) this.samples.shift();
      this.best = this.samples.reduce((a, b) => (b.rtt < a.rtt ? b : a));
      this.base = this.wallMinusMono();
    }

    /** ms since the epoch, server-aligned when there is a sample to align with. */
    now() { return Date.now() + (this.best ? this.best.offset : 0); }

    info() {
      return this.best
        ? { offset: Math.round(this.best.offset), rtt: Math.round(this.best.rtt),
            age: Date.now() - this.best.at, samples: this.samples.length }
        : { offset: 0, rtt: null, age: null, samples: 0 };
    }

    /** True if the device's wall clock has been stepped since the last sample (NTP, the user). */
    stepped() { return Math.abs(this.wallMinusMono() - this.base) > 500; }
  }

  // --------------------------------------------------------------------- api

  /**
   * JSON in, {status, body} out. Never throws for an HTTP status -- the caller branches on it.
   * `credentials: 'same-origin'` so the session cookie travels once a page is gated.
   */
  async function api(path, body) {
    const opts = { credentials: 'same-origin' };
    if (body !== undefined) {
      opts.method = 'POST';
      opts.headers = { 'Content-Type': 'application/json' };
      opts.body = JSON.stringify(body);
      opts.cache = 'no-store';
    }
    let res;
    try {
      res = await fetch(path, opts);
    } catch (e) {
      return { status: 0, body: { ok: false, error: 'cannot reach the server: ' + e.message } };
    }
    let out = null;
    try { out = await res.json(); } catch (e) { out = null; }
    if (!out || typeof out !== 'object') {
      out = { ok: false, error: res.status + ' ' + res.statusText + ' (not JSON)' };
    }
    return { status: res.status, body: out };
  }

  /**
   * A 401 from the edge gate means the session expired. Reload once -- the gate's redirect to
   * /auth/refresh is what renews it -- but not twice in a minute, or an account that cannot sign
   * in would reload forever. The same guard the archive viewer uses (app.js).
   */
  function recoverSession() {
    const KEY = 'sigen.session.reloaded';
    const last = Number(sessionStorage.getItem(KEY) || 0);
    if (Date.now() - last < 60000) return false;
    sessionStorage.setItem(KEY, String(Date.now()));
    location.reload();
    return true;
  }

  // ------------------------------------------------------------------- store

  /**
   * Every message in the window, by `t`. Data messages and markers (m == 0) are kept apart:
   * markers draw nothing themselves; they are evidence about the gaps between data.
   */
  class Store {
    constructor(windowS) {
      this.windowMs = windowS * 1000;
      this.data = new Map();
      this.markers = new Map();
      this.dupes = 0;
      this.version = 0;
    }

    add(m) {
      if (!m || !Number.isFinite(m.t)) return false;
      const into = m.m === 0 ? this.markers : this.data;
      if (into.has(m.t)) { this.dupes++; return false; }
      into.set(m.t, m);
      this.version++;
      return true;
    }

    prune(nowMs) {
      const cut = nowMs - this.windowMs - 30000;
      for (const map of [this.data, this.markers]) {
        for (const t of map.keys()) if (t < cut) map.delete(t);
      }
    }

    sorted(map) { return Array.from(map.values()).sort((a, b) => a.t - b.t); }

    /** [[t, value], ...] for one metric, oldest first. */
    series(key) {
      const out = [];
      for (const m of this.sorted(this.data)) {
        if (typeof m[key] === 'number') out.push([m.t, m[key]]);
      }
      return out;
    }

    latest() {
      let best = null;
      for (const m of this.data.values()) if (!best || m.t > best.t) best = m;
      return best;
    }

    cadenceMs() {
      const last = this.latest();
      return ((last && last.p) || 2) * 1000;
    }

    /**
     * Hatched spans, in the archive viewer's two kinds.
     *
     *   orange "device not answering"  -- the logger was running and probing: the gap holds
     *                                     at least one marker, and markers kept arriving
     *   grey   "no live data"          -- nothing at all reached the cloud: no marker, or a
     *                                     stretch longer than a marker's own quiet bound q
     *
     * Only gaps between consecutive DATA messages wider than the line-break limit are
     * considered, so a hatch always sits exactly where the line is broken. The trailing gap
     * (last data -> now) is classified the same way, against now.
     */
    regions(nowMs) {
      const limit = C.gapLimitS(this.cadenceMs() / 1000) * 1000;
      const data = this.sorted(this.data);
      const marks = this.sorted(this.markers);
      const out = [];
      const edges = [];
      for (let i = 1; i < data.length; i++) edges.push([data[i - 1].t, data[i].t]);
      const lastT = data.length ? data[data.length - 1].t : nowMs - this.windowMs;
      edges.push([lastT, nowMs]);
      for (const [a, b] of edges) {
        if (b - a <= limit) continue;
        const inside = marks.filter((m) => m.t > a && m.t < b);
        if (!inside.length) {
          out.push({ from: a, to: b, pattern: 'hatch-norecords', label: 'no live data' });
          continue;
        }
        const q = Math.max(...inside.map((m) => m.q || 42)) * 1000 + 3000;
        const points = [a, ...inside.map((m) => m.t), b];
        for (let i = 1; i < points.length; i++) {
          const x = points[i - 1], y = points[i];
          const kind = y - x > q
            ? { pattern: 'hatch-norecords', label: 'no live data' }
            : { pattern: 'hatch-nodata', label: 'device not answering' };
          const prev = out[out.length - 1];
          if (prev && prev.pattern === kind.pattern && prev.to === x) prev.to = y;
          else out.push({ from: x, to: y, pattern: kind.pattern, label: kind.label });
        }
      }
      return out;
    }
  }

  // ------------------------------------------------------------------ stream

  /**
   * One viewer slot and one MQTT subscription, and everything needed to keep them.
   *
   * Reconnection is ours, not mqtt.js's (`reconnectPeriod: 0`): every connection needs a fresh
   * presigned URL, which mqtt.js cannot get for itself. The slot and its lease are kept in
   * sessionStorage, so a reload reclaims the same slot instead of taking a second one while the
   * first waits out its lease.
   */
  class Stream {
    constructor(opts) {
      this.session = opts.session;
      this.clock = opts.clock;
      this.onMessage = opts.onMessage || (() => {});
      this.onState = opts.onState || (() => {});
      this.onSubscribed = opts.onSubscribed || (() => {});
      this.storeKey = 'sigen.live.lease:' + this.session;
      this.client = null;
      this.timers = [];
      this.running = false;
      this.backoff = 2000;
      this.state = 'idle';
      this.lastMessageAt = null;
      this.subscribedAt = null;
      // Attempts since the last acknowledged subscription, and whether there has ever been one.
      // A page that has NEVER connected is in a different situation from one that dropped: the
      // first is usually something between this browser and IoT refusing the WebSocket (a
      // content blocker, a VPN, a work network), and the page says so rather than spinning.
      this.failures = 0;
      this.everLive = false;
    }

    set(state, detail) {
      this.state = state;
      this.onState(state, detail || {});
    }

    held() {
      try { return JSON.parse(sessionStorage.getItem(this.storeKey) || 'null'); }
      catch (e) { return null; }
    }

    keep(slot, lease) {
      sessionStorage.setItem(this.storeKey, JSON.stringify({ slot, lease }));
    }

    after(ms, fn) {
      const id = setTimeout(() => { this.timers = this.timers.filter((x) => x !== id); fn(); }, ms);
      this.timers.push(id);
    }

    clearTimers() { for (const id of this.timers) clearTimeout(id); this.timers = []; }

    async start() {
      if (this.running) return;
      this.running = true;
      await this.connect();
    }

    retry(state, ms, detail) {
      if (!this.running) return;
      this.set(state, Object.assign({ retryInMs: ms, failures: this.failures,
                                      everLive: this.everLive }, detail || {}));
      this.after(ms, () => this.connect());
    }

    async connect() {
      if (!this.running) return;
      this.set('connecting');
      const prior = this.held() || {};
      const t0 = Date.now();
      const r = await api(this.session, { op: 'connect', slot: prior.slot, lease: prior.lease });
      const t3 = Date.now();
      if (!this.running) return;
      if (r.status === 401 && recoverSession()) return;
      if (r.status === 429 && r.body.full) {
        return this.retry('full', Math.min(60, r.body.retry_s || 30) * 1000);
      }
      if (!r.body.ok) {
        this.failures++;
        const ms = this.backoff;
        this.backoff = Math.min(60000, this.backoff * 2);
        return this.retry('error', ms, { error: r.body.error, status: r.status });
      }
      const s = r.body;
      this.clock.add(t0, s.recv, s.send, t3);
      this.slot = s.slot;
      this.lease = s.lease;
      this.keep(s.slot, s.lease);
      this.open(s);
    }

    open(s) {
      this.closeClient();
      const url = s.url;
      // transformWsUrl hands mqtt.js the presigned URL verbatim. Left to build the URL itself
      // from host and path, a client is one refactor away from dropping the query string --
      // which is the signature -- and the handshake fails with no reason given.
      const client = mqtt.connect(url, {
        clientId: s.clientId, protocolVersion: 4, clean: true, keepalive: 30,
        reconnectPeriod: 0, connectTimeout: 15000,
        transformWsUrl: () => url,
      });
      this.client = client;
      let subscribed = false;
      client.on('connect', () => {
        client.subscribe(s.topic, { qos: 0 }, (err, granted) => {
          // A SUBACK of 128 is IoT saying "not authorised" -- a subscription refused while the
          // connection stays up, which would otherwise read as a quiet stream.
          if (err || !granted || !granted.length || granted[0].qos === 128) {
            this.set('error', { error: 'subscription refused' });
            client.end(true);
            return;
          }
          this.backoff = 2000;
          subscribed = true;
          this.failures = 0;
          this.everLive = true;
          this.subscribedAt = this.clock.now();
          this.set('live', { slot: s.slot });
          this.onSubscribed(this.subscribedAt);
          this.scheduleRenew(s.renewS || 60);
        });
      });
      client.on('message', (_topic, payload) => {
        let m;
        try { m = JSON.parse(payload.toString()); } catch (e) { return; }
        this.lastMessageAt = this.clock.now();
        m._seen = this.lastMessageAt;
        this.onMessage(m);
      });
      client.on('close', () => {
        if (this.client !== client || !this.running) return;
        // Dropped -- by the network, by IoT, or by another tab taking this slot's client id --
        // or never opened at all. Reconnect asks for a fresh URL and, holding the lease, the
        // same slot.
        if (!subscribed) this.failures++;
        const ms = this.backoff;
        this.backoff = Math.min(60000, this.backoff * 2);
        this.client = null;
        this.retry('reconnecting', ms);
      });
      client.on('error', () => { /* 'close' follows; that is where recovery lives */ });
    }

    scheduleRenew(renewS) {
      this.after(renewS * 1000, async () => {
        if (!this.running) return;
        const r = await api(this.session, { op: 'renew', slot: this.slot, lease: this.lease });
        if (!this.running) return;
        if (r.status === 401 && recoverSession()) return;
        if (r.body.ok) return this.scheduleRenew(r.body.renewS || renewS);
        if (r.body.lost) {
          // The lease lapsed (a laptop slept, say). Start over: a new connect claims a slot.
          sessionStorage.removeItem(this.storeKey);
          this.closeClient();
          return this.connect();
        }
        this.scheduleRenew(Math.min(renewS, 20));   // transient; try again sooner
      });
    }

    closeClient() {
      const c = this.client;
      this.client = null;
      if (c) { try { c.end(true); } catch (e) { /* already closed */ } }
    }

    /** Stop streaming. `release` frees the slot now rather than when its lease runs out. */
    stop(release) {
      this.running = false;
      this.clearTimers();
      this.closeClient();
      if (release && this.lease !== undefined) {
        const body = JSON.stringify({ op: 'release', slot: this.slot, lease: this.lease });
        // sendBeacon survives the page being hidden or unloaded, which fetch does not.
        const sent = navigator.sendBeacon &&
          navigator.sendBeacon(this.session, new Blob([body], { type: 'application/json' }));
        if (!sent) api(this.session, JSON.parse(body));
        sessionStorage.removeItem(this.storeKey);
        this.lease = undefined;
      }
      this.set('paused');
    }
  }

  // -------------------------------------------------------------------- chart

  const TICK_MS = 60000;

  /**
   * One metric, scrolling. Built once; per frame only two transforms change.
   *
   * Points are placed in TIME coordinates inside a group that is translated so that `now` sits
   * on the right edge -- so the animation is one attribute write per frame, and the path is
   * rebuilt only when data arrives or the y-scale changes. The data is choppy (a point every two
   * seconds); the axis moves smoothly, which is the point.
   */
  class LiveChart {
    constructor(host, spec) {
      this.host = host;
      this.spec = spec;            // {key, title, unit, dec, slot, zero, domain, min_span, sign}
      this.windowMs = (spec.windowS || 300) * 1000;
      this.height = spec.height || 150;
      this.lo = null;
      this.hi = null;
      this.events = [];
      this.build();
    }

    build() {
      const M = C.margins;
      this.width = Math.max(320, this.host.clientWidth || 640);
      this.pw = this.width - M.l - M.r;
      this.ph = this.height - M.t - M.b;
      // About one y gridline per 45 px, so a chart drawn a screen tall gets a finer scale
      // than a 150 px tile rather than four lines stretched across it.
      this.yTicks = Math.max(3, Math.min(10, Math.round(this.ph / 45)));
      this.pxPerMs = this.pw / this.windowMs;
      C.clear(this.host);
      const id = 'lc-' + this.spec.key + '-' + Math.random().toString(36).slice(2, 8);
      const defs = C.hatchDefs();
      defs.appendChild(C.svg('clipPath', { id: id + '-plot' }, [
        C.svg('rect', { x: M.l, y: 0, width: this.pw, height: this.height })]));
      this.yAxis = C.svg('g', { class: 'y-axis' });
      this.xScroll = C.svg('g', { class: 'x-scroll' });
      this.regionG = C.svg('g', { class: 'regions' });
      this.eventG = C.svg('g', { class: 'events' });
      this.lineG = C.svg('g', { class: 'line' });
      this.dataScroll = C.svg('g', { class: 'data-scroll' },
                              [this.regionG, this.eventG, this.lineG]);
      this.svgEl = C.svg('svg', {
        viewBox: `0 0 ${this.width} ${this.height}`, class: 'chart-svg',
        role: 'img', 'aria-label': this.spec.title + ', last five minutes',
        preserveAspectRatio: 'none',
      }, [
        defs, this.yAxis,
        C.svg('g', { 'clip-path': `url(#${id}-plot)` }, [this.xScroll, this.dataScroll]),
        C.svg('line', { class: 'now-line', x1: M.l + this.pw, x2: M.l + this.pw,
                        y1: M.t, y2: M.t + this.ph }),
      ]);
      this.host.appendChild(this.svgEl);
      this.origin = null;
      // A rebuilt SVG has an empty y-axis group, so force the next update() to draw it: the
      // domain alone being unchanged must not mean "nothing to draw".
      this.lo = this.hi = null;
    }

    /** A new domain only when the data has outgrown the old one, or shrunk to under half. */
    yDomain(values) {
      const s = this.spec;
      if (s.domain) {
        const t = C.niceTicks(s.domain[0], s.domain[1], this.yTicks || 4);
        return [s.domain[0], s.domain[1], t.length > 1 ? t[1] - t[0] : s.domain[1] - s.domain[0]];
      }
      let lo = Math.min(...values), hi = Math.max(...values);
      if (!values.length) { lo = 0; hi = 1; }
      if (s.zero) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
      const minSpan = s.min_span || 0;
      if (hi - lo < minSpan) {
        const mid = (hi + lo) / 2;
        lo = mid - minSpan / 2; hi = mid + minSpan / 2;
        if (s.zero && lo > 0) { hi -= lo; lo = 0; }
        if (s.zero && hi < 0) { lo -= hi; hi = 0; }
      }
      const pad = (hi - lo) * 0.08 || 0.5;
      lo -= s.zero && lo >= 0 ? 0 : pad;
      hi += s.zero && hi <= 0 ? 0 : pad;
      if (this.lo !== null) {
        const fits = lo >= this.lo && hi <= this.hi;
        const tooLoose = (hi - lo) < 0.5 * (this.hi - this.lo);
        if (fits && !tooLoose) return [this.lo, this.hi, this.step];
      }
      const ticks = C.niceTicks(lo, hi, this.yTicks || 4);
      const step = ticks.length > 1 ? ticks[1] - ticks[0] : (hi - lo);
      return [Math.min(lo, Math.floor(lo / step) * step),
              Math.max(hi, Math.ceil(hi / step) * step), step];
    }

    y(v) { return C.margins.t + this.ph * (1 - (v - this.lo) / ((this.hi - this.lo) || 1)); }

    x(t) { return C.margins.l + (t - this.origin) * this.pxPerMs; }

    drawYAxis() {
      const M = C.margins;
      C.clear(this.yAxis);
      // The step the domain was rounded to, not a fresh niceTicks() over the rounded domain:
      // that picks a coarser step, and the outer ticks then fall outside and are dropped -- a
      // chart labelled "0" and nothing else.
      const ticks = [];
      const step = this.step || (this.hi - this.lo) / 4;
      for (let v = Math.ceil(this.lo / step - 1e-9) * step; v <= this.hi + step * 1e-9; v += step) {
        ticks.push(Math.abs(v) < step * 1e-9 ? 0 : v);
      }
      const dec = C.tickDec(ticks);
      for (const v of ticks) {
        this.yAxis.appendChild(C.svg('line', { class: 'grid', x1: M.l, x2: M.l + this.pw,
                                                y1: this.y(v), y2: this.y(v) }));
        this.yAxis.appendChild(C.svg('text', { class: 'tick', x: M.l - 6, y: this.y(v) + 3.5,
                                                'text-anchor': 'end', text: v.toFixed(dec) }));
      }
      if (this.lo < 0 && this.hi > 0) {
        this.yAxis.appendChild(C.svg('line', { class: 'zeroline', x1: M.l, x2: M.l + this.pw,
                                                y1: this.y(0), y2: this.y(0) }));
      }
      this.yAxis.appendChild(C.svg('text', { class: 'tick unit', x: M.l + this.pw + 6,
                                              y: M.t + 9, text: this.spec.unit || '' }));
    }

    /** Rebuild from the store. Cheap enough to run on every message and every second. */
    update(store, nowMs, tz) {
      const M = C.margins;
      const pts = store.series(this.spec.key).filter((p) => p[0] >= nowMs - this.windowMs - 10000);
      const [lo, hi, step] = this.yDomain(pts.map((p) => p[1]));
      if (lo !== this.lo || hi !== this.hi) {
        this.lo = lo; this.hi = hi; this.step = step; this.drawYAxis();
      }
      this.origin = nowMs - this.windowMs;
      this.tz = tz;

      C.clear(this.regionG);
      for (const r of store.regions(nowMs)) {
        const x1 = this.x(r.from), x2 = this.x(Math.max(r.to, r.from) + (r.to >= nowMs ? 120000 : 0));
        if (x2 <= x1) continue;
        this.regionG.appendChild(C.svg('rect', {
          class: 'region ' + r.pattern, x: x1, y: M.t, width: Math.max(1, x2 - x1), height: this.ph,
          fill: `url(#${r.pattern})`, 'data-pattern': r.pattern }));
      }

      C.clear(this.eventG);
      for (const e of this.events) {
        if (e.t < this.origin - 60000) continue;
        const x = this.x(e.t);
        this.eventG.appendChild(C.svg('line', { class: 'event', x1: x, x2: x, y1: M.t, y2: M.t + this.ph }));
        this.eventG.appendChild(C.svg('text', { class: 'event-label', x: x + 3, y: M.t + 10, text: e.label || '' }));
      }

      C.clear(this.lineG);
      const limit = C.gapLimitS(store.cadenceMs() / 1000) * 1000;
      const color = C.slotColor(this.spec.slot || 1);
      let seg = [];
      const flush = () => {
        if (seg.length === 1) {
          this.lineG.appendChild(C.svg('circle', { cx: this.x(seg[0][0]), cy: this.y(seg[0][1]),
                                                   r: 1.6, fill: color }));
        } else if (seg.length > 1) {
          const d = seg.map((p, i) => (i ? 'L' : 'M') + this.x(p[0]).toFixed(1) + ' ' +
                                      this.y(p[1]).toFixed(1)).join('');
          this.lineG.appendChild(C.svg('path', { d, fill: 'none', stroke: color,
                                                 'stroke-width': 1.6, 'stroke-linejoin': 'round' }));
        }
        seg = [];
      };
      for (const p of pts) {
        if (seg.length && p[0] - seg[seg.length - 1][0] > limit) flush();
        seg.push(p);
      }
      flush();
      this.points = pts.length;

      C.clear(this.xScroll);
      const first = Math.ceil((this.origin - TICK_MS) / TICK_MS) * TICK_MS;
      for (let t = first; t <= nowMs + 2 * TICK_MS; t += TICK_MS) {
        const x = this.x(t);
        this.xScroll.appendChild(C.svg('line', { class: 'grid', x1: x, x2: x, y1: M.t, y2: M.t + this.ph }));
        this.xScroll.appendChild(C.svg('text', { class: 'tick', x, y: this.height - 5,
                                                 'text-anchor': 'middle',
                                                 text: C.fmtTime(Math.round(t / 1000), tz, 'hm') }));
      }
      this.frame(nowMs);
    }

    /** The only per-frame work: slide both scrolling groups so `now` is the right edge. */
    frame(nowMs) {
      if (this.origin === null) return;
      const dx = -((nowMs - this.windowMs) - this.origin) * this.pxPerMs;
      const tr = `translate(${dx.toFixed(2)},0)`;
      this.dataScroll.setAttribute('transform', tr);
      this.xScroll.setAttribute('transform', tr);
    }

    setEvents(list) { this.events = list || []; }

    /** Rebuild at a new height (or the same one, at the host's current width). */
    resize(height) {
      if (height) this.height = Math.round(height);
      this.build();
    }

    /** True if the host is no longer the width this chart was built for. */
    stale() { return Math.abs(Math.max(320, this.host.clientWidth || 640) - this.width) > 2; }
  }

  // --------------------------------------------------------------- formatting

  /** "0.901 kW", or "1.234 kW exporting" for a signed power. */
  function fmtNow(spec, v) {
    if (typeof v !== 'number') return '—';
    const dec = spec.dec === undefined ? 2 : spec.dec;
    if (spec.sign && v !== 0) {
      return Math.abs(v).toFixed(dec) + ' ' + spec.unit + ' ' + spec.sign[v > 0 ? 0 : 1];
    }
    return v.toFixed(dec) + ' ' + (spec.unit || '');
  }

  function fmtAge(ms) {
    if (ms === null || ms === undefined || !isFinite(ms)) return '—';
    const s = Math.max(0, Math.round(ms / 1000));
    if (s < 90) return s + ' s';
    if (s < 5400) return Math.round(s / 60) + ' min';
    return Math.round(s / 3600) + ' h';
  }

  return { Clock, Store, Stream, LiveChart, api, recoverSession, fmtNow, fmtAge };
})();
