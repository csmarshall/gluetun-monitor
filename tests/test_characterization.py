"""Characterization / differential suite (ADR-0007 no-regressions gate).

These tests pin the v1.x bash contract by executing the *actual* legacy
functions from ``gluetun-monitor.sh`` and asserting the Python port produces
identical results. The bash script stays in-tree as the differential oracle.
Marked ``differential``; skipped automatically if bash or the script is absent.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from gluetun_monitor.config import Config
from gluetun_monitor.connectivity import WGET_PASS_CODES, decode_wget_exit_code
from gluetun_monitor.sites import trim

_BASH = shutil.which("bash")
_SCRIPT = Path(__file__).resolve().parent.parent / "gluetun-monitor.sh"

pytestmark = [
    pytest.mark.differential,
    pytest.mark.skipif(
        _BASH is None or not _SCRIPT.exists(),
        reason="bash and the legacy gluetun-monitor.sh are required for differential tests",
    ),
]


def _source_and_call(call: str, env_extra: dict[str, str] | None = None) -> str:
    """Source the legacy script (main disabled) and run one function call."""
    src = _SCRIPT.read_text(encoding="utf-8")
    # Disable the loop entry point the same way the bats suite does.
    src = src.replace('main "$@"', ': # main disabled for differential tests')
    program = f"{src}\n{call}\n"
    env = {**os.environ, **(env_extra or {})}
    result = subprocess.run(
        [_BASH, "-c", program],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, f"bash failed: {result.stderr}"
    return result.stdout


# ----- differential: trim (bash trim vs Python trim) -----


@pytest.mark.parametrize(
    "value",
    [
        "   hello world   ",
        "\t  hello\t",
        "",
        "     ",
        ' say "hi" ',
        " Provence-Alpes-Cote-d'Azur ",  # issue #17
        "no-surrounding-space",
    ],
)
def test_trim_matches_bash(value: str) -> None:
    """Differential: the Python trim must produce byte-identical output to the
    legacy bash trim for every input — including the #17 apostrophe case."""
    # Pass via env to avoid any shell-quoting differences in the harness itself.
    bash_out = _source_and_call('trim "$TRIM_IN"', {"TRIM_IN": value})
    assert trim(value) == bash_out  # bash trim uses printf '%s' (no trailing newline)


# ----- differential: wget exit-code decoding -----


@pytest.mark.parametrize("code", [0, 1, 2, 3, 4, 5, 6, 7, 8, 99])
def test_decode_wget_exit_code_matches_bash(code: int) -> None:
    """Differential: the Python exit-code reasons match the legacy bash function
    exactly — the no-regressions gate for the connectivity classification."""
    bash_out = _source_and_call(f"decode_wget_exit_code {code}").rstrip("\n")
    assert decode_wget_exit_code(code) == bash_out


def test_wget_pass_codes_match_bash_logic() -> None:
    """The pass set matches v1.x test_site_async: 0 (success) and 6/8 (site
    responded) count as the tunnel working."""
    assert frozenset({0, 6, 8}) == WGET_PASS_CODES


# ----- contract: env-var defaults match the legacy script's defaults -----


def _bash_default(var: str) -> str:
    """Extract the ``${VAR:-default}`` fallback from the legacy script."""
    text = _SCRIPT.read_text(encoding="utf-8")
    m = re.search(rf'^{var}="\$\{{{var}:-([^}}]*)\}}"', text, re.MULTILINE)
    assert m, f"could not find default for {var} in legacy script"
    return m.group(1)


def test_env_defaults_match_legacy_script() -> None:
    """Contract: every shared env default in the Python Config equals the
    ${VAR:-default} the legacy bash script used — so a port didn't silently
    change a default an existing deployment relies on."""
    c = Config()
    assert _bash_default("CONFIG_FILE") == c.config_file
    assert _bash_default("LOG_FILE") == c.log_file
    assert int(_bash_default("CHECK_INTERVAL")) == c.check_interval
    assert int(_bash_default("TIMEOUT")) == c.timeout
    assert int(_bash_default("FAIL_THRESHOLD")) == c.fail_threshold
    assert _bash_default("GLUETUN_CONTAINER") == c.gluetun_container
    assert int(_bash_default("HEALTHY_WAIT_TIMEOUT")) == c.healthy_wait_timeout
    assert _bash_default("DEPENDENT_CONTAINERS") == c.dependent_containers
