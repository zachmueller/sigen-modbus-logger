# Sigenergy SigenStor local telemetry

Read-only Modbus TCP capture and decode for a Sigenergy SigenStor, over your own
LAN. No vendor API, no vendor account. **Capture, decode and the local viewer are
stdlib-only Python 3.9+ with nothing to `pip install`** — a test asserts it, so the
part that has to keep running on a bare Python cannot quietly grow a dependency.

Two optional additions do not hold to that, and are off by default: `sync.py`, which
copies rotated archive files to your own S3 bucket and needs `boto3`; and `cloud/`,
which deploys a hosted viewer and needs Node at deploy time only. Neither runs unless
you configure it, and neither is needed to capture, decode or view locally.

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
- **The viewer is a second exposure, not a second client.** `serve.py` reads the
  archive on disk and never opens a Modbus connection, so it costs the inverter
  nothing. But it serves plain HTTP with no authentication, bound to `0.0.0.0` by
  default: anyone who can reach the host can read your telemetry. Its API withholds
  the serial, firmware and inverter address unless `web_show_identity` is true. Bind
  it to `127.0.0.1` and tunnel if your LAN is not trusted, and **never
  port-forward it.**
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

python3 serve.py                        # then open http://localhost:8787
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
| `web_port` | `8787` | Viewer port. Above 1024, so the viewer needs no privilege. |
| `web_bind` | `0.0.0.0` | Viewer bind address. `127.0.0.1` to require an SSH tunnel. |
| `web_launchd_label` | `local.sigen-viewer` | Viewer daemon label. Must differ from `launchd_label`. |
| `web_default_hours` | `6` | Default lookback when the page opens. |
| `web_show_identity` | `false` | Let the viewer's API report serial, firmware and the inverter's address. |
| `s3_bucket` | — | Bucket for the offsite copy. Unset means no uploading. |
| `s3_region` | `us-east-1` | Bucket region. |
| `s3_prefix` | `raw/` | Key prefix. Must end with `/`. |
| `aws_profile` | — | Named `~/.aws` profile. Credentials never live in `config.json`. |
| `sync_enabled` | `false` | Master switch for `sync.py`. Off until you mean it. |
| `sync_launchd_label` | `local.sigen-sync` | Uploader daemon label. Must differ from the other two. |

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
| `serve.py` | The local web viewer: an HTTP server that plots the archive. Never opens a Modbus connection. |
| `sync.py` | Copies rotated archive files to S3. The one module that needs `boto3`, imported lazily inside it. Off unless `sync_enabled`. |
| `tiles.py` | The wire format both read paths produce, so the local viewer and the hosted one cannot disagree. Not a CLI. |
| `ingest.py` | An archive directory to a directory of precomputed tiles. Knows nothing about S3, which is what makes it testable. Not a CLI. |
| `cloud/` | The hosted viewer: CDK stacks, the ingest Lambda, and the backfill. Deploy-time only; see [cloud/README.md](cloud/README.md). |
| `series.py` | Archive index, bucket aggregation and health scan behind the viewer. Not a CLI. |
| `web/` | The viewer's page: one HTML file, one stylesheet, two scripts. No framework, no CDN. |
| `regmap_gen.py` | Regenerates `regmap.json` from the upstream register definitions. |
| `regmap.json` | The register map: 358 fields, 10 alarm appendices, 6 enums. Generated, not hand-written. |
| `bin/status.sh` | Health check: logger and viewer daemon state, recent heartbeats, gap/degraded counts, archive size. No sudo. |
| `bin/latest.sh` | Newest values, vertically. The "is it working right now?" view. No sudo. |
| `bin/tunnel.sh` | Run on the *viewing* machine: SSH-forwards the viewer to `localhost`, for a laptop that cannot route to the LAN address. |
| `deploy/install-launcher.sh` | Run on a viewing **Mac**: generates a Spotlight-launchable `.app` that opens the tunnel and the page in one keystroke. |
| `deploy/install-daemon.sh`, `uninstall-daemon.sh` | LaunchDaemon install/removal for the logger. Needs sudo. |
| `deploy/install-viewer.sh`, `uninstall-viewer.sh` | The same for the viewer. Separate daemon, so restarting it cannot interrupt capture. |
| `deploy/install-sync.sh`, `uninstall-sync.sh` | The same for the uploader. A third daemon, for the same reason. Checks boto3 and the credential *as the user the daemon will run as*. |
| `deploy/sync.plist.template` | The uploader's LaunchDaemon. `StartInterval`, not `KeepAlive`: it is a job that finishes. |
| `deploy/launchd.plist.template` | LaunchDaemon definition, rendered from config at install time. |
| `deploy/viewer.plist.template` | The viewer's LaunchDaemon definition. `ProcessType: Background`, so the logger wins any contention. |
| `deploy/launchagent.plist.template` | LaunchAgent alternative, for a host that *does* auto-login. |
| `tests/test_offline.py` | The whole capture/decode path against a fake device. No hardware, no network. |
| `tests/test_web.py` | The viewer: index, aggregation, health, HTTP. Asserts it can never become a second Modbus client. |
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

