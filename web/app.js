/*
  The viewer's application layer: state, fetching, and turning /api/window into
  the panels serve.py described in /api/meta.

  Which charts exist, and which fields make them up, is NOT decided here -- it
  comes from the server's PANELS list, so a new chart is a dict in serve.py rather
  than new JavaScript.

  Three behaviours worth knowing:

    - Where the answers come from is a detail, deliberately. Everything goes through
      getJSON(), so this same file renders serve.py's live archive and a hosted
      deployment's precomputed tiles. There is one renderer, and a shared view looks
      like the screen it was taken from because it IS that code -- see SRC below.
    - Only expanded panels are requested. Collapsing a panel stops fetching its
      fields, which is why a page showing four charts costs a quarter of one
      showing sixteen.
    - Every number on screen says where it came from: the bucket width and the
      record stride are chips in the filter row, the tooltip reports how many
      records went into the point, and a partial answer (the server ran out of
      its warm budget) is labelled rather than shown as if it were complete.
*/
'use strict';

const C = Charts;
const $ = (id) => document.getElementById(id);

// Where the answers come from. This page is deployed two ways and must not fork:
// serve.py serves it with no SIGEN_SOURCE at all, so it defaults to fetching from
// that server; a hosted deployment sets the global in an inline <script> before this
// file loads, pointing at precomputed tiles that compose into the same three JSON
// answers. Everything below goes through getJSON() and nothing calls fetch()
// directly, which is what keeps one renderer rather than two.
//
//   {kind: 'server'}                        this server, live
//   {kind: 'tiles', base: '/d/'}            hosted, live
//   {kind: 'tiles', base: '/p/<uid>/',      a shared view: one fixed window,
//    frozen: true}                          nothing to pan to and nothing current
const SRC = window.SIGEN_SOURCE || { kind: 'server', base: '' };

// A frozen source is a copy of one view. applyFrozenMode() enforces the two things
// that follow: no control may ask for a different window, and nothing may claim to
// be current -- a view read tomorrow must not present yesterday's reading as now.
const FROZEN = !!SRC.frozen;

const PRESETS = [
  { label: '15m', hours: 0.25 }, { label: '1h', hours: 1 },
  { label: '6h', hours: 6 }, { label: '24h', hours: 24 },
  { label: '3d', hours: 72 }, { label: '7d', hours: 168 },
  { label: 'All', hours: null },
];

// Reached through an SSH tunnel? Then a "Close tunnel" button makes sense, and it
// has to act locally: the server is on the capture host and cannot touch this
// machine's ssh process, so the button hands off to the Stop app's URL scheme.
const TUNNELED = ['localhost', '127.0.0.1', '::1', '[::1]'].includes(location.hostname);

const state = {
  meta: null,
  win: null,
  latest: null,
  disconnected: false,
  hours: 6,
  end: null,              // null means "the newest data there is"
  live: true,
  expanded: new Set(),
  custom: [],
  showDupes: false,
  charts: [],
  group: null,
  timer: null,
  inflight: null,
  panelCards: new Map(),
  tables: new Set(),
};

// ---------------------------------------------------------------- boot & state

async function boot() {
  state.group = new C.CrosshairGroup($('tooltip'));
  try {
    state.meta = await getJSON('/api/meta');
  } catch (e) {
    return fatal('Cannot reach the viewer API: ' + e.message);
  }
  if (!state.meta.ok) return fatal(state.meta.reason);
  // AFTER meta, not before: a frozen share reads its window out of meta.view, so there is
  // nothing to read until meta is in hand. /api/meta takes no window parameters, so nothing
  // is lost by asking for it first. Everything below still runs after readHash(), which is
  // what fills state.expanded and decides whether default_hours applies.
  readHash();
  if (!state.expanded.size) {
    for (const p of state.meta.panels) if (!p.collapsed) state.expanded.add(p.id);
  }
  if (!FROZEN && !hashHas('h')) state.hours = state.meta.default_hours || 6;
  buildPresets();
  buildPanelCards();
  buildPicker();
  wireControls();
  renderProvenance();
  await refresh();
  schedule();
}

