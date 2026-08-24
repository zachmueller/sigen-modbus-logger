# Moving the capture host

A one-time procedure, written down because most of it is ordering and the mistakes are
quiet. It was used to move from a Mac Studio running LaunchDaemons to a Raspberry Pi 4
running systemd units, but nothing below is specific to those two machines.

[README.md § Deployment](../README.md#deployment) is how you *run* a capture host. This is
how you *replace* one.

Read [§1](#1-the-clock-read-this-before-you-buy-the-hardware) before anything else. It is the
only item here that can silently corrupt data that is already correct, and the only one whose
consequences reach into a CDN cache you cannot fully invalidate.

---

## 1. The clock: read this before you buy the hardware

**A Raspberry Pi has no battery-backed real-time clock.** A desktop or a server does. If the
machine you are moving *from* has one and the machine you are moving *to* does not, you have
introduced a new failure mode that no test in this repository can catch on its own, because it
only happens at boot on real hardware.

At boot, before `systemd-timesyncd` reaches an NTP server, the clock holds whatever
`fake-hwclock` last wrote to disk. After a clean shutdown that is close enough. After a power
cut, an unclean reboot, or a drive that has sat in a drawer, it can be **minutes to hours in
the past**.

### Why that is a data-integrity problem and not a cosmetic one

The archive is keyed on the host clock in three places, and all three fail without saying so:

1. **The filename.** `sigen-YYYYMMDDTHHMMSS-<hash>.bin`. `decode.py` sorts by the stamp in
   the name rather than by argv order — deliberately, so that a `*.bin *.bin.gz` glob decodes
   chronologically instead of plain-files-first. A file stamped in the past therefore sorts
   *before* files already written, and the series it produces steps backwards through data
   that arrived in order.
2. **`decode.py --check`.** Its backwards-clock-step detector fires, correctly, and the report
   is indistinguishable from the one a corrupted archive or a mixed-plan decode produces. The
   README's troubleshooting table will send you looking for mixed series.
3. **The ingest Lambda, which is the expensive one.** A tile is written once from a *complete*
   span and then served `Cache-Control: immutable` for a year. "Complete" is decided by
   comparing the clock against the archive's newest record. A record attributed to the wrong
   hour can close a span that is not closed, or be baked into a tile the CDN will keep for a
   year — and rewriting the S3 object is not enough to fix that. See
   [FINDINGS 30](FINDINGS.md) and [FINDINGS 38](FINDINGS.md).

None of it is detectable after the fact from the archive alone: a plausible timestamp is
indistinguishable from a correct one.

### What the code does about it

The mitigation is a **dependency**, not a retry — the logger does not start with a wrong clock
instead of starting and being wrong:

| Where | What |
|---|---|
| [`deploy/systemd/sigen-logger.service.template`](../deploy/systemd/sigen-logger.service.template) | `Wants=` / `After=systemd-time-wait-sync.service` |
| [`deploy/systemd/install-logger.sh`](../deploy/systemd/install-logger.sh) | enables that unit — ordering against a disabled unit orders against nothing |
| [`deploy/pi/bootstrap.sh`](../deploy/pi/bootstrap.sh) | enables `systemd-timesyncd` and the wait unit, and sets the timezone |
| [`bin/status.sh`](../bin/status.sh) | reports the clock first, **and** whether the gate is enabled |
| `tests/test_offline.py` | asserts the `After=` line is still in the template |

### What it does NOT do, so you know what you are relying on

- **There is no runtime guard in `log.py`.** The Python neither knows nor cares about the
  clock. Systemd ordering is the whole of the protection, so anything that bypasses systemd
  bypasses it — including running `python3 log.py --seconds 60` by hand, which the
  verification steps below tell you to do. Do that only once the clock is up.
- **Both places that enable the wait unit tolerate failure** (`|| echo WARNING`, `|| true`), so
  an install can succeed with the gate silently absent. That is why `bin/status.sh` reports
  `is-enabled` for it and not just `timedatectl`.
- **The gate is specific to `systemd-timesyncd`.** `systemd-time-wait-sync` waits on
  `/run/systemd/timesync/synchronized`, which only timesyncd creates. If you replace timesyncd
  with **chrony** or **ntpsec** — easy to do accidentally, since installing some NTP packages
  masks timesyncd — the `After=` names a unit that will not run and orders against nothing.
  Substitute `chrony-wait.service` in the template if you go that way.
- **A synchronised clock right now is not evidence the logger will wait at the next boot.**
  Those are two different facts and only the second protects the archive.

### The three checks that actually settle it

Run these on the new host, and run the third one *after a real reboot* — this is the only item
in this document that a reboot is the only test for.

```sh
timedatectl                                        # System clock synchronized: yes
                                                   # NTP service: active
systemctl is-enabled systemd-time-wait-sync        # enabled  (bin/status.sh reports this too)

sudo reboot
# then, once it is back:
bin/status.sh                                      # clock section first
python3 decode.py data/*.manifest.json data/*.bin --check    # NO backwards host-clock steps
```

If the third one reports backwards steps on an archive the logger wrote in one continuous run,
the gate did not hold. Stop, fix it, and segregate whatever was written — the affected files
cannot be told apart from correct ones later.

---

## 2. Before you touch anything

- **Match the plan.** Keep `fast_period_s` and the block plan identical to the old host's.
  They are what the plan hash is computed over, so an identical plan means the new host's
  files land in the same `raw/plan=<hash>/` prefix and the series is continuous across the
  cutover with no gap and no tile rebuild. Changing the cadence *and* the host at once turns
  one verifiable step into two entangled ones.
- **Give the new host its own credential.** A second access key on the same upload-only IAM
  user, not a copy of the old host's. Two hosts sharing one key cannot be revoked separately,
  and the point of the second key is that the old one can be deleted after the cutover without
  touching the new host. IAM allows two per user, which is exactly enough.
- **Match the timezone.** The archive's filenames, heartbeats and axis labels are all in the
  capture host's local clock, and the ingest Lambda runs with `TZ` set to `capture_tz`. A
  mismatch labels every axis in the wrong zone while the data itself stays correct — which
  reads as a rendering bug for a week.
- **Deploy any cloud-side code change first**, and verify the page *renders* rather than
  returns 200. A capture host that starts uploading into a stack that has not caught up is two
  problems at once.
- **Decide what happens to the old archive.** Copying it is optional; if you do, copy
  `.uploaded.json` with it or the new host re-uploads everything, which re-triggers ingest for
  every span and rewrites tiles that were already served.

---

## 3. The cutover, in order

**Only one client may poll the inverter at a time.** Concurrent-client behaviour is
unmeasured, so the old logger must stop before the new one starts. That is what makes this a
sequence rather than a checklist.

1. **Stop the old logger, just after an hourly rotation.** That leaves the least possible data
   in an un-rotated `.bin`.

2. **Rescue the final partial hour.** `sync.py` never uploads an open `.bin` — `log.py` gzips
   it on rotation and only the `.bin.gz` travels — so killing the logger mid-hour strands that
   hour on one disk.

   > **Before gzipping it, confirm no `.bin.gz` of the same stem already exists.** Gzipping a
   > stem that already has one produces two copies of one hour, which decode as a silent
   > double-count. This has happened once. Check with:
   >
   > ```sh
   > ls data/ | sed -E 's#\.bin\.gz$##; s#\.bin$##' | sort | uniq -d   # expect no output
   > ```

   Then run the uploader once by hand and confirm `sync.py --status` shows `0 pending` and no
   stranded block.

3. **Retire the old host's units**, and leave its archive in place as a cold copy. Do not
   upgrade the old host to the new code first: it deletes and renames things the old host's
   installed services were built from.

4. **Provision and start the new host.** Then reboot it, both because some host settings need
   one and because [§1](#1-the-clock-read-this-before-you-buy-the-hardware)'s only real test
   is a boot.

5. **Delete the old host's access key.**

6. **Give the new host a DHCP reservation**, and leave the inverter's alone.

### The cutover day's manifest

The new host writes a manifest for the current day with the same filename the old host wrote,
differing only in `started_at`. Its empty ledger uploads it, overwriting the old one in S3 and
re-triggering ingest for that day. Same plan and same device identity, so the result is
equivalent — and a versioned bucket keeps the original. Not a problem; just do not be
surprised by it.

---

## 4. Verifying the new host

In this order, because each one is dated by the one above it.

```sh
timedatectl && systemctl is-enabled systemd-time-wait-sync   # §1. First, always.

# The units start at all. Hardening is the likeliest thing to bite: systemd cannot
# bind-mount a ReadWritePaths entry that does not exist, and the error is about
# namespacing rather than about a missing directory.
systemctl show -p ActiveState,SubState,NRestarts <logger-unit>
sudo journalctl -u <logger-unit> -n 40

# A short capture, decoded, with the check §1 is about. Only once the clock is up, and
# only once nothing else is polling.
python3 log.py --seconds 60 --out /tmp/t
python3 decode.py /tmp/t/*.manifest.json /tmp/t/*.bin --last
python3 decode.py /tmp/t/*.manifest.json /tmp/t/*.bin --check

# TWO rotations, and the uploader running once per rotation with an EMPTY .err. A single
# successful upload proves the credential; it does not prove the schedule.
ls -l data/*.bin.gz
tail -6 logs/sync.log ; wc -c logs/sync.err
python3 sync.py --status
bin/status.sh

sudo reboot     # then bin/status.sh again. See §1.
```

Then the cloud end: the aggregate's `latest.json` `record_ts` advancing past the cutover, and
the page rendering. With no local viewer that is the only end-to-end "is it working" signal,
which is why `bin/status.sh` reports the clock, both units, the timer and free space.

**Re-measure latency.** Every number in [FINDINGS.md](FINDINGS.md) came from the old host. A
slower machine has more headroom against a 2000 ms tick than it looks like it needs, but
"more headroom" is an argument, not a measurement: run `decode.py --latency 10` over a day of
the new host's archive and compare.

---

## 5. Things that are not the clock but are also quiet

| Trap | Why it is quiet |
|---|---|
| `keep_days` on the new host | It deletes `*.bin.gz`, the exact form the uploader sends. An hour whose upload has not succeeded is deleted by ordinary housekeeping, and on a host whose archive is a spool there is no second copy. The retention window is an outage budget. [FINDINGS 40](FINDINGS.md). |
| A missing `.uploaded.json` | The new host re-uploads the whole archive. Idempotent in S3, but it re-triggers ingest for every span and rewrites tiles already served as immutable. |
| A different `fast_period_s` | Changes the plan hash, so the new files are a new series. Legal, but the page reads one plan at a time and the older side of a window will read as a gap. |
| `pip install --user` on Debian | Refused under PEP 668. The one dependency comes from `apt`. [FINDINGS 39](FINDINGS.md). |
| Enabling the uploader's *service* instead of its *timer* | It runs once at boot and never again, which looks like a working uploader for exactly one upload. |
| Wifi instead of Ethernet | Power save adds latency spikes to a p95 that is a measured property of this system, and it puts a passphrase in any image you later capture from the drive. |
