"""Local-first managed agent prompt/report lifecycle for ROSC.

Non-canonical, local-only tooling under ``.agent-telemetry/`` -- never repository
documentation and never product code. ``docs/agent_reporting.md`` owns the full
contract this package implements.

Everything that creates, verifies, or finalizes a local artifact works without
network or OAuth (standard-library Python only). ``drive.py``/``sync.py``/
``config.py`` hold the optional Google Drive transport, whose third-party
dependencies are imported lazily and never required for local commands or the
offline test suite. Prompt content is pulled Drive -> local and finalized report
content is archived local -> Drive; the two directions never cross.
"""

from __future__ import annotations

__all__ = ["artifacts", "cli", "config", "drive", "errors", "state", "sync"]