function readHash() {
  if (FROZEN) {
    // A frozen view is the one it was shared with, whatever the URL says: there is
    // nothing to ask for another, and a panel that was open when it was shared has to
    // be open when it is read -- including one PANELS marks collapsed.
    //
    // From meta, not from SIGEN_SOURCE: the share's meta.json already has to be fetched, so
    // putting the frozen window there keeps the share page's inline script to one line and
    // leaves one place where the answer lives.
    const v = state.meta.view || {};
    state.hours = v.hours || null;
    state.end = v.end || null;
    state.expanded = new Set(v.expanded || []);
    state.custom = (v.custom || []).slice();
    state.live = false;
    return;
  }
  const q = new URLSearchParams(location.hash.replace(/^#/, ''));
  if (q.has('h')) state.hours = q.get('h') === 'all' ? null : parseFloat(q.get('h'));
  if (q.has('end')) state.end = parseFloat(q.get('end')) || null;
  if (q.has('panels')) state.expanded = new Set(q.get('panels').split(',').filter(Boolean));
  if (q.has('fields')) state.custom = q.get('fields').split(',').filter(Boolean);
  if (q.has('live')) state.live = q.get('live') === '1';
}

function hashHas(k) {
  return new URLSearchParams(location.hash.replace(/^#/, '')).has(k);
}

function writeHash() {
  if (FROZEN) return;                // the view is fixed; a hash would imply otherwise
  const q = new URLSearchParams();
  q.set('h', state.hours === null ? 'all' : String(state.hours));
  if (state.end) q.set('end', String(Math.round(state.end)));
  q.set('panels', [...state.expanded].join(','));
  if (state.custom.length) q.set('fields', state.custom.join(','));
  q.set('live', state.live ? '1' : '0');
  history.replaceState(null, '', '#' + q.toString());
}

// The one seam every answer comes through. `url` is always one of the three API
// routes, with serve.py's query string; a tile source parses that query and composes
// the same object out of static objects instead of asking a server for it.
async function getJSON(url) {
  if (SRC.kind === 'tiles') return Tiles.getJSON(SRC, url);
  const r = await fetch((SRC.base || '') + url, { cache: 'no-store' });
  const body = await r.json().catch(() => null);
  if (!r.ok) throw new Error((body && body.error) || (r.status + ' ' + r.statusText));
  return body;
}

function fatal(msg) {
  const box = C.el('div', { class: 'error' }, [
    C.el('strong', { text: 'The viewer has nothing to show. ' }),
    document.createTextNode(msg || ''),
  ]);
  document.querySelector('main').prepend(box);
}

// -------------------------------------------------------------------- controls

function buildPresets() {
  const host = $('presets');
  C.clear(host);
  for (const p of PRESETS) {
    host.appendChild(C.el('button', {
      text: p.label, 'data-h': String(p.hours),
      'aria-pressed': String(sameHours(p.hours, state.hours)),
      onclick: () => { state.hours = p.hours; state.end = state.end; markPresets(); refresh(); },
    }));
  }
}

function sameHours(a, b) {
  if (a === null || b === null) return a === b;
  return Math.abs(a - b) < 1e-6;
}

function markPresets() {
  for (const b of $('presets').children) {
    const h = b.getAttribute('data-h');
    b.setAttribute('aria-pressed',
                   String(sameHours(h === 'null' ? null : parseFloat(h), state.hours)));
  }
}

function windowSpanS() {
  if (state.hours !== null) return state.hours * 3600;
  const ex = state.meta.extent;
  return Math.max(3600, (ex.last_ts || 0) - (ex.first_ts || 0));
}

function wireControls() {
  // A resize and a light/dark switch are re-renders of what is already here, so
  // they are wired in a frozen view too.
  window.addEventListener('resize', debounce(() => renderCharts(), 150));
  if (window.matchMedia) {
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    if (mq.addEventListener) mq.addEventListener('change', () => renderCharts());
  }
  // Everything below asks for a window other than the one on screen.
  if (FROZEN) return applyFrozenMode();
  if (SRC.share) {
    $('share').hidden = false;
    $('share-go').onclick = () => createShare();
  }
  $('back').onclick = () => pan(-0.5);
  $('fwd').onclick = () => pan(0.5);
  $('now').onclick = () => { state.end = null; setLive(true); refresh(); };
  $('reload').onclick = () => refresh();
  $('live').checked = state.live;
  $('live').onchange = (e) => setLive(e.target.checked);
  $('endat').onchange = (e) => {
    const ts = C.fromLocalInput(e.target.value, state.meta.tz);
    if (ts) { state.end = ts; setLive(false); refresh(); }
  };
  if (TUNNELED) {
    const btn = $('close-tunnel');
    btn.hidden = false;
    // A real <a href="sigen-stop://…"> rather than a scripted navigation: browsers
    // hand a custom scheme to the OS on a user gesture, and block it otherwise.
    // The click is allowed to proceed; this only updates the page around it.
    btn.addEventListener('click', () => tunnelClosing());
  }
  document.addEventListener('keydown', (ev) => {
    const t = ev.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'SELECT' || t.tagName === 'TEXTAREA')) return;
    if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
    if (ev.key === 'ArrowLeft') { ev.preventDefault(); pan(-0.5); }
    else if (ev.key === 'ArrowRight') { ev.preventDefault(); pan(0.5); }
    else if (ev.key === '+' || ev.key === '=') zoom(0.5);
    else if (ev.key === '-' || ev.key === '_') zoom(2);
    else if (ev.key === 'n') { state.end = null; setLive(true); refresh(); }
    else if (ev.key === 'l') setLive(!state.live);
  });
}

// ------------------------------------------------------ reading a shared view

// A frozen source is a copy of one view. Every control that would ask for a different
// one is removed rather than left in place doing nothing. Split in two because the two
// halves know different things at different times: the controls can go as soon as the
// page is wired, but the banner names the window, which is not known until it has been
// fetched -- reading state.win here gave "NaN-NaN-NaN", since wireControls() runs
// before refresh(). So the banner is rendered from renderAll() like everything else
// that quotes the data.
function applyFrozenMode() {
  for (const el of document.querySelectorAll('[data-live-only]')) el.hidden = true;
  document.querySelector('.brand h1').textContent += ' — shared view';
  $('hint').textContent =
    'Hover a chart, or focus it and use the arrow keys, for exact values; Table '
    + 'shows the numbers behind a panel. This is a fixed window, so nothing else moves.';
  if (!state.custom.length) $('custom-card').hidden = true;
  else document.querySelector('#custom-card .note').textContent =
    'The extra fields this view was built with.';
}

// Anything that was merely current when the view was shared has to say so -- a view
// read tomorrow must not present yesterday's reading as the latest one.
function renderFrozenBanner() {
  const win = state.win;
  if (!win) return;
  const box = $('frozen-banner');
  box.hidden = false;
  C.clear(box);
  box.appendChild(C.el('strong', { text: 'A shared view. ' }));
  box.appendChild(document.createTextNode(
    C.fmtTime(win.start, win.tz, 'full') + ' → ' + C.fmtTime(win.end, win.tz, 'full')
    + ' ' + C.zoneName(win.tz) + ' on the capture host\'s clock'
    + (state.meta.shared_at
        ? ', shared ' + C.fmtTime(state.meta.shared_at, win.tz, 'full') : '')
    + '. It holds this window and these panels only, and it cannot be panned, zoomed '
    + 'or refreshed. The headline numbers are the last good sample before it was '
    + 'shared, not now.'));
  if (state.meta.note) {
    box.appendChild(C.el('p', { style: { margin: '0.35rem 0 0' } }, [
      C.el('strong', { text: 'From the sender: ' }),
      document.createTextNode(state.meta.note)]));
  }
}

// ------------------------------------------------------ writing a shared view

// What the share will contain, said before it is made rather than after. The window and the
// expanded panels ARE the share, so the summary is the confirmation -- there is nothing else
// to review.
function renderShareSummary() {
  const box = $('share-summary');
  if (!box || !SRC.share || !state.win) return;
  const win = state.win;
  const panels = state.expanded.size;
  box.textContent = C.fmtTime(win.start, win.tz, 'short') + ' → '
    + C.fmtTime(win.end, win.tz, 'short') + ' · ' + fmtDuration(win.bucket_s)
    + ' buckets · ' + panels + (panels === 1 ? ' panel' : ' panels')
    + (state.custom.length ? ' · ' + state.custom.length + ' extra field'
        + (state.custom.length === 1 ? '' : 's') : '');
}

async function createShare() {
  const btn = $('share-go');
  const out = $('share-result');
  btn.disabled = true;
  C.clear(out);
  out.textContent = 'Copying tiles…';
  try {
    // hours/end rather than start/end: this is exactly what windowURL() sends, and the
    // Lambda rebuilds the window the same way, so the frozen copy cannot land on a
    // different span than the one on screen. null hours means the "All" preset.
    const r = await fetch(SRC.share, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        hours: state.hours,
        end: state.end || (state.win && state.win.end) || null,
        panels: [...state.expanded],
        fields: state.custom,
        note: $('share-note').value || '',
      }),
    });
    // Read once as text, then parse. A Response body can only be consumed once, and a
    // refusal that is not JSON -- or is JSON of a shape this page does not own -- still has
    // to be quotable rather than silently discarded. See refusalText().
    const raw = await r.text();
    let body = null;
    try { body = JSON.parse(raw); } catch (e) { /* not JSON; refusalText() quotes it */ }
    if (!r.ok || !body || !body.ok) {
      // 401 carries a `login`: the session lapsed while the page sat open, and the fix is
      // to sign in again rather than to retry. 403 has no login -- signing in cannot help.
      C.clear(out);
      out.appendChild(document.createTextNode(
        'Could not create the link: ' + refusalText(r, body, raw) + ' '));
      if (body && body.login) {
        out.appendChild(C.el('a', { href: body.login, text: 'Sign in again' }));
      }
      return;
    }
    showShareLink(body);
  } catch (e) {
    out.textContent = 'Could not create the link: ' + e.message;
  } finally {
    btn.disabled = false;
  }
}

