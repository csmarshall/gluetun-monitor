# Versioning & release policy

gluetun-monitor follows [Semantic Versioning](https://semver.org/) and ships
Docker images on every release. This document is the contract for what the tags
mean and how upgrades (including a future reimplementation) are handled, so the
next major is a mechanical, low-pain move — the same playbook the v1→v2
transition used.

## Image tags

Every release publishes, to both GHCR and Docker Hub:

| Tag | Example | Moves to | Use for |
|-----|---------|----------|---------|
| `MAJOR.MINOR.PATCH` | `2.0.0` | never (frozen) | reproducible / fully pinned deploys |
| `MAJOR.MINOR` | `2.0` | latest patch of that minor | pinning to a feature line |
| **`MAJOR`** | **`2`** | latest release within that major | **recommended for production** |
| `latest` | — | newest **stable** release, **including across a major** | "always newest", attended updates |
| `edge` | — | **every push to `main`** (rebuilt each merge) | testing the bleeding edge |

> ⚠️ **`:edge` is not a release and is not guaranteed stable.** It is the current
> `main`, rebuilt on every merge — unreleased fixes *and* unreviewed work in progress.
> It does **not** move `:latest`/`:MAJOR`, carries no SemVer, and may break. Pin it
> only to try the newest changes early; never for production. There's also an
> addressable `:edge-<sha>` per build if you need to pin a specific one.

`:latest` tracks the newest **non-pre-release** version only (the workflow uses
`latest=auto`). Two guarantees follow, both enforced in CI config rather than by
discipline:

- A **pre-release** (`-rc`, etc.) never moves `:latest`.
- An **EOL/older-major patch never claims `:latest`.** When v1 is EOL and we still
  ship a v1 patch (e.g. an end-of-life notice), that build sets `latest=false`, so
  it updates only `:1` / `:1.x` and **cannot** drag `:latest` back off v2. So once
  `:latest` is on a major, it only ever moves forward.

The previous major's `MAJOR` tag (e.g. `1`) is **retained but frozen** as a
rollback anchor after it goes EOL — it is never deleted, and an EOL patch to it
(see above) still won't disturb the current `:latest`.

### Why `:MAJOR` is the recommended pin
This is a privileged watchdog: it holds Docker `POST` rights and will
restart/recreate other containers. Pinning the major means an unattended updater
(Watchtower, etc.) picks up **features and fixes automatically** but **never a
breaking major** without a human deciding to move. `:latest` would silently cross
a major boundary — exactly the kind of surprise a container-restarting tool
should avoid (Tenet 1: first, do no harm).

`:latest` is still published for people who explicitly want the newest thing and
update attentively — but it is **newest *stable***, so it's safe from half-baked
pre-releases. If a pre-release channel is ever wanted, it would get its own
explicit tag (e.g. `:edge`/`:rc`); `:latest` would keep meaning "newest stable".

## What each bump means

- **PATCH** (`2.0.0 → 2.0.1`): bug fixes only. Always safe to take.
- **MINOR** (`2.0 → 2.1`): new features, backward-compatible. New config is
  **additive and defaults to current behavior** — upgrading changes nothing
  unless you opt in. Safe to take.
- **MAJOR** (`2 → 3`): a deliberate line that **may** change defaults, remove
  config, or alter behavior. Requires reading the migration notes. **The
  implementation language is irrelevant to the version** — a major is defined by
  *user-facing change*, not by how it's built (v1 was bash, v2 is Python; a
  hypothetical Go/Rust rewrite that kept the same interface could even be a
  minor — see below).

## The major-upgrade contract (how we keep it painless)

When a new major ships, we hold to the same guarantees that made v1→v2 a
drop-in:

1. **Config compatibility is preserved wherever feasible**, even across a
   rewrite. Same env var names + defaults, same file paths (`/config`, `/logs`),
   same Docker API permission set. The goal is "change the tag, it keeps working"
   for the common case. A reimplementation in another language is an
   *implementation detail* and should aim to honor the existing interface — if it
   does, the only reason it's a new major is any *intentional* behavior change it
   carries, not the rewrite itself.
2. **Breaking changes are loud and documented**, never silent: enumerated in the
   CHANGELOG `### Migration` section and a README "Upgrading from vN" section,
   with the conservative-equivalent settings spelled out.
3. **The upgrade is validated**, not assumed: a drop-in test boots the new image
   with the previous major's config/env against a real socket proxy and asserts a
   clean start (see `issue20-upgrade-validation.sh` for the v1→v2 instance), plus
   a differential check that shared defaults didn't drift.
4. **The previous major is EOL'd but kept pullable** as its `MAJOR` tag, so
   rollback is a one-line tag change.
5. **"Fail loud, don't guess" can tighten across a major**: a new major may
   reject a misconfiguration an older one tolerated silently (v2 made empty/bad
   config fatal). That's a deliberate, documented breaking change, not a
   regression.

## History

| Major | Implementation | Status |
|-------|----------------|--------|
| v1 | bash (`gluetun-monitor.sh`) | **EOL** — frozen at `:1` as rollback anchor |
| v2 | Python (`gluetun_monitor`, docker-py) | **current** ([ADR-0007](adr/0007-reimplement-in-python.md)) |
