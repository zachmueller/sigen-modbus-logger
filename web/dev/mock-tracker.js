'use strict';
/*
 * DEV ONLY -- never published. An in-memory /api/tracker/* behind window.fetch, so devices.js
 * can be exercised headlessly with no AWS (load after dev/mock-live.js, which stubs the stream).
 * It taps the "on" state once, a second after load, so a DOM dump shows a logged event.
 */
(function () {
  const devices = [{ id: 'dmockheat', name: 'Heat pump', desc: 'outdoor unit', off: 'smockoff1',
                     archived: false, states: [{ id: 'smockoff1', name: 'off', order: 0 },
                                               { id: 'smockon01', name: 'on', order: 1 },
                                               { id: 'smockdfr1', name: 'defrost', order: 2 }] }];
  const events = [];
  const json = (body, status) => new Response(JSON.stringify(body), {
    status: status || 200, headers: { 'Content-Type': 'application/json' } });
  const real = window.fetch.bind(window);
  window.fetch = async function (url, opts) {
    const u = new URL(url, location.href);
    if (!u.pathname.startsWith('/api/tracker/')) return real(url, opts);
    const route = u.pathname.slice('/api/tracker/'.length);
    const method = (opts && opts.method) || 'GET';
    const body = opts && opts.body ? JSON.parse(opts.body) : {};
    const now = Date.now();
    if (route === 'time') return json({ ok: true, recv: now + 250, send: now + 251 });
    if (route === 'devices' && method === 'GET') return json({ ok: true, devices });
    if (route === 'events' && method === 'GET') return json({ ok: true, events: events.slice().reverse() });
    if (route === 'events' && method === 'POST') {
      const tEst = Math.round(body.tPhone + (body.off || 0));
      const ev = Object.assign({}, body, { tGw: now, tServer: now, tEst, deleted: false,
                                           sk: String(tEst).padStart(13, '0') + '#' + body.id });
      events.push(ev);
      return json({ ok: true, event: ev });
    }
    if (route === 'events' && method === 'PATCH') {
      const ev = events.find((e) => e.sk === body.sk);
      Object.assign(ev, body.tEdit === null ? {} : body);
      if (body.tEdit === null) delete ev.tEdit;
      return json({ ok: true, event: ev });
    }
    return json({ ok: false, error: 'mock: no ' + method + ' ' + route }, 404);
  };
  setTimeout(() => {
    const on = Array.from(document.querySelectorAll('#states button')).find((b) => b.textContent === 'on');
    if (on) on.click();
  }, 1000);
})();