// Whatever refused this, said so that a reader can act on it. Four shapes arrive here, and
// only the first is ours:
//
//   {ok: false, error: …}    the gate or the share handler. The message IS the answer.
//   {Message: "Forbidden"}   the function URL's IAM authorizer refusing an unsigned request.
//   {message: "The request   the same authorizer refusing a signature that covers the wrong
//    signature we …"}        payload hash -- and `{message: "Internal Server Error"}` is a
//                            502 from the handler crashing. BOTH CASINGS occur, and reading
//                            only `error` reduced all of it to "403 ": no message, and no
//                            log either, since that refusal happens BEFORE the handler runs.
//   anything else            a CloudFront error page, or nothing. `statusText` is no help:
//                            it is always '' over HTTP/2, which the distribution serves.
//
// So the status is always named, and for the failures the page cannot see inside, it says
// which side of the endpoint they came from -- that is the difference between "retry" and
// "read the Lambda log".
function refusalText(r, body, raw) {
  if (body && body.error) return body.error + '.';
  const said = (body && (body.Message || body.message))
    || (raw || '').trim().replace(/\s+/g, ' ').slice(0, 200);
  return r.status + (said ? ' ' + said : '')
    + (r.status === 403
      ? ' — refused before it reached the endpoint, so nothing was created. CloudFront signs '
        + 'this request and Lambda rejects it if the payload hash is missing or wrong.'
      : r.status >= 500
        ? ' — the endpoint failed after accepting the request; its log has the reason.'
        : ' — no reason was given.');
}

function showShareLink(body) {
  const out = $('share-result');
  C.clear(out);
  const link = C.el('a', { href: body.url, text: body.url });
  link.target = '_blank';
  link.rel = 'noopener';
  const copy = C.el('button', {
    text: 'Copy',
    onclick: () => {
      // clipboard.writeText needs a secure context and a user gesture; both hold here. The
      // link is on screen and selectable either way, so a refusal is not a dead end.
      if (navigator.clipboard) {
        navigator.clipboard.writeText(body.url)
          .then(() => { copy.textContent = 'Copied'; })
          .catch(() => { copy.textContent = 'Copy failed — select it instead'; });
      } else {
        copy.textContent = 'Select it to copy';
      }
    },
  });
  out.appendChild(C.el('strong', { text: 'Anyone with this link can read it: ' }));
  out.appendChild(link);
  out.appendChild(document.createTextNode(' '));
  out.appendChild(copy);
  out.appendChild(C.el('p', { style: { margin: '0.35rem 0 0' }, text:
    'It needs no sign-in, and it is a copy: ' + body.tiles
    + (body.tiles === 1 ? ' tile was' : ' tiles were') + ' written at '
    + fmtDuration(body.bucket_s) + ' buckets. Re-aggregating the archive later cannot '
    + 'change what you just sent.' }));
}

// -- closing the tunnel from the page

function tunnelClosing() {
  state.wasLive = state.live;
  state.disconnected = true;          // stop polling something about to disappear
  state.live = false;
  $('live').checked = false;
  schedule();
  const box = $('disconnected');
  box.hidden = false;
  C.clear(box);
  box.appendChild(C.el('strong', { text: 'Closing the tunnel… ' }));
  box.appendChild(document.createTextNode(
    'If the browser asks permission to open “Sigen Viewer Stop”, allow it. '
    + 'This page will stop updating.'));
  // Verify rather than assume: if nothing handles sigen-stop:// the click silently
  // does nothing, and a page claiming "closed" would be lying. Poll rather than
  // check once -- the first click raises a browser permission prompt, and a single
  // check two seconds later would call failure while the reader is still reading it.
  setTimeout(() => verifyClosed(1), 1500);
}

const CLOSE_TRIES = 8;              // 8 x 2 s, enough to answer a permission prompt

async function verifyClosed(attempt) {
  const box = $('disconnected');
  let stillUp = false;
  try {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), 1500);
    const r = await fetch('/api/stats', { cache: 'no-store', signal: ctl.signal });
    clearTimeout(t);
    stillUp = r.ok;
  } catch (e) {
    stillUp = false;                  // the forward is gone, which is the point
  }
  if (stillUp && attempt < CLOSE_TRIES) {
    setTimeout(() => verifyClosed(attempt + 1), 2000);
    return;
  }
  C.clear(box);
  if (stillUp) {
    box.appendChild(C.el('strong', { text: 'The tunnel is still open. ' }));
    box.appendChild(document.createTextNode(
      'Either the browser blocked the hand-off to sigen-stop:// — look for a '
      + 'permission prompt — or the launcher apps are not installed '
      + '(deploy/install-launcher.sh). From a terminal: '));
    box.appendChild(C.el('code', { text: 'pkill -f sigen-viewer' }));
    state.disconnected = false;       // it still works, so carry on
    if (state.wasLive) setLive(true);
    else refresh();
  } else {
    box.appendChild(C.el('strong', { text: 'Tunnel closed. ' }));
    box.appendChild(document.createTextNode(
      'What you see is the last window fetched. Reopen with Cmd-Space → “sigen”, '
      + 'then reload.'));
    $('close-tunnel').hidden = true;
  }
}

function setLive(on) {
  state.live = on;
  $('live').checked = on;
  writeHash();
  schedule();
}

function pan(fraction) {
  const span = windowSpanS();
  // Anchor on the window actually on screen, not on the newest record: with a
  // stalled logger those differ, and panning from the record would jump further
  // than the arrow key implies.
  const end = state.end || (state.win ? state.win.end
                                      : (state.meta.extent.last_ts || Date.now() / 1000));
  let next = end + fraction * span;
  const ex = state.meta.extent;
  const newest = ex.last_ts || Date.now() / 1000;
  if (next >= newest) { state.end = null; setLive(true); }
  else {
    state.end = Math.max((ex.first_ts || 0) + span, next);
    setLive(false);
  }
  refresh();
}

function zoom(factor) {
  if (state.hours === null) { state.hours = 168; }
  state.hours = Math.max(0.05, Math.min(24 * 400, state.hours * factor));
  markPresets();
  refresh();
}

function schedule() {
  if (state.timer) clearInterval(state.timer);
  state.timer = null;
  if (state.live && !FROZEN) state.timer = setInterval(() => refresh(true), 10000);
}

function debounce(fn, ms) {
  let h = null;
  return (...a) => { clearTimeout(h); h = setTimeout(() => fn(...a), ms); };
}

// --------------------------------------------------------------------- fetching

function windowURL(extra) {
  const q = new URLSearchParams();
  if (state.hours === null) {
    const ex = state.meta.extent;
    q.set('start', String(Math.floor(ex.first_ts || 0)));
    q.set('end', String(Math.ceil(ex.last_ts || 0) + 1));
  } else {
    q.set('hours', String(state.hours));
    if (state.end) q.set('end', String(Math.round(state.end)));
  }
  const panels = [...state.expanded];
  panels.push('energy');
  q.set('panels', panels.join(','));
  if (state.custom.length) q.set('fields', state.custom.join(','));
  for (const k in (extra || {})) q.set(k, extra[k]);
  return '?' + q.toString();
}

