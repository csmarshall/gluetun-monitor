# Site stats & notifications

What the monitor records about each site, the advisories it raises from
that record, and how to get those events out of the log.

## Site stats & flaky-site advisory

A single flaky **test site** (one that intermittently times out or SSL-errors)
can trip `FAIL_THRESHOLD` and trigger a gluetun restart — even though the tunnel
is fine (your other sites pass). Restarting *can* fix a genuinely blocked
endpoint, so the monitor still tries it; but to stop you chasing the wrong thing,
it keeps a **persistent, rear-looking record** of how each site behaves and
**tells you** which one is the troublemaker.

It writes a human-readable JSON sidecar (`STATS_FILE`, default
`/logs/site-stats.json`) with, per site: total polls, total failures (→ failure
rate), failure **episodes** and the average episode length in polls (how long it
typically stays down when it breaks), the **longest** such streak, a
**failure-reason breakdown** (dns / tls / timeout / connection / http-error /
other), **response-latency** of successful polls (avg/min/max + **p50/p90/p99**, so
you see median vs mean — a site getting slow often precedes it failing), how many
gluetun restarts it triggered and the **restart-effectiveness** (fraction of those
restarts that actually cleared it — a low number means it's the site, not the VPN),
and first-seen / last-good / last-failure timestamps. It's written **every loop, crash- and power-loss-safely** (temp file
+ fsync + atomic rename), survives monitor restarts, and is best-effort (a
missing/unwritable/corrupt file never blocks the monitor). A site removed from
`sites.conf` is kept for `STATS_RETENTION_DAYS` (default 90) then pruned.

The file also has a top-level **`monitor`** section with monitor-wide totals:
version, first-/last-started, current uptime, **total loops**, **total runtime**
(accumulated, excluding downtime), and cumulative **gluetun restarts**,
**dependent remediations**, and **advisories** raised.

When one site dominates the recent restarts, the monitor logs a **flaky-site
advisory** (once, not per loop):

```
[WARN] FLAKY SITE: https://dognzb.cr caused 17 of the last 22 gluetun restarts
over the last 24h — it may be flaky; consider reviewing or removing it from sites.conf
```

That's the signal to prune that site from `sites.conf` (re-read each loop — see the
[live-editing caveat](CONFIGURATION.md#sites--config_file--sites) on in-place edits vs. a recreate).
Tune with `ADVISORY_WINDOW`, `ADVISORY_MIN_RESTARTS`, and
`ADVISORY_DOMINANCE`. See [ADR-0008](adr/0008-persistent-site-stats-and-advisory.md).

The **dependent-flapping advisory** is the same idea aimed at a *dependent* rather
than a site: a container that keeps needing remediation but won't stay healthy
self-heals every loop (a quiet `recovery` event) and would otherwise never reach a
human. When one is remediated `DEPENDENT_ADVISORY_MIN_REMEDIATIONS` times within
`DEPENDENT_ADVISORY_WINDOW`, it escalates to an `attention` alert:

```
[WARN] FLAPPING DEPENDENT: qbittorrent remediated 6 times in the last 24h
— it won't stay healthy; investigate
```

It's **count-based, not dominance-based** (each dependent is independent — there's
no shared gluetun to contend for), and the per-loop DEBUG logs already show *which*
sites/DNS failed each time, so the alert just points you at the right container to
investigate.

> **By design, the monitor does not auto-suppress a flaky site** — it keeps
> applying the cheap restart fix and escalates to you. (A future automatic
> back-off is possible; it would be opt-in.)

### Viewing the stats: `gluetun-monitor-stats`

The image ships a read-only command that renders the sidecar as a per-site matrix
(it reads the same file the monitor writes, using the same code, so the numbers
always match). It touches no Docker API and never mutates state — safe to run any
time:

```console
$ docker exec gluetun-monitor gluetun-monitor-stats
monitor v2.0.0  loops=205  runtime=2.0h  gluetun_restarts=0  remediations=0  advisories=0
tracking since 2026-06-02 14:24

site                     polls  fails  rate%   avg   p50   p90   p99   max  eff%  last_fail
-----------------------  -----  -----  -----  ----  ----  ----  ----  ----  ----  ---------
https://thepiratebay.org   319      0   0.00  2255  2191  2527  2679  3136   n/a  —
https://www.google.com     319      0   0.00   716   631   911  1238  1274   n/a  —
...
latency in ms; eff% = restart-effectiveness (n/a = no restarts triggered)
```

Sites are sorted by `p90` (worst tail first) by default; `--sort` accepts
`p90|p99|avg|max|p50|rate|polls|eff|name`. Add `--json` to emit the same data for
`jq`/dashboards, and `--file PATH` if your `STATS_FILE` lives elsewhere.

The latency columns show the **recent** window (last ~200 polls) by default. Add
`--lifetime` for **all-time** percentiles — these come from a bounded per-site
histogram (DDSketch-style, within 5% relative error at a few dozen buckets/site;
see [ADR-0009](adr/0009-all-time-latency-histogram.md)) that records every
successful poll for the site's whole life, so you get a lifetime baseline rather
than just "recently." `--json` includes both windows (`latency_ms` and
`lifetime_latency_ms`). Exact avg/min/max are kept either way; only the percentiles
are approximate.

## Notifications

