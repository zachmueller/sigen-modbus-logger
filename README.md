# Sigenergy SigenStor local telemetry

Read-only Modbus TCP capture and decode for a Sigenergy SigenStor, over your own
LAN. No cloud, no account, no vendor API. Stdlib-only Python 3.9+ — no
dependencies, nothing to `pip install`.

**Nothing here writes to the inverter.** Holding registers are read with function
code 3 — which is a read — and no write function code is ever issued. Registers
documented write-only are skipped entirely. `tests/` asserts both.

The design in one line: **capture wide, decode narrow.** Modbus cost is per
*request*, not per register, so read the largest block the device accepts, store
the raw bytes, and decide which fields you care about later.

For *why* things are the way they are — the latency measurements, the device
quirks, the analysis mistakes worth not repeating — see
[docs/FINDINGS.md](docs/FINDINGS.md). This file is the operating manual.

---

## Status and compatibility

Developed against, and tested on, exactly one unit:

| | |
|---|---|
| Model | SigenStor EC 10.0 SP AU — single phase, one battery pack |
| Firmware | `V100R001C22SPC116` |
| Host | macOS, `/usr/bin/python3` 3.9.6, running as a LaunchDaemon |
| Cadence | 0.5 Hz fast tier, indefinitely |

It should work on any SigenStor reachable over Modbus TCP, but *nothing about
three-phase, multiple inverters, AC/DC chargers or an EV charger has been
exercised* — those register groups are read and reported, and the unit here
refuses most of them. Expect to widen the block plan for a bigger install. See
[Known limits](docs/FINDINGS.md#known-limits) for what is unproven.

---

## Safety and privacy

Worth reading once before pointing this at your own hardware.

- **Read-only, by construction.** Only FC3 and FC4 are ever issued. Both are
  reads. Nothing in this repository can change an inverter setting, and the test
  suite fails if a write function code appears in the source.
- **Modbus TCP has no authentication.** Anyone who can reach port 502 on your
  inverter can read it, and — with other software — write to it. Keep the
  inverter on a trusted LAN or its own VLAN. **Never port-forward it.**
- **Only one client should poll at a time.** Concurrent-client behaviour is
  unmeasured. If Home Assistant is already polling, don't also run this.
  `log.py --check` counts as a second client; run it deliberately, not on a timer.
- **Archives identify your installation.** Every manifest records the unit's
  model, serial number and the address polled. Set `"manifest_identity": false`
  before sharing raw archives — decoding needs none of it. `dump.py --json`
  writes the serial in plain text too; `.gitignore` covers both by default.
- **Your inverter is grid-tied equipment.** Polling it is passive, but this
  software comes with no warranty of any kind (see [LICENSE](LICENSE)), and how
  your installer or utility feels about third-party clients is between you and
  them.

---

## Quick start

```sh
cp config.example.json config.json      # then set "host" to your inverter's IP
python3 config.py --show                # confirm what resolved

python3 dump.py                         # decode every documented register, once
python3 log.py --seconds 60             # capture 60 s into ./data
python3 decode.py data/*.manifest.json data/*.bin --last
```

Every script takes the host as an optional first positional argument, which
overrides the config file:

```sh
python3 dump.py 192.168.1.50
```

Finding the inverter: it is whatever address your router gave the SigenStor's
network module. `nc -z <ip> 502` confirms the port is open. Give it a DHCP
reservation — the archive is keyed to nothing but the plan hash, so a changed
address costs you only a config edit, but a silent change looks like an outage.

Run the offline test suite any time, with or without hardware:

```sh
python3 -m unittest discover -s tests
```

---

## Configuration

Everything installation-specific lives in `config.json`, which is **not
committed**. Copy [`config.example.json`](config.example.json) and edit. Only
`host` is required; every other key falls back to the default in
[`config.py`](config.py).

Resolution order, later wins:

```
DEFAULTS  <  config.json  <  SIGEN_* environment  <  CLI flags
```

The file is looked for at `$SIGEN_CONFIG`, then `config.json` beside `config.py`,
then `~/.config/sigen/config.json`.

| Key | Default | Meaning |
|---|---|---|
| `host` | — | **Required.** Inverter address. |
| `port` | `502` | Modbus TCP port. |
| `timeout_s` | `6.0` | Socket timeout. This is also what an outage costs per attempt. |
| `plant_unit` | `247` | Slave id of the plant/EMS. |
| `inverter_unit` | `1` | Slave id of the first inverter. |
| `install_dir` | this directory | Root for `data/` and `logs/`. |
| `data_dir` | `<install_dir>/data` | Archive directory. |
| `log_dir` | `<install_dir>/logs` | Daemon stdout/stderr. |
| `python` | `/usr/bin/python3` | Interpreter written into the launchd plist. |
| `launchd_label` | `local.sigen-logger` | Daemon label. Change it and boot out the old one first. |
| `run_as_user` | — | Required by `install-daemon.sh`: the daemon runs as this user, not root. |
| `fast_period_s` | `2` | Fast-tier cadence. `2` = 0.5 Hz. **Changing it changes the plan hash.** |
| `rotate_minutes` | `60` | Rotate and gzip the archive file this often. |
| `keep_days` | `0` | Retention. See below. |
| `degrade_after` | `5` | Dead ticks before dropping to a single-block probe. |
| `degrade_probe_s` | `30` | While degraded, probe about this often instead of every tick. |
| `recycle_s` | `3600` | Scheduled connection recycle. `0` disables. |
| `max_lag_s` | `5` | Fall this far behind schedule and rebase instead of replaying. |
| `gap_log_quiet_s` | `60` | Collapse repeated `[gap]` lines to one per this interval. |
| `bucket_s` | `300` | Heartbeat and soak-bucket width. |
| `manifest_identity` | `true` | Record model/serial/host in manifests. |

`keep_days` is retention, and it is narrower than it looks: on **rotation** only,
any `*.bin.gz` older than N days is deleted. It never touches the open `.bin`,
never touches manifests, and never runs between rotations — so pruning can leave
you holding a manifest for a window whose data is gone. `0` keeps everything,
which at ~2.2 MB/day gzipped is about 800 MB/year: for most people, leave it at
`0`.

The same config drives the shell scripts and the launchd plist, so nothing
hardcodes a path in two places:

```sh
python3 config.py --show                              # resolved config as JSON
python3 config.py --sh                                # SIGEN_*='...' for eval in sh
python3 config.py --render deploy/launchd.plist.template
```

---

## Files

| File | Role |
|---|---|
| `config.py` | Config loader. Also `--show`, `--sh`, `--render`. |
| `lib.py` | Modbus transport and decode primitives. Not a CLI. Owns the conventions capture and decode must agree on. |
| `log.py` | The logger. Tiered raw capture, topology drift detection, soak harness. |
| `decode.py` | Offline decode of the raw archive into CSV/JSONL/derived series. |
| `dump.py` | One-shot full-map decode: what every documented register returns on this unit. |
| `regmap_gen.py` | Regenerates `regmap.json` from the upstream register definitions. |
| `regmap.json` | The register map: 358 fields, 10 alarm appendices, 6 enums. Generated, not hand-written. |
| `bin/status.sh` | Health check: daemon state, recent heartbeats, gap/degraded counts, archive size. No sudo. |
| `bin/latest.sh` | Newest values, vertically. The "is it working right now?" view. No sudo. |
| `deploy/install-daemon.sh`, `uninstall-daemon.sh` | LaunchDaemon install/removal. Needs sudo. |
| `deploy/launchd.plist.template` | LaunchDaemon definition, rendered from config at install time. |
| `deploy/launchagent.plist.template` | LaunchAgent alternative, for a host that *does* auto-login. |
| `tests/test_offline.py` | The whole capture/decode path against a fake device. No hardware, no network. |
| `examples/` | A redacted excerpt of `dump.py --json` output. |

The Python files sit flat at the repository root on purpose: Python puts the
script's own directory on `sys.path`, so `import lib` resolves with no
`__init__.py`, no package and no `PYTHONPATH`. `regmap.json` is located relative
to `__file__`, so the directory can be moved anywhere as a unit — which is how
deployment works.

---

## The core idea: capture wide, decode narrow

Modbus cost is **per request, not per register** — a 124-register read costs the
same as a 2-register read (~23 ms either way). So there is no point curating a
narrow field list at capture time. Read the largest block each request allows,
store the raw bytes, and choose fields later with `decode.py`.

The practical payoff: when new hardware is fitted — PV strings, a second battery
pack — the data is already being recorded, with no config change and no gap in
the series. The capture blocks here already span all 36 possible PV string
channels.

What you *do* economise on is **requests per second**. Latency p95 degrades
superlinearly past about 3 requests per tick.

---

## `log.py` — the logger

```
log.py [host] [options]
```

| Flag | Default | Meaning |
|---|---|---|
| `--seconds N` | 60 | Run for N seconds. **`0` = run until interrupted** (deployment mode). |
| `--out DIR` | `data_dir` | Archive directory. |
| `--rotate-minutes N` | `rotate_minutes` | Rotate the archive file every N minutes, gzipping the completed one. |
| `--keep-days N` | `keep_days` | Delete `.bin.gz` older than N days on rotation. `0` = keep everything. |
| `--soak-report` | off | Bucket latency, apply pass/fail gates, abort on breach. |
| `--baseline` | — | Snapshot the full register map as a topology baseline, then exit. |
| `--check` | — | Diff the full map against that baseline. Exits 1 if anything changed. |
| `--fault-inject N` | — | Test hook: close the socket at tick N to exercise reconnect. |

### The tier table

The block plan is `build_tiers()` at the top of `log.py`. One row per block read:

```python
# label, unit, addr, count, fc, period_s, offset_s
("plant_live", plant, 30000, 124, 4, fast, 0),
```

A block fires when `tick % period_s == offset_s`. The scheduler ticks once per
second regardless, so `--seconds N` counts ticks, not samples.

| Cadence | Block | Unit | Covers |
|---|---|---|---|
| `fast_period_s` | `30000+124` | plant | All live plant fields: grid P/Q, PV, battery, SOC, plant P/Q, ESS limits, running state, alarms 1–5. Plus SOH, cut-offs, daily energy free in-span. |
| `fast_period_s` | `30200+87` | plant | Lifetime counters, PV daily, alarm6, general load power, cell temperature. |
| `fast_period_s` | `31000+106` | inverter | Grid frequency, phase A V/I, power factor, PCS temp, insulation resistance, **all 36 PV string V/I pairs**. |
| 60 s | `30540+84` | inverter | Battery detail: cell voltage, min/max temp, available energy, inverter alarms. |
| 60 s | `31509+4` | inverter | Inverter daily + lifetime PV energy. |
| 300 s | `40029+40` | plant | FC3. Export/import limits, PV cap, grid code, derating — the export-policy surface. |

317 registers per sample in 3 requests. At 0.5 Hz that averages **1.54
requests/s**, 1932 records/hour, ~28 MB/day raw and ~2.2 MB/day gzipped.

The three fast blocks share one tick on purpose — a sample is only coherent if
grid, PV, battery and load were read at the same instant, and the energy balance
depends on that. Slow blocks sit on **odd** offsets so they land on ticks the fast
tier skips, which caps peak requests per tick at **3** rather than the 4 it was
when everything shared even ticks.

Ticks with nothing scheduled write no record at all, and are not counted as dead
ticks for degraded-mode purposes.

> **If you change a block, validate it first.** A read must both start *and* end on
> a field boundary or the device answers exception 2. Max span is 124–125
> registers depending on base. `dump.py` derives valid blocks automatically;
> hand-written spans need checking. And retuning changes the plan hash — see
> [Archive format](#archive-format).

### Persistent-run behaviour

Designed to run indefinitely:

- **Bounded memory** — completed buckets are capped at `BUCKET_HISTORY` (24, i.e.
  2 h) outside soak mode and `drift` is a bounded deque, so memory is flat
  regardless of runtime.
- **Idle ticks write nothing.** The scheduler ticks once per second whatever
  `fast_period_s` is, so at 0.5 Hz half the ticks have no block due. Those write no
  record and are *not* counted as dead ticks — otherwise five seconds of ordinary
  operation would trip degraded mode.
- **Schedule rebase** — if the loop falls more than `max_lag_s` behind, it rebases
  and logs `[gap]` instead of replaying every missed tick. Without this, a one-hour
  machine sleep would fire ~3600 back-to-back requests at the device.
- **Degraded probe** — after `degrade_after` ticks with no data it drops to a
  single block *and* stops probing every tick, retrying only about every
  `degrade_probe_s` until the device answers. Logs `[degraded]` then `[recovered]`.
  Without the interval, each attempt costs a full socket timeout against a dead
  host, so tick-rate probing becomes a tight retry loop that floods the log and
  hammers the network for nothing.
- **Rate-limited gap logging** — one `[gap]` line per `gap_log_quiet_s`, with a
  count of what was suppressed.
- **Flush per record** — the archive is durable against process death to within one
  second. Not `fsync`; a power cut can still lose the page cache.
- **Hourly connection recycle** (`recycle_s`) — closes the socket every 3600 ticks;
  the next read reconnects in ~17 ms. Kept because it regularly exercises the
  reconnect path, so breakage there surfaces during normal operation rather than
  during a real outage. (Its *original* justification turned out to be a
  measurement artefact — see [docs/FINDINGS.md](docs/FINDINGS.md).) Set to 0 to
  disable.
- **Graceful shutdown** — SIGTERM/SIGINT close the archive and join outstanding
  gzip threads rather than truncating.
- **Heartbeat** — one line per `bucket_s` with record count, size, latency
  percentiles, retries, gaps, recycles and *unexpected* reconnects (scheduled
  recycles are counted separately so the latter stays a real alarm). Run under
  `python3 -u` or stdout buffering will hide it entirely. It fires on **bucket
  transition, not at a tick index**, because boundaries land on odd ticks which are
  idle whenever `fast_period_s` is even. Percentiles are computed over ticks that
  returned data; a bucket with none says `NO DATA — device not answering` rather
  than quoting the socket timeout as a device latency.

---

## Archive format

Two things on disk per day: one manifest, and a series of rotated record files.

```
sigen-YYYYMMDD-<planhash>.manifest.json      block plan, byte offsets, identity, regmap provenance
sigen-YYYYMMDDTHHMMSS-<planhash>.bin         open file, plain
sigen-YYYYMMDDTHHMMSS-<planhash>.bin.gz      rotated and compressed (~13x)
topology-baseline.json                       written by --baseline
```

`<planhash>` is an 8-hex fingerprint of the block plan — units, addresses, spans,
function codes, periods and offsets. **Retuning any of those changes the hash**, so
a file can never be silently decoded against a manifest describing a different
plan. `decode.py` refuses on mismatch rather than emitting plausible wrong numbers,
and treats a missing hash on either side as a mismatch too.

When you retune, move the previous series into its own subdirectory along with its
manifest, so each series stays decodable:

```sh
mkdir -p data/prev-$(date +%Y%m%d)
mv data/sigen-*T*.bin* data/sigen-*.manifest.json data/prev-$(date +%Y%m%d)/
```

**The manifest is the format of record — without it the archive is undecodable.**
It is re-emitted when the date rolls over, so every day's files sit beside a
manifest that can decode them.

Each record:

| Bytes | Field |
|---|---|
| 8 | host epoch, `>d` |
| 2 | present-block bitmask, `>H` |
| 2 | tick latency in ms, `>H` |
| var | raw bytes of each present block, in block-index order |

Record length is **derived from the bitmask**, not fixed — a tick only contains
the blocks that fired. A reader that hits a short read stops there, so a file
killed mid-write decodes cleanly up to its last complete record. A tick where
every block failed still writes a 12-byte header with `mask = 0`, which is how
outages appear in the archive: not as a hole, but as records that carry the tick
latency and nothing else.

---

## `decode.py` — offline decode

```
decode.py MANIFEST FILE.bin [FILE.bin.gz ...] [options]
```

Files can be mixed plain and gzipped. **They are sorted chronologically by the
timestamp in their filename, not by argv order** — `*.bin *.bin.gz` expands to
plain files first, which is reverse-chronological for a rotating archive and would
otherwise produce backwards time steps and phantom counter drops that look exactly
like data corruption.

| Flag | Meaning |
|---|---|
| `--last` | Vertical snapshot of the newest record. Reports **data** freshness, not record freshness: if the device is unreachable it says so, gives the age of the last good sample, and shows the last known good values rather than blanks. |
| `--check` | Integrity report: record count, latency over data records, clock steps, counter monotonicity, backwards clock steps, absent blocks. |
| `--latency [MIN]` | Latency trend by wall-clock bucket (default 10 min), with the median range, spread, and an explicit monotonic-or-oscillating verdict. Outage probes are counted separately, never averaged in. |
| `--balance` | Derived export series — see below. |
| `--fields a,b,c` | Field keys to emit. Default is a curated live set (`DEFAULT_FIELDS`). |
| `--all` | Every field the captured blocks cover. |
| `--format csv\|jsonl` | Output format, default `csv`. |
| `--downsample N` | Keep one row per N seconds. Use this for the slow counters. |
| `--limit N` | Stop after N rows. |

Unknown or uncaptured field keys are reported on stderr and skipped, so a typo
degrades rather than crashes.

```sh
python3 decode.py data/*.manifest.json data/*.bin --check
python3 decode.py data/*.manifest.json data/*.bin.gz --fields plant_ess_soc,plant_active_power
python3 decode.py data/*.manifest.json data/*.bin --all --downsample 60 > day.csv
python3 decode.py data/*.manifest.json data/*.bin* --latency 10
```

### `--balance`

The export/self-consumption view, and the first thing you'll want once panels are
producing. Emits per row: grid kW, import kW, export kW, PV kW, battery kW, load
kW, self-consumption fraction, an `exporting` flag, grid Hz, and lifetime export
kWh. It notes the first export timestamp on stderr, or says there was none.

Sign conventions, from the upstream register descriptions:

- `30005` grid active power: **> 0 buying from grid, < 0 selling to grid**
- `30037` ESS power: **> 0 charging, < 0 discharging**

So `export = −min(grid, 0)` and the balance is
`PV = load + battery_charge + export`.

> **Caveat:** the reference unit has never exported, so `30005 < 0` has never
> actually been observed. Confirm the sign on your first real export. `30220`
> lifetime export starting at exactly 0.000 is the independent cross-check.

---

## `dump.py` — one-shot full-map decode

```
dump.py [host] [--json out.json] [--quiet] [--ac-charger UNIT]
```

Reads all 346 addressable fields in ~1 s and classifies each:

| Status | Meaning |
|---|---|
| `ok` | Decoded a value. |
| `unsupported` | Device rejected the address (exception 2) — no such subsystem. |
| `sentinel` | Register exists but returned all-ones: not available / not set. |
| `write-only` | Documented `wo`, never read. |

Then a derived summary: identity, AC topology, PV topology, battery, current
power, lifetime energy, control posture, refused ranges, alarms, clock skew. See
[`examples/dump-snapshot.example.json`](examples/dump-snapshot.example.json) for
the shape of `--json` output.

Baseline for the reference unit: **304 ok / 21 unsupported / 17 sentinel / 4
write-only.** Yours will differ with topology; what matters is that it stays
stable between runs, and that `unsupported → ok` transitions are explained by
hardware you know was added.

`--ac-charger UNIT` additionally sweeps AC-charger registers at a given slave id.
No AC charger exists on the reference install; only units **1** (inverter) and
**247** (plant) answer at all, and their address spaces are strictly partitioned.

> `--json` writes the serial number in plain text. `.gitignore` covers
> `dump-snapshot*.json` at the repository root for exactly that reason.

---

## Topology drift detection

If your install is unfinished — a battery pack on order, an EV charger to come —
the logger needs to notice hardware appearing rather than silently recording
zeros.

```sh
python3 log.py --baseline     # snapshot
python3 log.py --check        # diff; exits 1 on any change
```

`--check` reports:

- `unsupported → ok` — new hardware appeared (EV charger, extra battery pack)
- `sentinel → ok` — the installer configured something, e.g. an export limit
- changes to `inverter_pack_count` / `pv_string_count` / `mppt_count`
- AC-charger slave ids beginning to answer

**It does not catch PV strings arriving.** All 36 documented string voltage/current
registers answer `ok` from the moment the inverter is up — the populated channels
reading a clean `0`, the unpopulated ones the `−1` signed sentinel — so the
`unsupported → ok` transition never happens for them. `pv_string_count` and
`mppt_count` report the inverter's MPPT hardware capability, not what is physically
wired, so they do not move either. On the reference unit, a baseline taken before
any panel was connected and one taken the same day after the arrays were generating
differed in **nothing but `captured_at`**. Watch per-string voltage in the archive
instead — the `inv_ac_dc` block already covers all 36 channels. See
[Findings](docs/FINDINGS.md) for the two gotchas that follow from this.

**Run it deliberately, not on a schedule** — it is a full-map sweep, so it acts as
a second Modbus client, and concurrent-client behaviour is unmeasured. After an
installer visit is the right time.

`--baseline` **overwrites** `<data_dir>/topology-baseline.json` in place, so it is
only ever "the last baseline taken". If a particular snapshot is worth keeping — the
state before an installer visit, say — copy it aside under its own name first.
Retention never prunes it: `keep_days` only touches `*.bin.gz`.

---

## Deployment

Runs as a **system LaunchDaemon** on macOS, chosen over a LaunchAgent because the
target host has no auto-login and no persistent console session — a user agent
would not return after an unattended reboot. It still runs as an ordinary user;
nothing here needs privilege.

```
<install_dir>/          code (rsync'd or cloned)
<install_dir>/config.json   local config — never rsync this over
<install_dir>/data/     archive
<install_dir>/logs/     sigen.log, sigen.err
```

Set `install_dir`, `run_as_user`, `launchd_label` and `python` in the target
host's own `config.json`, then:

```sh
sudo deploy/install-daemon.sh      # renders the plist, installs root:wheel 644, bootstraps
sudo deploy/uninstall-daemon.sh    # boots out and removes; leaves the archive
```

The plist sets `KeepAlive` to restart on crash, `ThrottleInterval: 30` so a crash
loop can't hot-spin, `ProcessType: Interactive` so macOS doesn't throttle a
latency-sensitive polling loop, and `python3 -u` so the heartbeat isn't buffered
away.

Updating the code:

```sh
rsync -av --exclude __pycache__ --exclude data --exclude logs --exclude config.json \
      ./ youruser@yourhost.local:~/sigen/
ssh youruser@yourhost.local 'kill -TERM $(pgrep -f sigen/log.py)'
```

`kill -TERM` as the owning user needs no sudo: the logger shuts down gracefully
and `KeepAlive` respawns it with the new code within ~30 s. Verify with
`launchctl print system/<label> | grep -E "state|runs|last exit"` — a graceful
restart shows `last exit code = 0` and an incremented `runs`.

**If the change touches the block plan or `fast_period_s`**, segregate the existing
series first, because the plan fingerprint changes and old files must keep the
manifest that describes them:

```sh
ssh youruser@yourhost.local 'cd ~/sigen/data && mkdir -p prev-$(date +%Y%m%d) \
  && mv sigen-*T*.bin* sigen-*.manifest.json prev-$(date +%Y%m%d)/'
```

Then rsync, restart, and confirm the new files carry the new hash.

Two host settings worth checking: `pmset sleep 0` so the machine stays up, and
`sudo pmset -a autorestart 1` so a power cut doesn't stop capture until someone
presses the button.

### Monitoring

```sh
bin/latest.sh                          # newest record, vertical
bin/status.sh                          # daemon state, heartbeats, archive size
tail -f <log_dir>/sigen.log
```

Log lines worth grepping: `[gap]` (schedule rebase after a stall), `[degraded]`
(device unreachable, probing), `[recovered]`, and the heartbeat.

---

## Regenerating the register map

```sh
python3 regmap_gen.py                    # fetch upstream from GitHub, regenerate
python3 regmap_gen.py path/to/defs.py    # or parse a local copy
```

Source is `custom_components/sigen/modbusregisterdefinitions.py` from
[TypQxQ/Sigenergy-Local-Modbus](https://github.com/TypQxQ/Sigenergy-Local-Modbus).
It imports `homeassistant`, so it cannot simply be imported — `regmap_gen.py`
parses it with `ast` and flattens the dataclasses, alarm appendices and enums to
JSON, overwriting `regmap.json` in place.

After regenerating, run `dump.py` and confirm the status tally is unchanged. If
upstream renamed a field key, `DEFAULT_FIELDS` and `DTYPE_OVERRIDE` may need
updating.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `no inverter host configured` | No `config.json`, or no `host` in it. Copy `config.example.json`. |
| `exception 2 (ILLEGAL DATA ADDRESS)` on everything | Using FC3 on `ro` registers. Use FC4. |
| `exception 2` on one specific block | The span ends mid-field, or exceeds ~124 registers. |
| Values look absurd but read cleanly | Suspect a dtype error in the map. Sanity-check against physical bounds. |
| Timestamps 12 h out | Decoded with `localtime` instead of `gmtime`. See device quirk 4. |
| Heartbeat stops but daemon shows `running` | Check for `[degraded]` — device unreachable. |
| `latest.sh` says no complete records | Archive just rotated; the new file has fewer than one full record. |
| `no manifest for plan <hash>` | The newest archive was written under a plan whose manifest is missing — usually a manifest moved without its data. |
| Daemon won't load | Plist must be `root:wheel` mode 644 in `/Library/LaunchDaemons`. `install-daemon.sh` does this. |
| Repeated `runs` increments with nonzero exit | Real crash. Check `logs/sigen.err`. |
| `plan-hash mismatch -- refusing to decode` | Files and manifest come from different block plans. Decode each series with the manifest stored beside it. |
| `device clock ... REAL GAPS [...]` | Samples genuinely missing. Check for `[degraded]` around that time. |
| `device clock ... N jitter (balanced, no loss)` | Benign. A read lands wherever it falls relative to the device's internal second tick, so at a 2 s cadence ±1 s steps are expected and arrive in compensating pairs. |
| `device clock` step count wrong entirely | Wrong manifest for these files — the cadence recorded in it differs from the one they were captured at. |
| `host clock ... BACKWARDS steps` | Files from different series mixed in one invocation, or the host clock stepped. Ordering itself is handled automatically. |
| Latency looks like it is climbing | Check with `--latency` before acting. It oscillates ~1.6x; consecutive heartbeats routinely look like a trend when there is none. |
| `latest.sh` age bounces around over repeated runs | Empty probe records land every ~35 s during an outage, so record age sawtooths. `--last` labels this explicitly as DEVICE NOT ANSWERING. |
| Flood of `[gap]` lines, `retries` climbing | The device is unreachable. Confirm with `ping` / `nc -z host 502`. Expected during installer work or a power-down; capture resumes by itself. |
| `[degraded]` with no `[recovered]` | Still unreachable. Empty records (mask 0) continue to mark the outage in the archive. |
| Heartbeat silent but records accumulating | Only possible if `bucket_s` and `fast_period_s` interact badly. Emission is on bucket transition. |

---

## Licence and attribution

MIT — see [LICENSE](LICENSE).

`regmap.json` is generated from
[TypQxQ/Sigenergy-Local-Modbus](https://github.com/TypQxQ/Sigenergy-Local-Modbus)
(MIT, © 2025 Andrei Ignat), whose notice is reproduced in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Register semantics originate in
Sigenergy's published Modbus protocol documentation, which this project does not
redistribute.

Not affiliated with, endorsed by or supported by Sigenergy.
