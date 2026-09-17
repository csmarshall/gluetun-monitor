# ADR-0018: Per-dependent app-level (HTTP status) checks gate a restart

- **Status:** Proposed
- **Date:** 2026-09-15
- **Relates to:** ADR-0001 (test from inside the namespace), ADR-0003 (ordered
  gated recovery), ADR-0006 (per-dependent viability testing), ADR-0015
  (per-site role)

## Context

ADR-0001's root test and ADR-0006's per-dependent viability pool both answer
"does the tunnel work" — DNS resolves, a TCP/TLS handshake completes, a server
answers. Tenet 3 deliberately treats *any* HTTP response, even 401/403/5xx, as
a pass: the point of those tests is to distinguish "the VPN path is down" from
"a test site is unhappy," and a broken tunnel is not a sad website.

That leaves a real gap uncovered: a VPN provider whose *specific exit
endpoint* is L7-blocked by a destination site. The tunnel is up, DNS is fine,
TCP/TLS complete, the site answers — and answers with a 403, because it
fingerprinted the exit IP, not because anything is broken. ADR-0001/0006's
tests correctly call this healthy; it isn't, for the one thing that matters to
a dependent that needs that specific site. The one action that plausibly
fixes it is unrelated to anything ADR-0003/0004 already does: not restarting
or recreating the *dependent* (its container is fine), but restarting
*gluetun* in the hope of landing on a different exit endpoint.

This needs a check that is deliberately narrower than Tenet 3's "any response
is a pass": for this specific purpose, a 4xx/5xx response is the failure
signal, not proof of a working path. It is not a replacement for the root
test or the viability pool — it is a third, additional signal, scoped to
whichever dependents and sites actually need it, since most dependents don't
talk to an L7-fingerprinting endpoint at all and shouldn't pay for or gate on
this check.

A single global URL is not sufficient: different dependents talk to different
providers, so the natural shape is *N independent rules*, each pairing a set
of dependents (matched by name) with a URL to check from inside each of them.

## Decision

**New config surface, not a `SiteSpec` role.** `sites.conf`'s `|key=value`
suffix (ADR-0015) is tempting to reuse, but a container-name selector needs to
be a regex, and a regex legitimately contains `|` (alternation) — colliding
with the very character `sites.conf` uses as its field separator. Reusing that
grammar would silently break exactly the regexes operators would want to
write (`^(radarr|lidarr)$`). This is also conceptually a different kind of
check — sites.conf drives what's probed *from gluetun*; this drives what's
probed *from specific dependents* — so a separate file keeps both grammars
simple instead of overloading one into two purposes.

**`APP_CHECKS_FILE`** (default `/config/app-checks.conf`, live-reloaded each
loop like `sites.conf`), one rule per line:

```
# <container-name-regex><whitespace><url>[|timeout=N|tries=N|failures=N]
^sonarr-.*$          https://provider-a.example/index.html
^(radarr|lidarr)$    https://provider-b.example/index.html|timeout=15|failures=5
```

Each line splits on the *first* run of whitespace. This is collision-free
because Docker container names are restricted to `[a-zA-Z0-9][a-zA-Z0-9_.-]+`
— no valid name-matching regex ever needs to match a literal space, so
whitespace is a separator a regex field can never legitimately contain. The
remainder after the split is `sites.conf`'s existing `url[|timeout=N|tries=N]`
grammar, parsed by the same `sites.parse_entry()`, extended with one new
option key (`failures`, below) alongside the existing `timeout`/`tries` —
same forgiving-and-loud validation, same unsafe-URL rejection (`sites.
unsafe_site_reason()`), same per-option sanity caps (`_OPTION_CAPS`). A
`role=` option parses but is meaningless here (an app-check rule always gates
a restart by construction) and is warned-and-ignored, not silently accepted —
consistent with every other unrecognized-but-parseable option in that parser.

**The probe itself is unchanged.** `connectivity.probe_site()` already execs
`wget --spider -S` inside a container and parses the final HTTP status
(following redirects — GNU and busybox wget both do, by default) into
`SiteResult.http_code`. No new exec path, no new wget invocation. Only the
pass/fail *interpretation* is new and specific to this check:

```
pass  iff  http_code != "N/A" and int(http_code) < 400
```

