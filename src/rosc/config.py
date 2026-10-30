from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    path = Path(os.environ.get("ROSC_DATA_DIR", "~/.local/share/rosc")).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path
