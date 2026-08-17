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

### 27. CloudFront signs the request; Lambda refuses an unsigned payload, and the 403 lands before any log

"Create link" answered **`Could not create the link: 403 .`** — a status, a space, and a full
stop. Every browser click had always failed that way, on every window.

`/api/share` is a Lambda function URL with `AWS_IAM` auth behind Origin Access Control, so
CloudFront signs each origin request with SigV4. For a function URL that is not enough:

> If you use `PUT` or `POST` methods with your Lambda function URL, your users must compute
> the SHA256 of the body and include the payload hash value of the request body in the
> `x-amz-content-sha256` header when sending the request to CloudFront. **Lambda doesn't
> support unsigned payloads.**

Nothing sent it. Lambda recomputed the body hash, the signature did not match, and the
function URL answered 403.

Both halves of that are measurable against the deployed endpoint, and worth doing before
believing any of it — sign a POST to the function URL twice with the same body, once over the
body and once over an empty payload:

```
signature over the ACTUAL body  : 400 {"ok": false, "error": "hours must be a number, …"}
signature over an EMPTY payload : 403 {"message":"The request signature we calculated does
                                       not match the signature you provided…"}
```

Note the shapes. An *unsigned* request gets `{"Message":"Forbidden"}`; a *mis-signed* one gets
`{"message":"The request signature…"}`; a crashed handler gets `{"message":"Internal Server
Error"}`. Capital and lowercase both occur, none of them is `error`, and the 400 is the only
one of the four that the page could read.

**Three separate things then conspired to say nothing.** The refusal happens at Lambda's
*authorizer*, so the handler is never invoked and its log group stays **empty** — the deploy
looked healthy because nothing had failed in it. The gate, one hop earlier, logged
`{"gate":"allow","uri":"/api/share"}`, so the last thing in any log was a success. And the
refusal body carries `Message`, not `error`, so `web/app.js` — which reads `error` — fell back
to `r.status + ' ' + r.statusText`, and `statusText` is always `''` over HTTP/2. Hence "403 ".

What made it findable was the *pair* of logs, read together: the gate said `allow` at
04:39:21, 04:39:53 and 04:40:08, and the share Lambda had not been invoked since 03:42. A
refusal between two hops that both claim innocence is the hop *between* them.

It is also why it survived a session of testing that looked thorough. The Lambda had been
verified by direct invocation, and the resulting `/p/<uid>` page by headless Chrome — both
real, neither of them through CloudFront. The one untested hop was the only broken one, and
the handoff notes said so in writing: *"Nobody has clicked the Create link button in a
browser."*

The gate now hashes the body, because the OAC is what demands it and the gate is already on
that behaviour: one place, and any caller works. That needs `includeBody: true`, which brings
its own trap — CloudFront truncates a viewer-request body at 40 KB before exposing it, but
sends the **full** body to the origin when the function leaves it read-only, so hashing a
truncated body signs bytes the origin never receives. Same 403, one layer down, with the
header present and looking correct. So an oversized body is refused with a 413 instead. The
two limits are now coupled and worth watching: the gate can hash 40 KB, while
`_strings(value, limit=400)` in the share handler would accept about 41.5 KB. Nothing the UI
can produce comes near either — all 10 panels, all 259 catalog fields and a full 2000-character
note is 10.0 KB — but the ceiling above the cap is the next way this breaks.

**The rule: a refusal that happens before your code runs leaves no log in your code. When
every log you own says "fine", suspect the hop between two of them — and make every layer
that can refuse a request say so in the one shape the caller already reads.**

#### 27b. …and behind it, a second cause wearing the same 403

The payload hash was real, documented and necessary — and fixing it did not make the button
work. The next click failed with a 403 again, except the page could now quote it:

```
403 Forbidden. For troubleshooting Function URL authorization issues,
see: https://docs.aws.amazon.com/lambda/latest/dg/urls-auth.html
```

That is a *different* string, and the difference is the whole diagnosis. Signing the same POST
four ways maps every 403 this endpoint can produce:

| How the request is signed | Response |
|---|---|
| Not signed at all | `{"Message":"Forbidden"}` |
| Signed over an empty payload, or with `UNSIGNED-PAYLOAD`, while sending a body | `{"message":"The request signature we calculated does not match…"}` |
| Correct payload hash, **admin** credentials | **400** from the handler — it arrives |
| Correct payload hash, **CloudFront** | `Forbidden. For troubleshooting Function URL authorization issues…` |