## `serve.py` — the local web viewer

```
serve.py [--port N] [--bind ADDR] [--data-dir DIR] [--check] [--verbose]
```

A single page that plots what the house and the inverter have been doing, decoded
on demand from the archive already on disk. Defaults to the last
`web_default_hours` and scrolls back over the whole series.

**It never touches the inverter.** It imports the decode half of the toolchain and
nothing else; `lib.Modbus` is never constructed, and `tests/test_web.py` asserts
that with an AST scan rather than a grep, so the docstring saying so cannot be what
satisfies the test. That matters because the device should have exactly one client
— the logger — and concurrent-client behaviour is unmeasured.

```sh
python3 serve.py                    # config.json's port and bind address
python3 serve.py --check            # what it can see, without serving. Debug a deploy.
python3 serve.py --data-dir data/1hz-20260814   # view a superseded series
```

`--check` prints the plan hash, the archive extent, how many fields are plottable,
and how long the default window takes to build — the quickest way to tell "the
viewer is broken" from "that directory has no archive in it".

### What is on the page

| | |
|---|---|
| Live strip | PV, load, battery, grid, SOC and plant state, each with a sparkline over the window. Values are the newest **good** sample, labelled `last known good` when the device is not answering. |
| Energy this window | Differences of the device's own lifetime counters — the independent cross-check, not an integral of the power series. |
| Power flow | Grid, load, battery and PV on one kW axis, with the sign conventions on the card. |
| Battery SOC | Its own chart. Never a second y-axis on the power plot. |
| PV strings | Per-string current, and voltage behind a toggle. `pv1..pv4` only: the other 32 documented channels return the −1 absent marker here. |
| Grid quality | Frequency, phase A voltage and power factor, as separate small charts. |
| Temperatures | PCS internal and average cell, both °C, so one axis is honest. |
| Plant state | Running state, grid connection and EMS work mode as labelled bands; all six alarm words OR-ed over each bucket. |
| Capture health | Tick latency median/p95/max per bucket, with outage and no-record spans shaded. |
| Custom chart | Any of the ~259 captured fields. Fields sharing a unit share an axis; a mixed-unit pick becomes one chart per unit. |

Arrow keys move through history, `+`/`−` zoom, `n` jumps to now. Focus a chart and
the arrows move its crosshair instead. The window is in the URL, so a view can be
bookmarked or sent to anyone who can reach the viewer.

**Two of these controls exist only here.** The *Updates* group — `Live (10 s)` and `Reload` —
and `Download CSV` are marked `data-server-only`, and the hosted viewer hides them. It reads
precomputed tiles that are published when the logger rotates, roughly hourly, and
`web/tiles.js` caches every object it fetches for the life of the page, so polling there
cannot return anything new at any interval; `/api/csv` is not a route a tile source has at
all. `app.js` derives that from the source rather than being told (`POLLS`), so there is
nothing for a deployment to remember. This viewer is the live one, and it is unchanged.

### Windows, buckets and stride

The page asks for a *window*; how to build it is `series.py`'s business, and every
choice it makes is reported back and shown in the filter row.

- **Bucket width** comes from a fixed ladder (…30 s, 60 s, 120 s, 300 s…) targeting
  ~900 points, so 6 h → 30 s, 24 h → 120 s, 7 d → 900 s. A ladder rather than
  span÷900 means panning and re-polling land on cache entries that already exist.
- Each bucket carries **min, mean and max**, so one point per 30 s of 0.5 Hz data
  still shows the spikes. The line is the mean; the min–max is in the tooltip, the
  table view and the CSV.
- Buckets are keyed on **absolute epoch**, so a bucket straddling two archive files
  is assembled exactly from both.
