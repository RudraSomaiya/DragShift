"""Real OLE drag-and-drop onto the real wheel, with Shift held (DS-11, FR-1.8).

Off by default because it drives the real mouse and Shift key for a few seconds. Run it with:

    $env:DRAGSHIFT_REAL_DND = "1"; uv run pytest tests/test_real_dnd.py -q
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("real_dnd_script.py")

pytestmark = pytest.mark.skipif(
    os.environ.get("DRAGSHIFT_REAL_DND") != "1",
    reason="moves the real cursor; set DRAGSHIFT_REAL_DND=1 to run",
)


def run_script(*args: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"}  # a real window system
    out = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=True,
        env=env,
        timeout=90,
    ).stdout
    return json.loads(out.strip().splitlines()[-1])


def test_shift_drag_onto_a_petal_reports_copy_never_move() -> None:
    result = run_script()
    assert result["petal"] == "CopyAction"  # MoveAction here would make Explorer delete the source
    assert result["hub"] == "IgnoreAction"
    assert result["text_on_petal"] == "IgnoreAction"  # no files: every petal is disabled (FR-1.10)
    assert result["source_still_exists"] == "True"


def test_the_harness_does_produce_move_for_the_forbidden_call() -> None:
    """Negative control: with acceptProposedAction() the same drag reports Move."""
    assert run_script("--unsafe-control")["petal"] == "MoveAction"
