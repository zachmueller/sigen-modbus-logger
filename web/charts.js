/*
  Chart primitives for the viewer. No dependencies, no build step.

  Everything is inline SVG drawn against CSS custom properties, so light and dark
  come from style.css rather than from anything computed here.

  Three rules the drawing code exists to enforce, all of them about not lying:

    1. A line BREAKS across a gap. A missing bucket is missing data, and joining
       across it draws a straight line through an outage as if the value had
       glided from one side to the other. The break threshold scales with the
       field's own cadence, so a 60 s register plotted in 30 s buckets is not
       treated as full of holes.
    2. Regions where the device was not answering, or where nothing was recorded
       at all, are HATCHED AND LABELLED rather than left blank -- blank reads as
       zero, and the two cases have different causes.
    3. The line is the bucket MEAN and the tooltip always shows the bucket's
       min-max as well, because with one point per 30 s of 0.5 Hz data the extremes
       are the interesting part.

  A shared CrosshairGroup keeps every panel on the same x position, so reading
  across panels at one instant needs no eye alignment.
*/
'use strict';

const Charts = (function () {

  const SVGNS = 'http://www.w3.org/2000/svg';
  const M = { l: 54, r: 60, t: 10, b: 20 };

  // -- DOM helpers. textContent only: field keys, descriptions and enum labels
  // -- all come from the register map and are not ours to trust as markup.

  function el(tag, attrs, kids) {
    const n = document.createElement(tag);
    apply(n, attrs, kids);
    return n;
  }

  function svg(tag, attrs, kids) {
    const n = document.createElementNS(SVGNS, tag);
    apply(n, attrs, kids);
    return n;
  }

  function apply(n, attrs, kids) {
    for (const k in (attrs || {})) {
      const v = attrs[k];
      if (v === null || v === undefined || v === false) continue;
      if (k === 'text') n.textContent = String(v);
      else if (k === 'class') n.setAttribute('class', v);
      else if (k === 'style' && typeof v === 'object') Object.assign(n.style, v);
      else if (k.startsWith('on') && typeof v === 'function') {
        n.addEventListener(k.slice(2), v);
      } else n.setAttribute(k, String(v));
    }
    for (const kid of (kids || [])) {
      if (kid === null || kid === undefined) continue;
      n.appendChild(typeof kid === 'string' ? document.createTextNode(kid) : kid);
    }
    return n;
  }

  function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); }

  // -- palette. Read out of the stylesheet as concrete values rather than left as
  // -- var() inside SVG presentation attributes, which is where cross-browser
  // -- support gets thin. Re-read on every render, so a light/dark switch is a
  // -- re-render and nothing has to be kept in sync by hand.

  function palette() {
    const cs = getComputedStyle(document.body);
    const g = (n, fallback) => (cs.getPropertyValue(n).trim() || fallback);
    return {
      series: [g('--series-1', '#2a78d6'), g('--series-2', '#eb6834'),
               g('--series-3', '#1baf7a'), g('--series-4', '#eda100')],
      ramp: [g('--ramp-1', '#86b6ef'), g('--ramp-2', '#5598e7'),
             g('--ramp-3', '#2a78d6'), g('--ramp-4', '#184f95')],
      ink: g('--text-primary', '#0b0b0b'),
      secondary: g('--text-secondary', '#52514e'),
      muted: g('--text-muted', '#898781'),
      surface: g('--surface-1', '#fcfcfb'),
      good: g('--good', '#0ca30c'),
      warning: g('--warning', '#fab219'),
      serious: g('--serious', '#ec835a'),
      critical: g('--critical', '#d03b3b'),
    };
  }

  // Slot n of the categorical theme, 1-based, never cycled past its end: a chart
  // with more series than slots is a chart that needs faceting, not a ninth hue.
  function slotColor(n, ramp) {
    const P = palette();
    const list = ramp ? P.ramp : P.series;
    return list[Math.min(list.length, Math.max(1, n)) - 1];
  }

  // -- time. The archive is stamped in the CAPTURE HOST's local time, and so are
  // -- its filenames, so the page labels axes that way too rather than in the
  // -- viewer's own zone. Offsets arrive as change points, so a window straddling
  // -- a DST switch labels both halves correctly.

  function offsetAt(tz, ts) {
    if (!tz || !tz.length) return 0;
    let off = tz[0][1];
    for (const run of tz) { if (ts >= run[0]) off = run[1]; }
    return off;
  }

  function zoneName(tz) { return (tz && tz.length) ? tz[tz.length - 1][2] : ''; }

  function parts(ts, tz) {
    const d = new Date((ts + offsetAt(tz, ts)) * 1000);
    return {
      y: d.getUTCFullYear(), mo: d.getUTCMonth(), d: d.getUTCDate(),
      h: d.getUTCHours(), mi: d.getUTCMinutes(), s: d.getUTCSeconds(),
      wd: d.getUTCDay(),
    };
  }

  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const p2 = (n) => String(n).padStart(2, '0');

  function fmtTime(ts, tz, mode) {
    const p = parts(ts, tz);
    const hm = p2(p.h) + ':' + p2(p.mi);
    if (mode === 'hm') return hm;
    if (mode === 'hms') return hm + ':' + p2(p.s);
    if (mode === 'day') return p.d + ' ' + MONTHS[p.mo];
    if (mode === 'daytime') return p.d + ' ' + MONTHS[p.mo] + ' ' + hm;
    return p.y + '-' + p2(p.mo + 1) + '-' + p2(p.d) + ' ' + hm + ':' + p2(p.s);
  }

  // For <input type="datetime-local">, which wants the host's wall clock.
  function toLocalInput(ts, tz) {
    const p = parts(ts, tz);
    return p.y + '-' + p2(p.mo + 1) + '-' + p2(p.d) + 'T' + p2(p.h) + ':' +
           p2(p.mi) + ':' + p2(p.s);
  }

  function fromLocalInput(text, tz) {
    const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(text || '');
    if (!m) return null;
    const asUTC = Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0)) / 1000;
    return asUTC - offsetAt(tz, asUTC);   // undo the host's offset
  }

  // -- scales and ticks

  function niceTicks(lo, hi, count) {
    if (!(hi > lo)) { hi = lo + 1; }
    const raw = (hi - lo) / Math.max(1, count);
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag;
    const step = (norm >= 5 ? 10 : norm >= 2 ? 5 : norm >= 1 ? 2 : 1) * mag;
    const out = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) {
      out.push(Math.abs(v) < step * 1e-9 ? 0 : v);
    }
    return out;
  }

  const TIME_STEPS = [60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600,
                      43200, 86400, 172800, 604800];

  function timeTicks(t0, t1, tz, want) {
    const span = Math.max(1, t1 - t0);
    let step = TIME_STEPS[TIME_STEPS.length - 1];
    for (const s of TIME_STEPS) { if (span / s <= want) { step = s; break; } }
    const off = offsetAt(tz, t0);
    const out = [];
    let v = Math.ceil((t0 + off) / step) * step - off;
    for (; v <= t1; v += step) out.push(v);
    return { ticks: out, step: step };
  }

  function fmtVal(v, dec) {
    if (v === null || v === undefined) return '—';
    if (!isFinite(v)) return '—';
    return v.toFixed(dec === undefined ? 2 : dec);
  }

  // -- gap rule: how many empty buckets may be bridged before the line breaks.

  function maxGapBuckets(bucketS, cadenceS) {
    return Math.max(2, Math.ceil(3 * (cadenceS || bucketS) / bucketS));
  }

  function segments(values, bucketS, cadenceS) {
    const limit = maxGapBuckets(bucketS, cadenceS);
    const segs = [];
    let cur = null, lastIdx = -1;
    for (let i = 0; i < values.length; i++) {
      const v = values[i];
      if (v === null || v === undefined) continue;
      if (cur && i - lastIdx > limit) { segs.push(cur); cur = null; }
      if (!cur) cur = [];
      cur.push(i);
      lastIdx = i;
    }
    if (cur && cur.length) segs.push(cur);
    return segs;
  }

  // ------------------------------------------------------------- LineChart

  class LineChart {
    /* spec: {t, bucket_s, tz, unit, series[], domain?, zero?, height?,
              regions[], band?} */
    constructor(host, spec) {
      this.host = host;
      this.spec = spec;
      this.node = svg('svg', { class: 'chart', tabindex: '0',
                               role: 'img', 'aria-label': spec.aria || spec.title || '' });
      host.appendChild(this.node);
      this.idx = null;
    }

    layout() {
      const w = Math.max(320, this.host.clientWidth || 640);
      const h = this.spec.height || 200;
      this.w = w; this.h = h;
      this.pw = Math.max(40, w - M.l - M.r);
      this.ph = Math.max(30, h - M.t - M.b);
      const t = this.spec.t;
      this.t0 = t.length ? t[0] : 0;
      this.t1 = t.length ? t[t.length - 1] + this.spec.bucket_s : 1;
      let lo = Infinity, hi = -Infinity;
      for (const s of this.spec.series) {
        for (const arr of [s.min || s.mean, s.max || s.mean]) {
          for (const v of (arr || [])) {
            if (v === null || v === undefined) continue;
            if (v < lo) lo = v;
            if (v > hi) hi = v;
          }
        }
      }
      if (!isFinite(lo)) { lo = 0; hi = 1; }
      if (this.spec.zero) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
      if (lo === hi) { lo -= 0.5; hi += 0.5; }
      const pad = (hi - lo) * 0.08;
      lo -= pad; hi += pad;
      if (this.spec.domain) { lo = this.spec.domain[0]; hi = this.spec.domain[1]; }
      this.lo = lo; this.hi = hi;
    }

    x(ts) { return M.l + (ts - this.t0) / (this.t1 - this.t0) * this.pw; }
    xi(i) { return this.x(this.spec.t[i] + this.spec.bucket_s / 2); }
    y(v) { return M.t + this.ph - (v - this.lo) / (this.hi - this.lo) * this.ph; }

    indexAt(px) {
      const t = this.t0 + (px - M.l) / this.pw * (this.t1 - this.t0);
      const i = Math.round((t - this.spec.t[0]) / this.spec.bucket_s);
      return Math.max(0, Math.min(this.spec.t.length - 1, i));
    }

    render() {
      this.layout();
      const s = this.spec;
      const g = [];
      this.node.setAttribute('viewBox', `0 0 ${this.w} ${this.h}`);
      this.node.setAttribute('height', this.h);
      clear(this.node);

      this.node.appendChild(hatchDefs());

      // y gridlines + labels
      const yt = niceTicks(this.lo, this.hi, Math.max(2, Math.floor(this.ph / 38)));
      for (const v of yt) {
        const y = this.y(v);
        if (y < M.t - 1 || y > M.t + this.ph + 1) continue;
        g.push(svg('line', { class: 'gridline', x1: M.l, x2: M.l + this.pw, y1: y, y2: y }));
        g.push(svg('text', { x: M.l - 6, y: y + 3, 'text-anchor': 'end',
                             text: fmtVal(v, s.tickDecimals !== undefined
                                             ? s.tickDecimals : tickDec(yt)) }));
      }
      // x ticks
      const tt = timeTicks(this.t0, this.t1, s.tz, Math.max(3, Math.floor(this.pw / 90)));
      const mode = tt.step >= 86400 ? 'day' : (tt.step >= 21600 ? 'daytime' : 'hm');
      for (const ts of tt.ticks) {
        const x = this.x(ts);
        g.push(svg('line', { class: 'gridline', x1: x, x2: x, y1: M.t, y2: M.t + this.ph }));
        g.push(svg('text', { x: x, y: this.h - 6, 'text-anchor': 'middle',
                             text: fmtTime(ts, s.tz, mode) }));
      }
      g.push(svg('line', { class: 'baseline', x1: M.l, x2: M.l + this.pw,
                           y1: M.t + this.ph, y2: M.t + this.ph }));
      // No unit label inside the plot: the card heading already carries it, and at
      // the top-left corner it collides with the topmost tick label whenever the
      // domain is pinned (0-100 for a percentage, say).
      if (this.lo < 0 && this.hi > 0) {
        g.push(svg('line', { class: 'zeroline', x1: M.l, x2: M.l + this.pw,
                             y1: this.y(0), y2: this.y(0) }));
      }

      // regions: not-answering / nothing-recorded
      for (const r of (s.regions || [])) {
        const x1 = Math.max(M.l, this.x(r.from)), x2 = Math.min(M.l + this.pw, this.x(r.to));
        if (x2 <= x1) continue;
        g.push(svg('rect', { x: x1, y: M.t, width: Math.max(1, x2 - x1), height: this.ph,
                             fill: `url(#${r.pattern})` }));
        if (x2 - x1 > 58 && r.label) {
          g.push(svg('text', { class: 'band-label', x: (x1 + x2) / 2, y: M.t + 11,
                               'text-anchor': 'middle', text: r.label }));
        }
      }

      // bands then lines, so a line is never buried under its own band
      const showBand = s.band !== false && s.series.length <= 2;
      for (const ser of s.series) {
        if (!showBand || !ser.min || !ser.max) continue;
        for (const seg of segments(ser.mean, s.bucket_s, ser.cadence_s)) {
          if (seg.length < 2) continue;
          let d = '';
          for (const i of seg) d += (d ? 'L' : 'M') + this.xi(i) + ' ' + this.y(ser.max[i]);
          for (let k = seg.length - 1; k >= 0; k--) {
            const i = seg[k];
            d += 'L' + this.xi(i) + ' ' + this.y(ser.min[i]);
          }
          g.push(svg('path', { class: 'band', d: d + 'Z', fill: ser.color }));
        }
      }
      const labels = [];
      for (const ser of s.series) {
        const segs = segments(ser.mean, s.bucket_s, ser.cadence_s);
        for (const seg of segs) {
          let d = '';
          for (const i of seg) d += (d ? 'L' : 'M') + this.xi(i) + ' ' + this.y(ser.mean[i]);
          if (seg.length === 1) {
            g.push(svg('circle', { cx: this.xi(seg[0]), cy: this.y(ser.mean[seg[0]]),
                                   r: 1.6, fill: ser.color }));
          } else {
            g.push(svg('path', { class: 'line', d: d, stroke: ser.color }));
          }
        }
        const last = segs.length ? segs[segs.length - 1] : null;
        if (last && last.length) {
          const i = last[last.length - 1];
          labels.push({ y: this.y(ser.mean[i]), label: ser.label, color: ser.color });
        }
      }
      // Direct labels: mandatory once four series share a chart. Pushed apart so
      // two close lines do not overprint their names.
      labels.sort((a, b) => a.y - b.y);
      for (let i = 1; i < labels.length; i++) {
        if (labels[i].y - labels[i - 1].y < 11) labels[i].y = labels[i - 1].y + 11;
      }
      for (const L of labels) {
        const y = Math.min(M.t + this.ph, Math.max(M.t + 6, L.y));
        g.push(svg('line', { x1: M.l + this.pw + 4, x2: M.l + this.pw + 14,
                             y1: y - 3, y2: y - 3, stroke: L.color, 'stroke-width': 2 }));
        g.push(svg('text', { class: 'direct-label', x: M.l + this.pw + 17, y: y,
                             text: L.label }));
      }

      this.cross = svg('g', { class: 'crosshair-layer' });
      g.push(this.cross);
      g.push(svg('rect', { class: 'hit', x: M.l, y: M.t, width: this.pw, height: this.ph }));
      for (const n of g) this.node.appendChild(n);
      if (this.idx !== null) this.drawCrosshair(this.idx);
    }

    drawCrosshair(i) {
      this.idx = i;
      if (!this.cross) return;
      clear(this.cross);
      if (i === null || i === undefined) return;
      const x = this.xi(i);
      this.cross.appendChild(svg('line', { class: 'crosshair', x1: x, x2: x,
                                           y1: M.t, y2: M.t + this.ph }));
      for (const ser of this.spec.series) {
        const v = ser.mean[i];
        if (v === null || v === undefined) continue;
        this.cross.appendChild(svg('circle', { class: 'marker', cx: x, cy: this.y(v),
                                               r: 3.2, fill: ser.color }));
      }
    }
  }

  function tickDec(ticks) {
    let step = Infinity;
    for (let i = 1; i < ticks.length; i++) step = Math.min(step, Math.abs(ticks[i] - ticks[i - 1]));
    if (!isFinite(step) || step >= 10) return 0;
    if (step >= 1) return step % 1 === 0 ? 0 : 1;
    return Math.min(4, Math.ceil(-Math.log10(step)));
  }

  function hatchDefs() {
    const P = palette();
    const defs = svg('defs', {}, []);
    const mk = (id, color, angle) => {
      const p = svg('pattern', { id: id, width: 7, height: 7,
                                 patternTransform: `rotate(${angle})`,
                                 patternUnits: 'userSpaceOnUse' }, [
        svg('rect', { width: 7, height: 7, fill: color, opacity: 0.10 }),
        svg('line', { x1: 0, y1: 0, x2: 0, y2: 7, stroke: color, 'stroke-width': 1.6,
                      opacity: 0.5 }),
      ]);
      defs.appendChild(p);
    };
    mk('hatch-nodata', P.serious, 45);
    mk('hatch-norecords', P.muted, 135);
    return defs;
  }

  // ------------------------------------------------------------- BandChart
  // States as one band per bucket. Same margins as LineChart so the shared
  // crosshair lines up across panels.

  class BandChart {
    constructor(host, spec) {
      this.host = host;
      this.spec = spec;      // {t, bucket_s, tz, rows:[{name, values, labels, colorOf}]}
      this.node = svg('svg', { class: 'chart', tabindex: '0', role: 'img',
                               'aria-label': spec.aria || 'State bands' });
      host.appendChild(this.node);
      this.idx = null;
    }

    layout() {
      this.w = Math.max(320, this.host.clientWidth || 640);
      this.rowH = 18; this.gap = 6;
      this.ph = this.spec.rows.length * (this.rowH + this.gap);
      this.h = this.ph + M.t + M.b;
      this.pw = Math.max(40, this.w - M.l - M.r);
      const t = this.spec.t;
      this.t0 = t.length ? t[0] : 0;
      this.t1 = t.length ? t[t.length - 1] + this.spec.bucket_s : 1;
    }

    x(ts) { return M.l + (ts - this.t0) / (this.t1 - this.t0) * this.pw; }
    xi(i) { return this.x(this.spec.t[i] + this.spec.bucket_s / 2); }
    indexAt(px) {
      const t = this.t0 + (px - M.l) / this.pw * (this.t1 - this.t0);
      const i = Math.round((t - this.spec.t[0]) / this.spec.bucket_s);
      return Math.max(0, Math.min(this.spec.t.length - 1, i));
    }

    render() {
      this.layout();
      const s = this.spec, P = palette();
      clear(this.node);
      this.node.setAttribute('viewBox', `0 0 ${this.w} ${this.h}`);
      this.node.setAttribute('height', this.h);
      const g = [];
      const bw = this.pw / Math.max(1, s.t.length);
      s.rows.forEach((row, r) => {
        const y = M.t + r * (this.rowH + this.gap);
        // The left margin matches LineChart's so the shared crosshair lines up
        // across panels, which leaves room for a short name only -- the full one
        // goes in the title, and the legend spells out the values.
        g.push(svg('text', { x: M.l - 6, y: y + 13, 'text-anchor': 'end',
                             text: row.short || row.name }, [
          svg('title', { text: row.name }),
        ]));
        g.push(svg('rect', { x: M.l, y: y, width: this.pw, height: this.rowH,
                             fill: 'none', stroke: P.muted, 'stroke-opacity': 0.25 }));
        let i = 0;
        while (i < s.t.length) {
          const v = row.values[i];
          if (v === null || v === undefined) { i++; continue; }
          let j = i + 1;
          while (j < s.t.length && row.values[j] === v && !row.mixed[j]) j++;
          const x1 = this.x(s.t[i]), x2 = this.x(s.t[j - 1] + s.bucket_s);
          g.push(svg('rect', {
            x: x1, y: y, width: Math.max(1, x2 - x1), height: this.rowH,
            fill: row.colorOf(v), opacity: row.mixed[i] ? 0.6 : 1,
          }));
          if (x2 - x1 > 44) {
            g.push(svg('text', { x: (x1 + x2) / 2, y: y + 13, 'text-anchor': 'middle',
                                 fill: P.surface, text: row.labelOf(v) }));
          }
          i = j;
        }
      });
      const tt = timeTicks(this.t0, this.t1, s.tz, Math.max(3, Math.floor(this.pw / 90)));
      const mode = tt.step >= 86400 ? 'day' : (tt.step >= 21600 ? 'daytime' : 'hm');
      for (const ts of tt.ticks) {
        g.push(svg('text', { x: this.x(ts), y: this.h - 6, 'text-anchor': 'middle',
                             text: fmtTime(ts, s.tz, mode) }));
      }
      this.cross = svg('g', {});
      g.push(this.cross);
      g.push(svg('rect', { class: 'hit', x: M.l, y: M.t, width: this.pw, height: this.ph }));
      for (const n of g) this.node.appendChild(n);
      if (this.idx !== null) this.drawCrosshair(this.idx);
    }

    drawCrosshair(i) {
      this.idx = i;
      if (!this.cross) return;
      clear(this.cross);
      if (i === null || i === undefined) return;
      const x = this.xi(i);
      this.cross.appendChild(svg('line', { class: 'crosshair', x1: x, x2: x,
                                           y1: M.t, y2: M.t + this.ph }));
    }
  }

  // ------------------------------------------------------------- sparkline

  function sparkline(values, width, height, color) {
    const n = svg('svg', { width: width, height: height, class: 'spark',
                           viewBox: `0 0 ${width} ${height}`, 'aria-hidden': 'true' });
    let lo = Infinity, hi = -Infinity;
    for (const v of values) {
      if (v === null || v === undefined) continue;
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
    if (!isFinite(lo)) return n;
    if (lo === hi) { lo -= 0.5; hi += 0.5; }
    const x = (i) => (i / Math.max(1, values.length - 1)) * (width - 2) + 1;
    const y = (v) => height - 1 - (v - lo) / (hi - lo) * (height - 2);
    if (lo < 0 && hi > 0) {
      n.appendChild(svg('line', { x1: 0, x2: width, y1: y(0), y2: y(0),
                                  stroke: palette().muted, 'stroke-width': 1,
                                  'stroke-opacity': 0.5 }));
    }
    let d = '', open = false;
    for (let i = 0; i < values.length; i++) {
      const v = values[i];
      if (v === null || v === undefined) { open = false; continue; }
      d += (open ? 'L' : 'M') + x(i).toFixed(1) + ' ' + y(v).toFixed(1);
      open = true;
    }
    n.appendChild(svg('path', { d: d, fill: 'none', stroke: color, 'stroke-width': 1.5 }));
    return n;
  }

  // --------------------------------------------------------- CrosshairGroup
  // One tooltip, every series in the hovered panel; the crosshair itself is
  // mirrored into every panel so reading across them needs no eye alignment.

  class CrosshairGroup {
    constructor(tooltipNode) {
      this.tip = tooltipNode;
      this.charts = [];
    }

    reset() { this.charts = []; }

    add(chart, tooltipFor) {
      this.charts.push(chart);
      chart.tooltipFor = tooltipFor;
      const node = chart.node;
      const move = (ev) => {
        const r = node.getBoundingClientRect();
        const scale = (r.width || 1) / (chart.w || 1);
        const i = chart.indexAt((ev.clientX - r.left) / scale);
        this.show(i, chart, ev.clientX, ev.clientY);
      };
      node.addEventListener('pointermove', move);
      node.addEventListener('pointerdown', move);
      node.addEventListener('pointerleave', () => this.hide());
      node.addEventListener('focus', () => {
        const i = chart.idx === null ? chart.spec.t.length - 1 : chart.idx;
        const r = node.getBoundingClientRect();
        this.show(i, chart, r.left + r.width / 2, r.top + 20);
      });
      node.addEventListener('blur', () => this.hide());
      node.addEventListener('keydown', (ev) => {
        if (ev.key !== 'ArrowLeft' && ev.key !== 'ArrowRight') return;
        ev.preventDefault();
        ev.stopPropagation();          // arrows pan the window unless a chart has focus
        const n = chart.spec.t.length;
        const step = ev.shiftKey ? 10 : 1;
        let i = chart.idx === null ? n - 1 : chart.idx;
        i = Math.max(0, Math.min(n - 1, i + (ev.key === 'ArrowLeft' ? -step : step)));
        const r = node.getBoundingClientRect();
        const scale = (r.width || 1) / (chart.w || 1);
        this.show(i, chart, r.left + chart.xi(i) * scale, r.top + 24);
      });
    }

    show(i, chart, cx, cy) {
      for (const c of this.charts) c.drawCrosshair(i);
      const rows = chart.tooltipFor ? chart.tooltipFor(i) : null;
      if (!rows) { this.tip.hidden = true; return; }
      clear(this.tip);
      this.tip.appendChild(el('div', { class: 'when', text: rows.when }));
      const body = el('tbody', {}, rows.rows.map((r) => el('tr', {}, [
        el('td', { class: 'n' }, [
          el('span', { class: 'stroke', style: { background: r.color || 'transparent' } }),
          document.createTextNode(r.name),
        ]),
        el('td', { class: 'v', text: r.value }),
        r.range ? el('td', { class: 'r', text: r.range }) : null,
      ])));
      this.tip.appendChild(el('table', {}, [body]));
      if (rows.foot) this.tip.appendChild(el('div', { class: 'when', text: rows.foot }));
      this.tip.hidden = false;
      const box = this.tip.getBoundingClientRect();
      const pad = 12;
      let x = cx + pad, y = cy + pad;
      if (x + box.width > window.innerWidth - 4) x = cx - box.width - pad;
      if (y + box.height > window.innerHeight - 4) y = Math.max(4, cy - box.height - pad);
      this.tip.style.left = Math.max(4, x) + 'px';
      this.tip.style.top = y + 'px';
    }

    hide() {
      this.tip.hidden = true;
      for (const c of this.charts) c.drawCrosshair(null);
    }
  }

  return { el, svg, clear, LineChart, BandChart, CrosshairGroup, sparkline,
           fmtTime, fmtVal, toLocalInput, fromLocalInput, zoneName,
           palette, slotColor };
})();
