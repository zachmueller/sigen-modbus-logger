/*
  Composing serve.py's three answers out of precomputed static tiles.

  web/app.js reaches the outside world through one seam, getJSON(). On the capture host
  that seam is a fetch to serve.py, which decodes the archive live. Here it is this file,
  which fetches gzipped tiles from a CDN and assembles the SAME objects. That is the whole
  arrangement: one renderer, two sources, and nothing in app.js that knows the difference.

  What makes the assembly possible rather than approximate: series.py keys every bucket on
  ABSOLUTE epoch (ts // bucket_s), and the tile spans are UTC-aligned, so a bucket has one
  identity no matter which tile it arrived in and no matter what window asked for it. So a
  window is not "re-aggregated" from tiles -- each of its buckets is looked up by its own
  absolute start time. A requested window need not align with tile boundaries, and does not.

  Three things worth knowing before changing anything here:

    - **A missing tile means no data, not an error.** ingest.py writes nothing for a span
      with no records, so a 404 is the normal way to learn the logger was off. Only a
      failure that is NOT a 404 is a failure.
    - **`empty: true` on a column has to be expanded.** ingest.py drops the arrays for a
      field whose every sample in that span was absent -- over a day tile carrying 240
      fields that is most of the payload. The reader owes those buckets nulls.
    - **A counter's reset can hide in a seam.** Each tile reports `reset` only for what
      happened inside it, because a counter stepping back exactly between two tiles is
      invisible from within either. So the seams are checked here (FINDINGS 11).
*/
'use strict';

