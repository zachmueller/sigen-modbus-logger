# Findings

Why the code is the way it is. Measurements, device quirks, and the analysis
mistakes that produced confident wrong conclusions before being caught.

All measurements are from one SigenStor EC 10.0 SP AU (single phase, one battery
pack, firmware `V100R001C22SPC116`) polled over a home LAN from a Mac. Treat the
numbers as one data point and the *methods* as the transferable part.

---

## Latency: oscillation, not degradation

Measured across ~3.5 hours, bucketed with `decode.py --latency`:

| Series | Median range | p95 as % of tick window | Verdict |
|---|---|---|---|
| 1 Hz, 08:00–09:20 | 70–107 ms (1.53x) | 11–23% | oscillating, no monotonic trend |
| 0.5 Hz, 09:20–11:00 | 80–133 ms (1.66x) | **9–14%** | oscillating, no monotonic trend |

Median swings by a factor of ~1.6 with multi-minute excursions in both directions
and no progressive worsening. Worst single ticks were 1147 ms and 904 ms, both
isolated, both well inside a 2000 ms window.

Ruled out as drivers, each by measurement:

| Hypothesis | How it died |
|---|---|
| Network | ICMP RTT 3.7–11.6 ms, avg 7.8, against ~30 ms per request |
| The logger | 0.0% CPU, ~18 MB RSS, flat |
| Contention | Exactly one TCP connection to the device |
| Device thermals | PCS internal temp 30.4–30.9 °C, cell temp 14.6–14.9 °C |
| Grid instability | Frequency 49.92–50.06 Hz, voltage 238–239 V, insulation constant |
| A fault developing | All twelve alarm words zero throughout |
| Household load | Fell 2.16 → 1.69 kW *while* latency rose |
| PV activity | Identically zero across all 3094 samples — array not yet connected |
| Installer config changes | Export limits still unset in the config tier |
| Connection age | A 17-minute-old connection gave 80 ms; a 7-minute-old one gave 133 ms |

No cause identified. The device simply varies. Since it does not trend and stays
under 15% of the tick window, it needs monitoring rather than mitigation.

The neat trick in that table: **the archive already held the variables that would
explain a slowdown.** Reading the config tier out of the archive answers "did the
installer change something?" without opening a second Modbus connection.

### Gates are window-relative

`GATE_P95_FRAC` (0.50) and `GATE_MAX_FRAC` (0.90) are fractions of the tick window,
which is `fast_period_s` seconds. At 0.5 Hz that resolves to 1000 ms and 1800 ms.

This was a latent trap: the gates were originally absolute (500 / 900 ms) and tuned
for a 1 s window, so halving the cadence silently made them **twice as strict as
intended**. A 904 ms tick read as a breach of the 900 ms gate when it was really
45% of its 2000 ms budget.

---

## Device quirks that will bite you

All of these produce wrong-looking behaviour rather than obvious errors. `lib.py`
handles them; anything new must too.

1. **Read-only registers are *input* registers — use FC4.** The map marks
   running-info registers `ro`, which means FC4, not FC3. Using FC3 returns
   exception 2 on nearly everything, which reads as "device broken".
2. **Reads must not end mid-field.** A block read may not terminate inside a
   multi-register field. This is why single-register reads mostly fail, and why a
   naive per-register sweep produces a nonsense sparse map.
3. **`gain` is a divisor**: `value = raw / gain`.
4. **`30000` holds local time as an epoch count.** It already includes the reg
   `30002` timezone offset, so `localtime()` applies it twice (+12 h at the
   reference site). Decode with `gmtime` — `lib.device_local()` does this. Applies
   to `inverter_startup_time` and `inverter_shutdown_time` too.
5. **`inverter_power_factor` (31023) is typed wrong upstream.** The map says U16
   gain 1000, which yields impossible values like 64.538. It is S16. Corrected in
   `lib.DTYPE_OVERRIDE`; assume other sign errors lurk where a value reads cleanly
   but looks absurd.
6. **Two different "nothing here" encodings.** Unsigned fields return all-ones
   (`0xFFFF`/`0xFFFFFFFF`) — treated as a sentinel and suppressed. Signed fields
   return **−1**, which is annotated but *kept*, because −1 is a legal reading for
   plenty of signed fields.
7. **Duplicate registers.** `30284` is identical to `30282`; the `_2` lifetime
   block duplicates `30200..30220`; several unit-1 fields duplicate plant fields
   (`30587`→`30031`, `30591`→`30047`, `30599`→`30037`, `30601`→`30014`,
   `30603`→`30286`). Don't poll or plot both.
8. **A single-phase unit reports three phases anyway.** Phase B/C voltages return
   all-ones, phase B/C currents return −1, and only pv1–pv4 are real out of 36
   documented string channels. Those columns are noise, not zeros.

---

## Analysis gotchas that will mislead you

The device quirks above make wrong *readings*. These make wrong *conclusions*,
which is worse, because nothing errors. Every one of them was actually hit.

### 1. Never judge latency from a single number or a run of heartbeats

Reading consecutive 5-minute heartbeats over a hand-picked window showed median
climbing 70 → 124 ms and looked like clear progressive degradation. A process
restart then appeared to "recover" it, 124 → 96 ms, which implied drift with
connection age. Both were wrong: bucketed across the whole series the signal
oscillates ~1.6x in both directions with no trend, and the restart landed inside
that band.

Use `decode.py --latency`, which prints the median range, the spread, and an
explicit monotonic-or-oscillating verdict. Two hours of misdiagnosis went into
building that flag — and the connection-recycle feature it motivated is now kept
for an entirely different reason.