The browser's 403 is not the signature string, so authentication was **succeeding**: the gate's
hash was accepted, and *authorization* was refused. Which AWS documents plainly:

> Starting in **October 2025**, new function URLs will require both `lambda:InvokeFunctionUrl`
> and `lambda:InvokeFunction` permissions. […] If a function's resource-based policy doesn't
> grant [both], users get a 403 Forbidden error code.

`aws-cdk-lib` 2.265.0's `FunctionUrlOrigin.withOriginAccessControl()` grants exactly one of
them. The same library adds *both* for `authType: NONE` a few files away, so it knows the
requirement; the OAC path simply does not do it. The function URL here was created after the
cutoff, so it needs the second grant, and `site-stack.ts` now adds it explicitly.

**The part worth keeping is how the first fix was "verified".** A SigV4 probe against the
function URL, signed correctly, returned the handler's 400 — an apparently perfect end-to-end
proof. It was signed with `AdministratorAccess` credentials. When a caller in the same account
has the actions in its *identity* policy, the resource policy is never consulted, so that probe
could not have detected a missing resource-policy action however many times it was run. It
proved authentication and was silent about authorization, while looking like proof of both.

**The rule: a probe that authenticates differently from production proves nothing about
production's authorization. Test as the principal that will actually call — or admit the test
covers only the half that principal shares with you.**

---

## Installing the uploader: what the archive's own lifecycle turned out to hide

### 28. `.bin.gz` is what travels, so a `.bin` that never rotated never leaves

`sync.py` uploads rotated records and manifests and skips the open `.bin`, correctly: it grows
every couple of seconds, so uploading it means re-uploading a partial file forever. What that
also means is that a `.bin` which *never* rotates is never uploaded, and nothing said so.

`log.py` gzips the open file when it rotates. A logger killed mid-hour leaves the `.bin`
behind, and no later run gzips it — so that hour reaches S3 only if somebody notices. There
are four such files in this archive, from commissioning-day restarts on 2026-08-14. They are
offsite only because `cloud/backfill.py` swept the directory once and did not care about
extensions.

`sync.py --status` now names them. The test that separates a stranded file from the open one is
not its name or its age but **the existence of a newer `.bin.gz`** — that is the proof rotation
has happened since, so this file is finished and was simply never compressed. Age would be
wrong: the open file is the newest thing there and is not stranded. Without that test the
current hour is reported every five minutes, for the whole hour, every hour, and a warning
that is always on is a warning nobody reads.

It deliberately does **not** affect the exit status. A file already carried offsite by another
route is stranded here forever, so failing on it would pin `--status` to 1 for good — and a
permanently-red signal says less than silence.

**The same lifecycle produced a second, quieter problem.** That one-off backfill uploaded the
then-open `.bin`, and rotation later produced the `.bin.gz` for the same stem. Both sat in
`raw/plan=08c047b8/`, and `series.discover()` admits `.bin` *and* `.bin.gz` into one Series
group — so that hour decoded twice. `sort_chronologically` breaks the identical-stamp tie on
basename, so the `.bin` sorts first and `spans()` gives it `last_ts == first_ts`; but
`FileSpan.overlaps()` returns true for a degenerate span inside the queried range, so it is
still read. Mean, min and max absorb duplicated identical samples, which is why nothing looked
wrong. Confirmed harmless to delete by decompressing both and checking the partial was a
byte-exact prefix of the rotated file — 721,228 of 1,175,904 bytes — which is a stronger claim
than comparing record timestamps.

**Two `.bin` extensions for one span is a collision the archive format permits and the reader
does not detect. Check for it after any backfill that predates the uploader.**

### 29. A red pill blamed the logger for the uploader's absence

Two live shares rendered `no record for 4.5 h when shared — the logger may have stopped`, and
`11.1 h` on the other. FINDINGS 24's fix was working exactly as intended: the age was measured
from `shared_at`, not the clock, so it did not grow after sending.

