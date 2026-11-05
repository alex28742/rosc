"""ROSC path policy: one data root, one global definition library, one set of repo-private developer folders.

Data root rule (``resolve_data_dir``): a non-empty ``ROSC_DATA_DIR`` wins (``~`` expanded, resolved against the current
directory when relative); otherwise ``<source checkout>/data``.  The checkout is found from this file's own location, so
no user name or absolute path is embedded.  That default exists only for a **source checkout / editable install**; from
an installed (non-editable) package there is no checkout to anchor it on and ``ROSC_DATA_DIR`` must be set explicitly.
An empty ``ROSC_DATA_DIR`` is treated as unset (it must never silently resolve to the current directory).

``resolve_data_dir`` has no side effects; only ``data_dir`` creates the directory.  ``scripts/local_app.py`` is stdlib-only
and keeps its own copy of the same rule; ``tests/test_p020_repository_consolidation.py`` pins the two together.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

DATA_DIR_ENV = "ROSC_DATA_DIR"
DEFAULT_DATA_DIRNAME = "data"
GLOBAL_LIBRARY_DIRNAME = "global"
# Developer/private folders of the ROSC source checkout that SourceRead must never serve (see project_context).
REPO_PRIVATE_DIRNAMES = (".local-dev", ".local-data", "stail")


class DataRootError(RuntimeError):
    """No data root can be determined; the message says what the Owner must set."""


def source_checkout() -> Path | None:
    """The ROSC source checkout this module runs from, or ``None`` for a non-editable (installed) package."""
    here = Path(__file__).resolve()
    root = here.parents[2]
    if here == root / "src" / "rosc" / "config.py" and (root / "pyproject.toml").is_file():
        return root
    return None


def resolve_data_dir(environ: Mapping[str, str] | None = None, checkout: Path | None = None) -> Path:
    """Pure data-root lookup: never creates anything."""
    env = os.environ if environ is None else environ
    explicit = (env.get(DATA_DIR_ENV) or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    root = checkout if checkout is not None else source_checkout()
    if root is None:
        raise DataRootError(f"{DATA_DIR_ENV} is not set and ROSC is not running from a source checkout "
                            f"(non-editable install): set {DATA_DIR_ENV} to an existing private directory.")
    return (Path(root) / DEFAULT_DATA_DIRNAME).resolve()


def data_dir() -> Path:
    path = resolve_data_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def global_library_dir() -> Path | None:
    """``<checkout>/global`` (Git-tracked definitions), or ``None`` when there is no source checkout."""
    root = source_checkout()
    return root / GLOBAL_LIBRARY_DIRNAME if root is not None else None


def repo_private_paths() -> tuple[Path, ...]:
    """Physical developer-private folders of THIS checkout (empty for a non-editable install)."""
    root = source_checkout()
    return tuple(root / name for name in REPO_PRIVATE_DIRNAMES) if root is not None else ()
