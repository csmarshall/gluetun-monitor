# ADR-0013: Release & versioning automation strategy

- **Status:** Accepted (implemented; #26)
- **Date:** 2026-06-05
- **Relates to:** #26 (implementation), ADR-0010/0011/0012 (the notification layer —
  the human-notification safety net that makes set-and-forget shipping defensible),
  `docs/VERSIONING.md` (the `:latest` / pin-the-major contract)

## Context
Releases are currently **manual**: a human bumps the version and pushes a `vX.Y.Z`
tag, which triggers `release.yml` to build and publish. That human-in-the-loop was
deliberate for a privileged, container-restarting watchdog.

But this tool's profile is **mature and low-feature, yet security-sensitive**: once
it works, human changes are rare, while dependency, base-image, and OS security
updates keep arriving. Under manual releases, a security fix sits unreleased until
someone next cuts a tag — possibly months — so the **stable** image people run
(`:2`) goes stale on security. That is not secure-by-default.

The naive fixes each fail a requirement:
- **Auto-release everything** (semantic-release style) → removes human judgment over
  feature scope.
- **Rebuild stable in place** (re-push the same version with new base layers) →
  makes semvers *mutable* (the same version string maps to different bytes).
- **Gate everything** (vanilla release-please) → stable rots on security for a
  low-touch tool.

The prerequisites this needed are now met: Tier-1-only auto-merge (#23), the
real-daemon test (#24), and a human notification channel (#22).

## Decision

**1. The release gate is keyed on AUTHORSHIP, not on the SemVer level.** Bump level
and release gate are independent axes:

| Axis | Set by | |
|---|---|---|
| **Bump level** (major/minor/patch) | *scope of change* (SemVer) | our bugfix = patch; feature = minor; break = major; a dep bump = patch |
| **Release gate** (auto vs human) | **authorship — do we own it?** | our code → human-gated (any level); upstream/deps/base/security → auto |

> A human gates the release of anything a human authored — feature, fix, or
> refactor, **at any level**. Automation auto-releases only changes it owns
> end-to-end (dependency / base-image / security — which are always patches).

Self-consistent: automation cannot *decide* a minor/major, because deciding
scope-of-change is exactly the human judgment those bumps encode. A human bugfix is
a `patch` **and** gated — no contradiction, since level and gate are separate axes.

**2. Tooling: release-please Release PR + authorship-based auto-merge.**
[release-please](https://github.com/googleapis/release-please) maintains an open
**Release PR** that accumulates the changelog + version from conventional commits and
keeps itself rebased on `main`. A human merges it to cut a release (→ tag → existing
`release.yml`). A small workflow **auto-merges that Release PR iff every commit in it
is authored by `dependabot[bot]`** (or the base-drift job); any human-authored commit
holds it for a click. (Same authorship signal the Dependabot auto-merge already uses.)
We squash-merge, so the **PR title** is the conventional-commit message.

**3. A rolling `:edge` channel.** A separate per-merge workflow publishes `:edge`
(bleeding edge, deps + human work, no gate) so "available now" is decoupled from
"stable." Documented loudly in `VERSIONING.md` as *not guaranteed stable* — the
warning lives in the docs, the tag stays idiomatic (not `:unstable`).

**4. Semvers are immutable; a version is cut only when resolved inputs change.** We
do **not** re-push a version with different bytes. Instead a weekly **input-fingerprint
drift check** (a "faux build" — resolve, don't build) compares the *current* base
image digest (`crane digest python:3.x-slim`, a registry manifest query — no pull, no
build) against what the last release baked in (recorded as the OCI label
`org.opencontainers.image.base.digest`). If it moved → auto-cut a **patch** release
(real build `FROM` the new base, new immutable semver); if unchanged → no-op. This
keeps stable patched without churn or mutable tags.

**5. Determinism prerequisite: a hashed dependency lockfile.** "What would I pull"
is only trustworthy if inputs can't drift invisibly: pin the base by digest, and lock
the Python dep tree (transitives included) so the Python layer changes only when the
lock changes (via a Dependabot PR — already a release trigger).

**6. Provenance: the digest is identity; tags are pointers.** Stamp OCI labels (git
SHA via `…image.revision`, base digest, build time) and ideally SBOM/provenance
attestations (`buildx --provenance --sbom`). Anyone needing exact bytes pins by
`@sha256:` digest; `:2`/`:latest` are moving pointers; `:X.Y.Z` is the immutable
release artifact.

## Consequences
- **Secure-by-default for `:2`:** the unowned dependency/security stream (incl. weekly
  base-image patches) reaches *stable* automatically; features stay deliberate and
  reviewed.
- **Immutable semvers + a supply-chain trail** (digest, OCI labels, optional SBOM).
- **Linear-history caveat:** while a human change is unreleased, later dep patches
  stack *with* it (they can't ship past it) until the human cuts — but `:edge` carries
  everything immediately, so nothing actually waits.
- New machinery to maintain: release-please config, the edge workflow, the drift-check
  job, and the lockfile. Light conventional-commit discipline on PR titles.

## Alternatives considered
- **Manual tag-triggered (status quo).** Rejected: stable rots on security for a
  low-touch tool.
- **Fully automatic (semantic-release / P3).** Rejected: removes human control over
  feature scope.
- **Everything gated (vanilla release-please / P1).** Rejected for *this* profile:
  same security-staleness problem.
- **"patch = automation, minor/major = human."** An earlier framing — rejected as too
  coarse: a human bugfix is also a patch but must be reviewed/gated. Corrected to the
  authorship axis above.
- **Mutable-tag rebuilds** (re-push the same version with patched layers). Rejected:
  violates immutable semvers; provenance lives in the digest/labels, and a real input
  change should mint a real (patch) version — hence the drift check.
- **`:unstable` instead of `:edge`.** Rejected: `:edge` is the idiomatic rolling-main
  tag; the caveat belongs in the docs.