The sentence was still wrong. The logger had not missed a tick — `~/sigen/data` held a record
from minutes earlier the whole time. What had stopped, or rather had never started, was the
*uploader*: `sync.py` was not installed, so S3 was eleven hours behind a healthy capture.

On a `tiles` source `record_ts` is the newest record that reached the bucket, and that only
advances on rotation-plus-upload. A stopped logger and a stopped uploader are therefore the
**same symptom**, and the page has nothing to tell them apart with. It now names both there,
and still names the logger alone on `serve.py`, which reads the archive directly and where the
inference is available.

**This is FINDINGS 7's distinction one hop further out.** That one separated "the logger
stopped" from "the device stopped answering" and kept them apart because conflating them
misdirects the person reading. The hosted page added a third component — the uploader — and
inherited a message written when only two existed.

**A page can only name the failure its evidence distinguishes. Where two components produce an
identical symptom, name both or name neither.**

### 30. A tile was promised immutable for a year, from raw that had not arrived

Reported from the live viewer: 11:06 to 12:00 NZST on 2026-08-16 read as **"no records"**
while everything either side of it was fine. In UTC that is hour 23 of 2026-08-15.

`complete()` asked one question — has the clock passed the end of this span? It had, by eleven
hours. So a rebuild run at 15:50 NZST wrote that hour with `Cache-Control: public,
max-age=31536000, immutable`, built from raw that stopped 54 minutes short of the hour's end
because the uploader had not been installed yet.

**Two things then made it invisible rather than obviously broken.** The tile's `covered` range
claimed the whole hour anyway — `series.window()` extends the newest file to the end of the
window it was asked for, since it has no way to know where an open file ends. So `web/tiles.js`
read those buckets as *covered, with no records*, which is precisely how a real outage looks.
And the promise was kept: uploading the rest of that hour rewrote the object in S3 and could
not dislodge the copy in the CDN. It took a manual `/agg/*` invalidation, and then a hard
reload, because the browser had cached the same year.

The architecture notes state the invariant plainly: *a tile is written once from a complete
span, then never rewritten — which is what makes `immutable` safe.* The clock does not
establish that a span is complete, because **raw can arrive late**. Here it arrived eleven
hours late, which is how long the uploader took to install; a backfill of any old date does the
same thing.

`complete()` now takes `data_end`, the archive's true newest record from `ingest.extent()` —
which scans, rather than trusting `series.extent()`'s indexed end (FINDINGS 17) — and calls a
span finished only once the archive reaches *past* it. Whatever gaps lie inside such a span are
real gaps, because the archive demonstrably continued through them.

**The fix on its own would have made things worse, quietly.** The hour a rotation closes now
starts out fresh, and nothing revisited it — so every hour tile would have sat at `max-age=60`
revalidating forever, undoing the reason the tiles are precomputed at all. `run_for()` also
rebuilds the span *before* the earliest it is handed: identical bytes, changed header, the same
reasoning the month block already used. Explicitly, not by relying on a rotation straddling the
UTC boundary — it usually does, since `rotate_minutes` rarely divides the hour, but an aligned
one would leave every hour fresh forever, and that is too quiet to rest on a coincidence of
phase. The test for it rotates *on* the hour for exactly that reason.

Months tightened rather than loosened: an incomplete-by-data month now gets no tile, where the
clock alone used to be enough. The reader falls back to that month's day tiles, which is the
designed path, and a month tile short of its last days would have answered for them instead.

**`immutable` is a promise about the future made from the present. Only make it from evidence
that the input is finished — the clock is evidence about the clock.**

---

## Verifying the handoff: two ways a finished task can still be wrong

Both of these came out of a pass whose only goal was to confirm that work already recorded as
done really was done. Neither is a new feature's bug. One is a trap laid by the *fix* in
FINDINGS 28, and the other is the difference between "I rotated the credential" and "the
credential works".

### 31. Gzipping a stranded file recreated the collision FINDINGS 28 had just described

FINDINGS 28 added a `sync.py --status` warning naming the four stranded `.bin` files, and closed
by saying two `.bin` extensions for one span is a collision the reader does not detect: *check
for it after any backfill that predates the uploader.* The collision then happened again — from
the opposite direction, and by acting on that very entry's suggestion to gzip them so the
permanent warning would go quiet.

