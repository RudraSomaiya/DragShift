"""The wheel must not pull engine libraries into the UI process (NFR-1, CLAUDE.md rule 2)."""

import subprocess
import sys

HEAVY = ["PIL", "fitz", "pandas", "numpy", "pdf2docx", "pypandoc"]

SCRIPT = f"""
import sys
import dragshift.ui.wheel
print(",".join(m for m in {HEAVY!r} if m in sys.modules))
"""


def test_wheel_imports_no_heavy_libs() -> None:
    out = subprocess.run(
        [sys.executable, "-c", SCRIPT], capture_output=True, text=True, check=True
    ).stdout
    assert out.strip() == ""
