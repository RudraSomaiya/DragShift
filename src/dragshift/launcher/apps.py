"""Apps wheel slots (FR-3.3): an ``.exe``, an ``.lnk``, a folder, or a Store app (AUMID).

Runs in the UI process: launching is a ShellExecute, not a job.
"""

import os
import re
import subprocess
from pathlib import Path

from dragshift.config import LauncherApp

APPS_FOLDER = "shell:AppsFolder\\"


def aumid_of(target: str) -> str | None:
    """The AppUserModelID of a Store app target, else None.

    Accepts ``shell:AppsFolder\\<AUMID>`` or the bare AUMID
    (``Microsoft.WindowsCalculator_8wekyb3d8bbwe!App``), which is never a path.
    """
    if target.lower().startswith(APPS_FOLDER.lower()):
        return target[len(APPS_FOLDER) :] or None
    if "!" in target and not Path(target).is_absolute():
        return target
    return None


def label_of(app: LauncherApp) -> str:
    """The petal label: the slot's own label, else a name made from the target."""
    if app.label.strip():
        return app.label.strip()
    target = app.target.strip()
    if aumid := aumid_of(target):
        # "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App" -> "WindowsCalculator" -> "Calculator":
        # the part after the publisher ("Microsoft.MicrosoftEdge.Stable" -> "MicrosoftEdge")
        parts = aumid.split("!")[0].split("_")[0].split(".")
        name = parts[1] if len(parts) > 1 else parts[0]
        words = re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", name) or [name]
        while len(words) > 1 and words[0] in ("Microsoft", "Windows"):
            words.pop(0)
        return " ".join(words)
    path = Path(os.path.expandvars(target))
    return path.stem or str(path)  # a drive root ("D:\\") has no stem


def launch_app(app: LauncherApp) -> str | None:
    """Start the slot's target. Returns None on success, else a one-line reason."""
    target = os.path.expandvars(app.target.strip())
    if not target:
        return "this slot is empty"
    try:
        if aumid := aumid_of(target):
            subprocess.Popen(["explorer.exe", APPS_FOLDER + aumid])
            return None
        path = Path(target)
        if not path.exists():
            return f"{path} does not exist"
        if path.suffix.lower() == ".exe":
            os.startfile(path, cwd=str(path.parent))  # many programs expect their own folder
        else:
            os.startfile(path)  # a shortcut keeps its own start-in folder
    except OSError as e:
        return str(e)
    return None
