# ADR-0010: Opt-in external notification layer via Apprise

- **Status:** Accepted
- **Date:** 2026-06-05
- **Relates to:** ADR-0008 (flaky-site advisory — its log line is now also pushable),
  Tenet 7 (best-effort, non-blocking), Tenet 8 (safe, additive defaults)

## Context
Every "alert" and the flaky-site "advisory" were **log lines only**. Significant,
infrequent events — gluetun restarted, a restart that did **not** bring gluetun back
(stuck/can't-come-up), a dependent remediated or failed, a site that keeps causing
restarts, a refusal to start on bad config — are exactly the things an operator
wants pushed out-of-band, not discovered later by grepping a log.

This matters more now that the dependency pipeline auto-merges and we are weighing
auto-release (the deferred #26): set-and-forget shipping is only defensible if a
human is told when something goes wrong. The notification channel is that safety
net, so it lands first.

## Decision
Add an **opt-in** notification layer built on
[Apprise](https://github.com/caronc/apprise) (one dependency, 100+ backends —
ntfy/Discord/Telegram/email/Pushover/Gotify/webhook/… — all configured by URL).

- **Drop-in:** `APPRISE_URLS` unset → a `NullNotifier` (no-op). Zero behavior change;
  the monitor stays a log-only tool unless you opt in. (Tenet 8.)
- **The seam mirrors ADR-0007's Docker seam:** a `Notifier` Protocol with
  `NullNotifier` (disabled), `AppriseNotifier` (real, apprise imported lazily), and
  a `FakeNotifier` in tests. The monitor fires `NotifyEvent`s; it never imports
  apprise directly.
- **Event contract** — the events surfaced (gluetun restart/recovery, dependent
  remediation, the flaky-site advisory, refusal to start). How they're *classified*
  (the `NOTIFY_LEVEL` tier dial), *grouped* (per-loop rollup), and *re-notified*
  (edge-trigger / repeat / resolve) is decided in **ADR-0011** and **ADR-0012** —
  this ADR's original severity-floor (`NOTIFY_MIN_LEVEL`) and per-event throttle were
  superseded there before release.
- **Best-effort, non-blocking (Tenet 7):** every send is wrapped, tier-filtered,
  and on any failure swallowed + logged at DEBUG. A notification problem can only
  ever degrade notifications — it can never touch the monitoring loop or
  restart/remediation behavior. Apprise URLs carry tokens and are never logged.
  Apprise's `notify()` is synchronous, so sends run on a daemon thread bounded by
  `NOTIFY_TIMEOUT` (default 10s): a slow or hung backend can't stall the watchdog.
- **Following Apprise's idioms:** one reused `Apprise()` instance (not per-send),
  branded with an `AppriseAsset` (`gluetun-monitor`), `add()` return values checked,
  the official `notify_type` values used, and apprise's own logger quieted so a
  DEBUG run stays clean.
- **`--notify-test`** sends a one-off test notification so an operator can verify
  their URL without waiting for a real event.

## Consequences
- Apprise becomes a second pinned runtime dependency. To keep it from being an
  unvalidated auto-merge (the trap docker-py had under `FakeDockerClient`), CI
  exercises the **real** library end to end against a localhost HTTP sink — if an
  apprise update broke the parse/dispatch path we use, CI goes red. That test runs
  in the normal (required) test job, so it gates auto-merge. With it, apprise
  patch/minor are safe to auto-merge on the same principle as docker-py: *a
  dependency may auto-merge once CI genuinely exercises it.* Majors still get a human.
- The advisory/alert log lines are unchanged; this is purely an **additional**
  outward channel (ADR-0008's "advise a human" made external). It does **not** change
  any restart/remediation decision.

## Alternatives considered
- **A specific backend (ntfy/Discord) directly.** Rejected: locks users to one
  service; Apprise is one dependency for all of them, by URL.
- **Optional extra (`pip install …[notify]`).** Rejected: the primary artifact is a
  Docker image, and "set `APPRISE_URLS` and it works" requires apprise in the image.
- **A webhook-only callout we write ourselves.** Rejected: re-implements a fraction
  of Apprise (retries, 100+ schemas) for no gain.
