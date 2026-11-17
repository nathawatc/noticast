"""Top-level launcher adding src/ to sys.path for direct `.venv/bin/python main.py` runs."""

from __future__ import annotations

import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from noticast.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
