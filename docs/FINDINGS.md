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
9. **The dedicated PV registers lag the power registers when DC arrives.** On the
   tick the array was first energised, the ESS and grid registers already reflected
   about a kilowatt of production — recoverable as `ess + load − grid` — while
   `plant_sigen_photovoltaic_power` and every string voltage still read zero. The
   string voltages caught up one sample later, the PV power register two. Treat the
   PV register's first nonzero timestamp as a couple of seconds late, not as the
   moment of connection.
10. **MPPT sits at open-circuit voltage for ~30 s before it starts tracking.** After
   the DC side is energised the strings park at full Voc drawing no current, with PV
   power at zero, then drop to Vmp and ramp to full output within two samples. Full
   voltage with no current is the startup self-check, not a fault and not a
   disconnected string.

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

### 9. Topology drift detection is blind to the hardware you most expect it to catch

`--baseline`/`--check` were built to notice an unfinished install completing. They
caught nothing when the arrays were energised — a baseline from before any panel was
connected and one from after they were generating differed in nothing but
`captured_at`. Two independent reasons, both structural:

- The 36 string voltage/current registers never *refuse*. They answer `ok` from the
  moment the inverter is up, so the `unsupported → ok` transition the check looks for
  cannot occur. Populated channels read a clean `0` before connection; unpopulated
  ones read the `−1` signed sentinel.
- `pv_string_count` and `mppt_count` are MPPT hardware capability, not a live count
  of what is wired. They read their final value with nothing connected at all.

The check remains the right tool for a subsystem that is currently refusing its
registers — an EV charger, an extra battery pack, an AC-charger slave id. It is the
wrong tool for anything whose registers already answer. Generalise: a diff over
*register status* only sees hardware that changes its answer from "no such thing" to
a value, which is a much narrower class than "hardware appeared".

### 10. Simultaneous energisation looks like staged connection mid-transient

Catching the DC bus charging produced a sample with the strings at wildly different
voltages — some at roughly half of others. Read naively that says two arrays in two
different states, i.e. connected at different times.

It says the opposite. Compare each string to **its own** settled Voc rather than to
the other strings: every one was at the same fraction of it, within 0.3 points. Equal
fractions mean one switching action charging every input together; independent
connections would show each string arriving at its own full Voc at its own time.
Strings legitimately differ in Voc because they differ in module count, so absolute
voltages across strings are not comparable and the ratio between them is a topology
fact, not an event.

The corollary is that a single isolator closing is *all* you can see. Panels wired
with the isolator open are invisible until it closes, so per-array connection times
are not recoverable from Modbus at all — no cadence would have helped.

### 11. Cutting AC to the inverter is indistinguishable from an outage, and rolls a counter back

An installer isolating the inverter to work on it presents exactly as an unreachable
device: dead ticks, degraded probing, a multi-hour hole. Two things distinguish it
afterwards, and both look alarming if you do not expect them:

- The **last sample before the device vanished** carried the cause — `alarm2` bit 4,
  "Off-grid protection", raised two seconds before the running state went to
  `STANDBY`. The alarm registers are worth decoding across an outage boundary rather
  than only when something is obviously wrong.
- A lifetime accumulator came back **lower than it went away** — a fraction of a kWh
  of grid import lost across the power cycle, which `--check` duly reports as
  `1 DROPS` next to the monotonicity assertion. It is the device losing an unflushed
  increment, not archive corruption. Expect one drop per power cut and do not chase
  it.

---

## Reading the archive back is cheap; reading it back *repeatedly* needs care

Measured on the capture host, decoding straight out of the rotated `.bin.gz` files:

| Work | Cost |
|---|---|
| 6 h, 11,651 records, 30 fields → CSV (`decode.py`) | **0.46 s** |
| 6 h, 21 fields, bucketed to 720 points (`series.py`, cold) | **~0.2 s** |
| the same window again, per-file summaries cached | **~3 ms** |

So an interactive viewer over a day of data needs no index and no database. What it
does need is a bound on work that does not grow with the window, because the archive
does: at 0.5 Hz it is ~43,000 records/day, so a year is ~15.8 M records and decoding
every one of them per page load is minutes, not milliseconds.