async function refresh(quiet) {
  // Nothing to poll once the tunnel is deliberately gone; without this the page
  // would fill the filter row with fetch failures the user just asked for.
  if (!state.meta || state.disconnected) return;
  writeHash();
  const cards = document.querySelectorAll('.card');
  if (!quiet) cards.forEach((c) => c.classList.add('loading'));
  const url = '/api/window' + windowURL();
  const token = {};
  state.inflight = token;
  try {
    const [win, latest] = await Promise.all([
      getJSON(url),
      getJSON('/api/latest').catch(() => null),
    ]);
    if (state.inflight !== token) return;    // a newer request already landed
    state.win = win;
    if (latest) state.latest = latest;
    // Merge, don't replace: the window carries only first_ts/last_ts, while
    // /api/meta's extent also holds the file count and size the footer reports.
    if (win.extent) Object.assign(state.meta.extent, win.extent);
    renderAll();
  } catch (e) {
    const chip = $('chip-warming');
    chip.hidden = false;
    chip.textContent = 'reload failed: ' + e.message;
  } finally {
    cards.forEach((c) => c.classList.remove('loading'));
  }
}

// -------------------------------------------------------------------- rendering

function renderAll() {
  const win = state.win;
  $('chip-bucket').hidden = false;
  $('chip-bucket').textContent =
    fmtDuration(win.bucket_s) + ' buckets · ' + win.t.length + ' points · ' +
    win.records.toLocaleString() + ' records';
  const stride = $('chip-stride');
  stride.hidden = win.stride <= 1;
  stride.textContent = 'sampled 1 record in ' + win.stride + ' (min/max may miss a spike)';
  const warm = $('chip-warming');
  if (win.pending_files) {
    warm.hidden = false;
    // A view shared while the server was still reading cold files is permanently
    // partial: there is nothing left to re-ask, so it says that instead of promising
    // a reload that will never come.
    warm.textContent = 'partial: ' + win.pending_files + ' file(s) still being read — ' +
                       (FROZEN ? 'this view was shared before they finished'
                               : 'reloading shortly');
    if (!FROZEN) setTimeout(() => refresh(true), 3000);
  } else if (win.unreadable && win.unreadable.length) {
    // Usually keep_days pruning a file mid-request. Say which, rather than
    // showing a window that is quietly missing a chunk.
    warm.hidden = false;
    warm.textContent = win.unreadable.length + ' file(s) could not be read: ' +
                       win.unreadable.map((u) => u.file).join(', ');
  } else {
    warm.hidden = true;
  }
  if (!FROZEN) {
    $('csv').href = (SRC.base || '') + '/api/csv' + windowURL();
    $('endat').value = C.toLocalInput(win.end, win.tz);
  }

  if (FROZEN) renderFrozenBanner();
  renderShareSummary();
  renderPickerAvailability();
  renderFreshness();
  renderLiveTiles();
  renderEnergy();
  renderPanels();
  renderCustom();
  renderFooter();
}

function renderProvenance() {
  const m = state.meta;
  const dev = m.device || {};
  const bits = [dev.model || 'unknown', 'plan ' + m.plan_hash,
                m.fast_period_s + ' s fast tier',
                m.extent.files + ' files',
                (m.extent.bytes / 1e6).toFixed(1) + ' MB'];
  $('provenance').textContent = bits.join(' · ');
}

function renderFreshness() {
  const host = $('freshness');
  const l = state.latest;
  C.clear(host);
  if (!l) return;
  const alarms = Object.keys(l.alarms || {});
  // In a shared view every age was measured when it was shared, not now. Saying
  // "data 12 s old" in a page read a week later is the one lie this page must not
  // tell, so the ages carry when they were taken.
  const when = FROZEN ? ' when shared' : '';
  let cls = 'ok', text = '';
  if (!l.ok) {
    cls = 'bad';
    text = 'no data in this archive';
  } else if (l.logger_stalled) {
    // Only a genuinely absent record means the capture itself stopped -- and on a tiles
    // source that inference is not available. There, `record_ts` is the newest record that
    // reached the bucket, which sync.py only ever advances on rotation, so a stopped
    // uploader and a stopped logger produce the identical symptom. This page said "the
    // logger may have stopped" over an archive whose logger had never missed a tick and
    // whose uploader was not installed. Name both, or name neither (FINDINGS 29).
    cls = 'bad';
    text = 'no record for ' + fmtAge(l.record_age_s) + when +
           (SRC.kind === 'server'
             ? ' — the logger may have stopped'
             : ' — the logger or the uploader may have stopped');
  } else if (!l.device_answering) {
    // The logger is fine; the device is not. Keeping these apart is the whole
    // point (FINDINGS 7).
    cls = 'serious';
    text = 'device not answering for ' + fmtAge(l.data_age_s) + when +
           ' — logger healthy, still probing';
  } else {
    text = 'data ' + fmtAge(l.data_age_s) + ' old' + when;
  }
  host.appendChild(C.el('span', { class: 'pill ' + cls }, [
    C.el('span', { class: 'dot' }), document.createTextNode(text)]));
  if (alarms.length) {
    const words = alarms.map((k) => l.alarms[k].join('; ')).join(' · ');
    host.appendChild(C.el('span', { class: 'pill bad' }, [
      C.el('span', { class: 'dot' }),
      document.createTextNode('⚠ alarm: ' + words)]));
  }
  if (l.values && l.values.plant_system_time && l.labels.plant_system_time) {
    host.appendChild(C.el('span', { class: 'pill', title: 'the inverter\'s own clock' },
                          [document.createTextNode('device clock ' +
                                                   l.labels.plant_system_time)]));
  }
}

const TILES = [
  { key: 'plant_sigen_photovoltaic_power', label: 'PV', slot: 4, dec: 3, unit: 'kW' },
  { key: 'plant_general_load_power', label: 'Load', slot: 2, dec: 3, unit: 'kW' },
  { key: 'plant_ess_power', label: 'Battery', slot: 3, dec: 3, unit: 'kW',
    sign: ['charging', 'discharging'] },
  { key: 'plant_grid_sensor_active_power', label: 'Grid', slot: 1, dec: 3, unit: 'kW',
    sign: ['importing', 'exporting'] },
  { key: 'plant_ess_soc', label: 'State of charge', slot: 3, dec: 1, unit: '%' },
];