`gzip` on the Studio, one sync tick, and `raw/plan=08c047b8/` held both
`sigen-…T092508-08c047b8.bin` (uploaded by `backfill.py` on 2026-08-16) and `….bin.gz`
(uploaded by `sync.py` on 2026-08-17) for all four stems. `backfill.py`'s `upload_series()` keys
on `os.path.basename(p)` and does not care about the extension, so the `.bin` had been there all
along under a name nobody thought to look for.

**The first check was wrong, and confidently.** Asking "are these already in S3?" by listing
`….bin.gz` found exactly one version, created minutes earlier, and produced the conclusion that
the handoff's "already offsite via `backfill.py`" was false — that four hours of raw had sat on
one disk for three days. It was true; it was under `.bin`. **When two extensions can carry the
same content, the absence of one proves nothing.** Same shape as FINDINGS 21: a lookup that
cannot see the other case answers the wrong question without hesitating.

**The detection signal is cheap and specific.** Duplicated identical samples vanish into mean,
min and max (FINDINGS 28), the tiles render, and the numbers look plausible. What does not hide
is the tile's own bookkeeping — a **zero-length `covered` sub-range**, which is
`sort_chronologically` breaking the identical-stamp tie on basename so the `.bin` sorts first
and `spans()` hands it `last_ts == first_ts`:

```
BEFORE  records 1554   covered: 3 sub-ranges
          21:25:08Z .. 21:25:08Z   (0s)      <- the giveaway
          21:25:08Z .. 21:38:25Z   (797s)
          21:38:25Z .. 22:00:00Z   (1294s)
AFTER   records 1125   covered: 2 contiguous sub-ranges, none degenerate
```

429 phantom records in one hour tile. **A degenerate zero-length range in `covered` means a file
is being read twice.**

**The cleanup is four steps, and skipping the last one leaves it broken for a year.** The
affected spans were old and closed, so their tiles were already published `immutable` — FINDINGS
30 applies and rewriting S3 is not enough:

```sh
# 1. Every stem carrying both extensions. Empty output = healthy. Add to any post-backfill check.
aws s3 ls s3://<bucket>/raw/ --recursive --profile <p> | awk '{print $4}' \
  | grep -E "\.bin(\.gz)?$" | sed -E 's#\.bin\.gz$##; s#\.bin$##' | sort | uniq -d
# 2. Prove equivalence before deleting either: sha256(.bin) == sha256(gunzip -c .bin.gz).
# 3. Delete the .bin, keep .bin.gz -- the form that travels. Versioned bucket, so it is a
#    delete marker, not a loss. Note the logger principal is denied delete; use an admin.
# 4. Rebuild in ONE pass -- '{"rebuild":"<plan>"}' -- then invalidate /agg/*.
```

**A warning that a fix adds is an instruction someone will follow. FINDINGS 28 made the stranded
files visible and implied gzipping them; nothing on that path checked whether the same span was
already offsite under another name.**

### 32. A credential's timestamp is evidence about a file, not about the credential

The Google client secret was reported rotated. The local evidence disagreed:
`cloud/.google-secret` unmodified since the previous afternoon, `SigenAuthPool` not updated since
before the handoff was written, and the handoff itself still listing rotation as outstanding. A
rotation that stops at Google and never reaches Cognito breaks every *fresh* sign-in while
leaving everything else looking healthy — `allow` decisions in the gate log ride an existing
cookie and never touch Google at all, so the logs cannot see it.

Comparing `sha256` of the file against `describe-identity-provider`'s `client_secret` establishes
that the file and Cognito **agree**. It does not establish that either is current: two copies of
the same dead secret match perfectly.

**Ask the issuer.** Google validates client credentials *before* the grant, so a deliberately
invalid authorization code separates the two failures in one unauthenticated request:

```sh
curl -s -X POST https://oauth2.googleapis.com/token \
  -d "client_id=$CID" -d "client_secret=$(cat cloud/.google-secret)" \
  -d "grant_type=authorization_code" -d "code=deliberately-invalid-probe" \
  -d "redirect_uri=https://<prefix>.auth.<region>.amazoncognito.com/oauth2/idpresponse"
```

`invalid_grant` ("Malformed auth code") means the id and secret were **accepted** and only the
junk code refused — the credential is live, and this is the pass. `invalid_client` means Google
rejects the secret, Cognito holds a dead credential, and fresh sign-in is already broken. It
answered `invalid_grant`, so the rotation was genuine.

