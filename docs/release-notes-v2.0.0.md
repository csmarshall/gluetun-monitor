# gluetun-monitor v2.0.0

**v2 is a ground-up reimplementation that makes the monitor *dependent-aware*: it
now detects and heals the containers behind gluetun, not just gluetun itself.**
It is a drop-in upgrade from v1 — same env vars, same files, same socket-proxy
permissions — and **v1 is now end-of-life**.

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
  rules). 220+ tests, including a differential suite that checks behavior against
  the original bash.
- **Configuration is validated; bad config fails loud.** Empty `sites.conf`, a
  malformed env value, or an explicit `DEPENDENT_CONTAINERS` naming a missing
  container now refuse to start with a clear message instead of running degraded.
- **New controls:** `EXCLUDE_CONTAINERS` (never-manage denylist), `SITES`
  (test URLs via env, unioned with `sites.conf`), `DEPENDENT_VIABILITY`,
  `DEPENDENT_CONTAINER_FAILURES`, `MAX_PARALLEL_CHECKS`, `MAX_JITTER_MS`,
  `DNS_WAIT_TIMEOUT`, `LOG_LEVEL`.

## Upgrading from v1 (drop-in)

Change the image tag — that's it for the common case. v2 reads the same env vars
(same names + defaults), the same `/config/sites.conf` and `/logs`, and needs the
same socket-proxy permissions (`CONTAINERS` / `POST` / `EXEC`).

```diff
-    image: ghcr.io/csmarshall/gluetun-monitor:1
+    image: ghcr.io/csmarshall/gluetun-monitor:2
```

Two behavior changes to know about:
1. v2 **heals dependents by default**. To stay close to v1, set `AUTO_RECREATE=0`
   (alert instead of recreate) and/or `DEPENDENT_VIABILITY=0` (interface check
   only).
2. **Bad config is now fatal** (see above) — if it refuses to start after the
   upgrade, the log says exactly what to fix.

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