function renderLiveTiles() {
  const host = $('live-tiles');
  const l = state.latest, win = state.win;
  C.clear(host);
  if (!l || !l.values) return;
  if (!Object.keys(l.values).length) {
    // The record was found but no field resolved out of it. Seen once and not
    // reproduced; say so rather than showing a row of bare dashes, which reads as
    // "the inverter is idle" when it means "ask why this is empty".
    host.appendChild(C.el('div', { class: 'tile', style: { gridColumn: '1 / -1' } }, [
      C.el('div', { class: 'label', text: 'Latest values unavailable' }),
      C.el('div', { class: 'meta', text:
        'The newest record was read, but none of its fields decoded' +
        (l.missing && l.missing.length ? ' (' + l.missing.length + ' unresolved)' : '') +
        '. The charts below are unaffected. Reload; if it persists, run ' +
        'python3 serve.py --check on the capture host.' }),
    ]));
    return;
  }
  const stale = !l.device_answering || l.logger_stalled;
  for (const t of TILES) {
    const v = l.values[t.key];
    const color = C.slotColor(t.slot);
    const meta = [];
    if (t.sign && typeof v === 'number' && v !== 0) meta.push(v > 0 ? t.sign[0] : t.sign[1]);
    if (t.key === 'plant_ess_soc' && l.values.plant_ess_soh !== undefined) {
      meta.push('SOH ' + C.fmtVal(l.values.plant_ess_soh, 1) + '%');
    }
    if (stale) meta.push('last known good');
    const tile = C.el('div', { class: 'tile' }, [
      C.el('div', { class: 'label' }, [
        C.el('span', { class: 'key', style: { background: color } }),
        document.createTextNode(t.label)]),
      C.el('div', { class: 'value' }, [
        document.createTextNode(v === null || v === undefined ? '—' : C.fmtVal(v, t.dec)),
        C.el('span', { class: 'unit', text: ' ' + t.unit })]),
      C.el('div', { class: 'meta', text: meta.join(' · ') }),
    ]);
    const ser = win && win.series[t.key];
    if (ser && ser.mean) tile.appendChild(C.sparkline(ser.mean, 150, 26, color));
    host.appendChild(tile);
  }
  // State, as words rather than a colour: three enums in one tile.
  const words = ['plant_running_state', 'plant_on_off_grid_status', 'plant_ems_work_mode']
    .map((k) => l.labels[k]).filter(Boolean);
  host.appendChild(C.el('div', { class: 'tile' }, [
    C.el('div', { class: 'label', text: 'Plant state' }),
    C.el('div', { class: 'value', style: { fontSize: '1rem' }, text: words[0] || '—' }),
    C.el('div', { class: 'meta', text: words.slice(1).join(' · ') }),
  ]));
}

function renderEnergy() {
  const host = $('energy-tiles');
  const e = (state.win && state.win.energy) || {};
  C.clear(host);
  let any = false;
  for (const t of state.meta.energy_tiles) {
    const v = e[t.key];
    if (!v) continue;
    any = true;
    const meta = [];
    if (v.reset) {
      // A counter that went backwards across a power cut, not corruption
      // (FINDINGS 11). Say so instead of showing a negative total.
      meta.push('counter stepped back in this window — total is a floor');
    }
    host.appendChild(C.el('div', { class: 'tile' }, [
      C.el('div', { class: 'label', text: t.label }),
      C.el('div', { class: 'value' }, [
        document.createTextNode(C.fmtVal(v.kwh, 2)),
        C.el('span', { class: 'unit', text: ' kWh' })]),
      C.el('div', { class: 'meta', text: meta.join(' ') }),
    ]));
  }
  if (e.self_consumption !== undefined) {
    host.appendChild(C.el('div', { class: 'tile' }, [
      C.el('div', { class: 'label', text: 'Self-consumed' }),
      C.el('div', { class: 'value' }, [
        document.createTextNode((e.self_consumption * 100).toFixed(1)),
        C.el('span', { class: 'unit', text: ' %' })]),
      C.el('div', { class: 'meta', text: 'of PV generated in this window' }),
    ]));
  }
  $('energy-card').hidden = !any;
}

// -- panels

function buildPanelCards() {
  const host = $('panels');
  C.clear(host);
  state.panelCards.clear();
  for (const p of state.meta.panels) {
    const open = state.expanded.has(p.id);
    const chartHost = C.el('div', { class: 'chart-host' });
    const legend = C.el('div', { class: 'legend' });
    const foot = C.el('div', { class: 'card-foot' });
    const toggle = C.el('button', {
      text: open ? 'Hide' : 'Show', 'aria-expanded': String(open),
      onclick: () => togglePanel(p.id),
    });
    const tableBtn = C.el('button', {
      text: 'Table', 'aria-pressed': String(state.tables.has(p.id)),
      onclick: () => {
        if (state.tables.has(p.id)) state.tables.delete(p.id);
        else state.tables.add(p.id);
        renderPanels();
      },
    });
    const tableHost = C.el('div', { class: 'table-wrap' });
    const card = C.el('section', { class: 'card', id: 'panel-' + p.id }, [
      C.el('div', { class: 'card-head' }, [
        C.el('h2', { text: p.title + (p.unit ? '  (' + p.unit + ')' : '') }),
        C.el('div', { class: 'spacer' }),
        tableBtn, toggle,
      ]),
      p.note ? C.el('p', { class: 'note', text: p.note }) : null,
      legend, chartHost, tableHost, foot,
    ]);
    host.appendChild(card);
    state.panelCards.set(p.id, { card, chartHost, legend, foot, toggle, tableBtn,
                                 tableHost, panel: p });
  }
}

function togglePanel(id) {
  if (state.expanded.has(id)) state.expanded.delete(id);
  else state.expanded.add(id);
  const c = state.panelCards.get(id);
  c.toggle.textContent = state.expanded.has(id) ? 'Hide' : 'Show';
  c.toggle.setAttribute('aria-expanded', String(state.expanded.has(id)));
  refresh();
}

function regionsFor(win) {
  const out = [];
  for (const [a, b] of (win.health.no_data || [])) {
    out.push({ from: a, to: b, pattern: 'hatch-nodata', label: 'device not answering' });
  }
  for (const [a, b] of (win.health.no_records || [])) {
    out.push({ from: a, to: b, pattern: 'hatch-norecords', label: 'no records' });
  }
  return out;
}

function decimalsOf(key) {
  const meta = catalogOf(key);
  const gain = meta ? (meta.gain || 1) : 1;
  return gain > 1 ? String(gain).length - 1 : 1;
}

let _catalog = null;
function catalogOf(key) {
  if (!_catalog) {
    _catalog = new Map();
    for (const c of state.meta.catalog) _catalog.set(c.key, c);
  }
  return _catalog.get(key);
}

function renderPanels() {
  state.group.reset();
  state.charts = [];
  const win = state.win;
  for (const [id, c] of state.panelCards) {
    const open = state.expanded.has(id);
    c.chartHost.hidden = !open;
    c.tableHost.hidden = !open || !state.tables.has(id);
    c.legend.hidden = !open;
    c.tableBtn.hidden = !open;
    c.tableBtn.setAttribute('aria-pressed', String(state.tables.has(id)));
    C.clear(c.chartHost);
    C.clear(c.legend);
    C.clear(c.foot);
    C.clear(c.tableHost);
    if (!open) continue;
    if (c.panel.kind === 'state') renderStatePanel(c, win);
    else if (c.panel.kind === 'latency') renderLatencyPanel(c, win);
    else renderLinePanel(c, win);
  }
  renderRegionLegend();
}

