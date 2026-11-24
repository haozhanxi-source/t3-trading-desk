"""T3 Trading Desk web gateway: a thin FastAPI wrapper over ``scripts/`` (no duplicated domain logic)."""
from __future__ import annotations

import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(ROOT_DIR, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

__version__ = "1.0.0"
