"""Test package bootstrap.

`python3 -m unittest` does not load a pytest-style conftest, so the import root
is bootstrapped here: importing the `tests` package puts `<repo>/scripts` on
`sys.path` so test modules can `import agent...` (mirrors the Polymarket
`ROOT/scripts` convention — there is no installed package).
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