The corroborating evidence agreed once it was read correctly: a cold sign-in by a
**non-allowlisted** account — which had no Cognito session to ride and therefore had to complete
the Google exchange — succeeded after Cognito's last update, and the callback Lambda shows 0
errors across 24 h.

**For anything whose validity lives in another system, verify against that system. A local mtime
and a matching hash are consistent with both success and total failure — the same shape as
FINDINGS 30, where the clock was evidence about the clock.**

---

## Two controls the cloud viewer could not honour

Both were found by asking why the hosted page behaves worse than the local one. Neither is a
bug in anything that was measured; both are a control whose *precondition* was never true, and
in both cases the repository's own documentation already said so.

### 33. The id token was the session, so Google was the session store

The hosted viewer asked for a Google sign-in several times a day. Three facts, none of which is
wrong on its own:

- `auth-callback` read `id_token` out of the token exchange and discarded the rest of the
  response. The `refresh_token` was in it the whole time.
- The Cognito app client set no `idTokenValidity`, so Cognito issued one valid for **one hour**
  — its default. `describe-user-pool-client` reported `IdTokenValidity: null`.
- The session cookie's `Max-Age` was 12 hours, with a comment explaining that as "long enough
  not to interrupt an afternoon of looking at charts".

The cookie was therefore never the thing that expired. The gate refused the token inside it
every hour and redirected to Cognito, which redirected to Google — and *that* was the session:
whether the hop was silent depended on Google's account chooser, on the consent screen's
publishing status, and on whatever else the browser had signed into. The 12-hour comment
described a property the code did not have, which is why nobody looked at the hour.

What makes it hard to see from the outside is that the mechanism was *working*. Every hop
succeeded. The gate logged `allow` afterwards, the callback Lambda logged no errors, and a
CloudWatch reader would find nothing at all wrong. It is the same blind spot as FINDINGS 32,
where `allow` decisions rode an existing cookie and never touched Google: **a log of successes
cannot show you how often success was needed.**

The fix is the token that was being thrown away, held in a cookie scoped to `Path=/auth/` so it
never rides along on the hundreds of `/agg/*` fetches a page makes, and spent at a new
`/auth/refresh` route on the same Lambda. Google is now touched about once a month.

Two things fell out of it that are worth keeping apart from the fix:

- **A long-lived credential is not the same as a long-lived permission.** A 30-day session gives
  up the stolen-laptop case, and the reason that is affordable is that the allowlist is
  re-checked *at the edge on every single request*. Removing an address takes effect on the next
  tile fetch whatever any token's lifetime says. The cookie buys a redirect, not access.
- **`ExplicitAuthFlows: null` is not "the defaults are fine", it is "nobody decided".** Cognito's
  legacy defaults do include `ALLOW_REFRESH_TOKEN_AUTH`, so the grant would probably have worked
  — but the entire session now rests on it. CDK emits the property only for a *non-empty*
  `authFlows` object and appends the refresh flag itself, so `authFlows: {}` silently emits
  nothing and `authFlows: { userSrp: false }` is how you ask for exactly the refresh flow.

**A credential's lifetime is a fact about a system, not about the comment above the cookie.
Read it back from the system — `describe-user-pool-client` answers in one call.**

### 34. "Live (10 s)" could not return new data at any interval

The hosted page shipped with a checked *Live (10 s)* box. It could never work, and the reason is
one line in `web/tiles.js`: `getObject()` caches every fetched object in a `Map` with no expiry,
and `ensureMeta()` caches `index.json`, `meta.json` and `latest.json` the same way. A poll
re-rendered bytes the page already held.

The only network traffic it did produce was the one case that makes it worse. As the window
slides, a poll eventually asks for a tile in a new UTC hour — which is normally *not published
yet*, because tiles appear when the logger rotates and `sync.py` uploads within five minutes of
that. The 404 is then cached as `null`, which is how this reader represents "the logger was off
for that span". So the one thing polling accomplished was to record a permanent gap over the
hour that was merely still in flight.

