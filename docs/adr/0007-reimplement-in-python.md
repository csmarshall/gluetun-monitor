# ADR-0007: Reimplement the monitor in Python (v2.0.0)

- **Status:** Accepted
- **Date:** 2026-05-28
- **Enables:** ADR-0004, ADR-0005, ADR-0006 (the dependent-aware design they
  describe is what motivates the rewrite)

## Context
v1.x is pure bash (`gluetun-monitor.sh`): a flat loop that tests a URL set from
inside gluetun (ADR-0001) and restarts gluetun + dependents on failure. The #20
design (ADRs 0004–0006) adds materially harder logic on top of that:

- a **22-node state machine** (ADR-0006) with two failure classes and per-loop
  re-evaluation,
- **per-dependent** counters, an independent **shuffle** per container, and a
  bounded-concurrency fan-out,
- a restart-vs-recreate **decision** keyed on resolving each dependent's
  `NetworkMode` to gluetun's current `.Id` (ADR-0004), and
- the part that forces the question — the **default-on `AUTO_RECREATE`
  reconstruct** (ADR-0005): inspect the dependent, copy its `Mounts` (including
  anonymous volumes), strip the netns-conflicting fields, recreate without `-v`,
  and verify. That is JSON surgery — read a container's full inspect, mutate a
  nested structure, and POST a create body.

In bash this is `curl | jq` against the Docker API (or `docker inspect --format`
gymnastics), with `set -euo pipefail` foot-guns on every non-zero return and no
practical way to unit-test the mutate-and-recreate path without a live daemon.
The logic is now complex enough that **testability is the dominant concern**, and
the recreate path is destructive-adjacent (it `rm`s a container) so it must be
tested hard before it ships on by default.

Python is the preferred language for this kind of tooling (Ruff, mypy, pytest,
pyproject). The blocker was never preference — it was **no-regressions** on a
published v1.x tool.

## Decision
Reimplement as a **Python package** (`gluetun_monitor`) and release it as
**v2.0.0**. Use **docker-py** (the official SDK) as the Docker seam rather than
shelling out to the `docker` CLI: it speaks the same Docker HTTP API the
tecnativa socket-proxy already exposes (`CONTAINERS`/`POST`/`EXEC`), and a thin
wrapper over it is the injection point that makes the whole monitor unit-testable
with a `FakeDockerClient` — **no live daemon required for the test suite**.

The connectivity test keeps its ADR-0001 semantics exactly: still
`exec` **inside gluetun's netns** running `wget --spider -S`, still the same
exit-code → pass/fail map (`0/6/8` = pass). docker-py's `exec_run` returns the
same exit code shell did, so Python only reorganizes **orchestration**, not the
HTTP probe — which is where almost all of the regression surface would be.

**No-regressions strategy (the contract):**
1. **Characterization-first.** Before porting behavior, pin v1.x's observable
   contract as tests: env-var defaults, `sites.conf` parsing (comments, blanks,
   whitespace, the issue-#17 apostrophe), the wget exit-code map, the
   `FAIL_THRESHOLD` consecutive-failure counting, auto-discovery by NetworkMode
   match, and the log tokens (`[CHECK]`, `[ENDPOINT]`, `ip getter` parsing).
   These pass against bash today and are the bar Python must clear green.
2. **Differential harness.** Run old-bash and new-python against identical
   mock-Docker inputs and assert identical decisions (pass/fail/restart) on the
   shared contract.
3. **Port the bats regressions to pytest** — especially #17 (apostrophe parse)
   and shellcheck-clean's spirit (ruff + mypy --strict gate instead).
4. **Promote the `issue20-*.sh` experiment scripts** to integration tests.
5. **Staged rollout (ROC, Tenet 8).** Bash stays in-tree and tagged; the v1.x
   image stays pullable as `:1`. Ship Python as `:2` / `:latest` / v2.0.0,
   dogfood on rosa, and keep **rollback = repin `:1`** — one-step, non-destructive.

## Consequences
- **The hard path becomes testable.** The recreate reconstruct (ADR-0005) gets
  real unit coverage against a fake daemon, including the data-loss guards (copy
  anon volumes, `rm` without `-v`) — the single biggest reason to move.
- **Type safety + lint gate.** `mypy --strict` and `ruff` replace shellcheck;
  the state machine's states/transitions become explicit types, not string
  conventions.
- **New runtime dependency.** v2 needs a Python base image + docker-py instead of
  `docker:cli`. Image grows modestly; the `docker` CLI is no longer required in
  the image (the SDK talks the API directly). Accepted.
- **A real rewrite carries risk** — mitigated by the characterization +
  differential gates above; we do not merge until the bash contract passes green
  in Python and the differential harness agrees.
- **Bash is now legacy.** `gluetun-monitor.sh` is retained for rollback and as
  the differential oracle, not for further feature work. v1.x bug-fixes, if any,
  are backports.
- **Versioning:** the language swap + behavior expansion (dependent-aware) is a
  major bump → **v2.0.0**. The `:1` tag is the compatibility anchor.
- Follow-ups unchanged from the design: notification layer + socket-proxy
  hardening remain in `docs/ROADMAP.md`.