function renderRegionLegend() {
  const win = state.win;
  const rs = regionsFor(win);
  for (const [, c] of state.panelCards) {
    if (c.chartHost.hidden) continue;
    if (rs.length) {
      const seen = new Set();
      for (const r of rs) {
        if (seen.has(r.label)) continue;
        seen.add(r.label);
        c.foot.appendChild(C.el('span', { class: 'legend' }, [
          C.el('span', { class: 'item' }, [
            C.el('span', {
              class: 'swatch box ' + (r.pattern === 'hatch-nodata' ? 'key-nodata'
                                                                  : 'key-norecords'),
            }),
            document.createTextNode(r.label === 'no records'
              ? 'no records at all — logger stopped, or files moved/pruned'
              : 'records written but no blocks returned — device not answering'),
          ]),
        ]));
      }
    }
  }
}

function seriesSpecs(panel, win) {
  const out = [];
  for (const s of panel.series || []) {
    const col = win.series[s.key];
    if (!col || col.empty || col.tile_only || !col.mean) continue;
    out.push({
      key: s.key, label: s.label,
      color: C.slotColor(s.slot || 1, !!panel.ramp),
      mean: col.mean, min: col.min, max: col.max,
      cadence_s: col.cadence_s, unit: col.unit,
      dec: decimalsOf(s.key),
    });
  }
  return out;
}

function renderLinePanel(c, win) {
  const panel = c.panel;
  const series = seriesSpecs(panel, win);
  if (!series.length) {
    c.chartHost.appendChild(C.el('p', { class: 'note', text:
      'Nothing in this window: every sample of ' +
      (panel.series || []).map((s) => s.key).join(', ') +
      ' was absent, a sentinel, or the -1 not-present marker.' }));
    return;
  }
  const aggregated = win.bucket_s > (series[0].cadence_s || 1);
  const chart = new C.LineChart(c.chartHost, {
    t: win.t, bucket_s: win.bucket_s, tz: win.tz, unit: panel.unit,
    series: series, domain: panel.domain, zero: panel.zero,
    height: panel.height || 200, regions: regionsFor(win),
    title: panel.title, band: true,
  });
  chart.render();
  state.charts.push(chart);
  state.group.add(chart, (i) => ({
    when: C.fmtTime(win.t[i], win.tz, 'full') + ' ' + C.zoneName(win.tz),
    rows: series.map((s) => ({
      name: s.label, color: s.color,
      value: C.fmtVal(s.mean[i], s.dec) + (panel.unit ? ' ' + panel.unit : ''),
      range: (aggregated && s.min && s.min[i] !== null && s.min[i] !== undefined)
        ? C.fmtVal(s.min[i], s.dec) + '–' + C.fmtVal(s.max[i], s.dec) : '',
    })),
    foot: bucketFoot(win, i, aggregated),
  }));
  // Legend: always present at two or more series, mirroring the mark (a line).
  if (series.length >= 2) {
    for (const s of series) {
      c.legend.appendChild(C.el('span', { class: 'item' }, [
        C.el('span', { class: 'swatch', style: { background: s.color } }),
        document.createTextNode(s.label)]));
    }
  }
  if (aggregated) {
    c.foot.appendChild(C.el('span', {
      text: series.length <= 2
        ? 'line = bucket mean, shaded = bucket min–max'
        : 'line = bucket mean; min–max per point is in the tooltip and the table',
    }));
  }
  if (state.tables.has(panel.id)) renderTable(c, win, series, panel);
}

function bucketFoot(win, i, aggregated) {
  const n = win.health.records[i] || 0;
  const empty = win.health.empty[i] || 0;
  if (!n) return 'no records in this bucket';
  const bits = [n + (n === 1 ? ' record' : ' records') + ' in bucket'];
  if (empty) bits.push(empty + ' with no data');
  if (win.stride > 1) bits.push('1 in ' + win.stride + ' decoded');
  return bits.join(' · ');
}

function renderLatencyPanel(c, win) {
  const h = win.health;
  // A source that does not carry this panel drops the latency arrays, so read them
  // defensively: an absent series is "no latency here", not a crash.
  const has = (h.latency_median || []).some((v) => v !== null && v !== undefined);
  if (!has) {
    c.chartHost.appendChild(C.el('p', { class: 'note',
      text: 'No records returned data in this window.' }));
    return;
  }
  // One measure at three quantiles is magnitude, not identity: one hue, darker
  // for higher.
  const series = [
    { label: 'median', color: C.slotColor(2, true), mean: h.latency_median, dec: 0 },
    { label: 'p95', color: C.slotColor(3, true), mean: h.latency_p95, dec: 0 },
    { label: 'max', color: C.slotColor(4, true), mean: h.latency_max, dec: 0 },
  ].map((s) => Object.assign(s, { cadence_s: win.bucket_s, min: null, max: null }));
  const chart = new C.LineChart(c.chartHost, {
    t: win.t, bucket_s: win.bucket_s, tz: win.tz, unit: 'ms', series: series,
    height: c.panel.height || 170, regions: regionsFor(win), band: false,
    title: 'Capture health',
  });
  chart.render();
  state.charts.push(chart);
  state.group.add(chart, (i) => ({
    when: C.fmtTime(win.t[i], win.tz, 'full') + ' ' + C.zoneName(win.tz),
    rows: series.map((s) => ({ name: s.label, color: s.color,
                               value: C.fmtVal(s.mean[i], 0) + ' ms' }))
      .concat([{ name: 'records', value: String(win.health.records[i] || 0) },
               { name: 'empty probes', value: String(win.health.empty[i] || 0) }]),
    foot: h.note,
  }));
  for (const s of series) {
    c.legend.appendChild(C.el('span', { class: 'item' }, [
      C.el('span', { class: 'swatch', style: { background: s.color } }),
      document.createTextNode(s.label)]));
  }
  c.foot.appendChild(C.el('span', { text: h.note }));
}

