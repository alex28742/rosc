"""Console entry point ``local-app``: a thin shim that loads the repository's ``scripts/local_app.py`` by path.

Developer tooling only (editable install from a ROSC checkout); it is not part of the product runtime. Loading by path
keeps ``local-app`` working from any current directory without making ``scripts/`` an installed package."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    launcher = root / "scripts" / "local_app.py"
    if not launcher.is_file() or not (root / "pyproject.toml").is_file():
        print("local-app needs an editable install from a ROSC checkout: run `pip install -e '.[dev]'` inside the "
              "repository's .venv.", file=sys.stderr)
        return 1
    spec = importlib.util.spec_from_file_location("rosc_local_app", launcher)
    module = importlib.util.module_from_spec(spec)
    sys.modules["rosc_local_app"] = module
    spec.loader.exec_module(module)
    return module.main()


if __name__ == "__main__":
    sys.exit(main())
