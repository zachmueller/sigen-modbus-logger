# Sigenergy SigenStor local telemetry

Read-only Modbus TCP capture and decode for a Sigenergy SigenStor, over your own LAN. No
vendor API, no vendor account. **Capture and decode are stdlib-only Python 3.9+ with nothing
to `pip install`** — a test asserts it, so the part that has to keep running on a bare Python
cannot quietly grow a dependency.

The shape of a running installation: a **Raspberry Pi** on the LAN polls the inverter and
uploads rotated archive files to your own S3 bucket, where a Lambda turns them into
precomputed tiles and a **hosted viewer** plots them behind Google sign-in. The Pi runs two
systemd units and serves no pages itself.

Two parts do not hold to the stdlib rule. `sync.py`, the uploader, needs `boto3` — one
`apt install`, and it is the only module that imports it. `cloud/` deploys the hosted
viewer and needs Node for the CDK stacks. Capture and decode need neither, and
`decode.py` will read an archive on any machine with a bare Python.

There is **no local web server**. There was one, `serve.py`, and removing it is what
[Findings 41](docs/FINDINGS.md) is about; the capability it cost is named in
[Known limits](docs/FINDINGS.md#known-limits). For an unattended capture host the upload
is the only path the data has, which makes `sync.py` load-bearing rather than optional.

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
| Host | Raspberry Pi 4, booting from USB, Raspberry Pi OS Lite (Bookworm), `/usr/bin/python3` 3.11, two systemd units |
| Cadence | 0.5 Hz fast tier, indefinitely |

Every latency measurement in [docs/FINDINGS.md](docs/FINDINGS.md) was taken on the previous
host, a Mac Studio, and has not been re-taken on the Pi. The capture path is platform-neutral
Python and needed no changes to move; what needed changing was the deployment around it, and
the one genuinely new failure mode is that a Pi has no real-time clock
([Findings 38](docs/FINDINGS.md)).

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
- **The capture host listens on nothing.** It used to run a local viewer on `0.0.0.0:8787`
  with no authentication, which meant anyone who could reach the host could read your
  telemetry. That is gone. The Pi opens one *outbound* connection to the inverter and one to
  S3, and accepts nothing but SSH. **Never port-forward either the inverter or the host.**
- **The telemetry leaves your LAN, and that is the design.** `sync.py` uploads to a bucket
  you own, under a credential that can `PutObject` and nothing else — no `GetObject`, no
  `ListBucket` — so a key lifted off the Pi cannot read the archive back or enumerate it.
  What is published from there is gated by Google sign-in against an explicit allowlist; see
  [cloud/README.md](cloud/README.md). Tiles withhold the serial, firmware and the inverter's
  address, because none of them is needed to read a chart.
- **Your inverter is grid-tied equipment.** Polling it is passive, but this
  software comes with no warranty of any kind (see [LICENSE](LICENSE)), and how
  your installer or utility feels about third-party clients is between you and
  them.

---

## Quick start

Anywhere with a Python and a route to the inverter — a laptop is fine to try it on:

```sh
cp config.example.json config.json      # then set "host" to your inverter's IP
python3 config.py --show                # confirm what resolved

python3 dump.py                         # decode every documented register, once
python3 log.py --seconds 60             # capture 60 s into ./data
python3 decode.py data/*.manifest.json data/*.bin --last
python3 decode.py data/*.manifest.json data/*.bin --check
```

**Only one client should poll at a time**, so if a capture host is already running, stop its
logger before pointing anything else at the inverter.

To deploy it properly — a Pi that captures unattended and uploads — see
[Deployment](#deployment). To read the data, see [The viewer](#the-viewer).

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
| `log_dir` | `<install_dir>/logs` | Unit stdout/stderr, appended to by both units. |
| `python` | `/usr/bin/python3` | Interpreter written into the unit files. |
| `logger_unit` | `sigen-logger` | Logger unit name. Installed as `<name>.service`. Change it and disable the old one first. |
| `sync_unit` | `sigen-sync` | Uploader unit name. Installed as `<name>.service` plus `<name>.timer`. Must differ from `logger_unit`. |
| `run_as_user` | — | **Required.** The units run as this user, not root — an empty systemd `User=` is not an error, it just runs as root. |
| `fast_period_s` | `2` | Fast-tier cadence. `2` = 0.5 Hz. **Changing it changes the plan hash.** |
| `rotate_minutes` | `60` | Rotate and gzip the archive file this often. |
| `keep_days` | `0` | Retention, and a way to lose data. See below. |
| `degrade_after` | `5` | Dead ticks before dropping to a single-block probe. |
| `degrade_probe_s` | `30` | While degraded, probe about this often instead of every tick. |
| `recycle_s` | `3600` | Scheduled connection recycle. `0` disables. |
| `max_lag_s` | `5` | Fall this far behind schedule and rebase instead of replaying. |
| `gap_log_quiet_s` | `60` | Collapse repeated `[gap]` lines to one per this interval. |
| `bucket_s` | `300` | Heartbeat and soak-bucket width. |
| `manifest_identity` | `true` | Record model/serial/host in manifests. |
| `s3_bucket` | — | Bucket for the offsite copy. Unset means no uploading. |
| `s3_region` | `us-east-1` | Bucket region. |
| `s3_prefix` | `raw/` | Key prefix. Must end with `/`. |
| `aws_profile` | — | Named `~/.aws` profile. Credentials never live in `config.json`. |
| `sync_enabled` | `false` | Master switch for `sync.py`. Off in the defaults; **true** in the shipped example, because on a capture host the upload is the only path the data has. |

### `keep_days` is a deletion policy

It is narrower than it looks and sharper than it sounds. On **rotation** only, any
`*.bin.gz` older than N days is deleted. It never touches the open `.bin`, never touches
manifests, and never runs between rotations — so pruning can leave you holding a manifest
for a window whose data is gone.

The sharp part: **`*.bin.gz` is the exact form `sync.py` uploads.** An hour whose upload has
not yet succeeded — a router rebooted, a credential expired, the timer disabled while someone
was debugging — is deleted by the same mechanism that is supposed to be housekeeping, and on
this deployment there is no second copy. So the number is an outage budget, not a tidiness
setting: with hourly rotation and a five-minute timer, `7` gives a week to notice and fix a
broken upload, and bounds the drive at roughly 15 MB of records plus a manifest per day.

`config.py`'s default is `0` — keep everything, ~2.2 MB/day gzipped, about 800 MB/year —
because someone whose archive is the only copy should not lose it by accepting a default.
`config.example.json` sets `7`, beside `sync_enabled: true`. Those two belong together, and
neither is safe without the other. See [Findings 40](docs/FINDINGS.md).

### One config, every consumer

The same file drives the shell scripts and the systemd units, so nothing hardcodes a path in
two places:

```sh
python3 config.py --show                              # resolved config as JSON
python3 config.py --sh                                # SIGEN_*='...' for eval in sh
python3 config.py --render deploy/systemd/sigen-logger.service.template
```

---

## Files

| File | Role |
|---|---|
| `config.py` | Config loader. Also `--show`, `--sh`, `--render`. |
| `lib.py` | Modbus transport and decode primitives. Not a CLI. Owns the conventions capture and decode must agree on. |
| `log.py` | The logger. Tiered raw capture, topology drift detection, soak harness. The only module that constructs `lib.Modbus`. |
| `discover.py` | Finds the inverter again when its address changes: a bounded LAN sweep, then the caller's identity probe, adopting the fastest **serial match**. Deliberately not a Modbus client — see its docstring. Not a CLI. |
| `alerts.py` | Push notifications, and the one place that knows where the credential lives. Never raises and never blocks for long: a monitoring failure must not become an outage in the thing it monitors. Not a CLI. |
| `decode.py` | Offline decode of the raw archive into CSV/JSONL/derived series. |
| `dump.py` | One-shot full-map decode: what every documented register returns on this unit. |
| `sync.py` | Copies rotated archive files to S3. The one module that needs `boto3`, imported lazily inside it. Off unless `sync_enabled` — and on a capture host it is the only path the data has. |
| `panels.py` | The presentation contract: which fields make up which chart, and which are "live". Data only; imports nothing. Read by `ingest.py` and shipped to the page in `meta.json`. |
| `tiles.py` | The wire format the page reads, so a live window and a frozen share cannot disagree. Not a CLI. |
| `series.py` | Archive index, bucket aggregation and health scan. Not a CLI. |
| `ingest.py` | An archive directory to a directory of precomputed tiles. Knows nothing about S3, which is what makes it testable. Not a CLI. |
| `cloud/` | The hosted viewer: CDK stacks, the ingest and share Lambdas, and the maintenance scripts — a raw backfill, a tile rebuild, and one that gives older shares their own page. Deploy-time only; see [cloud/README.md](cloud/README.md). |
| `web/` | The page: one HTML file, one stylesheet, three scripts — the renderer, the charts, and the tile adapter. Published by the site stack. No framework, no CDN. |
| `regmap_gen.py` | Regenerates `regmap.json` from the upstream register definitions. |
| `regmap.json` | The register map: 358 fields, 10 alarm appendices, 6 enums. Generated, not hand-written. |
| `bin/status.sh` | Health check: clock sync, all three units, recent heartbeats, gap/degraded counts, the timers, which address is actually being polled, archive size, free space. No sudo. |
| `bin/latest.sh` | Newest values, vertically. The "is it working right now?" view. No sudo. |
| `bin/watchdog.py` | The check that pushes rather than waiting to be read. Tells "the inverter stopped answering" apart from "the capture process is stuck", and notifies on a change of state. No sudo. |
| `bin/leak-check.sh` | Run before every push: does anything installation-specific appear in a tracked file, now or in history? |
| `deploy/systemd/*.template` | The three units and the two timers, rendered from config at install time. |
| `deploy/systemd/install-logger.sh`, `uninstall-logger.sh` | Unit install/removal for the logger. Verifies the rendered unit *before* installing it. Needs sudo. |
| `deploy/systemd/install-sync.sh`, `uninstall-sync.sh` | The same for the uploader's service and timer. Checks boto3 and the credential *as the user the unit will run as*. |
| `deploy/systemd/install-watchdog.sh`, `uninstall-watchdog.sh` | The same for the staleness check. Warns rather than fails when `alert.conf` is missing or too readable — a check that cannot tell you anything is worth installing loudly, not quietly. |
| `deploy/systemd/_lib.sh` | The render → verify → install → enable → prove-it sequence both installers share. |
| `deploy/pi/bootstrap.sh` | Runs **on the Pi**: packages, timezone, the clock, flash-wear settings, the watchdog, logrotate, the units. The one description of a configured host. |
| `deploy/pi/provision.sh` | Runs **on your workstation**: rsyncs the code, pushes `config.json` and one AWS profile over SSH, then runs `bootstrap.sh`. Idempotent; re-running is how you deploy a change. |
| `deploy/pi/sanitize.sh` | Runs **on the Pi** before capturing a golden image: removes both secrets, the archive, the SSH host keys and the machine id, then zero-fills free space. |
| `deploy/pi/capture-image.sh` | Runs **on macOS**: reads a sanitized drive to a compressed `.img.xz`. The one script here that can destroy something, so it refuses anything that is not external and makes you type the disk id twice. |
| `deploy/pi/logrotate.sigen` | Rotation for the log files. `copytruncate`, because systemd holds them open. |
| `tests/test_offline.py` | The whole capture/decode path against a fake device. No hardware, no network. Also the config loader and the unit templates. |
| `tests/test_web.py` | The read side: aggregation, health, tiles, ingest, the uploader's ledger. Asserts none of it can become a second Modbus client. Also the cloud and page contracts, at source level, since there is no JS harness. |
| `docs/FINDINGS.md` | Every bug this project has found, what made each one quiet, and the rule it produced. Referenced by number from the code. |
| `docs/MIGRATION.md` | Moving the capture host to another machine: the clock risk a host without an RTC introduces, the ordering the cutover needs, and what to verify. Read before the hardware is bought. |
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
  and logs `[gap]` instead of replaying every missed tick. Without this, an hour of the
  process not running — a machine asleep, a host suspended, a long stall — would fire ~3600
  back-to-back requests at the device the moment it resumed.
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
- **Rediscovery** (`--discover`, off by default) — after `REDISCOVER_AFTER_S` (120 s) with
  no data, sweep the LAN for the inverter and adopt it if its **serial matches**. See
  [Rediscovery](#rediscovery-when-the-address-changes) below.
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

### Rediscovery: when the address changes

**The failure this exists for.** On 2026-09-04 the inverter's controller lost its DHCP
lease, came back on a different address, and an unrelated device took over the old one —
which answered ICMP and refused every TCP port. The logger polled that address for
**fourteen hours**. Nothing was broken in a way any check could see: the daemon was
running, the archive was growing, and `decode.py --last` correctly reported
`logger is HEALTHY`, because it was. The *device* was the thing that had gone.

Enable it with `log.py --discover`. It is **off by default**, and the flag lives on the
unit's `ExecStart` rather than in `config.json`, because sweeping somebody's LAN is not
something a logger should do unless the command that started it said so.

What it does, once the device has been silent for `REDISCOVER_AFTER_S` and at most once per
`REDISCOVER_EVERY_S`:

1. Open plain TCP to `port` across the /24 of the address in use **and** the /24 this host
   is on. Both, because if the router's LAN range is what changed, the old subnet is empty
   and the local one has the device — and if the capture host moved, the reverse.
2. For each host that answers, read the identity block (one FC4 read of 40 registers at
   30500) and compare the **serial number**.
3. Adopt the **lowest-latency exact match** and log `[rediscovered]`, then notify.

Three properties worth knowing, because each one is a decision rather than an accident:

- **No serial, no adoption.** "Something is listening on 502" is not "this is my inverter":
  a neighbour's unit, a heat pump or a building's BMS all answer the same way. The expected
  serial comes from the live identity read, or — the case that matters, since the device was
  already unreachable at startup — from the newest manifest on disk. If neither knows one,
  the search refuses to return a host and says so.
- **Fastest match wins, not first found.** The unit answers on both its wired port and its
  Wi-Fi module with the same serial, and the wired one is several times faster. Choosing by
  latency lands on it without anything having to know which address is which.
- **`plan_hash` does not include the host**, so adopting an address keeps the same series
  and the archive stays continuous. The *manifest* records the address polled and is written
  once per day, so a mid-day adoption leaves the morning's address recorded there. That is
  provenance only — decoding needs none of it.

The address actually in use is written to `logs/logger-host`, which is what
`bin/status.sh` reports and what distinguishes "adopted" from "as configured".
`config.json` is still what a restart uses, so set `host` to the adopted address once you
have seen it settle.

Rediscovery is **not** a substitute for a DHCP reservation — it turns a multi-hour outage
into a gap of a few minutes, but the gap is still real. Reserving the controller's address
on the router prevents it instead.

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

## The viewer

There is no viewer on the capture host. The page is served by CloudFront out of the bucket the
Pi uploads to, behind Google sign-in against an explicit allowlist, and it plots **precomputed
tiles** rather than decoding an archive on demand. Deploying it is
[cloud/README.md](cloud/README.md)'s subject; what follows is how to read it.

The trade, stated once: the page is up to an hour behind — a tile appears when the logger
rotates — and it can only chart a field that a tile carries. In exchange it needs no server,
survives the capture host being off, and costs the inverter nothing. For anything outside
that, `decode.py` reads the raw archive at the full 2 s cadence over all 358 documented
registers.

> **There used to be a local viewer**, `serve.py`, on `0.0.0.0:8787`. It was removed with the
> move to a Pi: it was the host's only listening port and its only unauthenticated exposure,
> and the hosted page had made it redundant for everything except a close look at an odd
> register. What that cost is in [Known limits](docs/FINDINGS.md#known-limits); why the
> removal needed a refactor first is [Findings 41](docs/FINDINGS.md). `web/app.js` still
> carries its inert `server` branch, gated and unreachable, because the page is pinned by
> every existing share's bundle hash and trimming it is a change worth making on its own.

### What is on the page

| | |
|---|---|
| Live strip | PV, load, battery, grid, SOC and plant state, each with a sparkline over the window. Values are the newest **good** sample, labelled `last known good` when the device is not answering. |
| Energy this window | Differences of the device's own lifetime counters — the independent cross-check, not an integral of the power series. |
| Power flow | Grid, load, battery and PV on one kW axis, with the sign conventions on the card. |
| Battery SOC | Its own chart. Never a second y-axis on the power plot. |
| PV panels | Current per DC input, and voltage behind a toggle. `pv1..pv4` — the inverter's four inputs, each a group of panels — only: the other 32 documented channels return the −1 absent marker here. |
| Grid quality | Frequency, phase A voltage and power factor, as separate small charts. |
| Temperatures | PCS internal and average cell, both °C, so one axis is honest. |
| Plant state | Running state, grid connection and EMS work mode as labelled bands; all six alarm words OR-ed over each bucket. |
| Capture health | Tick latency median/p95/max per bucket, with outage and no-record spans shaded. |
| Custom chart | Any of the ~259 captured fields. Fields sharing a unit share an axis; a mixed-unit pick becomes one chart per unit. |

Arrow keys move through history, `+`/`−` zoom, `n` jumps to now. Focus a chart and
the arrows move its crosshair instead. The window is in the URL, so a view can be
bookmarked or sent to anyone who can sign in.

**The page is up to an hour behind, and says so.** Tiles are published when the logger
rotates, and `web/tiles.js` caches every object it fetches for the life of the page — so
nothing in the page can ask for newer data, and a browser reload is genuinely the only thing
that works. The hint in the filter row says so. `Live (10 s)`, `Reload` and `Download CSV`
are hidden (`data-server-only`): they belonged to the local server, and `app.js` derives that
from the source rather than being told, so there is nothing for a deployment to remember. See
[Findings 34](docs/FINDINGS.md).

**If the page looks stale, two different things can cause it** and the page cannot tell them
apart: the logger stopped, or the *uploader* stopped. `record_ts` only advances on rotation,
so both produce one symptom, and the warning names both rather than picking one
([Findings 29](docs/FINDINGS.md)). `bin/status.sh` on the Pi is what distinguishes them.

Every panel's heading carries a link icon, which copies a URL opening the page on that panel,
and a `−`/`+` to collapse or expand it; an expanded one also offers `Table`, the numbers behind
the chart. Those `+`/`−` **buttons** are not the `+`/`−` **keys** above — the keys zoom the
window, the buttons open one chart. Collapsing is not cosmetic: only expanded panels are
requested, so a page showing four charts costs a quarter of one showing sixteen.

### Windows, buckets and stride

The page asks for a *window*, and `web/tiles.js` composes it from precomputed tiles. How
those were built is `series.py`'s business — `ingest.py` calls it — and every choice it made
travels in the tile and is shown in the filter row.

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
- **Which fields a tile carries depends on its width**, and this is the limit worth knowing:
  an hour tile holds the 32 fields the panels, energy tiles and live strip need; from 120 s up
  a tile spans a day and carries 240. So **the field picker only opens on a window wider than
  about 15 hours.** `meta.picker_min_bucket_s` and `meta.target_buckets` carry the numbers, so
  the page states the real limit rather than offering a field that would come back empty. For
  any of the 358 documented registers at the full 2 s cadence, the tool is `decode.py` against
  the raw archive.

The cache, the warm budget and the newest-first read order all still apply — they are
`series.py`'s, and they now run inside the ingest Lambda rather than in front of a browser.
What they bought when they did run in front of a browser (a 6 h window in 0.15 s over 26
files) is no longer a number anyone waits on: a tile is built once and served from a CDN.

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

The answer used to be a self-contained HTML file, written by the local server. It has been
**retired** in favour of a shared URL, which is a better answer whenever the recipient is
online: nothing to attach, and nothing to bounce off a mailbox size limit.

It is still a frozen copy, which is the part of the export's design worth keeping: a share
copies the tiles it needs *and* the page that draws them, so what someone opens next year is
what was on screen the day it was sent. What the export could not be is a link.

The pieces:

- **A share.** `Save this view` copies the tiles it needs *and* the page that draws them to a
  `/p/<id>` prefix that needs no account. That is the one thing you can send to an installer.
  See [cloud/README.md](cloud/README.md).
- **The URL.** The window, the expanded panels and the custom fields are all in the page's
  hash, so a view is a link for anyone who can sign in.
- **A link to one panel.** `#focus=<panel>` opens that panel and scrolls to it, leaving
  everything else as the rest of the hash says. The link icon in a panel's heading copies
  such a link for you. It is accepted on every form of this page — this server, the hosted
  viewer, and a `/p/<id>` share, which is where it earns the most: the reason for sending a
  view is usually one panel, and the alternative is describing which one in prose.

  A panel is named by its id or by its title, so `#focus=temps` and `#focus=temperatures`
  are the same request, as are `#focus=panels-volts` and `#focus=pv-panels-voltage`. Both
  come from `PANELS` in `panels.py`, so a new chart there is focusable with no other change.
  A name that matches nothing is ignored — a link outlives the deployment that made it, and
  a stale name should still render the page. Which is also why an **id** is worth leaving
  alone once links exist: renaming one drops that panel from any saved `panels=` or `#focus=`
  that names it. A frozen share is unaffected, since it carries its own copy of the list.

There is no CSV download. It was a route on the local server, and the link is hidden wherever
nothing answers it. For numbers rather than a picture, `decode.py` on the archive is the
better tool anyway — it gives per-record rows, which the bucketed download never could.

The page is source-agnostic, and that is what makes a frozen share the same code as a live
window rather than a second renderer: `web/app.js` reads every answer through one `getJSON()`
seam, and each entry point sets `window.SIGEN_SOURCE` before the script loads to point that
seam at the right tiles. A shared view is that same page with a fixed window —
`applyFrozenMode()` removes every control that would ask for another one, and labels the ages
`when shared` rather than presenting them as now.

**One renderer, and a share still gets a copy of it.** Those are not in tension, and the
distinction cost a bug to learn. There is one `web/` in this repository and no second
implementation to drift — but a *deployment* overwrites its files in place, so a share that
pointed at them was frozen in its data and live in its code, drawn by whatever existed when
it was opened. Each deploy therefore publishes an immutable copy of the whole bundle under a
key naming its own contents, and a share copies that page in beside its tiles. Same code,
pinned; see [Findings 35](docs/FINDINGS.md) and [cloud/README.md](cloud/README.md).

### The three answers

`web/app.js` reaches the outside world through exactly one seam, `getJSON()`, and asks for
only three things. `web/tiles.js` composes all three out of static objects — there is no
server to route them.

| Answer | Carries |
|---|---|
| `/api/meta` | Plan hash, archive extent, block plan, the panel definitions, and the field catalogue with units, cadences, enum labels and duplicate markers. Plus the tile geometry the page needs in order to state its own limits. |
| `/api/window?hours=6&end=…&panels=…&fields=…` | The bucket grid, per-field min/mean/max, health, window energy, bucket width, stride, timezone change points. |
| `/api/latest` | Newest good sample, with data-age and record-age reported separately. |

A test pins that set: adding a fourth fails until something can compose it, and a route
nothing answers has to be provably gated or the suite refuses it.

Which fields make up which chart lives in `PANELS` in [`panels.py`](panels.py), so a new chart
is a dict there, not new JavaScript. A test asserts every field it names is actually covered
by the block plan — otherwise a typo would render an empty chart that looks like a quiet
night.

`ingest.py` copies that list into each plan's `meta.json` when it builds tiles, so an edit
reaches the page on the next rotation — about an hour — or immediately if you replay one S3
event. It also means a frozen share keeps the panels, titles and ids it was made with, which
is why renaming an `id` cannot break an existing share and *can* break a saved link.
[cloud/README.md](cloud/README.md) has the commands.

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

A **Raspberry Pi 4 booting from a USB drive**, running two systemd units as an ordinary user.
Nothing here needs privilege, and the host listens on nothing but SSH.

```
<install_dir>/               code (rsync'd by provision.sh)
<install_dir>/config.json    local config — pushed separately, never rsync'd over
<install_dir>/data/          archive: the open .bin, rotated .bin.gz, manifests, .uploaded.json
<install_dir>/logs/          sigen.log, sigen.err, sync.log, sync.err, watchdog.log, watchdog.err
<install_dir>/logs/watchdog.state   what the last check reported, so the next one does not repeat it
<install_dir>/logs/logger-host      the address actually being polled (see Rediscovery)
<install_dir>/alert.conf     notification target, mode 600 — pushed separately, never rsync'd
~/.aws/credentials           one profile, PutObject only — pushed separately
```

### The three units

| Unit | What it is | Why it is its own unit |
|---|---|---|
| `sigen-logger.service` | `log.py --seconds 0 --discover`, `Restart=always`, `Nice=-5` | The latency-sensitive one. It must win contention and nothing may be able to restart it as a side effect. |
| `sigen-sync.timer` → `sigen-sync.service` | `sync.py --once`, `Type=oneshot`, every 5 minutes | A job that finishes, not a daemon. Separate so restarting the uploader cannot interrupt capture; it reads rotated files and writes only its own ledger. |
| `sigen-watchdog.timer` → `sigen-watchdog.service` | `bin/watchdog.py --stale-after 1800`, `Type=oneshot`, every 15 minutes | Separate from the logger for the reason that matters: **a check inside the process it is checking cannot report that process being wedged.** Distinguishing that from "the inverter stopped answering" is the whole job. |

Four details in those units are load-bearing:

- **`Wants=`/`After=systemd-time-wait-sync.service` on the logger.** A Pi has no real-time
  clock. Until NTP answers, the clock holds whatever `fake-hwclock` last wrote — and the
  archive is keyed on that clock in the filename stamp `decode.py` sorts by, in its
  backwards-step check, and in the ingest Lambda's test for whether a tile span is closed. A
  record filed under the wrong hour can be baked into a tile promised `immutable` for a year.
  This is the most important line in the file. There is **no runtime guard in `log.py`** —
  systemd ordering is the whole of the protection — so what it depends on, and the three
  checks that settle it, are written out in
  [docs/MIGRATION.md § 1](docs/MIGRATION.md#1-the-clock-read-this-before-you-buy-the-hardware).
  See also [Findings 38](docs/FINDINGS.md).
- **`ConditionPathExists=<install_dir>/config.json` on both.** A host with no config comes up
  quiet instead of crash-looping every 30 s on "no inverter host configured". That is what
  makes a freshly written image safe to boot before it has been given a credential.
- **`StandardOutput=append:` to files under `log_dir`**, not the journal, so the paths
  `bin/status.sh` greps and every `tail -f` below keep meaning what they say.
- **`Nice=-5`.** systemd applies it as root before dropping to `User=`, so an unprivileged
  user gets a negative nice it could not set for itself. This is what `ProcessType: Interactive`
  was on the Mac.

### Provisioning a Pi

From a stock **Raspberry Pi OS Lite (64-bit)** install with SSH enabled and your key
authorised — Raspberry Pi Imager's customisation does all of that when it writes the drive.
Use **Ethernet**: p95 latency is a measured property of this system and wifi power save adds
spikes to it, and it keeps a passphrase out of any image you later capture.

> **USB boot on a Pi 4** needs a bootloader with mass-storage support. The stock
> `BOOT_ORDER=0xf41` is SD-then-USB, so with **no SD card inserted** it boots the thumb drive
> with no change at all. Check with `rpi-eeprom-config`; if the Pi shipped with an old EEPROM,
> update it once from an SD card first.

Set `host`, `run_as_user`, `install_dir`, `s3_bucket`, `aws_profile` and `sync_enabled` in
your local `config.json`, then, **from your workstation**:

```sh
deploy/pi/provision.sh pi@sigen-pi.local
```

That rsyncs the code (excluding `data/`, `logs/`, `config.json` and `cloud/`), pushes the two
secrets over SSH, runs `deploy/pi/bootstrap.sh` on the Pi, restarts the logger and prints
`bin/status.sh`. It is **idempotent** — re-running it is how you deploy a change.

The two secrets travel separately from the code, and never in an image: `config.json`, and
**one** `~/.aws` profile — the one `aws_profile` names, not the whole file, because your
workstation's `~/.aws` also holds a credential that can create and delete infrastructure. The
Pi has no business with it. Give the Pi its **own access key** on the same PutObject-only IAM
user (two are allowed), so either can be revoked without touching the other.

`bootstrap.sh` is the one description of a configured host — packages, timezone, the clock,
journald to RAM, `noatime`, no swapfile, the hardware watchdog, logrotate, then the units. The
golden image is captured from a host it has been run on, so there is no second list to keep in
step. It needs a reboot at the end for `noatime` and the watchdog.

`timedatectl`'s timezone must match `capture_tz` in `cloud.json`: the ingest Lambda runs with
`TZ` set to that and `series.tz_runs()` reads `localtime()`, so a mismatch labels every axis
in the wrong zone while the data itself stays correct.

Installing or removing one unit at a time, if you would rather not run the whole thing:

```sh
sudo deploy/systemd/install-logger.sh     # renders, verifies, installs root:root 644, enables
sudo deploy/systemd/install-sync.sh       # checks boto3 and the credential AS run_as_user
sudo deploy/systemd/uninstall-logger.sh   # disables and removes; leaves the archive
sudo deploy/systemd/uninstall-sync.sh
```

Each renders its unit and runs `systemd-analyze verify` on it **before** installing — a
verify against a file already in `/etc/systemd/system` is reporting on something live, which
is too late to be a question.

boto3 comes from **apt**, not pip: Debian refuses `pip install --user` with
`error: externally-managed-environment` (PEP 668), so `sudo apt install python3-boto3` is the
answer and `python` stays `/usr/bin/python3`. `bootstrap.sh` installs it; see
[Findings 39](docs/FINDINGS.md).

### Updating the code

```sh
deploy/pi/provision.sh pi@sigen-pi.local
```

`systemctl restart` sends SIGTERM, which `log.py` handles: it closes the archive and joins
outstanding gzip threads rather than truncating the open `.bin`. Verify with
`systemctl show -p NRestarts,ExecMainStatus sigen-logger` — a graceful restart shows
`ExecMainStatus=0`.

**If the change touches the block plan or `fast_period_s`**, segregate the existing series
first, because the plan fingerprint changes and old files must keep the manifest that
describes them:

```sh
ssh pi@sigen-pi.local 'cd ~/sigen/data && mkdir -p prev-$(date +%Y%m%d) \
  && mv sigen-*T*.bin* sigen-*.manifest.json prev-$(date +%Y%m%d)/'
```

Then provision, and confirm the new files carry the new hash.

### A bootable image, so the drive is rebuildable

A thumb drive is the component most likely to fail, and reinstalling from scratch is an
afternoon. So: provision one Pi properly, strip it back, and capture its drive.

```sh
ssh pi@sigen-pi.local 'sudo ~/sigen/deploy/pi/sanitize.sh'   # both secrets, archive, host keys, machine-id
ssh pi@sigen-pi.local 'sudo shutdown -h now'
# move the drive to your Mac
deploy/pi/capture-image.sh                                   # lists candidate disks
deploy/pi/capture-image.sh disk4 sigen-pi-20260824.img.xz
```

`sanitize.sh` zero-fills free space at the end, which is what makes the image compress to
roughly its *used* size rather than the drive's. It also leaves a one-shot unit that
regenerates the SSH host keys, gated on `ConditionPathExists=!`, so it is a no-op in every
image made from that one.

**The constraint, stated plainly:** the image describes a whole drive, so it can only be
written to a drive **at least as large** as the one it came from. Nominal capacities lie — two
"32 GB" drives from different makers differ by megabytes — so keep a matched pair. Shrinking
the partition would need `resize2fs` and a loop device, neither of which macOS has; if it
becomes a problem, run `pishrink` in a privileged Linux container rather than teaching macOS
about ext4.

A written clone boots with both units **installed, enabled, and not running** — their start
condition has nothing to find yet, and they hold no credential. One command finishes it:

```sh
deploy/pi/provision.sh --config-only pi@sigen-pi.local
```

`capture-image.sh` only ever *reads* the drive; there is no `of=` pointing at a disk anywhere
in it. But it takes a raw device identifier from a human, so it refuses anything that is not
an external, non-virtual, non-root disk, prints what it is about to read, and makes you type
the identifier a second time.

### Monitoring

```sh
bin/status.sh                          # clock, all three units, heartbeats, timers, archive, space
bin/latest.sh                          # newest record, vertical
python3 sync.py --status               # uploaded, pending, stranded, when it last ran
python3 bin/watchdog.py --dry-run      # is capture stale, and which of the device or logger
tail -f <log_dir>/sigen.log
tail -f <log_dir>/sync.log
tail -f <log_dir>/watchdog.log
```

`bin/status.sh` reports the clock **first**, above everything the clock dates, and every
section states an absence rather than printing an empty heading — an inactive unit whose
start condition simply did not hold is not a failure, and it says which.

Log lines worth grepping: `[gap]` (schedule rebase after a stall), `[degraded]` (device
unreachable, probing), `[recovered]`, `[discover]` / `[rediscovered]` (the address moved),
and the heartbeat.

**The real monitor is the hosted page.** With no local viewer, "is it working?" is answered by
`agg/latest.json`'s `record_ts` advancing — which covers the logger and the uploader together.
`bin/status.sh` is what tells you which of the two stopped when it does not.

### The watchdog: being told, rather than looking

Everything above requires someone to look. On 2026-09-04 nobody did for fourteen hours, and
none of it would have helped if they had not: the daemon was up, the archive was growing, and
`bin/status.sh` was entirely happy. `bin/watchdog.py` is the piece that pushes.

```sh
python3 bin/watchdog.py --dry-run        # what it would say, sending nothing
python3 bin/watchdog.py --stale-after 1  # force one real notification, to prove delivery
```

It reports **two states separately**, because they have different causes and different fixes:

| State | Meaning | Where to look |
|---|---|---|
| `DEVICE` | The logger is fine and writing empty probe records; the inverter is not answering. | The network and the inverter. This is the 2026-09-04 failure. |
| `LOGGER` | No record of any kind is being written — the capture process itself is stuck. | The daemon and `logs/sigen.err`. |
| `ARCHIVE` | No decodable series at all. | Whether capture has ever run here. |

It computes no freshness arithmetic of its own: `series.latest()` already draws the
data-freshness vs record-freshness distinction the whole incident turned on, and it is what
the hosted page reads, so a second implementation could disagree with the page.

Notifications go to whatever single line is in **`alert.conf`** beside `config.json` — an
ntfy topic name or a full URL. Keep it `chmod 600`: an ntfy topic is a *capability*, not just
a name, since anyone holding it can both read the alerts and post to them. It is gitignored,
and `bin/leak-check.sh` reads it to add the topic to the patterns it scans the tree and
history for. With no `alert.conf` the check still runs and still writes `watchdog.state` —
it just has nothing to tell you, and says so.

It notifies on a **change** of state and re-nags at most once every 6 hours. A watchdog that
pushes on every run trains you to ignore it, which is the same outcome as not having one. A
notification that fails to send does **not** count as sent, so the next run retries.

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
| Heartbeat stops but the unit shows `active (running)` | Check for `[degraded]` — device unreachable. |
| `latest.sh` says no complete records | Archive just rotated; the new file has fewer than one full record. |
| `no manifest for plan <hash>` | The newest archive was written under a plan whose manifest is missing — usually a manifest moved without its data. |
| Unit won't load | `systemd-analyze verify /etc/systemd/system/sigen-logger.service`. The installer verifies before installing, so this usually means the file was hand-edited. |
| Unit is `inactive` right after install | Check `ConditionResult`. If it is `no`, the start condition did not hold: `config.json` is not at `install_dir`. `bin/status.sh` says so explicitly. |
| Unit fails with `Failed to set up mount namespacing` | `data_dir` or `log_dir` does not exist. `ProtectSystem=strict` names them in `ReadWritePaths` and systemd cannot bind-mount a missing path. Re-run the installer, which creates them. |
| `NRestarts` climbing with nonzero `ExecMainStatus` | Real crash. Check `logs/sigen.err`. |
| Archive stamped in the past; `decode.py --check` reports backwards clock steps | The logger started before NTP answered. `timedatectl` should say `System clock synchronized: yes`; `systemctl is-enabled systemd-time-wait-sync` should say `enabled`. See [Findings 38](docs/FINDINGS.md). |
| `boto3 is not installed` from `install-sync.sh`, and `pip install --user` fails | `error: externally-managed-environment` is PEP 668. Use `sudo apt install python3-boto3`. |
| Uploader never runs | The **timer** is what gets enabled, not the service. `systemctl list-timers sigen-sync.timer`. Enabling the service alone runs it once per boot and never again. |
| `sync.py --status` shows a growing `pending` count | Uploads are failing. Check `logs/sync.err` and the credential. **Act on this**: `keep_days` deletes `.bin.gz` on rotation whether or not it was uploaded, so the pending backlog has a deadline. |
| `sync.py --status` reports files `stranded` | Un-rotated `.bin` files rotation has moved past — a logger killed mid-hour leaves one, and nothing gzips it afterwards. Never counted as pending. Check they reached S3 another way before gzipping one by hand: a stem carrying both `.bin` and `.bin.gz` is a silent double-decode. |
| Drive filling up | `keep_days` is `0` or too high for the medium. `bin/status.sh` reports `df`. |
| `plan-hash mismatch -- refusing to decode` | Files and manifest come from different block plans. Decode each series with the manifest stored beside it. |
| `device clock ... REAL GAPS [...]` | Samples genuinely missing. Check for `[degraded]` around that time. |
| `device clock ... N jitter (balanced, no loss)` | Benign. A read lands wherever it falls relative to the device's internal second tick, so at a 2 s cadence ±1 s steps are expected and arrive in compensating pairs. |
| `device clock` step count wrong entirely | Wrong manifest for these files — the cadence recorded in it differs from the one they were captured at. |
| `host clock ... BACKWARDS steps` | Files from different series mixed in one invocation, or the host clock stepped. Ordering itself is handled automatically. |
| Latency looks like it is climbing | Check with `--latency` before acting. It oscillates ~1.6x; consecutive heartbeats routinely look like a trend when there is none. |
| `latest.sh` age bounces around over repeated runs | Empty probe records land every ~35 s during an outage, so record age sawtooths. `--last` labels this explicitly as DEVICE NOT ANSWERING. |
| Flood of `[gap]` lines, `retries` climbing | The device is unreachable. Confirm with `ping` / `nc -z host 502`. Expected during installer work or a power-down; capture resumes by itself. |
| `[degraded]` with no `[recovered]` | Still unreachable. Empty records (mask 0) continue to mark the outage in the archive. |
| `ping` answers but `nc -z host 502` says **connection refused** | Almost certainly not your inverter any more. A refusal is a live host with nothing listening — a different device took the DHCP lease. Sweep for it: `bin/watchdog.py` will say `DEVICE`, and `--discover` finds it. A *timeout* instead means no host or a filter, which is a different diagnosis. |
| `[discover] nothing listening on :502` | The unit really is down or off the network, not merely moved. Rediscovery is working; there is nothing to find. |
| `[discover] ... none reported serial` | Something answers Modbus but is not this inverter. Deliberately not adopted. |
| `[discover] no expected serial is known` | The device was unreachable at startup *and* no manifest on disk carries a serial — a brand-new install that has never captured. Nothing will be adopted until one successful capture records one. |
| `bin/status.sh` says `polling: X <- ADOPTED` | Rediscovery moved to a new address. Capture is fine, but `config.json` still names the old one and that is what a restart uses. Set `host` to the adopted address. |
| Watchdog notifies `DEVICE` but the page looks fine | The page lags by up to the rotation interval. The watchdog reads the local archive and is the earlier signal of the two. |
| Watchdog says nothing while capture is clearly stale | Check `logs/watchdog.log` for `no alert.conf` — the check ran and had nowhere to report to. Also confirm the timer, not just the service, is enabled. |
| Heartbeat silent but records accumulating | Only possible if `bucket_s` and `fast_period_s` interact badly. Emission is on bucket transition. |
| The page's newest data is up to an hour old | Expected. Tiles appear when the logger rotates. Nothing in the page can ask for newer — reload the browser. |
| The page says the logger **or the uploader** may have stopped | It cannot tell which: `record_ts` only advances on rotation, so both look the same from there. `bin/status.sh` on the Pi distinguishes them. |
| `sampled 1 record in N` chip appears | Expected above ~10 min buckets: at most 64 records per bucket were decoded when the tile was built. Zoom in for exact min/max. |
| The field picker is greyed out, or a field comes back empty | The window is narrower than ~15 h, so it resolves to hour tiles, which carry 32 fields rather than 240. Widen the window; the page states the threshold. For any register at the full cadence, use `decode.py`. |
| A window reaching back past a cadence change shows the older side as a gap | `web/tiles.js` reads one plan. The tiles exist under the previous hash; "no data" there means "not in this plan". `decode.py` reads across it. |
| The page shows a straight line where you expected a gap | It should not — report it. Lines break when the empty run exceeds the field's cadence; a joined line means the gap was shorter than that. |
| The page's values differ from `bin/latest.sh` | `latest.sh` prints the newest record; the page's tiles print the newest record that **carried data**, and label it `last known good`. They differ exactly during an outage. |
| Page loads but every chart is empty | Check the window: panning back past the start of the archive is legal and shows nothing. Press **Now**. |
| A saved link no longer opens the panel it names | Its `panels=` or `#focus=` names a panel `id` that has since been renamed. An unknown name is ignored rather than reported, so the page renders normally and one chart stays shut. Re-copy the link; ids are in `PANELS`. |
| The page still shows an old panel title after editing `PANELS` | Expected for up to an hour. `ingest.py` copies the list into `meta.json` when it builds tiles, so the change arrives with the next rotation — or immediately, by replaying one S3 event (`cloud/README.md`). |

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
