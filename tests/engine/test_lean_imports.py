"""The UI-side engine modules must not pull engine libraries into the UI process (NFR-1)."""

import subprocess
import sys

HEAVY = ["PIL", "fitz", "pandas", "numpy", "pdf2docx", "pypandoc"]

SCRIPT = f"""
import sys
from dragshift.engine import jobs, registry
registry._ensure_loaded()  # imports every Op module, as the UI does to build wheels
assert registry._OPS, "no ops registered"
print(",".join(m for m in {HEAVY!r} if m in sys.modules))
"""


def test_engine_ui_side_imports_no_heavy_libs() -> None:
    out = subprocess.run(
        [sys.executable, "-c", SCRIPT], capture_output=True, text=True, check=True
    ).stdout
    assert out.strip() == ""
