"""Pytest setup for comfyui-workflow-diff.

Puts the project root on ``sys.path`` so the package's siblings
(``format_adapter``, ``workflow_diff``, ...) can be imported as
top-level modules by the test files. We intentionally avoid having a
conftest.py at the project root because pytest would then treat the
root as a package (the package's ``__init__.py`` lives next to it) and
try to import that ``__init__.py`` during collection — which fails
outside a running ComfyUI.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
