"""Dependent-aware connectivity monitor and self-healer for gluetun VPN stacks.

See ``docs/adr/`` for the architecture: ADR-0001 (test from inside the netns),
ADR-0004 (dependent-aware health + restart-vs-recreate), ADR-0005 (the recreate
mechanism), ADR-0006 (per-dependent viability testing), ADR-0007 (this Python
rewrite).
"""

__version__ = "2.5.1"  # x-release-please-version
