'use strict';
/*
 * DEV ONLY -- never published (not in WEB_ASSETS or LIVE_ASSETS). Replaces the network under
 * live.js so the page can be checked in a headless browser with no AWS at all:
 *
 *   python3 -m http.server -d web 8765        # root-absolute paths need web/ as the root
 *   open http://127.0.0.1:8765/dev/live.html
 *
 * The seed holds five minutes with every kind of gap the page must draw, in order:
 *   an inverter outage (markers every ~36 s)   -> orange, "device not answering"
 *   a stretch with nothing at all              -> grey, "no live data"
 * and the stream then repeats the seed's newest point once, which must be drawn once.
 * `?fail=1` makes every connect fail instead, the way a blocked WebSocket does.
 */
(function () {
  const series = [
    { key: 'plant_general_load_power', label: 'Load', title: 'House load', unit: 'kW', slot: 2, dec: 3, zero: true, min_span: 0.5 },
    { key: 'plant_sigen_photovoltaic_power', label: 'PV', title: 'Solar (PV)', unit: 'kW', slot: 4, dec: 3, zero: true, min_span: 0.5 },
    { key: 'plant_grid_sensor_active_power', label: 'Grid', title: 'Grid', unit: 'kW', slot: 1, dec: 3, zero: true, min_span: 0.5, sign: ['importing', 'exporting'] },
    { key: 'plant_ess_power', label: 'Battery', title: 'Battery power', unit: 'kW', slot: 3, dec: 3, zero: true, min_span: 0.5, sign: ['charging', 'discharging'] },
    { key: 'plant_ess_soc', label: 'SOC', title: 'Battery state of charge', unit: '%', slot: 3, dec: 1, zero: false, domain: [0, 100] },
    { key: 'inverter_grid_frequency', label: 'Frequency', title: 'Grid frequency', unit: 'Hz', slot: 1, dec: 2, zero: false, min_span: 0.2 },
    { key: 'inverter_phase_a_voltage', label: 'Voltage', title: 'Grid voltage (phase A)', unit: 'V', slot: 1, dec: 2, zero: false, min_span: 4 },
  ];
  function point(t) {
    const s = t / 1000;
    const load = 0.9 + 0.4 * Math.sin(s / 40) + (Math.floor(s / 90) % 2 ? 2.1 : 0);  // a kettle
    const pv = Math.max(0, 1.5 + Math.sin(s / 70));
    return { v: 1, t, m: 7, lat: 60, p: 2,
      plant_general_load_power: +load.toFixed(3),
      plant_sigen_photovoltaic_power: +pv.toFixed(3),
      plant_grid_sensor_active_power: +(load - pv).toFixed(3),
      plant_ess_power: +(-0.3 + 0.2 * Math.cos(s / 30)).toFixed(3),
      plant_ess_soc: +(74 + s % 7 / 10).toFixed(1),
      inverter_grid_frequency: +(50 + 0.03 * Math.sin(s / 9)).toFixed(2),
      inverter_phase_a_voltage: +(241 + 1.5 * Math.sin(s / 13)).toFixed(2) };
  }
  const now = Date.now();
  const msgs = [];
  let last = null;
  for (let t = now - 298000; t <= now - 1000; t += 2000) {
    const age = now - t;
    if (age < 240000 && age > 160000) {                  // outage: markers only, every ~36 s
      if (Math.round((240000 - age) / 2000) % 18 === 0) msgs.push({ v: 1, t, m: 0, lat: 6500, q: 42 });
      continue;
    }
    if (age < 110000 && age > 50000) continue;           // nothing at all
    last = point(t);
    msgs.push(last);
  }
  window.SIGEN_LIVE_MOCK = { msgs };

  Live.api = async function (path, body) {
    if (body === undefined) {
      return { status: 200, body: { ok: true, gen: Date.now(), window_s: 300,
        tz: [[0, 46800, 'NZDT']], series, msgs } };
    }
    return { status: 200, body: { ok: true } };
  };
  Live.Stream.prototype.connect = function () {
    if (!this.running) return;
    if (/[?&]fail=1/.test(location.search)) {           // a WebSocket that never opens
      this.failures++;
      return this.retry('reconnecting', 3000);
    }
    this.set('live', { slot: 0 });
    this.subscribedAt = this.clock.now();
    this.onSubscribed(this.subscribedAt);
    const self = this;
    setTimeout(() => self.onMessage(Object.assign({}, last)), 300);   // a duplicate
    this._mock = setInterval(() => self.onMessage(point(Date.now())), 2000);
  };
  Live.Stream.prototype.closeClient = function () { clearInterval(this._mock); };
})();
