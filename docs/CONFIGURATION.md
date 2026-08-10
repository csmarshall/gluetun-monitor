# Configuration

Every environment variable, the per-URL tunable syntax, and how the
timeout model fits together. For a runnable starting point see
[COMPOSE-EXAMPLES.md](COMPOSE-EXAMPLES.md).

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PUID` / `PGID` | *(unset → root)* | Run non-root (recommended): the entrypoint chowns `/logs` and drops to this uid/gid (LSIO-style). Unset = runs as root (drop-in). See [Running as non-root](#running-as-non-root-recommended) |
| [`DOCKER_HOST`](#docker_host) | *(unset)* | Docker daemon endpoint. Set to `tcp://docker-socket-proxy:2375` when using a socket proxy |
| [`GLUETUN_CONTAINER`](#gluetun_container) | `gluetun` | Name of the Gluetun container to monitor (must exist, else fatal) |
| [`CONFIG_FILE`](#sites--config_file--sites) | `/config/sites.conf` | Path to the sites file (re-read each loop → live-editable) |
| [`SITES`](#sites--config_file--sites) | *(unset)* | Comma-separated test URLs, **unioned** with the sites file. Set at startup (not live-reloaded) |
| [`DEPENDENT_CONTAINERS`](#dependent_containers) | `auto` | `auto` to discover dynamically, or comma-separated list (every named container must exist, else fatal) |
| [`EXCLUDE_CONTAINERS`](#exclude_containers) | *(unset)* | Comma-separated container names to **never** manage (denylist). Filters auto-discovery and subtracts from an explicit list; exclude wins on overlap |
| [`CHECK_INTERVAL`](#check_interval) | `30` | Seconds between health checks |
| [`TIMEOUT`](#timeout) | `10` | Per-request network timeout, applied identically to every probe — `wget --timeout` (gluetun site tests **and** the dependent-container probes) and `ping -W`. Overridable per URL — see [Per-URL tunables](#per-url-tunables) |
| `WGET_TRIES` | `1` | Attempts per `wget` probe (the shuffle + consecutive-failure thresholds, not retries, are how noise is tolerated). Overridable per URL — see [Per-URL tunables](#per-url-tunables) |
| [`FAIL_THRESHOLD`](#fail_threshold) | `2` | Consecutive site failures before restarting Gluetun |
| [`HEALTHY_WAIT_TIMEOUT`](#healthy_wait_timeout) | `120` | Max seconds to wait for Gluetun to become healthy after restart |
| `DEPENDENT_CONTAINER_FAILURES` | *(= `FAIL_THRESHOLD`)* | Consecutive per-dependent viability failures before remediating that dependent |
| `MAX_PARALLEL_CHECKS` | `6` | Cap on concurrent `docker exec` probes across dependents per loop |
| `DEPENDENT_VIABILITY` | `1` | Per-dependent L7 DNS/connectivity probe. `0` = interface/strand check only (no URL fetch); the interface check is always on |
| `DEPENDENT_VIABILITY_SAMPLES` | `1` | Sites each dependent tests per loop: `1` (one shuffled), `N` (N distinct), or `-1` (all). >1 is largely redundant (dependents share gluetun's netns), at `N` execs/dependent/loop |
| `MAX_JITTER_MS` | `0` | Optional per-dispatch jitter (ms) to spread the dependent probe burst. `0` = off (the concurrency cap already bounds it) |
| `DRY_RUN` | `0` | Observe-only: run all detection/probing but **take no action** — log `[DRY-RUN] would …` instead of restarting/recreating. For soak-testing alongside an active monitor |
| `STATS_FILE` | `/logs/site-stats.json` | Where persistent per-site stats are written (best-effort, atomic; survives restarts). See [Site stats & flaky-site advisory](NOTIFICATIONS.md#site-stats--flaky-site-advisory) |
| `STATS_RETENTION_DAYS` | `90` | Drop a site's stats if it hasn't been tested (e.g. removed from `sites.conf`) for this many days; `0` keeps them forever |
| `MONITOR_STATE_FILE` | `/logs/monitor-state.json` | Durable dependent memory: gluetun's container-id history + known dependent names, so dependents stranded by a gluetun recreate stay visible (and healable) across monitor restarts. Best-effort, atomic |
| `ADVISORY_WINDOW` | `86400` | Window (seconds) for the flaky-site advisory — and the recency window for `--suggest-tunables` (a site with no failure inside it yields no advice) |
| `ADVISORY_MIN_RESTARTS` | `5` | Minimum gluetun restarts in the window before an advisory can fire |
| `ADVISORY_DOMINANCE` | `0.5` | Fraction of those restarts one site must cause to be flagged flaky |
| `DEPENDENT_ADVISORY_WINDOW` | `86400` | Window (seconds) for the dependent-flapping advisory |
| `DEPENDENT_ADVISORY_MIN_REMEDIATIONS` | `5` | Remediations of one dependent in the window before it's flagged as flapping |
| `AUTO_RECREATE` | `1` | Recreate a dependent stranded by a Gluetun recreate (id changed). Set `0` to disable → such a dependent is reported FAILED instead |
| `DNS_WAIT_TIMEOUT` | `30` | Max seconds to wait for Gluetun DNS to stabilize after a restart |
| `LOG_LEVEL` | `INFO` | `DEBUG` to include per-site/per-dependent detail lines |
| `LOG_MAX_BYTES` | `10485760` | Rotate the `/logs` file at this size (≈10 MB); `0` disables rotation |
| `LOG_BACKUP_COUNT` | `5` | How many rotated log backups to keep (≈60 MB total at defaults) |
| `LOG_FILE` | `/logs/gluetun-monitor.log` | Path to the log file inside the `/logs` mount. Logs always go to stdout (`docker logs`) too; if this path isn't writable the monitor degrades to stdout-only rather than failing |
| `TZ` | `UTC` | Timezone for log timestamps |
| `APPRISE_URLS` | *(unset → off)* | Comma-separated [Apprise](https://github.com/caronc/apprise) URLs to push events to (ntfy/Discord/Telegram/email/webhook/…). Unset = notifications disabled. See [Notifications](NOTIFICATIONS.md) |
| `NOTIFY_LEVEL` | `attention` | Cumulative scope dial: `attention` (only when you must act) → `recovery` (self-healed incidents) → `activity` (non-fault changes) → `all` (firehose). See [Notifications](NOTIFICATIONS.md) |
| `NOTIFY_REPEAT_INTERVAL` | `0` | Re-notify cadence for an *ongoing* problem, in **loops**. `0` = announce once, then silent until it resolves; `N` = remind every `N` loops. Alerts are edge-triggered. See [Notifications](NOTIFICATIONS.md) |
| `NOTIFY_STATE_FILE` | `/logs/notify-state.json` | Where the active-alert lifecycle persists (so a monitor restart doesn't re-spam or miss a resolve). Best-effort. See [Notifications](NOTIFICATIONS.md) |
| `NOTIFY_TIMEOUT` | `10` | Max seconds to wait for a notification send before carrying on (sends run off the loop, so a slow backend can't stall the watchdog). See [Notifications](NOTIFICATIONS.md) |
| `WEDGE_ESCALATE_AFTER` | `3` | Consecutive **identical** remediation failures on one dependent before it's declared wedged: a distinct `dependent WEDGED` alert (with the operator runbook when the blocker is an unremovable parked twin) replaces the generic one, and remediation attempts back off. See [Notifications](NOTIFICATIONS.md) |
| `WEDGE_BACKOFF_CAP` | `600` | Ceiling (seconds) for the doubling remediation-retry backoff once wedged. Probes still run every loop — only the doomed remediation attempt is throttled. `0` = no backoff (retry every loop) but still escalate the alert |

## Configuration is validated — sane defaults, but bad config is fatal

The design goal is **good results with zero extra config**: with a container named
`gluetun` and a site to test, everything else has a sane default. But to protect
the wider system, the monitor **refuses to start** (exits non-zero) rather than
*guess* around misconfiguration:

- **Malformed env value** (a non-integer `CHECK_INTERVAL`, an unrecognized
  `AUTO_RECREATE`, an invalid `LOG_LEVEL`, …) → fatal. An *unset* variable is just
  its default; a *set-but-unparseable* one is an error we won't paper over.
- **No testable sites** (no `sites.conf` *and* no `SITES`, or both empty) → fatal.
  A monitor that tests nothing would report fake-green forever.
- **`GLUETUN_CONTAINER` doesn't exist** → fatal (nothing to monitor).
- **Explicit `DEPENDENT_CONTAINERS` names a container that doesn't exist** → fatal.
  You told us exactly what to manage; we won't silently drop or guess around a
  name we can't find (it could mean acting on the wrong container). `auto` finding
  zero is fine — that's gluetun-only monitoring, a valid setup.

## Variable Details

### Sites — `CONFIG_FILE` + `SITES`
Test URLs come from two **unioned** sources (de-duplicated):
- **`CONFIG_FILE`** (default `/config/sites.conf`) — one URL per line, `#` comments
  allowed. **Re-read every loop**, so adding/removing a URL takes effect on the
  next check — the change is even logged by name (`Sites changed: added …`).
- **`SITES`** — a comma-separated list in the environment, for config-via-env
  parity with the other knobs (no file mount needed). Fixed at process start —
  changing it requires a container restart.

Provide either, or both. At least one URL total is required or the monitor won't
start (see above).

> **"Re-read every loop" needs a directory mount.** The compose file mounts
> `./config:/config:ro` (a directory) rather than the single `sites.conf` file so
> that a live edit reloads with **any** editor. A single-file bind mount pins the
> file's inode and misses atomic-save edits — see
> [Editing `sites.conf` live](#editing-sitesconf-live) for the full explanation.

Entries are sanity-checked: a URL with no host, or one that looks like a
command-line flag (leading `-`, which could otherwise be parsed as an option by
the in-container `wget`/`ping`), is **dropped with a startup warning** rather than
probed. The probes also pass URLs/hosts after a `--` end-of-options separator, so
a stray value can never be interpreted as a flag.

An entry may also carry a `|key=value` suffix (e.g. `https://slow.example|timeout=25`
or `https://geo-blocked.example|role=advisory`) to override the probe timeout/retries
or the site's **role** for **that URL only** — see
[Per-URL tunables](#per-url-tunables). A bare URL behaves exactly as before.

### `DOCKER_HOST`
When unset, the Docker CLI connects via the local socket (`/var/run/docker.sock`). Set this to `tcp://<proxy-host>:2375` to connect through a [Docker socket proxy](ARCHITECTURE.md#docker-socket-proxy) instead of mounting the socket directly. See the [Docker Socket Proxy](ARCHITECTURE.md#docker-socket-proxy) section for setup details.

### `GLUETUN_CONTAINER`
The name of your Gluetun container as shown in `docker ps`. This is the container that will be:
- Used to execute site connectivity tests (via `docker exec`)
- Monitored for health status
- Restarted when connectivity fails
- Used to extract VPN endpoint information from logs

### `DEPENDENT_CONTAINERS`
Controls which dependents are watched and healed:
- `auto` - Automatically discovers containers using `network_mode: "container:<GLUETUN_CONTAINER>"` (queries the Docker API for each running container's `NetworkMode`). Discovering zero is fine — gluetun-only monitoring.
- `container1,container2` - Comma-separated list of container names. **Every name must exist at startup or the monitor exits** — an explicit list is a contract, and we won't guess around a missing name. If your dependents start alongside the monitor, order startup (`depends_on:`) so they exist first, or use `auto`.

### `EXCLUDE_CONTAINERS`
A comma-separated **denylist** of containers the monitor must **never** manage —
never interface-checked, viability-tested, restarted, or recreated. Most useful
with `auto` ("discover everything *except* these") so you keep auto-discovery of
new dependents while protecting specific ones. It also subtracts from an explicit
`DEPENDENT_CONTAINERS` list.

If a name appears in **both** `DEPENDENT_CONTAINERS` and `EXCLUDE_CONTAINERS`,
**exclude wins** and the monitor warns — the contradiction resolves toward *not*
touching the container ("first, do no harm") rather than failing. An exclude name
that matches no container warns too (likely a typo — the container you meant to
protect would otherwise still be managed).

### `CHECK_INTERVAL`
Time in seconds between health check cycles.

**Note:** sites are tested concurrently, bounded by `MAX_PARALLEL_CHECKS` (default 6). With ≤6 sites a cycle's tests finish within one `TIMEOUT`; with more, they run in batches, so a cycle can take up to `ceil(sites / MAX_PARALLEL_CHECKS) × TIMEOUT`.

### `TIMEOUT`
Maximum seconds to wait for each site to respond. Tests run concurrently (up to `MAX_PARALLEL_CHECKS` at a time), so this bounds each batch rather than each individual site.

Uses `wget --spider` which only fetches headers (no response body downloaded).

## Timeouts & retries — one model, everywhere

**Every timeout the monitor exposes, at a glance:**

| Knob | Default | Scope | Bounds | Per-URL? |
|---|---|---|---|---|
| `TIMEOUT` | `10` | per-request | each `wget --timeout` / `ping -W` (gluetun site tests **and** dependent probes) | ✅ [yes](#per-url-tunables) |
| `WGET_TRIES` | `1` | per-request | attempts per `wget` probe | ✅ [yes](#per-url-tunables) |
| `CHECK_INTERVAL` | `30` | loop cadence | sleep between check cycles | — |
| `HEALTHY_WAIT_TIMEOUT` | `120` | post-restart budget | wait for gluetun's healthcheck after a restart | — |
| `DNS_WAIT_TIMEOUT` | `30` | post-restart budget | poll for DNS to stabilize after a restart | — |
| `NOTIFY_TIMEOUT` | `10` | per-send | max wait for one (off-thread) notification send | — |
| `NOTIFY_REPEAT_INTERVAL` | `0` | alert cadence | reminder cadence for an ongoing problem (`0` = announce once) | — |
| `WEDGE_BACKOFF_CAP` | `600` | retry backoff | ceiling for the doubling remediation-retry delay on a wedged dependent (`0` = no backoff) | — |

(The Docker API client timeout isn't a separate knob — it's derived as
`max(TIMEOUT × 2, 60)`.)

**Only the two per-request knobs (`TIMEOUT`, `WGET_TRIES`) are per-URL-overridable**
([Per-URL tunables](#per-url-tunables)) — because they're the only ones that act on
*a specific URL's* probe. The rest are loop cadence (`CHECK_INTERVAL`), notification
plumbing, or **gluetun-restart-scoped wait budgets** (`HEALTHY_WAIT_TIMEOUT`,
`DNS_WAIT_TIMEOUT`) that wait on the *gluetun container* recovering, not on probing
any one site — so a per-URL value would be meaningless. See below for that split.

There is **one per-request timeout knob, `TIMEOUT`**, and it is applied identically
to every network probe the monitor makes:

| where | command | uses |
|---|---|---|
| gluetun site tests (through the tunnel) | `wget --spider --timeout=$TIMEOUT --tries=$WGET_TRIES` | `TIMEOUT`, `WGET_TRIES` |
| dependent viability (inside each dependent) | the same `wget …` | `TIMEOUT`, `WGET_TRIES` |
| DNS fallback (`ping`) | `ping -W $TIMEOUT` | `TIMEOUT` |
| post-restart DNS-readiness probe | the same `wget …` | `TIMEOUT`, `WGET_TRIES` |

So setting `TIMEOUT=10` **does** flow straight down to the `wget` run inside the
dependent containers (busybox or GNU — both honor `--timeout`). `getent`/`nslookup`
have no timeout flag and fall back to the container's resolver config (the cascade
moves on if a tool is slow/absent).

Don't confuse the **per-request** `TIMEOUT` with the **overall wait budgets** used
only after a gluetun restart: `HEALTHY_WAIT_TIMEOUT` (wait for gluetun's
healthcheck) and `DNS_WAIT_TIMEOUT` (poll for DNS to stabilize). Those are loops;
`TIMEOUT` is each individual request inside them.

**How the monitor "hunts" for a good result — and why `WGET_TRIES` defaults to 1.**
Reliability comes from *breadth and repetition over time*, not from retrying a
single request: gluetun tests the **whole site set** each loop and only acts after
`FAIL_THRESHOLD` **consecutive** loops fail; each dependent tests **one shuffled
site per loop** (so over loops it covers many names) and only acts after
`DEPENDENT_CONTAINER_FAILURES` consecutive failures. That shuffle-and-threshold
design already absorbs a flaky site, so a single fast attempt (`WGET_TRIES=1`) is
the right default — raise it only if your links are lossy enough that one in-loop
retry meaningfully helps.

## Per-URL tunables

`TIMEOUT`/`WGET_TRIES` set the **defaults**, and they're right for almost every
site. Occasionally one canary is *slow but alive* — it answers, just not within
the global ceiling. (`wget --timeout` bounds each connect/read/DNS step
**individually**, not the whole request, so a server that opens the connection and
then takes its time sending headers trips the timeout even though it's up — you'll
see `Read error (Operation timed out) in headers`.) Timing it out triggers a
gluetun restart that can't fix anything external.

For exactly that case you can override the probe knobs **per URL**, in either
source, with a `|key=value` suffix after the URL:

```bash
# sites.conf — bare URLs are unchanged; add |key=value only where you need it
https://www.google.com
https://slow-api.example|timeout=25
https://flaky.example|timeout=20|tries=2
```

```yaml
# SITES env — comma between entries, pipe within an entry
- SITES=https://www.google.com,https://slow-api.example|timeout=25
```

- **The separator is `|`** so one syntax works in both the file *and* the
  comma-separated `SITES` env: a URL never contains a bare `|`, and it survives the
  CSV split.
- **Keys:** `timeout` (seconds) and `tries` (attempts) — the per-URL equivalents of
  `TIMEOUT`/`WGET_TRIES`. A site with no override inherits the globals, so nothing
  changes for the rest.
- **Forgiving:** an unknown key or non-positive value is **warned about and skipped**
  — the URL is still monitored on the defaults; a typo never drops a site. The
  warning fires **the same way at startup and on a live reload** (deduped on reload),
  so a bad edit made while running is never applied silently.
- File overrides are **re-read live** like the URLs themselves, and the reload
  detects a change to a site's **full config** — not just added/removed URLs but a
  changed `role`/`timeout`/`tries` on an existing one. Any edit is logged with the
  specific before→after (`Sites changed: https://x (role critical→advisory)`), and
  the resolved config is re-logged (`Per-URL probe overrides: …`), so you always get
  confirmation the edit took effect.

### Editing `sites.conf` live

The compose file mounts a **directory** (`./config:/config:ro`) rather than the
single `sites.conf` file **on purpose**, and this is important for live reload:

> A single-file bind mount pins the file's **inode** at container start. Most
> editors (`vim` by default, `sed -i`, many IDEs) save by writing a temp file and
> renaming it over the original — which creates a **new inode**. With a single-file
> mount the container keeps reading the *old* inode, so your edit is invisible until
> you recreate the container — the live reload silently does nothing. A **directory**
> mount resolves `sites.conf` fresh on every read, so any editor's save is picked up
> live.

If you must use a single-file mount, edit **in place** (append, or truncate-and-
write) — or run `docker compose up -d --force-recreate` after an atomic-save edit.

> **Why only `timeout`/`tries`?** These are the only **per-request** knobs — they
> bound *this URL's* probe, so a per-URL value is meaningful. The other timeouts
> are not per-URL-overridable by design: `CHECK_INTERVAL` is the loop cadence, and
> `HEALTHY_WAIT_TIMEOUT`/`DNS_WAIT_TIMEOUT` are **gluetun-restart-scoped wait
> budgets** — they wait for the *gluetun container* to recover, which has nothing to
> do with any single site, so there's nothing to scope to a URL. See
> [Timeouts & retries](#timeouts--retries--one-model-everywhere) for the full split.

### Site roles — `critical` (default) vs `advisory`

`timeout`/`tries` tune *how* a site is probed; `role` decides *what its failure
means*. It's the same `|key=value` syntax:

```bash
https://www.google.com                     # critical (default)
https://geo-blocked.example|role=advisory  # probed, but never restarts gluetun
https://slow-and-flaky.example|role=advisory|timeout=25
```

- **`critical`** — the default (a bare URL). Its failure counts toward restarting
  gluetun, exactly as before — so existing configs are unchanged.
- **`advisory`** — the site is still probed and its reachability recorded in the
  stats, but its failure **never** triggers a gluetun restart, and it's excluded
  from the flaky-site advisory (you've already acknowledged it). Startup logs it
  under `Per-URL probe overrides: … role=advisory`, and a failing advisory site
  shows in the heartbeat as `failing: host (advisory)`. If you want to *hear* when
  an advisory site goes unreachable, it emits an opt-in, edge-triggered alert at the
  **`activity`** notification tier (silent at the default `attention`) — announced
  once it's been unreachable `FAIL_THRESHOLD` checks, resolved when it's back. So
  you can watch a geo-blocked endpoint's reachability without it ever restarting the
  tunnel or paging you.

Use `advisory` for a site you want to **watch** but that can't be reached through
the VPN regardless of tunnel health — a geo-blocked/anti-VPN endpoint (a torrent
indexer, say). As a `critical` site it would restart gluetun every loop trying to
roll to an exit that can reach it, churning every dependent, when the tunnel is
fine. As `advisory` you keep the reachability signal without the pointless restarts.

**advisory vs. deleting:** advisory *keeps probing* — the reachability data is the
value, for a site whose status actually varies. If a site is permanently
unreachable or you stop caring, just **delete the line** — probing it forever
teaches nothing. Deleting isn't a role; it's what you do to an advisory site you've
finished with. An unknown `role=` value is warned about at startup and falls back
to `critical` (fail-closed — an unrecognized site still protects the tunnel).

Unlike `timeout`/`tries` — which take their **global** defaults from `TIMEOUT`/`WGET_TRIES`
and are overridable per URL — `role` is **per-site only**: there's no global
default-role setting, because "everything advisory" would leave nothing gating the
tunnel. The startup/reload defaults line spells out the effective globals in force,
role included: `… all sites use TIMEOUT=10s, WGET_TRIES=1, role=critical`.

### Let the monitor suggest them — `--suggest-tunables`

You don't have to guess. The monitor already records how every site behaves
(latency percentiles, failure categories, restart effectiveness — see
[Site stats & flaky-site advisory](NOTIFICATIONS.md#site-stats--flaky-site-advisory))
and can read that history back as concrete recommendations.

Those stats are *lifetime* counters, which is what makes the advice well-evidenced — and what would otherwise make it immortal. So suggestions are gated on **recent evidence**: a site with no failure inside `ADVISORY_WINDOW` (24h) yields nothing, however damning its lifetime totals. Advice about an episode from last month is noise, not diagnosis.

```bash
docker exec gluetun-monitor gluetun-monitor --suggest-tunables
# (or, without a running container)
docker compose run --rm gluetun-monitor --suggest-tunables
```

It prints a ranked, evidence-backed list — the biggest restart driver first — with
the exact line to paste:

```
slow-api.example
  18 read-timeout failure(s) but answers within p99=2579ms / max=13544ms — slow,
  not dead; a 25s timeout keeps it a useful canary instead of triggering restarts
  → https://slow-api.example|timeout=25
```

When a longer timeout *wouldn't* help — a site whose restarts rarely clear it and
whose failures are DNS/connection (genuinely unreachable, not merely slow) — it
says so and points you at reviewing/removing it instead (or, if you want to keep
watching its reachability without it restarting the tunnel, marking it
[`role=advisory`](#site-roles--critical-default-vs-advisory)). The suggestions are
**advisory only**: the monitor never edits your config. The flaky-site advisory in
the logs and notifications carries the same suggestion inline when one applies.

## Site Test Success/Failure Logic

The monitor distinguishes between **connectivity failures** (VPN broken) and **site errors** (VPN working, site returned an error). The decision is **HTTP-response-first**, not exit-code-based:

- **Any HTTP response = PASS** — including 401/403/404/5xx. A status line proves DNS resolved, the connection traversed the tunnel, and a server answered, so the tunnel is up regardless of *what* the server said (Tenet 3 — a broken tunnel is not a sad website).
- **Only a failure to get any HTTP response = FAIL** — DNS failure, connection refused, TLS error, or timeout.

This is also why it's correct across wget implementations: gluetun ships **GNU wget** (HTTP errors → exit 6/8), but dependent containers commonly run **busybox wget** (exit 1 for *any* HTTP error). Keying on "did we get an HTTP status?" rather than on the exit code means a busybox dependent's harmless 404 is read as a PASS, not a spurious failure. The wget exit code is used only as a **fallback** when no HTTP status line was captured at all (GNU's `0/6/8` = "responded").

**Probe method:** the check is `wget --spider` — a **HEAD** request (headers only, no body). On a server that rejects HEAD (405, or no HEAD support) GNU wget falls back to a **GET**, but still as a spider check — the response body is never downloaded either way. busybox wget's `--spider` is HEAD too. So the method is HEAD by default and GET only as a fallback, never a full content fetch; classification keys on the response, not the method.

**Key insight:** If a site returns HTTP 403 Forbidden or 503 Service Unavailable, the VPN is working — the site just doesn't like the request. Only actual network/DNS/TLS/timeout failures indicate a VPN problem.

### `FAIL_THRESHOLD`
Number of **consecutive** failures for a **critical** site before triggering a restart. This prevents restarts from transient network blips. (An `advisory` site — see [Site roles](#site-roles--critical-default-vs-advisory) — is probed but never triggers a restart regardless of this threshold.)

Example with `FAIL_THRESHOLD=2`:
- Check 1: Site fails → Counter: 1 (no action)
- Check 2: Site fails → Counter: 2 (triggers restart)
- After restart: Counter reset to 0

### `HEALTHY_WAIT_TIMEOUT`
Maximum seconds to wait for Gluetun to report "healthy" status after a restart. This is the **only** place the monitor reads Gluetun's container health — it never restarts Gluetun *because* it is unhealthy.

If Gluetun doesn't become healthy within this timeout, the monitor logs an error but continues operating. If Gluetun has no healthcheck at all, the monitor detects that and settles briefly instead of burning the whole timeout.

**Keep Gluetun's own healthcheck.** Replacing it with a hand-rolled probe both fake-greens this gate and makes it slower — see [Gluetun's healthcheck — don't override it](ARCHITECTURE.md#gluetuns-healthcheck--dont-override-it).

## Dependent Container Discovery

By default (`DEPENDENT_CONTAINERS=auto`), the monitor automatically finds all containers that depend on Gluetun by querying the Docker API for containers with:

```
network_mode: "container:<GLUETUN_CONTAINER>"
```

This just works out of the box - no configuration needed. Discovery runs at startup (for logging) and again before each restart operation to ensure newly added containers are included.

**Note:** Containers added after startup will be discovered and restarted when the next failure triggers a recovery. There's no continuous polling for new containers during normal operation.

### Dependent memory — surviving restarts and recreates together

Discovery alone has a blind spot: a dependent stranded by a gluetun *recreate* points at the **old** container id, so current-id discovery can no longer see it — and if the monitor itself restarted moments earlier, its in-memory record of that dependent is gone too. The monitor closes this with a small persistent sidecar (`MONITOR_STATE_FILE`, default `/logs/monitor-state.json`) remembering two things Docker forgets:

- every container id gluetun has run under (Docker ids are never reused, so a container whose `network_mode` points at a dead id from this list *provably* belonged to this gluetun), and
- the names of dependents it has managed.

Each loop the monitor scans **all** containers (including exited ones — a stranded dependent's own restart policy usually drives it to Exited) and **adopts** any container stranded on a dead former-gluetun id, healing it through the normal remediation path. A container stranded on a dead id the monitor has *never* seen as gluetun is only warned about, never touched — it might belong to some other network owner.

The file is best-effort and human-readable; deleting it (or a corrupt file) simply resets the memory. One bootstrap gap: on the very first run there is no history yet, so dependents that were *already* stranded before the monitor ever saw gluetun can't be confirmed — list them explicitly in `DEPENDENT_CONTAINERS` for that one recovery. Details in [ADR-0014](adr/0014-durable-dependent-memory.md).

### Advanced: Manual Override

In rare cases where you need explicit control (e.g., restart only specific containers, or include containers that don't use network_mode), you can specify a manual list:

```yaml
environment:
  - DEPENDENT_CONTAINERS=container1,container2,container3
```

## Running as non-root (recommended)

By default the container runs as **root** — a drop-in match for v1, with nothing to
configure. Running it **non-root is recommended** (defense in depth), and it uses
the same `PUID`/`PGID` knob the rest of your stack (the LinuxServer.io `*arr`
images) already does:

```yaml
services:
  gluetun-monitor:
    environment:
      - PUID=1000   # your host user's uid — run `id` to find it
      - PGID=1000   # your host user's gid
```

When `PUID`/`PGID` are set, the entrypoint chowns the `/logs` mount to that user
and drops privileges to it — **no manual chown**. Unset, it stays root and behaves
exactly like v1 (so the upgrade is drop-in; non-root is opt-in). Either way the
monitor's real privilege is the Docker API it talks to, not its in-container uid.

> **Direct socket mount** (instead of the recommended socket proxy) **+ non-root:**
> a non-root process can't read the root:docker-owned `/var/run/docker.sock`. With
> the socket **proxy** (the default) this is a non-issue — it's reached over TCP.
> If you insist on the direct mount *and* non-root, use Docker's native
> `user: "<uid>:<gid>"` together with `group_add: ["<docker-gid>"]` instead of
> `PUID`/`PGID` (the privilege-drop resets supplementary groups, so `group_add`
> only composes with `user:`), or simply leave it as root there.