Two properties give that bound, and both are worth stating because the obvious
implementations of each are subtly wrong:

- **At most 64 records are decoded per bucket** (`series.SAMPLES_PER_BUCKET`), so
  cost tracks the ~900 points a screen can show, not the span. The stride must come
  from the bucket width and the manifest's cadence *only* — never from the requested
  window — or the same file summarises differently depending on how it was asked
  for, and the cache silently stops being a cache. It must also use **ceiling**
  division: flooring left 75 samples in a 600 s bucket, quietly breaking the bound
  it exists to enforce.
- **Bucket keys are absolute** (`ts // bucket_s`), not offsets from the window
  start. Panning then re-uses cached summaries instead of re-slicing every file, and
  a bucket that straddles two archive files is assembled exactly from both. With
  window-relative keys the straddling bucket loses whichever half arrives second,
  which reads as a small dip in the data rather than as a bug.

One more constraint, from the archive format rather than from performance: records
are **variable length** — the bitmask decides how long each one is — so there is
nothing to seek to. Any question about a time range costs a walk of every record
header in the overlapping files. That is why immutability of a rotated `.bin.gz` is
load-bearing.

### The open file is the expensive one, and the obvious cache key makes it worse

Keying a summary on `(path, size, mtime)` is right for a rotated file and quietly
wrong for the one being written. It changes every couple of seconds, so every poll
invalidated the entry and re-decoded up to an hour of records to learn what the last
five said: **0.70 s of CPU per 10 s poll**, continuously, on the host whose logger has
a 2 s tick to hit.

Records are append-only, so the fix is to carry the byte offset the summary reached
and resume from there — 0.70 s → **0.15 s**, and what remains is assembling the
response rather than decoding. Two things this needs to be correct:

- **Every field of the open file must share one offset.** Extending only the fields a
  caller happened to ask for, while advancing the shared resume marker, leaves the
  others stranded at the old offset — they then skip the records in between and read
  low forever. So a field added mid-poll rebuilds them all together.
- **The offset must be the byte after the last COMPLETE record.** A torn final record
  (a hard kill mid-write) is not counted, so the next poll re-reads those bytes, by
  which time they are usually a whole record.

### One core, whatever the browser does

The viewer is a threaded server, so N simultaneous requests decoded on N cores. Three
concurrent 6-hour windows during a deploy took the logger's 5-minute median from
~90 ms to **164 ms** and one tick to **1943 ms** — past its own 1800 ms soak gate,
though with zero retries, zero failures and no records lost. Decoding is now
serialised behind one re-entrant lock shared with the background warmer, so the
viewer costs at most one niced core no matter how many tabs are open.

The general lesson, and the reason this is in Findings rather than a commit message:
**a reader that shares a host with a real-time writer is not free.** Measure it
against the writer's own metric — here, `decode.py --latency` on the archive the
logger is still writing — not against the reader's response times.

### The viewer groups by plan hash itself rather than calling `check_plan_hash()`

`decode.py`'s guard `sys.exit()`s on a mismatch, which is right for a CLI and
unavailable to a server. So `series.discover()` groups files by the hash in their
filename and pairs each group with its own manifest, which makes a mixed-plan decode
*impossible* instead of *fatal*. The viewer then opens the plan holding the newest
record and says so on the page; a superseded series in a subdirectory is viewed by
pointing `--data-dir` at it.

Generalise: a guard that aborts the process is a guard a long-running reader cannot
use. Prefer making the bad state unrepresentable to detecting it late.

### 12. "It is listening" is not "it is reachable", and curl hides the difference

The viewer's first deploy served nothing to a browser while every command-line check
passed. `launchctl` said `state = running`, `lsof` showed `TCP *:8787 (LISTEN)`,
`curl http://yourhost.local:8787/api/latest` returned live JSON — and Chrome said
connection refused.

The listener was IPv4-only, because `ThreadingHTTPServer` is `AF_INET` and binding
`0.0.0.0` means only v4. mDNS advertises the host's AAAA records alongside its A
record, and macOS prefers IPv6, so a browser opening a `.local` name tries
`fe80::…` first and gets an immediate RST from a v4-only socket.