### 2. Correlate against captured data before blaming anything

See the elimination table above. The archive is wide precisely so that the
variables needed to kill a hypothesis are already recorded when the hypothesis
occurs to you.

### 3. Retuning a cadence invalidates old manifests

Every record file and manifest carries an 8-hex plan fingerprint. Change
`fast_period_s`, an offset, or a span, and it changes. `decode.py` refuses on
mismatch, and treats a missing hash on either side as a mismatch too — manifests
written before fingerprinting describe a plan that can no longer be verified.

**When you retune, segregate the old series with its manifest** or you will have a
directory whose files need two different manifests.

### 4. Thresholds must be relative to the tick window

The soak gates were absolute (500 / 900 ms), tuned when the window was 1 s. Halving
the cadence to 0.5 Hz silently made them **twice as strict as intended**.

Generalise the lesson: anything expressed in absolute milliseconds needs revisiting
when the cadence changes. `--check`'s clock-step expectation had the same bug and
now reads the period from the manifest.

### 5. File order is not argv order

`decode.py data/*.bin data/*.bin.gz` expands plain-before-compressed, which is
reverse-chronological for a rotating archive. Decoded in that order the old series
reported `OFFENDERS [-5681, 0, 3600]` and a phantom counter drop — indistinguishable
from real corruption. Files are now sorted by the timestamp in their name, and a
backwards host-clock step is reported as an integrity finding in its own right.

### 6. Resilience needs a real outage to validate

The reconnect path was tested by closing the socket deliberately, which proves
almost nothing: `read()` simply reconnects on the next call, so there is no failure
to handle. A genuine outage — the installer powering the inverter down mid-capture,
with `No route to host` — exercised paths nothing else reaches, and showed that
degraded mode engaged correctly but did not actually *back off*: it kept probing
every tick, at a full socket timeout per attempt, producing 31 gap messages and 167
failures in one bucket.

With the probe interval added, the same fault produces about one probe per 35 s and
one log line per minute. Observed over a subsequent 25-minute outage: 10 probes and
10 gap lines per 6-minute bucket, versus roughly 160 failures before.

If you add resilience logic, find a way to observe it under a real fault before
trusting it. What it does is often not what it was meant to do.

### 7. Record freshness is not data freshness

During an outage the logger keeps writing records on its probe schedule — with
`mask = 0`, containing no blocks at all. An early `--last` reported the age of that
*record* and warned "capture may be stalled", which is exactly backwards: the
capture was healthy and the device was not answering. Worse, the age cycled and
reset with each probe, so repeated runs showed it bouncing around, which looks
like intermittent connectivity rather than a total outage.

The same trap applies to *statistics*, not just freshness. An empty record carries
a tick latency equal to the socket timeout — 6.5 s, or 37 s before degraded mode
narrows the probe. Averaged in, a handful of them dominates every median and max in
the window. The live heartbeat had it worst: during an outage it reported
`median 6523.7 ms`, which reads as a catastrophically slow inverter when the truth
is that the inverter is not there. `--latency`, `--check`, the heartbeat and the
soak table now all count empty records separately and quote latency over data
records only; before that, a real max of 1147 ms was being reported as 37577 ms,
and a bucket with no data at all vanished from the soak table entirely.

Anything reporting liveness or performance must distinguish "the writer is alive"
from "the data is current".

The empty records are not waste — they are the archive's record of the downtime,
and they carry the tick latency, so an outage is visible in the data rather than
being an unexplained hole.

### 8. A 30-minute soak does not prove a day

The soak passed 12/12 gates with latency *improving* over its 30 minutes. Wider
oscillation only became visible across hours. Soaks establish that nothing is
structurally broken; they do not characterise slow variation.

---

## Known limits

- **Only one client should poll at a time.** Concurrent-client behaviour is
  unmeasured. This includes `log.py --check`, which is a brief second client — run
  it deliberately, not on a schedule.
- **Latency oscillates ~1.6x with no identified cause.** Median has ranged 70–133 ms
  across ~3.5 hours at both cadences, with isolated ticks to 1147 ms. It does not
  trend and stays under 15% of the tick window, so it is monitored rather than
  mitigated.
- **Multi-day operation is unproven.** A 30-minute soak passed 12/12 gates, and the
  daemon has run for hours with zero errors, zero retries and no unexpected
  reconnects. Days, thermal cycling and firmware updates remain untested.
- **Latency is not CPU.** There is no way to read the inverter's processor load over
  Modbus, so duty-cycle figures are a wall-clock share. The evidence that the load
  is safe is zero errors and a non-trending p95, not a direct measurement.
- **PV and battery behaviour is unverified.** On the reference unit the array was
  not yet producing (PV identically zero across 3094 samples) and the battery had
  never cycled (SOC 0%, lifetime charge 0.00 kWh). The capture blocks already cover
  all 36 possible string channels, so no change is needed when it comes online —
  but nothing downstream of a non-zero PV reading has been exercised.
- **Export sign convention is documentation, not observation.** `30005 < 0` has never
  been seen. Confirm on the first real export; `30220` starting at exactly 0.000 is
  the independent cross-check.
- **Three-phase, multiple inverters and chargers are untouched.** Those register
  groups are swept and reported, and this unit refuses most of them.
- **Reboot survival is configured but unobserved.** The LaunchDaemon is correct for
  it — root:wheel 644, not in the disabled list — but no reboot has been tested.
- The smart-load span `30124..30199` is deliberately uncaptured, and `30163+124` is
  rejected by the device, so folding it in would need a 4th request.
