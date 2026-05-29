# Roadmap / Backlog

Future work not yet scheduled. Items here are candidates, not commitments.

## Notification layer
A pluggable way to **announce** monitor events to the outside world — fired from
the **FAILED state** (ADR-0006) and optionally on recovery/endpoint-change.

- Decouple *what happened* (the monitor's state machine) from *how it's announced*.
- **Leverage a standard, maintained Python library rather than hand-rolling
  senders.** [Apprise](https://github.com/caronc/apprise) is the strong default:
  one well-supported dependency covers 100+ targets (ntfy, Slack, Discord,
  Telegram, email, generic webhook, …) behind a single URL-string API — a natural
  fit for "opt-in URL(s) via env." Hand-written senders per service are exactly
  the bespoke surface we'd avoid (cf. ADR-0007: use the SDK, don't reinvent).
- Triggers to consider: entering FAILED, recovering from FAILED, gluetun endpoint
  change, dependent recreated.
- Config: opt-in URL(s) via env; quiet by default (no surprise outbound traffic).
- Why backlog: it's a separate, sizeable feature surface; the **FAILED state** in
  ADR-0006 is the natural trigger point and is the only prerequisite. Until it
  lands, FAILED surfaces only as a **loud ERROR log** — there is no separate
  machine-readable health surface (no HTTP endpoint, no container `HEALTHCHECK`);
  exposing one could be a follow-up to this item.

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