`Reload` has the same defect, which is why it is hidden rather than kept: a browser reload is
genuinely the only thing that picks up a new batch. `Download CSV` was a third one — it resolved
to `/agg/api/csv?…`, a key that does not exist, so it downloaded a 404. All three are now
`data-server-only` in the one page, hidden by a `POLLS` derived from `SRC.kind` rather than by a
flag anyone has to remember in `site-stack.ts`.

`cloud/README.md` had said it all along: *"No live view. Tiles appear when the logger rotates, so
the hosted page is up to an hour behind and says so. `serve.py` on the LAN is the live one."* The
page had been contradicting the design document for as long as both existed, and the contradiction
was the default state of a checkbox.

**When one renderer serves two sources, a control is only real if the source can answer it. Derive
that from the source — a capability a deployment has to remember to switch off is one it will
eventually ship switched on.**

---

## What "frozen" turned out not to cover

Found while adding a way to point a link at one panel, which meant reading what a share link
actually resolves to.

### 35. A share was frozen in its data and live in its code

The share handler is emphatic about why it copies rather than points, and it is right:

> **Copies, not pointers.** A pointer into `agg/*` could not be read anonymously — that prefix is
> gated — and re-aggregating history would silently change what someone was sent. A copy costs a
> few hundred kilobytes and means a link keeps showing what it showed on the day.

Every word of that is about the numbers. None of it was true of the code that draws them.
`/p/<uid>` was rewritten at the edge to one `site/share-view` object serving every share id, and
that object loaded `/app.js`, `/tiles.js`, `/charts.js` and `/style.css` from the bucket root —
the same five keys `cdk deploy SigenSite` overwrites in place. A share's tiles were immutable for
a year; its renderer was immutable until the next deploy.

Nothing about it looks wrong from either end. The share is genuinely a copy, the tiles genuinely
cannot change, and the page genuinely renders — today, with today's code, which is also the code
that was current when the share was made. The defect has a *latency* built into it: it cannot be
observed at all until the renderer changes, and by then whoever holds the link is not looking at
it beside the original. FINDINGS 24 was the same shape one layer up — a frozen share whose *ages*
were still measured against the clock, so it aged into accusing the logger of dying. Both are the
word "frozen" asserted about one half of a page.

Two things made the fix cheap enough not to argue about:

- **Content-addressing turns "keep every version" into "keep every distinct version".** The bundle
  key is a hash of the page and everything it loads, so a redeploy of unchanged code writes the
  same keys again rather than accumulating one bundle per deploy. What is retained is bounded by
  how often the viewer actually changes, not by how often it is deployed.
- **The page is copied; the code is shared.** Each share gets its own ~9 KB `page.html`, a
  byte-for-byte copy of the published bundle's page, and that page names `/v/<version>/app.js`.
  Twenty shares made from one bundle are twenty small HTML objects and one copy of the renderer.
  Copying the JS per share would have been simpler to write and would have needed the page
  rewritten per share, which trades a guarantee ("the same bytes") for a string operation.

The pruning line is the one to be careful with. The deployment that uploads the site owns
`--delete` over the `site/` prefix, and previously published bundles are not in its source — each
was deployed and forgotten. They survive only because `v/*` is in its `exclude` list, which
withholds a key from deletion as well as from upload. Removing that line as untidy would leave a
green build, a working `/view`, and every share sent before that day rendering blank.

**"Immutable" is a claim about a whole page, and a page is code plus data. Check the claim against
both halves — the half nobody thought of as content is the half that gets overwritten in place.**

### 36. A headless screenshot of a scrolled page is blank, and the page is fine

`#focus=<panel>` scrolls the focused card into view. Checking it the obvious way —
`--headless=new --screenshot` at a viewport short enough to need scrolling — produced a
**uniformly blank image** for every target except the first panel, whose offset happens to be
near zero. The DOM dump from the same run was correct and byte-identical to the working case.

The page was never wrong. `--screenshot` captures with `captureBeyondViewport`, which composes
the clip from the scroll offset *and* then scrolls, so it lands at roughly `2 × scrollY`. For a
1449 px scroll on a 2579 px document that is past the end, hence the blank. The tell was there
and easy to misread: **no sticky header in the image**. A sticky `.topbar` is painted at the top
of the viewport at every real scroll position, so an image without it is not a view of the page
at any offset.

