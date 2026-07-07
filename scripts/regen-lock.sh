#!/usr/bin/env bash
# Regenerate requirements.lock using the SAME interpreter the image ships
# (python:3.14-slim), so the resolved dependency set is faithful to the runtime.
#
# Why a container and not the host: pip-compile resolves environment markers
# (python_version, platform_*) against whatever interpreter it runs under. A lock
# frozen under a host's 3.13 can pin different transitives than the 3.14 image needs
# (#82). Running it in the 3.14 container keeps the host Python-free and the lock
# honest. CI's lock-audit job enforces that the committed lock matches this output.
#
# Usage: ./scripts/regen-lock.sh   (needs docker; no host python required)
set -euo pipefail
unset TMOUT

PIP_TOOLS_VERSION="7.5.3"   # keep in lock-step with .github/workflows/ci.yml lock-audit
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Single source of truth: derive the interpreter from the Dockerfile's base image,
# so this script never drifts from what the image ships. A base bump (3.14 → 3.15)
# is one line in the Dockerfile and both this and CI's lock-audit follow.
PY_VERSION="$(grep -oP 'FROM python:\K[0-9]+\.[0-9]+' Dockerfile | head -1)"
if [ -z "${PY_VERSION}" ]; then
  echo "ERROR: could not read the Python version from the Dockerfile FROM line" >&2
  exit 1
fi

echo "Regenerating requirements.lock in python:${PY_VERSION}-slim (pip-tools==${PIP_TOOLS_VERSION})..."
docker run --rm \
  -v "$REPO_ROOT":/w -w /w \
  -e HOME=/tmp \
  --user "$(id -u):$(id -g)" \
  "python:${PY_VERSION}-slim" \
  sh -c "python -m venv /tmp/v \
    && /tmp/v/bin/pip install -q pip-tools==${PIP_TOOLS_VERSION} \
    && /tmp/v/bin/pip-compile --generate-hashes --output-file=requirements.lock pyproject.toml"

echo "Done. Review the diff and commit requirements.lock."
