'use strict';
/*
 * /devices: log when a device changes state, against the live whole-house load.
 *
 * What one tap records, and why each:
 *   tPhone       the phone's clock at the tap -- the moment you meant
 *   off, rtt     the phone's offset from the server and how sure of it we are (NTP-style,
 *   syncAge      from /api/tracker/time round trips); off + tPhone is the server-time estimate
 *   sent         the phone's clock when it was actually sent (an outbox retry can be minutes on)
 *   piT, piLat   the newest point the Pi's stream had delivered, and its tick latency --
 *   piSeenPhone  and when it arrived by the phone's clock, which ties the tap to the Pi's own
 *                timeline independently of the server
 * The server adds when the gateway received it (tGw) and computes tEst once. Editing a time
 * later sets tEdit beside them; nothing recorded at the tap is ever overwritten.
 *
 * Taps go through a localStorage outbox, so one made on a bad connection is kept and retried,
 * and the event id is generated here so a retry is the same event, not a second one.
 */
(function () {
  const C = Charts;
  const API = '/api/tracker/';
  const OUTBOX = 'sigen.tracker.outbox';
  const PICKED = 'sigen.tracker.device';
  const WINDOW_S = 300;
  const $ = (id) => document.getElementById(id);

  const clock = new Live.Clock();
  const store = new Live.Store(WINDOW_S);
  let tz = [];
  let devices = [];
  let current = localStorage.getItem(PICKED) || null;
  let events = [];          // the server's, newest first, for the current device
  let chart = null;
  let loadSpec = null;
  let lastSync = 0;
  let toastTimer = null;

  // ------------------------------------------------------------------ requests

  async function req(method, path, body) {
    const opts = { method, credentials: 'same-origin', cache: 'no-store' };
    if (body !== undefined) {
      opts.headers = { 'Content-Type': 'application/json' };
      opts.body = JSON.stringify(body);
    }
    let res;
    try { res = await fetch(API + path, opts); }
    catch (e) { return { status: 0, body: { ok: false, error: 'offline: ' + e.message } }; }
    let out = null;
    try { out = await res.json(); } catch (e) { out = null; }
    if (res.status === 401 && Live.recoverSession()) return new Promise(() => {});
    if (res.status === 403) notice('This account is not on the tracker\'s list.');
    return { status: res.status, body: out || { ok: false, error: res.status + ' (not JSON)' } };
  }

  function notice(text) {
    const n = $('notice');
    n.textContent = text || '';
    n.hidden = !text;
  }

  function status(state, detail) {
    document.body.dataset.state = state;
    $('status').className = 'pill ' + ({ live: 'ok', connecting: 'warn', reconnecting: 'warn',
                                         full: 'serious', error: 'bad' }[state] || '');
    $('status-text').textContent = state === 'live' ? 'live'
      : state === 'full' ? 'stream full' : state + (detail && detail.error ? ': ' + detail.error : '');
  }

  function randomId() {
    const a = new Uint8Array(12);
    crypto.getRandomValues(a);
    return 'e' + Array.from(a, (b) => 'abcdefghijklmnopqrstuvwxyz0123456789'[b % 36]).join('');
  }

  // --------------------------------------------------------------------- clock

  async function sync() {
    for (let i = 0; i < 6; i++) {
      const t0 = Date.now();
      const r = await req('GET', 'time');
      const t3 = Date.now();
      if (r.body.ok) clock.add(t0, r.body.recv, r.body.send, t3);
    }
    lastSync = Date.now();
    const ci = clock.info();
    $('clock-note').textContent = ci.rtt === null ? 'clock not synced'
      : 'this phone is ' + Math.abs(ci.offset) + ' ms ' + (ci.offset >= 0 ? 'behind' : 'ahead of') +
        ' the server (±' + Math.round(ci.rtt / 2) + ' ms)';
  }

  function syncInfo() {
    const ci = clock.info();
    return ci.rtt === null ? {} : { off: ci.offset, rtt: ci.rtt, syncAge: ci.age };
  }

  // ------------------------------------------------------------------- devices

  function dev() { return devices.find((d) => d.id === current) || null; }
  function stateName(d, sid) {
    const s = d && d.states.find((x) => x.id === sid);
    return s ? s.name : '(unknown state)';
  }

  async function loadDevices() {
    const r = await req('GET', 'devices');
    if (!r.body.ok) return notice('Could not load devices: ' + r.body.error);
    devices = r.body.devices;
    if (!dev()) current = (devices.find((d) => !d.archived) || {}).id || null;
    renderPicker();
    await loadEvents();
  }

  function renderPicker() {
    const sel = $('device');
    C.clear(sel);
    if (!devices.length) sel.appendChild(C.el('option', { value: '', text: 'No devices yet' }));
    for (const d of devices) {
      if (d.archived && d.id !== current) continue;
      sel.appendChild(C.el('option', { value: d.id, text: d.name + (d.archived ? ' (archived)' : ''),
                                       selected: d.id === current ? 'selected' : null }));
    }
    const d = dev();
    $('device-desc').textContent = d ? (d.desc || '') : 'Create a device to start.';
    $('rename-name').value = d ? d.name : '';
    $('rename-desc').value = d ? (d.desc || '') : '';
    $('manage').hidden = !d;
    renderStates();
    renderStateTable();
  }

  /** The state a device is in now: the newest event that is not undone, else its off state. */
  function currentState(d) {
    const all = [...pending().filter((e) => e.device === d.id), ...events]
      .filter((e) => !e.deleted)
      .sort((a, b) => timeOf(b) - timeOf(a));
    return all.length ? all[0].state : d.off;
  }

  function renderStates() {
    const box = $('states');
    C.clear(box);
    const d = dev();
    if (!d) return;
    const now = currentState(d);
    for (const s of d.states.filter((x) => !x.archived)) {
      box.appendChild(C.el('button', {
        type: 'button', class: s.id === d.off ? 'off' : '', 'aria-pressed': String(s.id === now),
        text: s.name, onclick: () => tap(s.id),
      }));
    }
  }

  function renderStateTable() {
    const body = $('state-table').querySelector('tbody');
    C.clear(body);
    const d = dev();
    if (!d) return;
    for (const s of d.states) {
      const name = C.el('input', { type: 'text', value: s.name, maxlength: 60, 'aria-label': 'State name' });
      body.appendChild(C.el('tr', { class: s.archived ? 'deleted' : '' }, [
        C.el('td', {}, [name]),
        C.el('td', { class: 'edit' }, [
          C.el('button', { type: 'button', text: 'Rename',
                           onclick: () => patchState(s.id, { name: name.value }) }),
          s.id === d.off ? C.el('span', { class: 'hint', text: 'off state' })
            : C.el('button', { type: 'button', text: 'Make off',
                               onclick: () => patchDevice({ off: s.id }) }),
          s.id === d.off ? null : C.el('button', {
            type: 'button', class: 'danger', text: s.archived ? 'Restore' : 'Archive',
            onclick: () => patchState(s.id, { archived: !s.archived }) }),
        ]),
      ]));
    }
  }

  async function patchDevice(fields) {
    const r = await req('PATCH', 'devices', Object.assign({ device: current }, fields));
    if (!r.body.ok) return notice(r.body.error);
    await loadDevices();
  }

  async function patchState(sid, fields) {
    const r = await req('PATCH', 'states', Object.assign({ device: current, state: sid }, fields));
    if (!r.body.ok) return notice(r.body.error);
    await loadDevices();
  }

  // -------------------------------------------------------------------- events

  function pending() {
    try { return JSON.parse(localStorage.getItem(OUTBOX) || '[]'); } catch (e) { return []; }
  }
  function setPending(list) { localStorage.setItem(OUTBOX, JSON.stringify(list)); }

  /** An event's time in server ms: a correction if there is one, else the best estimate. */
  function timeOf(e) {
    if (e.tEdit !== undefined && e.tEdit !== null) return e.tEdit;
    if (e.tEst !== undefined) return e.tEst;
    return e.tPhone + (e.off || 0);              // still in the outbox
  }

  function tap(sid) {
    const d = dev();
    if (!d) return;
    const latest = store.latest();
    const ev = Object.assign({
      id: randomId(), device: d.id, state: sid, tPhone: Date.now(),
    }, syncInfo(), latest ? { piT: latest.t, piSeenPhone: latest._seenPhone, piLat: latest.lat } : {});
    setPending([...pending(), ev]);
    toast('Logged “' + stateName(d, sid) + '” at ' + C.fmtTime(Math.round(timeOf(ev) / 1000), tz, 'hms'),
          ev);
    render();
    flush();
  }

  let flushing = false;
  async function flush() {
    if (flushing) return;
    flushing = true;
    try {
      for (const ev of pending()) {
        if (ev.sent === undefined) {
          ev.sent = Date.now();
          setPending(pending().map((x) => (x.id === ev.id ? ev : x)));
        }
        const r = await req('POST', 'events', ev);
        if (r.body.ok) {
          setPending(pending().filter((x) => x.id !== ev.id));
          if (ev.device === current) events = [r.body.event, ...events.filter((x) => x.id !== ev.id)];
        } else if (r.status >= 400 && r.status < 500 && r.status !== 401 && r.status !== 429) {
          // Refused for good (a state since archived, say): drop it, but say so.
          setPending(pending().filter((x) => x.id !== ev.id));
          notice('A logged change was refused: ' + r.body.error);
        } else {
          break;                                 // offline or throttled: keep it, retry later
        }
      }
    } finally {
      flushing = false;
      render();
    }
  }

  async function loadEvents() {
    if (!current) { events = []; return render(); }
    const r = await req('GET', 'events?device=' + encodeURIComponent(current) + '&limit=100');
    if (r.body.ok) events = r.body.events;
    render();
  }

  async function patchEvent(e, fields) {
    const r = await req('PATCH', 'events', Object.assign({ device: e.device, sk: e.sk }, fields));
    if (!r.body.ok) return notice(r.body.error);
    events = events.map((x) => (x.id === e.id ? r.body.event : x));
    render();
  }

  function render() {
    const d = dev();
    renderStates();
    const body = $('events').querySelector('tbody');
    C.clear(body);
    const rows = [...pending().filter((e) => d && e.device === d.id).map((e) => Object.assign({ _pending: true }, e)),
                  ...events].sort((a, b) => timeOf(b) - timeOf(a));
    for (const e of rows) {
      const t = timeOf(e);
      const when = C.fmtTime(Math.round(t / 1000), tz, 'hms') + '.' + String(Math.round(t) % 1000).padStart(3, '0');
      const edit = C.el('td', { class: 'edit' });
      if (!e._pending) {
        for (const dt of [-5000, -1000, 1000, 5000]) {
          edit.appendChild(C.el('button', { type: 'button', text: (dt > 0 ? '+' : '−') + Math.abs(dt / 1000) + 's',
                                            onclick: () => patchEvent(e, { tEdit: t + dt }) }));
        }
        const exact = C.el('input', { type: 'datetime-local', step: 1, 'aria-label': 'Exact time',
                                      value: C.toLocalInput(Math.round(t / 1000), tz) });
        exact.addEventListener('change', () => {
          const s = C.fromLocalInput(exact.value, tz);
          if (s !== null) patchEvent(e, { tEdit: s * 1000 + (Math.round(t) % 1000) });
        });
        edit.appendChild(exact);
        if (e.tEdit !== undefined) {
          edit.appendChild(C.el('button', { type: 'button', text: 'Reset time',
                                            onclick: () => patchEvent(e, { tEdit: null }) }));
        }
        edit.appendChild(C.el('button', { type: 'button', class: 'danger', text: e.deleted ? 'Restore' : 'Undo',
                                          onclick: () => patchEvent(e, { deleted: !e.deleted }) }));
      } else {
        edit.appendChild(C.el('span', { class: 'hint', text: 'waiting to send…' }));
      }
      body.appendChild(C.el('tr', { class: (e.deleted ? 'deleted ' : '') + (e._pending ? 'pending' : ''),
                                    'data-id': e.id }, [
        C.el('td', { class: 'num', text: when + (e.tEdit !== undefined ? ' (edited)' : '') }),
        C.el('td', { text: stateName(d, e.state) }),
        edit,
      ]));
    }
    const n = pending().length;
    $('outbox-note').textContent = n ? n + ' not yet sent' : '';
    document.body.dataset.events = String(rows.length);
    document.body.dataset.pending = String(n);
    if (chart) {
      chart.setEvents(rows.filter((e) => !e.deleted).map((e) => ({ t: timeOf(e), label: stateName(d, e.state) })));
      draw();                                    // a marker moved: draw it now, not next frame
    }
  }

  function toast(text, ev) {
    $('toast-text').textContent = text;
    $('toast').hidden = false;
    $('toast-undo').onclick = async () => {
      $('toast').hidden = true;
      const queued = pending();
      if (queued.some((x) => x.id === ev.id)) {
        setPending(queued.filter((x) => x.id !== ev.id));     // never sent: just forget it
        return render();
      }
      const sent = events.find((x) => x.id === ev.id);
      if (sent) patchEvent(sent, { deleted: true });
    };
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { $('toast').hidden = true; }, 10000);
  }

  // ----------------------------------------------------------------- the chart

  async function seed(subscribedAt) {
    const r = await Live.api('/api/live/seed');
    if (!r.body.ok) return;
    tz = r.body.tz || tz;
    loadSpec = (r.body.series || []).find((s) => s.key === 'plant_general_load_power');
    if (loadSpec && !chart) {
      chart = new Live.LiveChart($('load-chart'), Object.assign({ windowS: WINDOW_S, height: 170 }, loadSpec));
    }
    for (const m of r.body.msgs || []) store.add(m);
    render();
    draw();
    if (subscribedAt && r.body.gen < subscribedAt) setTimeout(() => seed(null), 2500);
  }

  // The same rule as live.js: the seed never waits for the stream, and while the stream is not
  // live the (cached) seed is polled, so a browser that cannot open the WebSocket still sees
  // the load it is logging against.
  let pollTimer = null;
  function polling(on) {
    if (on && !pollTimer) pollTimer = setInterval(() => seed(null), 10000);
    if (!on && pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  const stream = new Live.Stream({
    session: API + 'live', clock,
    onState: (s, d) => {
      status(s, d);
      polling(s !== 'live' && s !== 'paused');
      if (d && !d.everLive && d.failures >= 2) {
        notice('The live stream cannot connect from this browser (the WebSocket to ' +
               '*.iot.ap-southeast-2.amazonaws.com is refused or blocked). Logging still works; ' +
               'the load chart refreshes every ten seconds instead.');
      }
    },
    onSubscribed: (at) => seed(at),
    onMessage: (m) => { m._seenPhone = Date.now(); store.add(m); },
  });

  let lastDraw = 0, drawn = -1;
  function draw() {
    if (!chart) return;
    const now = clock.now();
    store.prune(now);
    chart.update(store, now, tz);
    const latest = store.latest();
    $('load-now').textContent = latest && loadSpec ? Live.fmtNow(loadSpec, latest[loadSpec.key]) : '—';
    drawn = store.version;
    lastDraw = now;
  }

  function tick() {
    const now = clock.now();
    if (chart) {
      if (store.version !== drawn || now - lastDraw > 1000) draw();
      else chart.frame(now);
    }
    if (clock.stepped() || Date.now() - lastSync > 600000) { lastSync = Date.now(); sync(); }
    if (!document.hidden) requestAnimationFrame(tick);
  }

  // ------------------------------------------------------------------ wiring

  $('device').addEventListener('change', (e) => {
    current = e.target.value;
    localStorage.setItem(PICKED, current);
    renderPicker();
    loadEvents();
  });
  $('new-device-btn').onclick = () => { $('new-device').hidden = false; $('new-name').focus(); };
  $('new-cancel').onclick = () => { $('new-device').hidden = true; };
  $('new-device').addEventListener('submit', async (e) => {
    e.preventDefault();
    const r = await req('POST', 'devices', { name: $('new-name').value, desc: $('new-desc').value });
    if (!r.body.ok) return notice(r.body.error);
    current = r.body.device.id;
    localStorage.setItem(PICKED, current);
    $('new-device').reset();
    $('new-device').hidden = true;
    await loadDevices();
  });
  $('rename').addEventListener('submit', (e) => {
    e.preventDefault();
    patchDevice({ name: $('rename-name').value, desc: $('rename-desc').value });
  });
  $('archive-device').onclick = () => {
    const d = dev();
    if (d && confirm('Archive “' + d.name + '”? Its history is kept; it just leaves the list.')) {
      patchDevice({ archived: true });
    }
  };
  $('add-state').addEventListener('submit', async (e) => {
    e.preventDefault();
    const r = await req('POST', 'states', { device: current, name: $('state-name').value });
    if (!r.body.ok) return notice(r.body.error);
    $('add-state').reset();
    await loadDevices();
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      stream.stop(true);
    } else {
      seed(null);
      stream.start();
      flush();
      tick();
    }
  });
  window.addEventListener('online', flush);
  // Rebuilt when its card changes width (rotation, a resize), as the live page does.
  if (window.ResizeObserver) {
    new ResizeObserver(() => { if (chart && chart.stale()) { chart.resize(); draw(); } })
      .observe($('load-chart'));
  }
  setInterval(flush, 5000);

  (async function boot() {
    status('connecting');
    await sync();
    await loadDevices();
    seed(null);
    stream.start();
    flush();
    tick();
  })();
})();