- **Stride**: at most 64 records are decoded per bucket, which is what makes a
  month-long window cost about what a six-hour one costs. It only engages when a
  bucket holds more than 64 records (from 600 s buckets upward at 0.5 Hz), it never
  skips a slow-tier record, and the filter row says `sampled 1 record in N` when it
  is on. Nothing is dropped silently.
- **Cache**: per file, per field, per bucket width. A rotated `.bin.gz` can never
  change, so its summary is final. The open `.bin` is **extended, not re-read** — it
  grows every couple of seconds, so its summary carries the byte offset it reached
  and resumes from there. Records are append-only, which is what makes that sound.
- If a window needs more cold decoding than the warm budget (3 s), the server
  returns what it has, says `pending_files`, finishes the rest on a background
  thread, and the page labels itself partial and re-asks. Files are read
  newest-first, so a partial answer is the recent end of the window.
- **Decoding is serialised** across every request thread and the warm thread. The
  server is threaded so a slow window cannot block the page's assets, but decoding
  is CPU-bound and the logger has a 2 s tick to hit on the same host.

Measured on the reference host (26 files, 4 MB, 0.5 Hz, 34 fields):

| | |
|---|---|
| Live poll, 6 h window, steady | **0.15 s** (0.70 s before the open file was read incrementally) |
| `/api/latest` | 0.03 s |
| First 24 h view, everything cold | 3.1 s for 5 of 26 files, rest warmed in the background |
| 24 h view once warm | 0.24 s |

### Reading a gap correctly

The three cases the page keeps apart, because they have different causes and the
same blank space:

| On screen | Means |
|---|---|
| Line **breaks** | More consecutive buckets are empty than the field's own cadence explains. Never a straight line drawn through an outage. |
| Orange hatch, "device not answering" | Records were written, with no blocks in them — the logger is healthy and the inverter is not. |
| Grey hatch, "no records" | Nothing was written at all: the logger stopped, or files were moved or pruned. |

Latency is quoted over records that **returned data**; an outage probe's latency is
the socket timeout, not the device's, so it would swamp any bucket it landed in
(see [Findings 7](docs/FINDINGS.md)). Bucket max is exact; median and p95 come from
up to 64 samples per bucket.

### Sending a view to someone

Something looks wrong and the installer needs to see it. A screenshot loses the
tooltips, the min–max and the provenance; a CSV loses the picture.

This server's answer used to be a self-contained HTML file. It has been **retired** in
favour of a shared URL from the hosted viewer, which is a better answer whenever the
recipient is online: nothing to attach, nothing to bounce off a mailbox size limit,
and it renders through the same `app.js` rather than a frozen copy of it.

From this server, what is left is:

- **The URL.** The window, the expanded panels and the custom fields are all in the
  page's hash, so a view is a link for anyone who can reach the viewer — over the LAN,
  or through `bin/tunnel.sh`.
- **A link to one panel.** `#focus=<panel>` opens that panel and scrolls to it, leaving
  everything else as the rest of the hash says. The link icon in a panel's heading copies
  such a link for you. It is accepted on every form of this page — this server, the hosted
  viewer, and a `/p/<id>` share, which is where it earns the most: the reason for sending a
  view is usually one panel, and until now that reason could only be written out in the note.

  A panel is named by its id or by its title, so `#focus=temps` and `#focus=temperatures`
  are the same request, as are `#focus=stringv` and `#focus=pv-string-voltage`. Both come
  from `PANELS` in `serve.py`, so a new chart there is focusable with no other change. A
  name that matches nothing is ignored — a link outlives the deployment that made it, and a
  stale name should still render the page.
- **Download CSV.** The bucketed numbers behind the view, matching the table view
  exactly; `&raw=1` gives per-record rows in `decode.py`'s shape.

The page itself is source-agnostic and that is what makes the hosted viewer possible
without a second renderer: `web/app.js` reads every answer through one `getJSON()`
seam, and a deployment that sets `window.SIGEN_SOURCE` before the script loads points
that seam at precomputed tiles instead of at this server. A shared view is the same
page with a fixed window — `applyFrozenMode()` removes every control that would ask
for another one, and labels the ages `when shared` rather than presenting them as now.

### The API

Plain JSON, same-origin, no CORS header — so a hostile page on your LAN cannot read
your archive through your browser.

| Endpoint | Returns |
|---|---|
| `/api/meta` | Plan hash, archive extent, block plan, the panel definitions, and the field catalogue with units, cadences, enum labels and duplicate markers. |
| `/api/window?hours=6&end=…&panels=…&fields=…` | The bucket grid, per-field min/mean/max, health, window energy, bucket width, stride, timezone change points. |
| `/api/latest` | Newest good sample, with data-age and record-age reported separately. |
| `/api/csv?…` | The window as CSV; `&raw=1` for per-record rows in `decode.py`'s shape. |
| `/api/stats` | Cache hit counts and warm-thread state. |