const Tiles = (() => {
  // Mirrors series.BUCKET_LADDER and series.TARGET_BUCKETS. Not fetched from meta at
  // module load because getJSON('/api/meta') is itself the first call; meta's copy is
  // used for validation once it is in hand.
  const LADDER = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600,
                  7200, 21600, 86400];
  const TARGET_BUCKETS = 900;
  const HOUR = 3600, DAY = 86400;

  const cache = new Map();       // url -> parsed JSON, or null for a known-absent tile
  let state = null;              // {base, plan, meta}

  // ---------------------------------------------------------------- fetching

  async function getObject(url, optional) {
    if (cache.has(url)) return cache.get(url);
    let r;
    try {
      r = await fetch(url, { cache: 'default' });
    } catch (e) {
      // A network failure is not an absent tile, even for an optional one: reporting it
      // as "no data" would draw a gap where the truth is "we could not look".
      throw new Error('cannot reach ' + url + ': ' + e.message);
    }
    if (r.status === 404 && optional) {
      cache.set(url, null);
      return null;
    }
    // A lapsed session, and the ONE failure here that the page can do something about: the
    // read gate refused this because the id token expired while the page sat open. It is
    // flagged rather than thrown flat because app.js recovers from it by reloading -- the gate
    // answers a NAVIGATION with a silent hop through /auth/refresh, which it has no way to do
    // for a fetch. See recoverSession() there.
    //
    // 401 and only 401. A 403 from the gate means "not on the allowlist", where signing in
    // again cannot help and a reload would loop forever, so it falls through to the throw
    // below and is reported like any other refusal.
    //
    // This is also why the gate answers /agg/* with a status at all: it used to redirect, and
    // a fetch following a 302 to the Cognito Hosted UI fails CORS, so an expired session
    // arrived here as "cannot reach" -- the same words as a dropped connection.
    if (r.status === 401) {
      let body = null;
      try { body = await r.json(); } catch (e) { /* the status is the answer */ }
      const err = new Error((body && body.error) || 'your session has expired');
      err.unauthorized = true;
      err.login = (body && body.login) || null;
      throw err;
    }
    if (!r.ok) throw new Error(url + ': ' + r.status + ' ' + r.statusText);
    // The tiles are stored gzipped with Content-Encoding: gzip, so the browser has
    // already inflated the body by the time we see it. Nothing to decompress here.
    const body = await r.json();
    cache.set(url, body);
    return body;
  }

  /** What "now" means for this source.
   *
   * The live hosted page: the clock, obviously. A FROZEN share: the moment it was shared.
   *
   * That distinction is not cosmetic. latest.json is copied into a share as it stood at
   * share time and then never touched, so measuring its ages against the clock makes them
   * grow forever -- and `stall_after_s` is 7200, so any share opened more than two hours
   * later rendered a red "no record for 3.4 d -- the logger may have stopped" over data that
   * was perfectly current when it was sent. The share was fine; only the clock had moved.
   *
   * Reading `shared_at` from meta rather than from SIGEN_SOURCE keeps the share page's inline
   * script to one line and gives the frozen view a single source of truth.
   */
  function nowFor(src, st) {
    if (src.frozen && st && st.meta && st.meta.shared_at) return st.meta.shared_at;
    return Date.now() / 1000;
  }

  async function ensureMeta(src) {
    const base = src.base.endsWith('/') ? src.base : src.base + '/';
    if (state && state.base === base && (!src.plan || state.plan === src.plan)) {
      return state;
    }
    const index = await getObject(base + 'index.json', false);
    const plan = src.plan || index.current;
    if (!plan) throw new Error('the archive index names no plan with any data in it');
    const meta = await getObject(base + 'plan=' + plan + '/meta.json', false);
    state = { base: base, plan: plan, meta: meta, index: index };
    return state;
  }

  // ------------------------------------------------------------ bucket sizing

  // series.choose_bucket. The floor is the plan's cadence: a bucket finer than the tick
  // holds at most one sample, so min == mean == max and half of them are empty.
  function chooseBucket(spanS, floorS) {
    const want = Math.max(floorS || 1, spanS / TARGET_BUCKETS);
    for (const b of LADDER) if (b >= want) return b;
    return LADDER[LADDER.length - 1];
  }

  function snapToLadder(b) {
    let best = LADDER[0];
    for (const x of LADDER) if (Math.abs(x - b) < Math.abs(best - b)) best = x;
    return best;
  }

  function granularityOf(st, bucketS) {
    const g = st.meta.granularity || {};
    if ((g.hour || []).includes(bucketS)) return 'hour';
    if ((g.day || []).includes(bucketS)) return 'day';
    if ((g.month || []).includes(bucketS)) return 'month';
    // A width this plan does not materialise. Fall back to the finest it does that is no
    // finer than asked for, so the page draws something honest rather than nothing.
    return null;
  }

  /** The widths this plan actually has tiles for, ascending. */
  function availableWidths(st) {
    const g = st.meta.granularity || {};
    return [].concat(g.hour || [], g.day || [], g.month || []).sort((a, b) => a - b);
  }

  /** The width to use for a span: what the ladder wants, snapped to what exists. */
  function widthFor(st, spanS) {
    const want = chooseBucket(spanS, st.meta.fast_period_s);
    const have = availableWidths(st);
    if (have.includes(want)) return want;
    // Prefer the next width UP rather than down: coarser is fewer requests and a smaller
    // payload, and going finer than the ladder asked for would fetch more data to draw
    // more points than the chart has pixels for.
    for (const b of have) if (b >= want) return b;
    return have.length ? have[have.length - 1] : want;
  }

  // -------------------------------------------------------------- tile spans

  function utcMonthSpan(ts) {
    const d = new Date(ts * 1000);
    const lo = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), 1) / 1000;
    const hi = Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 1) / 1000;
    return [lo, hi];
  }

  /** [{start, end, path}] covering [start, end) at this width, oldest first. */
  function tilePaths(st, kind, bucketS, start, end) {
    const out = [];
    let ts = start;
    while (ts < end) {
      let lo, hi;
      if (kind === 'hour') { lo = Math.floor(ts / HOUR) * HOUR; hi = lo + HOUR; }
      else if (kind === 'day') { lo = Math.floor(ts / DAY) * DAY; hi = lo + DAY; }
      else { [lo, hi] = utcMonthSpan(ts); }
      out.push({ start: lo, end: hi, path: tileKey(st, kind, bucketS, lo) });
      ts = hi;
    }
    return out;
  }

  /** Mirrors ingest.path_for(). Named by UTC span, so a key is legible and sorts. */
  function tileKey(st, kind, bucketS, spanStart) {
    const d = new Date(spanStart * 1000);
    const p = (n, w) => String(n).padStart(w, '0');
    const y = d.getUTCFullYear(), mo = p(d.getUTCMonth() + 1, 2);
    const stem = kind === 'hour'
      ? y + '/' + mo + '/' + p(d.getUTCDate(), 2) + '/' + p(d.getUTCHours(), 2)
      : kind === 'day' ? y + '/' + mo + '/' + p(d.getUTCDate(), 2)
        : y + '/' + mo;
    return st.base + 'plan=' + st.plan + '/' + kind + '/b' + bucketS + '/' + stem
      + '.json.gz';
  }

  // ------------------------------------------------------------------- /api/meta

  async function meta(src) {
    const st = await ensureMeta(src);
    // Copied, not returned by reference: app.js merges the window's extent into
    // state.meta.extent, and mutating the cached object would poison the next read.
    const out = Object.assign({}, st.meta);
    out.ok = true;
    out.now = nowFor(src, st);
    out.plans = (st.index.plans || []).map((p) => p.hash);
    out.extent = Object.assign({}, st.meta.extent);
    // The footer prints this. On the capture host it is an absolute path under someone's
    // home directory; here the plan is the honest answer to "which archive is this".
    out.data_dir = 'plan ' + st.plan;
    return out;
  }

  // ----------------------------------------------------------------- /api/latest

  async function latest(src) {
    const st = await ensureMeta(src);
    const lt = Object.assign({},
      await getObject(st.base + 'plan=' + st.plan + '/latest.json', false));
    // The ages are computed HERE, not baked in. latest.json is written once an hour, so a
    // stored age would be wrong the moment after it was written, and a stored
    // logger_stalled would be permanently true -- a red "the logger may have stopped" on
    // a page whose data is merely an hour old, which is normal.
    //
    // For a frozen share `now` is share time, not the clock -- see nowFor(). Otherwise the
    // same reasoning inverts and every old share claims the logger died.
    const now = nowFor(src, st);
    lt.now = now;
    if (lt.record_ts) lt.record_age_s = now - lt.record_ts;
    if (lt.data_ts) lt.data_age_s = now - lt.data_ts;
    const stallAfter = lt.stall_after_s || 7200;
    lt.logger_stalled = !!lt.record_ts && (now - lt.record_ts) > stallAfter;
    return lt;
  }

  // ----------------------------------------------------------------- /api/window

  function grid(start, end, bucketS) {
    // series._grid: END-INCLUSIVE, because a window ending "now" should show the bucket
    // now falls in. Tiles are end-exclusive; the lookup below reconciles the two.
    const lo = Math.floor(start / bucketS) * bucketS;
    const hi = Math.floor(end / bucketS) * bucketS;
    const out = [];
    for (let t = lo; t <= hi; t += bucketS) out.push(t);
    return out;
  }

  /** Contiguous [t0, t1] spans where flags[i] is true. Mirrors series._runs(). */
  function runs(flags, g, bucketS) {
    const out = [];
    let run = null;
    for (let i = 0; i < flags.length; i++) {
      if (flags[i] && run === null) run = g[i];
      else if (!flags[i] && run !== null) { out.push([run, g[i]]); run = null; }
    }
    if (run !== null) out.push([run, g[g.length - 1] + bucketS]);
    return out;
  }

  /** meta's tz change points, narrowed to this window: the one in force at `start`,
      plus any that happen inside it. One offset would label a span across a DST switch
      an hour out for half of it. */
  function tzFor(st, start, end) {
    const all = st.meta.tz || [];
    const out = [];
    for (const run of all) {
      if (run[0] <= start) { out.length = 0; out.push([start, run[1], run[2]]); }
      else if (run[0] <= end) out.push(run);
    }
    return out.length ? out : all.slice(0, 1);
  }

  const ARRAY_FIELDS = ['mean', 'min', 'max', 'last', 'bits'];

  async function windowOf(src, q) {
    const st = await ensureMeta(src);
    const t0 = performance.now();
    const ext = st.meta.extent || {};
    const now = nowFor(src, st);

    let end = num(q, 'end', null);
    const hours = num(q, 'hours', st.meta.default_hours || 6);
    if (end === null || end <= 0) end = Math.max(now, ext.last_ts || now);
    let start = num(q, 'start', end - Math.max(0.01, hours) * 3600);
    if (start >= end) throw new Error('start must be before end');

    let bucketS = num(q, 'bucket', null);
    bucketS = bucketS ? snapToLadder(bucketS) : widthFor(st, end - start);
    const kind = granularityOf(st, bucketS);
    if (!kind) throw new Error('no tiles at ' + bucketS + 's for this archive');

    const wanted = requestedKeys(st, q);
    const counters = st.meta.counters || [];

    const spans = tilePaths(st, kind, bucketS, start, end);
    const loaded = await Promise.all(spans.map((s) => getObject(s.path, true)));

    const g = grid(start, end, bucketS);
    const n = g.length;
    const index = new Map();
    for (let i = 0; i < n; i++) index.set(g[i], i);

    const series = {};
    const health = {
      records: zeros(n), empty: zeros(n),
      latency_median: nulls(n), latency_p95: nulls(n), latency_max: nulls(n),
    };
    let covered = [];
    let records = 0;
    // Counter endpoints on the WINDOW grid, so the totals measure from the window's edge
    // and not the tile's. See energyOf().
    const counters_ = {};

    for (let ti = 0; ti < spans.length; ti++) {
      const tile = loaded[ti];
      if (!tile) continue;       // absent tile: no data in that span, which is the truth
      if (tile.bucket_s !== bucketS) {
        throw new Error(spans[ti].path + ' is ' + tile.bucket_s
                        + 's, expected ' + bucketS + 's');
      }
      // Which of this tile's buckets land in the requested grid.
      for (let j = 0; j < tile.n; j++) {
        const t = tile.start + j * bucketS;
        const i = index.get(t);
        if (i === undefined) continue;
        health.records[i] = tile.health.records[j] || 0;
        health.empty[i] = tile.health.empty[j] || 0;
        for (const f of ['latency_median', 'latency_p95', 'latency_max']) {
          const arr = tile.health[f];
          if (arr && arr[j] !== undefined) health[f][i] = arr[j];
        }
        records += tile.health.records[j] || 0;
      }
      for (const [key, col] of Object.entries(tile.series || {})) {
        if (wanted.size && !wanted.has(key)) continue;
        const dst = ensureColumn(series, key, col, n);
        if (col.empty) continue;   // its buckets stay null: see the header note
        for (const f of ARRAY_FIELDS) {
          if (!col[f] || !dst[f]) continue;
          for (let j = 0; j < tile.n; j++) {
            const i = index.get(tile.start + j * bucketS);
            if (i !== undefined) dst[f][i] = col[f][j];
          }
        }
      }
      for (const [key, c] of Object.entries(tile.counters || {})) {
        const dst = counters_[key]
          || (counters_[key] = { first: nulls(n), last: nulls(n), reset: false });
        dst.reset = dst.reset || !!c.reset;
        for (let j = 0; j < tile.n; j++) {
          const i = index.get(tile.start + j * bucketS);
          if (i === undefined) continue;      // this bucket is outside the window
          if (c.first && c.first[j] !== null) dst.first[i] = c.first[j];
          if (c.last && c.last[j] !== null) dst.last[i] = c.last[j];
        }
      }
      for (const [a, b] of tile.covered || []) covered.push([a, b]);
    }

    // Counters travel as endpoints and are never a line, so app.js is told to skip them.
    for (const key of counters) {
      if (!series[key]) {
        series[key] = { unit: 'kWh', cadence_s: bucketS, tile_only: true };
      }
    }
    // A column that got no data at all says so once instead of in n nulls.
    for (const [key, col] of Object.entries(series)) {
      if (col.tile_only) continue;
      if (!col.mean && !col.last && !col.bits) { col.empty = true; continue; }
      const arr = col.mean || col.last || col.bits;
      if (arr.every((v) => v === null || v === undefined)) {
        for (const f of ARRAY_FIELDS) delete col[f];
        col.empty = true;
      }
    }

    const inside = (t) => covered.some(([a, b]) => a !== null && a <= t && t < b);
    const noData = [], noRecords = [];
    for (let i = 0; i < n; i++) {
      noData.push(health.records[i] > 0 && health.records[i] === health.empty[i]);
      noRecords.push(health.records[i] === 0 && inside(g[i]));
    }

    return {
      ok: true,
      start: start, end: end, bucket_s: bucketS,
      // Striding happens when the tile is BUILT, so the reader reports what the tile
      // recorded rather than recomputing a number that would not match.
      stride: strideOf(bucketS, st.meta.fast_period_s, st.meta.samples_per_bucket),
      samples_per_bucket: st.meta.samples_per_bucket || 64,
      t: g,
      tz: tzFor(st, start, end),
      records: records,
      files: spans.length,
      files_read: loaded.filter(Boolean).length,
      pending_files: 0,        // nothing is deferred: a tile either came back or is absent
      warming: 0,
      unreadable: [],
      unknown_fields: unknownOf(st, q),
      plan_hash: st.plan,
      extent: { first_ts: ext.first_ts, last_ts: ext.last_ts },
      health: Object.assign(health, {
        no_data: runs(noData, g, bucketS),
        no_records: runs(noRecords, g, bucketS),
        covered: covered,
        note: st.meta.health_note
          || 'latency is over records that returned data; an outage probe\'s latency is '
             + 'the socket timeout, not the device\'s.',
      }),
      series: series,
      energy: energyOf(counters_),
    };
  }

  function ensureColumn(series, key, col, n) {
    if (series[key]) return series[key];
    const dst = { unit: col.unit || '', cadence_s: col.cadence_s || null };
    for (const f of ARRAY_FIELDS) if (col[f]) dst[f] = nulls(n);
    if (col.bits) dst.bits = zeros(n);
    series[key] = dst;
    return dst;
  }

  /** series.energy(), composed across tiles and CLIPPED TO THE WINDOW.
   *
   * `counters` is {key: {first: [...], last: [...], reset: bool}} with one entry per
   * bucket OF THE WINDOW GRID -- already narrowed by the caller, which drops the buckets
   * a tile holds outside the requested span.
   *
   * That narrowing is the whole reason the arrays exist. Taking a tile's own endpoints
   * measures from the top of the hour, which for "the last six hours" starting at :05 is
   * five minutes of extra energy: a ~2% overstatement that reads as perfectly plausible.
   */
  function energyOf(counters) {
    const out = {};
    for (const [key, c] of Object.entries(counters)) {
      const firstVal = firstNonNull(c.first);
      const lastVal = lastNonNull(c.last);
      if (firstVal === null || lastVal === null) continue;
      const delta = lastVal - firstVal;
      out[key] = {
        kwh: Math.max(0, round(delta, 3)),
        first: firstVal, last: lastVal,
        // A step back within a bucket span is flagged by the tile that built it; a step
        // back between buckets is visible right here. Either way the total is a floor
        // rather than a negative (FINDINGS 11).
        reset: !!c.reset || delta < 0 || stepsBack(c),
      };
    }
    const pv = (out['plant_accumulated_pv_energy'] || {}).kwh;
    const ex = (out['plant_accumulated_grid_export_energy'] || {}).kwh;
    if (pv) out.self_consumption = round(Math.max(0, pv - (ex || 0)) / pv, 4);
    return out;
  }

  function firstNonNull(a) {
    for (const v of a || []) if (v !== null && v !== undefined) return v;
    return null;
  }

  function lastNonNull(a) {
    for (let i = (a || []).length - 1; i >= 0; i--) {
      if (a[i] !== null && a[i] !== undefined) return a[i];
    }
    return null;
  }

  /** Does the counter ever go backwards between buckets? Catches a reset that falls in a
   *  tile seam, which neither tile can see from the inside. */
  function stepsBack(c) {
    let prev = null;
    for (let i = 0; i < (c.last || []).length; i++) {
      const f = c.first ? c.first[i] : null;
      if (prev !== null && f !== null && f !== undefined && f < prev) return true;
      if (c.last[i] !== null && c.last[i] !== undefined) prev = c.last[i];
    }
    return false;
  }

  // series.stride_for, so the filter row's "sampled 1 record in N" is the truth about
  // how the tile was made rather than a guess.
  function strideOf(bucketS, cadenceS, perBucket) {
    const per = Math.max(1, Math.floor(bucketS / Math.max(1, cadenceS || 1)));
    return Math.max(1, Math.ceil(per / (perBucket || 64)));
  }

  function requestedKeys(st, q) {
    // The panel fields are always in the tile, so an explicit list is only needed to know
    // which CUSTOM fields to keep -- and dropping the rest keeps a day tile's 240 fields
    // out of the object app.js walks.
    const ids = list(q, 'panels');
    const extra = list(q, 'fields');
    if (!ids.length && !extra.length) return new Set();
    const keep = new Set(extra);
    for (const p of st.meta.panels || []) {
      if (ids.length && !ids.includes(p.id) && !ids.includes('all')) continue;
      for (const s of p.series || []) keep.add(s.key);
      for (const k of p.alarms || []) keep.add(k);
    }
    for (const k of st.meta.counters || []) keep.add(k);
    return keep;
  }

  /** Fields asked for that these tiles cannot answer, so the page can say which. */
  function unknownOf(st, q) {
    const extra = list(q, 'fields');
    if (!extra.length) return [];
    const known = new Set((st.meta.catalog || []).map((c) => c.key));
    return extra.filter((k) => !known.has(k));
  }

  // ------------------------------------------------------------------ helpers

  function num(q, name, dflt) {
    const raw = q.get(name);
    if (raw === null || raw === '' || raw === 'now') return dflt;
    const v = parseFloat(raw);
    return Number.isFinite(v) ? v : dflt;
  }

  function list(q, name) {
    const raw = q.get(name);
    return raw ? raw.split(',').filter(Boolean) : [];
  }

  function zeros(n) { return new Array(n).fill(0); }
  function nulls(n) { return new Array(n).fill(null); }
  function round(v, d) { const m = Math.pow(10, d); return Math.round(v * m) / m; }

  // --------------------------------------------------------------------- entry

  async function getJSON(src, url) {
    const cut = url.indexOf('?');
    const route = cut < 0 ? url : url.slice(0, cut);
    const q = new URLSearchParams(cut < 0 ? '' : url.slice(cut + 1));
    if (route === '/api/meta') return meta(src);
    if (route === '/api/latest') return latest(src);
    if (route === '/api/window') return windowOf(src, q);
    // Not "return an empty answer": a route this source cannot serve is a bug in the
    // page, and it should say so rather than render as a quiet night.
    throw new Error('a tile source cannot answer ' + route);
  }

  return { getJSON: getJSON, chooseBucket: chooseBucket, widthFor: widthFor,
           tileKey: tileKey, grid: grid, runs: runs, energyOf: energyOf,
           strideOf: strideOf, _reset: () => { cache.clear(); state = null; } };
})();
