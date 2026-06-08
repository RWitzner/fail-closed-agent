"""Pytest parity shim (the suite runs under `python3 -m unittest`).

Mirrors tests/__init__.py so that `import agent...` resolves if the suite is
ever run under pytest. Harmless under unittest.
"""
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