Which fields make up which chart lives in `PANELS` at the top of `serve.py`, so a
new chart is a dict there, not new JavaScript. A test asserts every field named in
it is actually covered by the block plan — otherwise a typo would render an empty
chart that looks like a quiet night.

### Exposure and privacy

A wildcard `web_bind` listens on **both IPv4 and IPv6** — one `AF_INET6` socket with
`IPV6_V6ONLY` cleared. That is not a detail: macOS prefers IPv6 for a `.local` name,
so a v4-only listener refuses the browser's first attempt while `curl` silently falls
back and appears to prove the server fine ([Findings 12](docs/FINDINGS.md)). The
startup banner states which families the socket accepts. An explicit address is
honoured as given: `127.0.0.1` is v4-only, `::1` is v6-only.

`web_bind` defaults to `0.0.0.0`, so **anyone who can reach the host on `web_port`
can read your telemetry**. That is usually what you want on a home LAN and is why
the API withholds the unit's serial, firmware and the inverter's address unless
`web_show_identity` is true — a manifest identifies an installation, and none of it
is needed to read a chart.

There is no authentication and no TLS. Do not port-forward it, for the same reason
you do not port-forward the inverter.

### When the browser cannot reach it: `bin/tunnel.sh`

A **managed laptop** — corporate VPN, content filter, MDM proxy policy — can refuse
RFC1918 destinations outright. The browser reports `ERR_ADDRESS_UNREACHABLE` for both
the `.local` name *and* the raw IP, while `curl` on the same machine, `ssh` to the
same host, and any other device on the LAN all work. Nothing reaches the server, so
its log stays empty and it looks dead.

Run this **on the viewing machine** and the browser only ever talks to `127.0.0.1`:

```sh
bin/tunnel.sh youruser@yourhost.local          # then open http://localhost:8787/
bin/tunnel.sh youruser@yourhost.local 8787 9000   # if 8787 is taken locally
```

It needs no sudo, changes nothing on the capture host, and leaves `web_bind` as it
is — other devices keep using the LAN address. For a stricter setup, set
`"web_bind": "127.0.0.1"` so the tunnel is the *only* way in.

### One keystroke on a Mac: `deploy/install-launcher.sh`

Typing an `ssh` command every time is friction that stops you looking at the data.
This generates **two** small apps in `~/Applications`, both of which Spotlight
indexes:

```sh
deploy/install-launcher.sh youruser@yourhost.local           # Sigen Viewer + Sigen Viewer Stop
deploy/install-launcher.sh youruser@yourhost.local 8787 9000 "Solar"
```

| Cmd-Space, then | Does |
|---|---|
| `sigen` | Opens the tunnel if needed, then the page. |
| `sigen stop` | Closes the tunnel. |

**Opening** is idempotent, and the reuse check is over an SSH **control socket**
(`ssh -O check`) rather than "is anything listening on 8787" — something else on
that port is not a thing to hand a browser. So:

- our tunnel already up → just opens the page (a second launch is instant);
- a tunnel *you* started with `bin/tunnel.sh` → reused, identified by asking
  `/api/stats` whether the thing on the port really is the viewer;
- port held by something that is not the viewer → says so, and suggests a rebuild
  on another port;
- nothing there → `ssh -M -S … -f -N -L …`, waits for the forward, opens the page.
  Cold start to page in under a second on the reference setup.

**Closing** is explicit: `sigen stop` does `ssh -O exit` on our own tunnel, or falls
back to matching the forward on the command line for one started by hand, and
notifies either way. From a terminal, `pkill -f sigen-viewer-8787` does the same.
The tunnel also ends by itself when the network drops (`ServerAliveInterval 30 × 3`).

**Or close it from the page.** A `Close tunnel` chip appears in the filter row
whenever the viewer is reached over `localhost` — i.e. only when there is a tunnel to
close. It cannot ask the server to do it (the server is on the capture host and
cannot touch your Mac), so it hands off to `sigen-stop://`, a URL scheme the Stop app
registers. The first click raises the browser's one-time *"Open Sigen Viewer Stop?"*
prompt; allow it. The page then stops polling, says what it is showing is the last
window it fetched, and tells you how to reopen. If nothing handles the scheme it says *that*
instead and resumes polling — it re-checks for up to 16 s rather than assuming, so
answering the prompt slowly is not reported as a failure.