function renderStatePanel(c, win) {
  const panel = c.panel;
  const rows = [];
  for (const s of panel.series || []) {
    const col = win.series[s.key];
    if (!col || !col.last) continue;
    const meta = catalogOf(s.key) || {};
    const enums = meta.enum || {};
    const codes = [...new Set(col.last.filter((v) => v !== null && v !== undefined))].sort();
    const colorOf = (v) => {
      const label = enums[String(v)] || '';
      if (/FAULT|ABNORMAL|offgrid/i.test(label)) return C.palette().critical;
      if (/STANDBY|SHUTDOWN/i.test(label)) return C.palette().warning;
      if (/RUNNING|ongrid/i.test(label)) return C.palette().good;
      return C.slotColor(1 + (codes.indexOf(v) % 4));
    };
    rows.push({
      name: s.label, short: s.short || s.label, values: col.last,
      mixed: col.last.map((v, i) => col.min && col.min[i] !== col.max[i]),
      colorOf: colorOf,
      labelOf: (v) => enums[String(v)] || ('code ' + v),
    });
  }
  if (!rows.length) {
    c.chartHost.appendChild(C.el('p', { class: 'note', text: 'No state samples here.' }));
  } else {
    const chart = new C.BandChart(c.chartHost, {
      t: win.t, bucket_s: win.bucket_s, tz: win.tz, rows: rows,
    });
    chart.render();
    state.charts.push(chart);
    state.group.add(chart, (i) => ({
      when: C.fmtTime(win.t[i], win.tz, 'full') + ' ' + C.zoneName(win.tz),
      rows: rows.map((r) => ({
        name: r.name,
        value: (r.values[i] === null || r.values[i] === undefined)
          ? '—' : r.labelOf(r.values[i]),
        range: r.mixed[i] ? 'changed in bucket' : '',
      })),
      foot: bucketFoot(win, i, true),
    }));
    // Bands carry an icon-and-word legend: a colour never states the state alone.
    for (const r of rows) {
      const seen = new Set();
      for (const v of r.values) {
        if (v === null || v === undefined || seen.has(v)) continue;
        seen.add(v);
        c.legend.appendChild(C.el('span', { class: 'item' }, [
          C.el('span', { class: 'swatch box', style: { background: r.colorOf(v) } }),
          document.createTextNode(r.labelOf(v))]));
      }
    }
  }
  // Alarms: words, not colours. Any bit set anywhere in the window is listed.
  const fired = [];
  for (const key of (panel.alarms || [])) {
    const col = win.series[key];
    if (!col || !col.bits) continue;
    let or = 0;
    for (const b of col.bits) or |= (b || 0);
    if (or) fired.push({ key: key, raw: or });
  }
  const box = C.el('div', { class: 'bands' });
  if (fired.length) {
    for (const f of fired) {
      box.appendChild(C.el('div', { class: 'alarm' }, [
        C.el('span', { class: 'icon', text: '⚠' }),
        C.el('span', { text: f.key + ': raw ' + f.raw + ' — see bin/latest.sh or ' +
                             'decode.py for the bit names' }),
      ]));
    }
  } else {
    box.appendChild(C.el('div', { class: 'alarm clear' }, [
      C.el('span', { class: 'icon', text: '✓' }),
      C.el('span', { text: 'all six alarm words clear for the whole window' }),
    ]));
  }
  c.foot.appendChild(box);
}

const TABLE_ROWS = 200;

function renderTable(c, win, series, panel) {
  const head = ['time'];
  for (const s of series) head.push(s.label + ' mean', 'min', 'max');
  const rows = [];
  for (let i = win.t.length - 1; i >= 0 && rows.length < TABLE_ROWS; i--) {
    if (series.every((s) => s.mean[i] === null || s.mean[i] === undefined)) continue;
    const cells = [C.fmtTime(win.t[i], win.tz, 'full')];
    for (const s of series) {
      cells.push(C.fmtVal(s.mean[i], s.dec), C.fmtVal(s.min ? s.min[i] : null, s.dec),
                 C.fmtVal(s.max ? s.max[i] : null, s.dec));
    }
    rows.push(cells);
  }
  const table = C.el('table', { class: 'data' }, [
    C.el('thead', {}, [C.el('tr', {}, head.map((h) => C.el('th', { text: h, scope: 'col' })))]),
    C.el('tbody', {}, rows.map((r) => C.el('tr', {}, r.map((v) => C.el('td', { text: v }))))),
  ]);
  c.tableHost.appendChild(table);
  c.tableHost.hidden = false;
  c.foot.appendChild(C.el('span', {
    text: 'table shows the ' + rows.length + ' most recent non-empty buckets, newest ' +
          'first; download the CSV for the whole window',
  }));
}

// -- custom charts

function buildPicker() {
  // Nothing to pick from in a frozen view: it carries the fields it was shared with
  // and there is nothing to fetch another from. applyFrozenMode() hides the controls;
  // the charts for the fields already chosen are rendered by renderCustom().
  if (FROZEN) return;
  const search = $('field-search');
  const render = () => renderResults(search.value);
  search.addEventListener('input', debounce(render, 120));
  $('show-dupes').addEventListener('change', () => { state.showDupes = $('show-dupes').checked; render(); });
  $('field-count').textContent = state.meta.catalog.length + ' fields captured';
  renderChosen();
}

// Whether the current window can answer for an arbitrary register.
//
// On this server, always: it decodes the archive and every captured field is available at
// every width. A tile source materialises the whole catalogue only at the coarser widths,
// because carrying 240 fields at a 30 s bucket would make a tile larger than the raw
// archive it came from. picker_min_bucket_s is where that starts, and it comes from meta
// rather than being hardcoded here, so widening a tile widens the picker with no change to
// this file.
//
// The point of saying so is that the alternative is worse than a disabled control: the
// field would be accepted, come back absent, and read as "this register is always zero".
function pickerAvailable() {
  const min = state.meta.picker_min_bucket_s;
  if (!min) return true;
  return !!state.win && state.win.bucket_s >= min;
}

function renderPickerAvailability() {
  const min = state.meta.picker_min_bucket_s;
  if (!min || FROZEN) return;
  const ok = pickerAvailable();
  const search = $('field-search');
  search.disabled = !ok;
  const note = $('field-count');
  if (ok) {
    note.textContent = state.meta.catalog.length + ' fields captured';
    return;
  }
  // Say what to do, and be specific about the number: the threshold is a real
  // consequence of the bucket ladder, not a round figure.
  const needS = pickerNeedsSpanS();
  note.textContent = state.meta.catalog.length + ' fields captured — but only the '
    + (state.meta.fine_fields || []).length + ' the panels use are stored at '
    + fmtDuration(state.win ? state.win.bucket_s : 0) + ' resolution.'
    + (needS ? ' Widen the window past ' + fmtDuration(needS)
               + ' to chart any of the others.' : '');
}

// The span at which choose_bucket() first returns picker_min_bucket_s.
//
// NOT min * target_buckets, which is what this used to compute. choose_bucket rounds UP to
// the next ladder width, so `min` is chosen as soon as span/target exceeds the width BELOW
// it -- for a 120 s minimum on this ladder, once span passes 60 * 900 = 54,000 s = 15 h.
// The old formula gave 120 * 900 = 108,000 s = 30 h, which is where the ladder has already
// moved on to the next width up, so the page demanded twice the window it needed.
//
// Both inputs come from meta rather than being restated here, so retuning the ladder or
// TARGET_BUCKETS moves this number with no change to this file.
function pickerNeedsSpanS() {
  const min = state.meta.picker_min_bucket_s;
  const ladder = state.meta.bucket_ladder || [];
  const target = state.meta.target_buckets;
  const i = ladder.indexOf(min);
  // i === 0 would mean the picker works at the finest width there is, so this branch is
  // unreachable; 0 says "no threshold to quote" rather than inventing one.
  if (!min || !target || i <= 0) return 0;
  return ladder[i - 1] * target;
}

