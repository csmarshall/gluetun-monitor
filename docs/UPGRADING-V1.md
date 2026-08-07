# Upgrading from v1 (v1 is end-of-life)

**v1 (the original bash implementation, image tag `:1`) is end-of-life.** v2 is
the supported line — please move to it. The upgrade is a **drop-in config
change**: v2 reads the **same env vars** (same names and defaults), the same
`/config/sites.conf` and `/logs` paths, and needs the **same socket-proxy
permissions** (`CONTAINERS` / `POST` / `EXEC`). In the common case you change
only the image tag and it keeps working — we validate that the v1 env surface
boots cleanly against a socket proxy as part of testing.

> **⚠️ v1 has a known defect — it does not actually recover.** A later review of
> the bash script found a `set -e` bug (#81): the backgrounded site-test subshell
> exits at the failing `wget` **before** writing its result, for any non-zero wget
> exit. The upshot is that on a **real** connectivity failure, v1's
> `restart_gluetun` **never fires** — it logged a DEBUG "test result missing" line
> and did nothing (and sites answering 4xx/5xx were silently miscounted). So `:1`
> isn't just unsupported; its core self-healing was broken. **The Python v2 port
> fixes this** (and is what these docs describe). The `:1` image is retained only
> as a rollback anchor / differential-test oracle — do not run it in production.

**Image tags** (full policy: [docs/VERSIONING.md](VERSIONING.md)):
- **`:2`** — **recommended for production**: you get every v2.x patch and never a
  surprise future major.
- `:2.0.0` — fully pinned to one release (reproducible; you update deliberately).
- `:latest` — always the newest **stable** release (excludes pre-releases, and an
  EOL v1 patch can't drag it back); **will** eventually roll to a future v3 major,
  so don't use it for unattended updates of a container-restarting watchdog.
- **`:1`** — frozen v1, kept only as a **rollback anchor**; unsupported (EOL).

**Two behavior changes to know about** (the config interface is compatible, but
v2 does more):
1. **It now heals dependents by default** (the #20 fix): a stranded dependent is
   restarted (same gluetun id) or **recreated** (id changed — volumes preserved;
   see [data safety](../README.md#what-it-will-and-wont-do-and-why-your-data-is-safe)). To
   stay close to v1's behavior, set `AUTO_RECREATE=0` (log a loud alert line
   instead of recreating) and/or `DEPENDENT_VIABILITY=0` (interface/strand check
   only, no L7 probing).
2. **Config is validated; bad config is now fatal.** v2 refuses to start on a
   few things v1 tolerated silently — an empty `sites.conf`, a malformed env
   value, or an explicit `DEPENDENT_CONTAINERS` naming a container that doesn't
   exist. If startup fails after the upgrade, the error message says exactly what
   to fix (see [Configuration is validated](CONFIGURATION.md#configuration-is-validated--sane-defaults-but-bad-config-is-fatal)).

**Rollback** is one step: repin the image to `:1`.