> Only the closer registers a scheme. Any page you visit can trigger a registered
> one, and "close a localhost forward to my own machine" is harmless to hand out,
> whereas "open an SSH connection to my home machine and pop a browser tab" is not.
> Reopening stays a Spotlight keystroke.

> **There is no idle auto-close, and that is a deliberate retreat.** One was built
> and measured: launchd owns a Spotlight-launched app as a job and tears the job
> down when the app exits, killing a watcher process the app spawned within a second
> or two — even with `setsid()` in its own session. It survived only when the
> executable was run from a terminal. `ControlPersist` is no help either: with a
> `-N` master its idle timer never starts. Doing it properly means a LaunchAgent on
> the viewing Mac, which is more footprint than a launcher deserves. See
> [Findings 14](docs/FINDINGS.md).

Both apps are `LSUIElement`, so they take no Dock slot and steal no focus, and both
log to `~/Library/Logs/sigen-viewer.log`. `BatchMode=yes` is set on the SSH call: a
GUI app has no terminal, so it fails visibly in a dialog rather than hanging on a
hidden password prompt. They are generated rather than committed — the host, user
and ports belong to your installation. Uninstall with
`rm -rf ~/Applications/"Sigen Viewer.app" ~/Applications/"Sigen Viewer Stop.app"`.

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

The viewer is a **second, separate daemon**, so restarting it — which you will do
whenever you change the page — cannot interrupt capture, and a crash in it cannot
take the logger down:

```sh
sudo deploy/install-viewer.sh      # needs web_launchd_label and web_port in config.json
sudo deploy/uninstall-viewer.sh
```

It runs `ProcessType: Background` and `os.nice(5)`s itself, so the logger — which is
`Interactive` and latency-sensitive — wins any contention for CPU. `web_port` must
be above 1024; the viewer runs as `run_as_user`, not root.

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
bin/status.sh                          # both daemons, heartbeats, archive size
tail -f <log_dir>/sigen.log
tail -f <log_dir>/viewer.log           # the viewer logs errors and page loads only
python3 serve.py --check               # what the viewer sees in the archive
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
| Viewer says "The viewer has nothing to show" | `data_dir` holds no `*-<planhash>.bin` **and** matching `*.manifest.json` pair. It does not recurse — point `--data-dir` at the subdirectory to view a moved series. |
| Viewer crash-loops, `viewer.err` says `cannot bind` | Something already holds `web_port` — usually an older viewer under a previous `web_launchd_label`. `pgrep -f serve.py`, and boot out the old label. |
| Browser says **connection refused**, but `curl` to the same URL works | An IPv4-only listener, refusing the IPv6 address mDNS advertises for a `.local` name. A wildcard bind is dual-stack now; the startup banner says `socket accepts IPv4 and IPv6`. If it says `IPv4`, that is the bug — see [Findings 12](docs/FINDINGS.md). |
| Browser says **`ERR_ADDRESS_UNREACHABLE`** for the `.local` name | The browser picked an IPv6 address from mDNS that this client cannot route to. Use the raw IPv4 address — the startup banner prints it. |
| `ERR_ADDRESS_UNREACHABLE` for the **raw IP too** | The client has no route to that network at all: a VPN or content filter capturing RFC1918, or a different subnet. Not a server problem — the viewer's log will show no request. Use `bin/tunnel.sh`. |
| Viewer log shows **no requests at all** from the machine you are on | The connection is not arriving. Check in this order: raw IP instead of the name, then `bin/tunnel.sh`. The server is fine if another device or `curl` on the host works. |
| Chart shows "partial: N file(s) still being read" | A cold window bigger than the 8 s warm budget. The rest is being summarised in the background; the page re-asks on its own. Only happens once per file. |
| `sampled 1 record in N` chip appears | Expected above ~10 min buckets: at most 64 records per bucket are decoded. Zoom in for exact min/max. |
| Viewer shows a straight line where you expected a gap | It should not — report it. Lines break when the empty run exceeds the field's cadence; a joined line means the gap was shorter than that. |
| Viewer values differ from `bin/latest.sh` | `latest.sh` prints the newest record; the page's tiles print the newest record that **carried data**, and label it `last known good`. They differ exactly during an outage. |
| Page loads but every chart is empty | Check the window: panning back past the start of the archive is legal and shows nothing. Press **Now**. |

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
