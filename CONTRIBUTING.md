# Contributing to gluetun-monitor

Thanks for your interest in contributing! As of v2, gluetun-monitor is a **Python
package** (`gluetun_monitor`); the original Bash script (`gluetun-monitor.sh`) is
retained only as a differential test oracle (see below).

## Getting started

1. Fork and clone the repo.
2. Copy the example configs:
   ```bash
   cp docker-compose.yml.example docker-compose.yml
   cp sites.conf.example sites.conf
   ```
3. Set up the Python toolchain:
   ```bash
   python -m venv .venv && . .venv/bin/activate
   pip install -e '.[dev]'
   ```
4. Make your change with tests, run the gate (below), open a PR.

The runtime Python is whatever the **Dockerfile**'s `FROM python:X.Y-slim` declares (currently **3.14**) — the single source of truth that CI derives its test matrix, lock audit, and integration job from. `pyproject.toml` (`requires-python` / ruff `target-version`) sets the supported floor.

## The gate — run this before every PR

```bash
ruff check gluetun_monitor tests     # lint + import order (line length 120)
mypy gluetun_monitor                 # types — strict
pytest                               # tests, incl. the differential suite vs. bash
pytest --cov=gluetun_monitor --cov-report=term-missing --cov-fail-under=95
```

CI runs exactly these (plus a Docker build, an integration smoke test, and
`pip-audit` for dependency CVEs) on every PR. All must pass. Coverage is
**branch** coverage with a **95% floor** — new code needs tests.

## Code style — the standards we follow

| Standard | What | Enforced by |
|---|---|---|
| **PEP 8** | style | ruff (E/W) — **line length 120** (wide-screen, not an 80s terminal) |
| **PEP 257** | docstring conventions | ruff `D` (pydocstyle) — *presence* on every public module/class/method/function, plus formatting |
| **PEP 484** | type hints | `mypy --strict` (+ `warn_unreachable`, `warn_redundant_casts`) — no untyped defs |
| **PEP 517/518/621** | packaging | `pyproject.toml` |

- The enabled ruff rule sets live in `pyproject.toml` (`[tool.ruff.lint]`):
  pycodestyle/pyflakes, isort, bugbear, pyupgrade, comprehensions, simplify,
  return, pathlib, **pydocstyle**, Ruff. We deliberately **exempt three** pydocstyle
  rules (documented inline): `D107` (`__init__` — class docstring covers it),
  `D205` (blank line after summary), `D401` (imperative mood) — our docstrings are
  already clear and we don't reflow them for those.
- **Docstrings explain _why_, not just _what_** — the non-obvious reasoning,
  trade-offs, and tenet/ADR references. Self-documenting names; comment only
  non-obvious logic. Remove dead code.
- No bare `except`; degrade deliberately and say why (the monitor must never let
  one bad loop kill it — Tenet 7).

## Tests

- Tests live in `tests/`, use **pytest**, and run **without a Docker daemon**: a
  `FakeDockerClient` is injected at the `DockerClient` seam (`tests/fakes.py`), so
  you script container state and `exec` results in-memory and assert on *what the
  monitor decided to do*.
- Each test module opens with a short **what/why docstring**. New behavior needs a
  test; a bug fix needs a regression test. Assert the negative too (e.g. that a
  line is *not* logged at INFO) — that's how log-level/contract behavior is pinned.
- Markers (`pyproject.toml`): `integration` (needs a live daemon — excluded by
  default) and `differential` (runs the **legacy bash functions** and asserts the
  Python port matches — the no-regressions gate from
  [ADR-0007](docs/adr/0007-reimplement-in-python.md)).
- The legacy `gluetun-monitor.sh` is **kept as the differential oracle**, so it
  still has `shellcheck` + `bats` CI jobs — if you touch it, keep those green.

## Commit messages & PRs

- Imperative mood, first line < 72 chars, reference issues (`Fixes #123`).
- Don't edit `CHANGELOG.md` — release-please owns it, generating entries from your
  Conventional Commit messages (`feat:`/`fix:`/…). Write a clear commit message
  instead; that becomes the changelog line.
- Update the relevant docs; for a **significant or hard-to-reverse design
  decision**, add an ADR under `docs/adr/` (see `docs/adr/README.md` for the bar —
  routine choices belong in code/docs, not an ADR).
- A PR should leave the gate green and coverage at/above the floor.

See [DEVELOPMENT.md](DEVELOPMENT.md) for architecture and deeper internals.

## Dependency updates

Dependabot opens PRs for the Python deps, GitHub Actions, and the Docker base
image. The dev toolchain is pinned in `requirements-dev.txt` (CI's source of
truth); the loose `[dev]` extras in `pyproject.toml` are for local installs.

Low-risk bumps **auto-merge after the full required-check matrix passes** —
nothing merges on red. Auto-merge covers (patch/minor only): dev tooling
(ruff/mypy/pytest/pytest-cov), GitHub Actions, and the runtime libraries
**`docker`** and **`apprise`** — those last two are safe to auto-merge because each
is exercised for real by a required check (docker by the real-daemon integration
job #24, apprise by the localhost-sink test #22), so a regression turns CI red.
These always get a human:

- any **major** version bump;
- the **Docker base image** (`python:3.x-slim`) — a feature-version jump needs a
  CI-matrix change (cf. #32).

`main` is branch-protected to require those checks; repo admins can still push
docs directly.

## Reporting issues & feature requests

Use the issue templates. Include sanitized logs and your environment (Docker
version, OS). Check existing issues first.

## License

By contributing, you agree your contributions are licensed under the MIT License.