This is a deliberate, scoped narrowing of Tenet 3's "any response is a pass"
— not a contradiction of it. Tenet 3 governs the root/viability tests, whose
job is "does the path work at all." This check's job is "is this specific
site blocking this specific exit," a strictly narrower question that a 403
answers directly. `exec_failed` (the probe never ran — no EXEC permission,
container gone, no wget) is excluded from both, unchanged: it is never
evidence of a site block, only of a fault on the monitor's own exec path
(#137, Tenets 1 and 7).

**Each rule runs serially against its matched dependents, short-circuiting on
first failure.** For a rule, the question is "can *any* matched container not
reach this site" — once one has failed, the rest add no information, and
running serially (rather than the `MAX_PARALLEL_CHECKS`-bounded fan-out
ADR-0006 uses for the viability pool) naturally avoids hammering an
already-blocking endpoint from every matched container in the same loop.

**Each rule gets its own independent consecutive-failure counter**, keyed by
its own identity (`"<pattern> -> <url>"`) in a `state.Counter` — the same
per-key counter class `site_failures` already uses, reused as-is (no new
counter type). A rule with zero currently-matched dependents is neither a
pass nor a failure this loop — nothing to test — and does not touch its
counter, mirroring how the root test's `unprobeable` state never touches
`site_failures` either.

**`DEPENDENT_APP_CHECK_FAILURES`** (default `= FAIL_THRESHOLD`) is the
consecutive-loop threshold before a rule's failure gates a restart — the same
"over-observe, under-react" posture (Tenet 8) as every other restart trigger
in this codebase, so a single L7 blip doesn't roll the tunnel any more
readily than a single flaky root-test site does. It is a **global default**,
overridable **per rule** via `|failures=N` on that rule's line — the same
option-grammar extension `|timeout=`/`|tries=` already use, capped the same
way (`_OPTION_CAPS`) so an absurd per-rule value can't silently create a
restart that never fires or fires on a single blip. A flakier provider can
tolerate more consecutive misses than a stricter one without a global
setting forcing one number on every rule.

**A breached rule feeds the existing restart path, not a new one.** A rule
that crosses its threshold is folded into the same `breached` list
`check_gluetun_sites()` already produces (as an attribution string,
`app-check[<pattern>] (<container>: <detail>)`), so it flows through the
single existing restart-and-reverify machinery in `monitor.py`'s main loop
unchanged: one attribution log line, one notification body, one
`_unrecovered_sites` hold on failure. No second restart path to keep in sync
with the first.

**Post-restart re-verification includes the triggering app-check rule(s).**
ADR-0003's re-verify-before-declaring-recovered discipline exists precisely
so a restart is never reported as having fixed something it didn't actually
fix (Tenet 7 — never fake-green). A restart triggered by an app-check failure
must re-run *that* check after the restart, not just gluetun's root site set:
confirming the root sites still pass says nothing about whether the new exit
endpoint actually cleared the L7 block that caused the restart in the first
place. Declaring "recovered" without that confirmation would be exactly the
fake-green this monitor exists to avoid.

**A rule matching zero dependents for an extended stretch is a probable
misconfiguration, not silence.** Mirroring the existing `_warned_missing`
treatment of a `DEPENDENT_CONTAINERS` entry that never resolves to a running
container, a rule whose regex matches nothing for
`_UNPROBEABLE_ALERT_LOOPS` (5) consecutive loops gets one deduped `WARN` —
otherwise a typo'd regex just quietly checks nothing, forever, with no
signal that it isn't doing its job.

## Consequences

Operators get a way to say "this specific site, through this specific set of
containers, is worth restarting the tunnel over" without that site's 403
being silently absorbed as "healthy" by the root test's Tenet-3 tolerance,
and without hand-rolling a sidecar script. The mechanism is additive and
off by default (an absent or empty `APP_CHECKS_FILE` is a no-op) — nothing
about the existing root test or viability pool changes.

The cost is a second small config-file grammar to document and reason about
alongside `sites.conf`, and a second thing (beyond the root site set) that
post-restart re-verification must cover — a small, bounded addition to
`_run_once_body`, not a parallel state machine.

This intentionally does not attempt to make the check free-form (an arbitrary
operator-supplied command). Dependents in this stack are a heterogeneous mix
of images (busybox wget on Alpine/linuxserver bases vs. others), and a single
free-form command applied across every regex-matched container inherits that
heterogeneity — it would need to work identically in every shell/toolset the
regex happens to match. `wget --spider -S` is already proven to work
consistently across the busybox/GNU divide (`connectivity.py`); standardizing
on URL + status code keeps that guarantee. A free-form command, if ever
needed, is a distinct follow-up with its own ADR, not an extension of this
one.

Deliberately deferred: folding app-check results into the persistent
per-site stats/advisory layer (ADR-0008) the way root-test sites already
are, since app-checks aren't `SiteSpec`s and don't currently flow through
`SiteStatsStore`.