function renderResults(term) {
  const host = $('field-results');
  C.clear(host);
  if (!pickerAvailable()) return;
  const q = (term || '').trim().toLowerCase();
  if (!q) return;
  let n = 0;
  for (const f of state.meta.catalog) {
    if (f.duplicate_of && !state.showDupes) continue;
    const hay = f.key + ' ' + (f.desc || '') + ' ' + f.addr + ' ' + (f.unit || '');
    if (!hay.toLowerCase().includes(q)) continue;
    if (++n > 40) break;
    const meta = [f.unit || 'no unit', f.dtype, 'reg ' + f.addr,
                  'every ' + fmtDuration(f.cadence_s)].join(' · ');
    host.appendChild(C.el('li', {
      role: 'button', tabindex: '0',
      onclick: () => addField(f.key),
      onkeydown: (ev) => { if (ev.key === 'Enter' || ev.key === ' ') addField(f.key); },
    }, [
      C.el('span', { class: 'fkey', text: f.key }),
      C.el('span', { class: 'fmeta', text: meta }),
      f.duplicate_of
        ? C.el('span', { class: 'fmeta dupe', text: 'same register as ' + f.duplicate_of })
        : null,
      C.el('span', { class: 'fmeta', text: (f.desc || '').slice(0, 70) }),
    ]));
  }
  if (!n) host.appendChild(C.el('li', {}, [C.el('span', { class: 'fmeta',
    text: 'nothing matches — the picker only lists fields the capture blocks cover' })]));
}

function addField(key) {
  if (state.custom.includes(key)) return;
  if (state.custom.length >= 8) state.custom.shift();
  state.custom.push(key);
  renderChosen();
  refresh();
}

function renderChosen() {
  const host = $('field-chosen');
  C.clear(host);
  for (const key of state.custom) {
    host.appendChild(C.el('span', { class: 'tag' }, [
      document.createTextNode(key),
      C.el('button', { text: '×', title: 'remove', 'aria-label': 'remove ' + key,
                       onclick: () => { state.custom = state.custom.filter((k) => k !== key);
                                        renderChosen(); refresh(); } }),
    ]));
  }
}

function renderCustom() {
  const card = $('custom-card');
  let holder = $('custom-charts');
  if (!holder) {
    holder = C.el('div', { id: 'custom-charts' });
    card.appendChild(holder);
  }
  C.clear(holder);
  if (!state.custom.length) return;
  const win = state.win;
  // Group by unit. Two units on one chart would mean two y-scales, so they become
  // two charts instead.
  const groups = new Map();
  state.custom.forEach((key, i) => {
    const col = win.series[key];
    if (!col) return;
    const unit = col.unit || '(no unit)';
    if (!groups.has(unit)) groups.set(unit, []);
    groups.get(unit).push({ key: key, col: col, slot: groups.get(unit).length + 1 });
  });
  for (const [unit, items] of groups) {
    const host = C.el('div', {});
    const legend = C.el('div', { class: 'legend' });
    const foot = C.el('div', { class: 'card-foot' });
    holder.appendChild(C.el('div', { class: 'card' }, [
      C.el('div', { class: 'card-head' }, [C.el('h2', { text: 'Custom · ' + unit })]),
      legend, host, foot]));
    const series = [];
    for (const it of items) {
      if (!it.col.mean) {
        foot.appendChild(C.el('span', {
          text: it.key + ': ' + (it.col.empty ? 'no samples in this window'
            : 'shown as a state or alarm field, not a line — see the panel above') }));
        continue;
      }
      series.push({
        key: it.key, label: it.key, color: C.slotColor(it.slot),
        mean: it.col.mean, min: it.col.min, max: it.col.max,
        cadence_s: it.col.cadence_s, dec: decimalsOf(it.key),
      });
    }
    if (!series.length) continue;
    const chart = new C.LineChart(host, {
      t: win.t, bucket_s: win.bucket_s, tz: win.tz, unit: unit === '(no unit)' ? '' : unit,
      series: series, height: 190, regions: regionsFor(win),
    });
    chart.render();
    state.charts.push(chart);
    state.group.add(chart, (i) => ({
      when: C.fmtTime(win.t[i], win.tz, 'full') + ' ' + C.zoneName(win.tz),
      rows: series.map((s) => ({
        name: s.label, color: s.color, value: C.fmtVal(s.mean[i], s.dec),
        range: (s.min && s.min[i] !== null && s.min[i] !== undefined)
          ? C.fmtVal(s.min[i], s.dec) + '–' + C.fmtVal(s.max[i], s.dec) : '',
      })),
      foot: bucketFoot(win, i, true),
    }));
    if (series.length >= 2) {
      for (const s of series) {
        legend.appendChild(C.el('span', { class: 'item' }, [
          C.el('span', { class: 'swatch', style: { background: s.color } }),
          document.createTextNode(s.label)]));
      }
    }
  }
}

function renderCharts() {
  for (const ch of state.charts) ch.render();
}

function renderFooter() {
  const m = state.meta, win = state.win;
  const host = $('footer');
  C.clear(host);
  const first = C.fmtTime(m.extent.first_ts, win.tz, 'full');
  const last = C.fmtTime(m.extent.last_ts, win.tz, 'full');
  host.appendChild(C.el('p', { text:
    'Archive ' + first + ' → ' + last + ' ' + C.zoneName(win.tz) +
    ' · plan ' + m.plan_hash + ' · manifest ' + m.manifest +
    ' · ' + m.extent.files + ' files in ' + m.data_dir }));
  host.appendChild(C.el('p', { text:
    'Window read ' + win.files_read + ' of ' + win.files + ' files in ' + win.took_ms +
    ' ms. Blocks: ' + m.blocks.map((b) => b.label + ' (' + b.addr + '+' + b.count +
    ', every ' + fmtDuration(b.period_s) + ')').join(', ') }));
  host.appendChild(C.el('p', { text:
    'Read-only: this page decodes bytes already on disk and never polls the ' +
    'inverter. Times are the capture host\'s local clock.' +
    (m.device && m.device.withheld ? ' Identity withheld: ' + m.device.withheld + '.' : '') }));
  if (FROZEN) {
    host.appendChild(C.el('p', { text:
      'Shared ' + C.fmtTime(state.meta.shared_at, win.tz, 'full') + ' ' +
      C.zoneName(win.tz) + ', holding the ' + m.panels.length +
      (m.panels.length === 1 ? ' panel' : ' panels') + ' and ' +
      Object.keys(win.series).length + ' fields that were on screen. The archive it ' +
      'came from covers far more fields and a longer history; this view is a fixed ' +
      'copy and cannot be refreshed, so anything it does not contain needs a new ' +
      'share from the viewer.' }));
  }
  if (m.device && m.device.why) {
    host.appendChild(C.el('p', { text: 'Device model unknown: ' + m.device.why }));
  }
}

// ------------------------------------------------------------------- formatting

function fmtDuration(s) {
  if (s === null || s === undefined) return '—';
  if (s < 60) return s + ' s';
  if (s < 3600) return (s / 60) % 1 === 0 ? (s / 60) + ' min' : (s / 60).toFixed(1) + ' min';
  if (s < 86400) return (s / 3600) % 1 === 0 ? (s / 3600) + ' h' : (s / 3600).toFixed(1) + ' h';
  return (s / 86400).toFixed(s % 86400 ? 1 : 0) + ' d';
}

function fmtAge(s) {
  if (s === null || s === undefined) return 'unknown';
  if (s < 90) return Math.round(s) + ' s';
  if (s < 5400) return Math.round(s / 60) + ' min';
  if (s < 172800) return (s / 3600).toFixed(1) + ' h';
  return (s / 86400).toFixed(1) + ' d';
}

boot();