By default the monitor is **log-only**. Set `APPRISE_URLS` to also push events
out-of-band via [Apprise](https://github.com/caronc/apprise) — one library, 100+
backends (ntfy, Discord, Telegram, Slack, email, Pushover, Gotify, generic webhook,
…), all configured by URL. Unset = disabled, so this is fully opt-in.

```yaml
    environment:
      # One or more comma-separated Apprise URLs:
      - APPRISE_URLS=ntfy://ntfy.example.com/gluetun
      # - APPRISE_URLS=ntfy://host/topic,discord://webhook_id/webhook_token
      - NOTIFY_LEVEL=attention   # attention | recovery | activity | all (default attention)
```

### One dial: `NOTIFY_LEVEL`

The scope is a single cumulative dial keyed on **who has to act**, not on how scary a
line looks. Each level adds its own row to everything above it (ADR-0011):

| `NOTIFY_LEVEL` | You get | Events |
|---|---|---|
| **`attention`** *(default)* | only when **you** must act/decide | recovery/remediation failed, refused to start, **cannot probe the gateway**, **cannot probe a dependent**, **flaky-site advisory**, **dependent-flapping advisory** |
| `recovery` | + self-healed incidents | gluetun recovered, dependent remediated |
| `activity` | + non-fault changes | `sites.conf` reloaded, **advisory site unreachable / recovered** |
| `all` | + the firehose | per-loop checks, restart play-by-play |

So enabling notifications gets you **`attention` only** — the monitor stays silent
through every self-heal and pings you when it's actually stuck. Raise the dial to
hear more.

### No notification storms

Alerts are **edge-triggered**: an ongoing problem is announced **once, when it
starts** — not every 30-second loop it persists. `NOTIFY_REPEAT_INTERVAL` (in
**loops**, default `0`) controls reminders: `0` = announce once then stay silent
until it resolves; `N` = remind every `N` loops. When a problem **clears** you get a
resolve note (so you hear it's back); when its subject is **removed** (site dropped,
dependent excluded) you get a "no longer monitored" note instead (the alert is
retired, not recovered). This state persists to `NOTIFY_STATE_FILE`,
so restarting the monitor neither re-spams still-broken problems nor misses a
resolve (ADR-0012).

A resolve means the condition was **observed** to clear, not that a counter
momentarily dipped. In particular the "gluetun cannot recover" alert stays active
until the sites that triggered it actually pass again — a site that is unreachable
through an otherwise-healthy tunnel (e.g. geo-blocked from the current exit) keeps
the alert firing once, without a false "recovered" every restart cycle.

### Wedged dependents — escalation with the runbook attached

Most remediation failures are transient and the next loop's retry clears them. But
some states **cannot** self-heal — the canonical one is an unremovable parked twin
left by an interrupted recreate (a storage-driver `dataset is busy` refusal: the
force-killed container's process tree survived and pinned the mount). Retrying is
free but futile, and the failure looks identical every loop.

When the **same** remediation failure repeats `WEDGE_ESCALATE_AFTER` consecutive
times (default 3), the monitor declares the dependent **wedged**:

- the generic `dependent unhealthy` alert is superseded by a distinct
  **`dependent WEDGED`** alert carrying the exact error, the parked twin's inspect
  state, and the **operator runbook** — the alert alone is enough to act on;
- remediation attempts **back off** (doubling per failed attempt, capped at
  `WEDGE_BACKOFF_CAP` seconds, default 10 min) instead of hammering the daemon with
  a doomed removal every loop;
- **probing never stops** — the dependent is still checked every loop, so once the
  blocker is cleared (or the container recovers on its own) the monitor finishes
  the heal itself and resolves the alert. A failure that *changes* restarts the
  count: a new error is a new situation, not a deeper wedge.

One deployment note: **a wedged alert is only as good as the `APPRISE_URLS` behind
it**. In log-only mode (the default) every attention-tier alert — this one included
— is just a log line, and "escalation" means hoping someone tails the log. If you
rely on the monitor to summon you when it's stuck, configure notifications.

### Grouped, best-effort

A loop's surviving events are **rolled up into one digest** (so one cycle = at most
one notification), colored by the most-urgent tier present. Sending is best-effort
and **never affects monitoring** (Tenet 7): run off the loop bounded by
`NOTIFY_TIMEOUT`, any failure swallowed (logged at `DEBUG`). Apprise URLs carry
tokens and are **never logged** — and at startup the log states exactly what you
signed up for, including what stays silent.

Verify your setup without waiting for a real event:

```bash
docker exec gluetun-monitor gluetun-monitor --notify-test
```

### Self-hosted backend with a self-signed certificate?

A common homelab gotcha: Apprise **verifies TLS certificates by default**, so a
self-hosted backend (mail server, ntfy, Gotify, …) presenting a **self-signed or
private-CA** cert fails with a vague *"Connection error"* — even when the URL is
correct. The image trusts only the public CA bundle, so it can't trust a private cert.

Two fixes — append **`?verify=no`** to the URL to skip verification (simplest, fine for
a homelab box):

```
mailtos://user:pass@mail.lan?verify=no
ntfy://ntfy.lan/gluetun?verify=no
```

…or, to keep verification on, mount your CA so the container trusts it:

```yaml
volumes:
  - ./my-ca.crt:/usr/local/share/ca-certificates/my-ca.crt:ro
```

See [ADR-0011](adr/0011-notification-tiers-and-rollup.md) (the dial + rollup) and
[ADR-0012](adr/0012-alert-lifecycle.md) (the lifecycle) for the design, and the
full [Apprise URL list](https://github.com/caronc/apprise/wiki) for backends.
