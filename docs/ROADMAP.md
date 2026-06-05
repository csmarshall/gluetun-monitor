# Roadmap / Backlog

Future work not yet scheduled. Items here are candidates, not commitments.

## Notification layer — ✅ shipped (ADR-0010)
Implemented as an opt-in [Apprise](https://github.com/caronc/apprise) layer:
`APPRISE_URLS` unset = disabled (drop-in); set = significant events
(gluetun restart, recovery failure, dependent remediation, the flaky-site advisory,
refusal to start) are pushed to any of 100+ backends, filtered by `NOTIFY_MIN_LEVEL`
and throttled per event. Best-effort (Tenet 7) and `--notify-test` to verify a URL.
See ADR-0010 and the README "Notifications" section.

Possible follow-ups: a machine-readable health surface (HTTP endpoint / container
`HEALTHCHECK`) distinct from the outbound notifications; more event types behind
the same contract.

## Portable injected viability probe
Today the per-dependent viability probe execs the dependent's **own** `wget`,
whose behavior varies (gluetun ships GNU wget; linuxserver/Alpine dependents ship
busybox wget with a different exit-code convention; distroless ships none). v2
works around this by keying on "did DNS resolve / did we get any HTTP response"
rather than wget's exit code, but it still depends on the container having a
usable `wget`.

A cleaner, fully portable approach: ship a tiny **static, CGO-free binary**
(`gm-probe`) inside the monitor image, `docker cp` it into each dependent (to an
ephemeral path) and `docker exec` it — a uniform DNS-resolve (+ optional TCP
connect) check with an unambiguous exit code, identical on every container.

- **Wins:** identical behavior everywhere; we define "success" exactly (no wget
  quirks); extends viability to **distroless/scratch** dependents (a static
  binary execs without a shell).
- **To resolve first:** (1) does the socket proxy allow the archive endpoints
  (`PUT /containers/{id}/archive`) under `CONTAINERS`, or is it a new permission?
  (Tenet 4 — least privilege.) (2) multi-arch binaries (amd64/arm64/arm) selected
  per dependent; (3) re-inject after a dependent is recreated; (4) it writes into
  the container's ephemeral layer — benign but a deliberate mutation.
- **Why backlog:** sizable (a Go sub-project + build/release pipeline) and gated
  on the proxy-permission question. The v2 HTTP-response/DNS-only classification
  makes the wget-based probe correct for the common case in the meantime.

## Socket-proxy hardening (verify first)
Today's reference proxy ships `CONTAINERS=1 + POST=1 + EXEC=1`. tecnativa provides
granular carve-outs (`ALLOW_RESTARTS`, `EXEC`) intended to permit those ops
**without** the broad `POST=1`.

- Hypothesis: restart + exec + inspect could run on `CONTAINERS + ALLOW_RESTARTS +
  EXEC` with **`POST=0`** — tighter than today (Tenet 4).
- Bonus: with that baseline, `POST=1` becomes the *natural permission-gate* for
  `AUTO_RECREATE` (the proxy config is the opt-in), with graceful fallback to
  alert when absent — no separate flag needed.
- **Verify the carve-outs actually work without `POST` before recommending** (a
  quick proxy-permission experiment), since the current example sets `POST=1`.
