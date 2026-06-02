# gluetun-monitor v2.0.0

**v2 is a ground-up reimplementation that makes the monitor *dependent-aware*: it
now detects and heals the containers behind gluetun, not just gluetun itself.**
It is a near drop-in upgrade from v1 — same env vars, same files, same
socket-proxy permissions (one note: it now runs as a non-root user, so make
`./logs` writable by it — see Upgrading) — and **v1 is now end-of-life**.

## Why v2

v1 tested connectivity only from inside gluetun, so it reported "healthy" while
dependent containers (`network_mode: service:gluetun`) could be **stranded
loopback-only** after gluetun restarted or was recreated — cut off from the
network while the monitor showed green (issue #20). v2 measures each dependent
directly and heals it.

## Highlights

- **Dependent-aware health (#20).** Every loop, each dependent is interface-checked
  (is it stranded to `lo`?) and given a quick DNS + connectivity probe from inside
  its own namespace. A healthy gluetun no longer masks a broken dependent.
- **Self-healing, non-destructively.** A stranded dependent is **restarted** when
  it still shares gluetun's current network, or **recreated** when gluetun was
  replaced (new container id). Recreate preserves all data — named, bind, **and
  anonymous** volumes are carried over; only the container's ephemeral writable
  layer is rebuilt from the image. On by default; `AUTO_RECREATE=0` to disable.
- **Reimplemented in Python** (docker-py) for testability — the connectivity test
  itself is unchanged (`wget --spider` inside gluetun's namespace, same pass/fail
  rules). 349 tests at 99% line+branch coverage, including a differential suite
  that checks behavior against the original bash.
- **Per-site stats + flaky-site advisory.** A best-effort JSON sidecar
  (`/logs/site-stats.json`) records each site's failure rate, episodes, restart
  effectiveness, response-latency percentiles (p50/p90/p99) and failure-reason
  breakdown, plus monitor-wide totals. When one site dominates recent gluetun
  restarts the monitor warns ("X of the last Y restarts were this site") and
  escalates to a human rather than auto-suppressing it.
- **Configuration is validated; bad config fails loud.** Empty `sites.conf`, a
  malformed or out-of-range env value (e.g. `TIMEOUT=0`), or an explicit
  `DEPENDENT_CONTAINERS` naming a missing container now refuse to start with a
  clear message instead of running degraded.
- **Hardened for release.** Runs as a non-root user; site entries can't be turned
  into `wget`/`ping` options; the site shuffle is thread-safe; a corrupt stats file
  can never crash startup. One standardized `TIMEOUT`/`WGET_TRIES` now reaches
  every probe, including the `wget` inside the dependents.
- **New controls:** `EXCLUDE_CONTAINERS` (never-manage denylist), `SITES`
  (test URLs via env, unioned with `sites.conf`), `DEPENDENT_VIABILITY`,
  `DEPENDENT_VIABILITY_SAMPLES`, `DEPENDENT_CONTAINER_FAILURES`,
  `MAX_PARALLEL_CHECKS`, `MAX_JITTER_MS`, `DRY_RUN`, `WGET_TRIES`,
  `DNS_WAIT_TIMEOUT`, log rotation (`LOG_MAX_BYTES`/`LOG_BACKUP_COUNT`), the
  flaky-site advisory knobs, and `LOG_LEVEL`.

## Upgrading from v1 (drop-in)

Change the image tag — that's it for the common case. v2 reads the same env vars
(same names + defaults), the same `/config/sites.conf` and `/logs`, and needs the
same socket-proxy permissions (`CONTAINERS` / `POST` / `EXEC`).

```diff
-    image: ghcr.io/csmarshall/gluetun-monitor:1
+    image: ghcr.io/csmarshall/gluetun-monitor:2
```

Three behavior changes to know about:
1. v2 **heals dependents by default**. To stay close to v1, set `AUTO_RECREATE=0`
   (alert instead of recreate) and/or `DEPENDENT_VIABILITY=0` (interface check
   only).
2. **Bad config is now fatal** (see above) — if it refuses to start after the
   upgrade, the log says exactly what to fix.
3. v2 **runs as a non-root user** (uid 10001). v1 ran as root, so an existing
   `./logs` dir is likely root-owned — make it writable by the new user
   (`sudo chown 10001:10001 ./logs`) to keep the file log + stats sidecar. If you
   skip this the monitor still runs and logs to `docker logs`; it just can't
   persist them. Using the direct socket mount instead of the proxy? Add your host
   `docker` group via `group_add` (see the compose example).

**Rollback** is one step: repin `:1`.

## Image tags

- **`:2`** — recommended for production (all v2.x patches, no surprise major).
- `:2.0.0` — fully pinned.
- `:latest` — newest; will eventually roll to a future major.
- `:1` — frozen v1, kept only as a rollback anchor (EOL, unsupported).

See [docs/VERSIONING.md](https://github.com/csmarshall/gluetun-monitor/blob/main/docs/VERSIONING.md)
for the full policy and [CHANGELOG](https://github.com/csmarshall/gluetun-monitor/blob/main/CHANGELOG.md)
for the complete list. Design rationale lives in [docs/](https://github.com/csmarshall/gluetun-monitor/tree/main/docs)
(tenets + ADRs).

**Closes #20.**