What settled it in one run was measuring instead of looking — a temporary script in the page
writing `window.scrollY`, `document.documentElement.scrollHeight`, the topbar's height and the
card's `getBoundingClientRect().top` into `document.title`, which `--dump-dom` then reports.
`cardTop=77` against a 69 px header is the assertion the screenshot could not make.

This is the same lesson as FINDINGS 23 from the other direction. There, `curl` said 200 while a
browser downloaded the file: the cheap check was too weak. Here the expensive check was
*actively misleading*, and the cheap one was right.

**A rendering check is only evidence if you know what the renderer does with the request. When an
image and a DOM disagree, believe the DOM and go find a number.**

### 37. The documented rebuild had already outgrown its timeout, and said 200

Renaming two panels changes `PANELS`, which `ingest.py` copies into each plan's `meta.json`. No
raw file arrives to trigger that, and `rebuild()`'s docstring names exactly this case: *"a
tile-format change, where every tile has to be rewritten and no raw file is arriving to trigger
it."* So `{"rebuild": "<planhash>"}` was the documented tool. It does not work any more.

Measured on four days of archive: three invocations, each killed at the function's **300 s
limit**, 1235 MB of 1769 MB used. `ingest.run()` writes tiles before documents, so every attempt
rewrote tiles it did not need to change and none reached `write_documents()` — the one thing a
presentation-only change actually needs. `meta.json` kept its old timestamp, which is how this
was noticed at all.

Three things conspired to make it look like a success:

- **`aws lambda invoke` retries a read timeout.** The CLI's default read timeout is 60 s, well
  under the function's 300 s, so it gave up and re-invoked — twice. Three rebuilds of one plan
  ran concurrently, which this function has no reserved concurrency to prevent (`data-stack.ts`
  explains why it cannot: a new account's total concurrency quota is 10, and reserving any of it
  is rejected outright). Harmless here only because the tiles were byte-identical.
- **The CLI reported the timeout and left the output file alone.** `--cli-binary-format ... out.json`
  was never written, so `cat out.json` printed a **stale response from the previous day** —
  `{"objects": 489}`, a genuine success, for a different invocation, 20 hours earlier. Confirmed
  by its mtime. A response file is not a response.
- **The docstring watched the wrong resource.** It bounds itself by `/tmp` at 512 MB and predicts
  trouble "somewhere in the second year". Time ran out in the first week.

What worked, and is the right tool for a documents-only change, is replaying one S3 event —
`{"Records":[{"s3":{"object":{"key":"raw/plan=<hash>/<newest>.bin.gz"}}}]}`. That is the
incremental path, it is ~62 s, and `write_documents()` is FRESH on every event, so it republishes
`meta.json` unconditionally. Simply waiting for the next hourly rotation would have done it too.

Both numbers are worth keeping: the incremental path was measured at ~10 s on two days of archive
and is 62 s on four. Whatever makes that grow is not the constant-time day rebuild the comment
describes, and it is the number to watch — it is the one the steady state depends on.

**A background job's stated limit is a guess until something measures it, and the resource that
runs out first is rarely the one the comment is watching. Verify the effect, not the exit code —
and never from a file the failing command did not write.**

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
- **The hosted viewer cannot pick up a new batch without a browser reload.** `web/tiles.js`
  caches every object it fetches for the life of the page and has no invalidation, so nothing
  in the page can ask for a newer tile — which is why `Reload` is hidden there rather than
  wired up, and why the hint says to reload the browser (FINDINGS 34). A negative result is
  cached too: a tile that 404s because it is not published yet stays "absent" until the page
  is reloaded. If this becomes worth fixing, the shape is `getObject()` recording whether the
  response was `immutable` and dropping only the mutable entries and the cached 404s;
  `Tiles._reset()` exists for the tests and is too blunt, since it re-fetches a year of
  immutable tiles.
- **The hosted viewer shows one plan, so history before the last cadence change is
  invisible.** `web/tiles.js` reads `index.current` and nothing else, so a window that
  reaches back past a `plan=` boundary draws the older side as a gap even though the tiles
  exist under the previous hash. A share is faithful to that — it copies what was on
  screen — but "no data" there means "not in this plan", not "the logger was off". The
  local viewer has the same seam and answers it differently: `serve.py` groups by plan
  hash itself.
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
