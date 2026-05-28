# Roadmap / Backlog

Future work not yet scheduled. Items here are candidates, not commitments.

## Notification layer
A pluggable way to **announce** monitor events to the outside world — fired from
the **FAILED state** (ADR-0006) and optionally on recovery/endpoint-change.

- Decouple *what happened* (the monitor's state machine) from *how it's announced*.
- Likely a single outbound hook with adapters: generic **webhook**, plus common
  targets (ntfy, Apprise, Slack/Discord, email). [Apprise](https://github.com/caronc/apprise)
  would cover many targets in one dependency.
- Triggers to consider: entering FAILED, recovering from FAILED, gluetun endpoint
  change, dependent recreated.
- Config: opt-in URL(s) via env; quiet by default (no surprise outbound traffic).
- Why backlog: it's a separate, sizeable feature surface; the **FAILED state** in
  ADR-0006 is the natural trigger point and is the only prerequisite. Until it
  lands, FAILED = loud log + unhealthy status.

## Socket-proxy hardening (verify first)
Today's reference proxy ships `CONTAINERS=1 + POST=1 + EXEC=1`. tecnativa provides
granular carve-outs (`ALLOW_RESTARTS`, `EXEC`) intended to permit those ops
**without** the broad `POST=1`.

- Hypothesis: restart + exec + inspect could run on `CONTAINERS + ALLOW_RESTARTS +
  EXEC` with **`POST=0`** — tighter than today (Tenet 3).
- Bonus: with that baseline, `POST=1` becomes the *natural permission-gate* for
  `AUTO_RECREATE` (the proxy config is the opt-in), with graceful fallback to
  alert when absent — no separate flag needed.
- **Verify the carve-outs actually work without `POST` before recommending** (a
  quick proxy-permission experiment), since the current example sets `POST=1`.