What made it hard to see is that **curl succeeded**: Happy Eyeballs falls back to the
next address after a refusal, so `curl -v` quietly reported
`Trying [fe80::…] … Connection refused` and then `Connected to 192.168.1.42`, exit
code 0. Every diagnostic that used curl confirmed a working server.

Two transferable rules:

- A wildcard bind should open an `AF_INET6` socket with `IPV6_V6ONLY` cleared, which
  accepts both families on one socket. An explicit address is honoured as given —
  asking for `127.0.0.1` means only that.
- When a service works from the shell and not from an application, compare *which
  address each one connected to* before anything else. `curl -v | grep Trying` and
  `lsof -nP -iTCP:<port>` answer it in one line each: if lsof says `IPv4` and curl
  says it tried `[…]` first, that is the bug. The viewer now prints
  `socket accepts IPv4 and IPv6` at startup so the claim is on the record rather
  than inferred.

### 13. The server log answers "is it me or them?" before any theory does

Fixing the dual-stack bug did not fix the reported symptom: a managed work laptop
still showed `ERR_ADDRESS_UNREACHABLE`, first for the `.local` name and then for the
raw `192.168.1.42` too. Meanwhile the same page loaded fine from `curl`, from an
automated Chrome with a throwaway profile on that same laptop, and over IPv4, the
IPv6 ULA and the IPv6 link-local address individually.

The measurement that ended the guessing was one line — every distinct client address
the viewer had ever logged:

```
grep -oE '\[2026[^]]*\] [0-9a-f.:]+' logs/viewer.log | awk '{print $3}' | sort | uniq -c
```

One address: the machine running the tests. **Not a single request from the browser
had ever arrived.** That excludes the server, the socket, the firewall and the
address family in one step, and it does so without a hypothesis — everything after
that is on the client. `ERR_ADDRESS_UNREACHABLE` on a raw RFC1918 address is the
kernel or a filter saying there is no route: a VPN or content-filter policy that
captures private ranges, or simply a different subnet.

Two lessons, and the second is the one worth keeping:

- **`ERR_CONNECTION_REFUSED` and `ERR_ADDRESS_UNREACHABLE` are different diagnoses.**
  Refused means something answered; unreachable means nothing was even attempted on
  the wire. The first fix (dual-stack) addressed a refusal. The second symptom was
  never the same bug, and treating "the browser still cannot see it" as evidence the
  first fix failed would have sent the search back to the server.
- **A reader can be perfectly healthy and still unreachable, and only the server's
  own log distinguishes those.** So the viewer logs page loads and errors while
  suppressing successful API polls: quiet enough to run for months, loud enough that
  "did anything arrive?" is answerable. `bin/tunnel.sh` is the answer once the client
  is the problem — it rides the SSH connection that demonstrably works, so the
  browser only ever talks to `127.0.0.1`.

### 14. launchd reaps what a Spotlight-launched app spawns, and a race hides it

The viewer's Mac launcher was meant to close its SSH tunnel after an idle period, via
a small watcher process the app started alongside the tunnel. It did not work, and
the way it failed is the interesting part.

`ssh -f` — forked into the background by the same script, from the same app —
survived indefinitely: still forwarding 95 seconds later, and after that for as long
as it was left. The watcher, a `/bin/sh` loop, was dead within a second or two of
every launch. Adding `nohup` changed nothing, which is expected: it only ignores
SIGHUP. Adding a proper `setsid()` via a Python double-fork changed nothing either,
which is *not* what the textbook predicts — a new session should escape a process
group teardown. Verified from both directions: run the app's executable from a
terminal and the watcher lives; launch the identical bundle from Spotlight
(`open -a`) and it does not.

Two traps sat on top of each other:

- **A race made it look intermittent.** The launcher forks the watcher and then
  `exec`s `open`; launchd tears down the job when that exits. Adding a single log
  line before the fork shifted the timing by a few milliseconds and the watcher
  survived — once. Long enough to believe the fix worked. Adding a *deterministic*
  wait (poll `pgrep` until the marker appears, proving the child had exec'd) removed
  the race and showed the truth: the watcher is confirmed running, and is then killed
  anyway.
- **`ControlPersist` is not an idle timeout for a forward.** Its timer measures time
  with no *multiplexed clients*; a `-N` master has none, so the timer never starts. A
  master with `ControlPersist=30s` was still alive 70 s later with no traffic — while
  a master under continuous load stayed up too. It answers neither question.

So the feature was removed rather than shipped broken, and closing is explicit: a
second Spotlight app that does `ssh -O exit`. Doing it properly needs a LaunchAgent,
because launchd will not kill what launchd owns.

The transferable rule: **background work started by a GUI app is not yours to keep.**
If it must outlive the launch, it has to be owned by launchd (an agent), not merely
detached — and when a lifetime bug looks intermittent, add a barrier that proves the
thing exists before you conclude anything from it surviving once.

---

## Hosting the archive: what measuring found that reading did not

The hosted viewer (`cloud/`, `ingest.py`, `tiles.py`, `web/tiles.js`) publishes the archive
to S3 and serves precomputed tiles through CloudFront. Almost every bug below survived code
review and died the moment something was run.

### 15. No gzip made the derived data twenty times the raw

The first tile run produced **141 MB from 6.9 MB of raw**. Gzip takes it to 9.3 MB — 15x,
26x on the widest tiles — because a tile is long arrays of similar numbers. Final ratio:
agg 6.74 MB against raw 6.9 MB, i.e. **0.97x**, about $0.05/month per year of data. The
plan had estimated `agg/` at ~2 GB/year; it is ~1.17 GB, marginally *smaller* than the raw.

gzip `mtime` is pinned to 0 so the bytes are a pure function of the contents. Otherwise
every re-run of an unchanged tile is a new object, which defeats S3's ETag and every CDN
validator.

### 16. Widths below the capture cadence were 28% of the aggregate and unreachable

`choose_bucket` floors at the plan's cadence, so a 2 s tick can never resolve to a 1 s
bucket. Generating those tiles anyway cost more than a quarter of the whole aggregate for
objects no reader could ever request — and below the cadence most buckets hold one sample,
so min == mean == max and half are empty.

**The rule: generate only what the reader's own width selection can ask for.**

### 17. `series.extent()` under-reports on purpose, and tiling believed it

It returns the newest *file's first* record, and its docstring says so — the true end needs
a scan. Fine for the local viewer, which polls `/api/latest` separately. For tiling it
dropped every hour after the open file started: **37 minutes of the real archive**, which is
exactly the data anyone opens the page to see. An archive still on its first file would have
produced one hour of tiles however long it ran. `ingest.extent()` calls `series.latest()` to
do the scan.

### 18. Two off-by-one errors in counters, both plausible-looking

`_grid()` is end-inclusive, so adjacent tiles shared a bucket: a duplicated point at every
seam, and a counter's `last_value` read from the *next* tile. Truncating afterwards cannot
fix it, because `first_value`/`last_value`/`reset` are derived inside `series.window` — the
fix is `end_exclusive=True`.

Then: counters measured from the tile edge, not the window edge. A tile spans an hour; "the
last six hours" starts at :05. Grid import read **5.35 kWh against a true 5.23** — 2%,
always in the same direction, on the number the README calls the independent cross-check
that agrees to a watt-hour. Fixed by carrying per-bucket `first`/`last` arrays.

**Caught only by diffing the browser's composed answer against `serve.py`'s, field by
field.** A 2% error on a plausible number is invisible to inspection.

### 19. Four ways a deploy can ship or serve the wrong thing

1. **`TZ` is a Lambda reserved variable** — CloudFormation refuses to set it. The zone
   arrives as `CAPTURE_TZ` and the handler calls `time.tzset()` before anything computes a
   local time. Without it every axis label is twelve hours out.
2. **CDK's asset hash did not cover the code.** It fingerprints the asset *source* —
   `handler.py` plus `PACKAGE.txt` — so editing `series.py` gave "no changes" and left old
   code running. `assetHashType: OUTPUT` hashes what actually ships. Silently deploying
   stale code is the worst failure a deploy tool has.
3. **The package list was incomplete**, missing `config.py`, which `serve.py` imports.
   Symptom: `No module named 'config'` in CloudWatch after a clean deploy. Now `PACKAGE.txt`
   with a test that recomputes the transitive import closure of every handler. Usefully,
   `log.py` is *excluded* — the only module that constructs `lib.Modbus` — so the ingest and
   share paths are read-only **because that code does not ship**, a stronger claim than "it
   is never called".
4. **`RemovalPolicy.RETAIN` orphans a bucket when the FIRST deploy fails.** Rollback skips
   deleting it and the next deploy fails with "already exists". Delete the empty bucket by
   hand. RETAIN is still right — it protects telemetry that exists nowhere else.

### 20. `index.json` named only one plan, because an invocation can only see one

Built from `series.discover()` over `/tmp`, and an invocation downloads one plan — so the
recovered 1 Hz series vanished from the index the moment the main plan rebuilt. Now assembled
from every plan's published `meta.json`, which is the only view that knows about all of them.

Related: **`plan_of()` treated a date as a hash.** `sigen-20260814.manifest.json` yielded
`"20260814"`, because a date is eight valid hex characters, and would have filed legacy files
under `plan=20260814/` where nothing could decode them. Now it requires `log.py`'s three
dash-separated components. Found by a test written expecting it to pass.

### 21. Under OAC without `s3:ListBucket`, S3 returns 403 for a missing key

It will not confirm absence to a caller that cannot list. But **an absent tile is how
`web/tiles.js` learns the logger was off** — `ingest.py` writes nothing for a span with no
records. `tiles.js` deliberately does *not* treat 403 as absent, because on a gated path a
403 is a refusal and must never read as "no data". So the fix is `ListBucket` scoped to the
published prefixes.

This bit **twice**. The share Lambda's first version probed with `head_object` and caught
only 404, so every share failed with `An error occurred (403) when calling HeadObject` while
checking whether a brand-new share id was free. Catching 403 as absent would have fixed the
symptom and reintroduced exactly the confusion above — a broken policy would produce a share
full of gaps instead of an error. It now *lists* instead of probing: absence is unambiguous,
and `list_objects_v2` sends a prefix, which is what lets the IAM grant stay scoped.

**The rule: never let "forbidden" and "absent" collapse into one branch, in either
direction.**

### 22. A pre-existing, user-visible bug the tiling work surfaced

The logger reads model and serial *once* at startup, so a restart during a brief outage
writes a manifest whose device block is just an error. `discover()` keeps the newest
manifest, so `[Errno 64] Host is down` became the answer to "what hardware is this?" — a
claim about *now*, from a file written days ago. The real archive has exactly this: day one
has the model, days two and three have the error. `Series.device()` now prefers whichever
manifest resolved a model. **This fixed the local viewer too.**

### 23. A 200 is not "it renders", and curl hides the difference

`buildSite()` writes the hosted entry points with **no file extension**, because `/view` and
`/p/<uid>` are the contract. `BucketDeployment` derives `Content-Type` from the extension, so
both deployed as `binary/octet-stream` and **the browser downloaded a file called `view`
instead of rendering the viewer**. The address bar never moved, which read exactly like being
bounced back to sign in — and the sign-in it appeared to be failing had in fact succeeded
minutes earlier.

Every check said fine. `curl` returned `200`. The handoff listed `/` and `/p/<id>` as
"verified working" on that basis. Nothing exercised whether a browser would *render* the
body. This is finding 12 — *"it is listening" is not "it is reachable", and curl hides the
difference* — recurring one layer up, at content type instead of at reachability.

The fix is a second `BucketDeployment` that declares `text/html`, plus a synth-time check
that every extensionless file it writes is one the stack knows to declare a type for.

**The rule: for anything a browser renders, assert the Content-Type, not the status.**

### 24. A frozen share aged, and started accusing the logger of dying

`tiles.js` computes `record_age_s` and `logger_stalled` from the clock on every read,
deliberately: a stored age would be wrong the moment after it was written, and a stored
`logger_stalled` would be permanently true on a page whose data is merely an hour old.

Correct for the live page, and exactly wrong for a frozen share. `latest.json` is copied at
share time and never touched, so measuring against the clock makes its ages grow forever —
and with `stall_after_s` at 7200, **any share opened more than two hours after it was made
rendered a red "the logger may have stopped"** over data that was current when it was sent.
The share was fine; only the clock had moved.

So "now" is a property of the source, not of the process: `nowFor()` returns `meta.shared_at`
for a frozen source and the clock otherwise. Verified by doctoring a share's `shared_at` to
60 s after its newest record — the page reports "data 60 s old when shared" and no stall,
where the clock would have said 4.6 h.

### 25. The read gate could not be changed, and said nothing while failing

Two independent problems, and the second hid the first.

**It was undeployable.** `SiteStack` takes the Lambda@Edge *version* ARN from
`SigenAuthEdge`, and CDK turns that into a CloudFormation export whose name embeds the
version's logical id, which embeds the code's asset hash. So editing one line of the gate
renames the export, and CloudFormation refuses: `Cannot delete export … as it is in use by
SigenSite`. An export's value cannot be changed while it is imported, and the old value's
resource is gone, so there is nothing to keep alive. Un-gating the site to break the cycle
would open a window where the telemetry is public, which is the one thing this must not do.
The fix is `@aws-cdk/core:defaultCrossStackReferences: weak`, which replaces
`Fn::ImportValue` with `Fn::GetStackOutput` — resolved by the CLI at deploy time and inlined,
so there is no export to deadlock on.

**It was silent.** Across 18 invocations the gate emitted zero log lines — no allow, no deny,
no reason. So "the cookie was rejected", "there was no cookie" and "it worked and something
downstream broke" were indistinguishable, and diagnosing a sign-in problem meant inspecting a
browser's cookie jar instead of reading a log. It now logs one line per decision, with the
reason a verification failed, while returning a byte-identical response — the visitor still
cannot tell "expired" from "forged", which was the original and correct instinct that had
been over-applied to the logs as well.

**The rule: a security decision that emits nothing cannot be operated. Log the decision, not
the credential.**

### 26. Wrong arithmetic, right answer, and the wrong one shipped to the UI

The field picker needs a window wide enough to reach the day tiles. The threshold is 15
hours, and both the design notes and `ingest.py` said so — with the derivation "a 120 s
bucket needs 108,000 s of span". 108,000 s is **30** hours.

The real reason 15 h is correct: `choose_bucket` rounds *up* to the next ladder width, so a
120 s bucket is chosen as soon as `span / TARGET_BUCKETS` passes the width *below* it —
`60 * 900 = 54,000 s`. 108,000 s is the *top* of the `b120` band, not its start.

The prose reached the right number by the wrong route, so it read as verified. Then
`web/app.js` computed `picker_min_bucket_s * 900` from the same reasoning and told users
**"widen the window to 30 h"** when 15.1 h suffices — a user-visible factor of two, live for
the life of the feature.

Worse: the first test written for this passed against the buggy JavaScript, because it
asserted `choose_bucket`'s behaviour in Python and never touched the page's copy of the
reasoning. **Mutating the fix back is what exposed the gap.**

**The rule: a plausible derivation attached to a correct number is not a checked derivation —
and a test for shared arithmetic has to exercise every implementation of it, not the
convenient one.**

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
- **The viewer is unproven past a few days of archive.** Its per-file summary cache
  is in memory only, so a restart re-warms lazily, and nothing has been measured
  against a month or a year of files. If deep history gets slow, the fix is an
  on-disk summary cache — rotated files are immutable, so one is safe to keep — not
  a coarser bucket ladder.
- **The viewer's effect on capture latency has not been measured.** It is
  `ProcessType: Background`, nices itself, and serialises decoding on one thread,
  but "the logger is unaffected" is a design argument, not an observation. Watch
  `decode.py --latency` across a period of heavy browsing before believing it.
- The smart-load span `30124..30199` is deliberately uncaptured, and `30163+124` is
  rejected by the device, so folding it in would need a 4th request.
