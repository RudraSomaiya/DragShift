"""The Actions wheel (Alt): any files or folders, mixed kinds allowed (PRD §6.3).

- **Copy path / Copy name** (UI): newline-separated, onto the clipboard.
- **Terminal** (UI): Windows Terminal in the folder (``wt.exe -d``), else Windows PowerShell.
  A dragged folder opens in itself, a file in its folder.
- **Copy to ▸ / Move to ▸** (worker): their outer ring holds the pinned folders, 1-4, from
  ``config.folders.pinned``. A name already taken in the folder gets " (2)", … (FR-5.2).
  Copies go through ``*.part`` and are renamed when complete. A move on the same drive is a
  rename; across drives it is a copy, and the original is deleted only once the copy is complete.
  Move to is the one Op allowed to move a source (FR-5.4). Undo removes the copy or moves the
  item back (FR-5.6).
- **Zip** (worker): ``<stem>.zip`` for one item, ``Archive.zip`` for several, next to the first.
- **SHA-256** (worker): ``hash  name`` lines, as ``sha256sum`` writes them, onto the clipboard.

The pinned folders are set from the UI process with ``pin_folders``, which re-registers the ring's
Ops with the folders' names. The worker gets its folder in ``params["folder"]`` (``params_for``).
Stdlib only.
"""

import hashlib
import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from dragshift.engine.kinds import Kind
from dragshift.engine.registry import Op, op, register
from dragshift.engine.worker import ERROR_NOT_SAME_DEVICE, finalize, free_path

if TYPE_CHECKING:
    from dragshift.engine.worker import JobContext

ANY = frozenset(Kind)
FILES = ANY - {Kind.FOLDER}
MAX_PINNED = 4  # PRD §6.3: 1-4 pinned folders
CHUNK = 1 << 20

_folders: tuple[Path, ...] = ()  # the pinned folders, in the UI process


class Cancelled(Exception):
    """The job was cancelled part-way; the caller cleans up. Not an OSError, so that
    ``shutil.copytree`` lets it through at once instead of collecting it."""


# --- UI ops --------------------------------------------------------------------------------------
@op(id="actions.copy_path", wheel="actions", label="Copy path", kinds=ANY, multi="all", where="ui")
def copy_path(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    ctx.copy_text("\n".join(str(p) for p in paths))
    return []


@op(id="actions.copy_name", wheel="actions", label="Copy name", kinds=ANY, multi="all", where="ui")
def copy_name(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    ctx.copy_text("\n".join(p.name for p in paths))
    return []


def terminal_command(folder: Path) -> tuple[list[str], int]:
    """(command line, creation flags) for a terminal in ``folder``."""
    wt = shutil.which("wt.exe")  # an app execution alias in WindowsApps when installed
    if wt:
        # wt splits its command line at ";", even inside quotes, unless it is escaped.
        return [wt, "-d", str(folder).replace(";", r"\;")], 0
    # Windows PowerShell 5.1 has no -WorkingDirectory (that is pwsh 7), so cwd= does it.
    return ["powershell.exe", "-NoExit"], subprocess.CREATE_NEW_CONSOLE


@op(id="actions.terminal", wheel="actions", label="Terminal", kinds=ANY, multi="all", where="ui")
def open_terminal(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    first = paths[0]
    folder = first if first.is_dir() else first.parent
    args, flags = terminal_command(folder)
    subprocess.Popen(args, cwd=folder, creationflags=flags)
    return []


# --- Copy to / Move to ---------------------------------------------------------------------------
def _slots(kind: str) -> tuple[str, ...]:
    return tuple(f"actions.{kind}.{n}" for n in range(1, MAX_PINNED + 1))


def folder_label(folder: Path) -> str:
    """What the ring shows: the folder's name, or ``D:`` for a drive."""
    return folder.name or str(folder).rstrip("\\/")


def slot_of(op_id: str) -> int | None:
    """0-based pinned-folder slot of a ring Op (``actions.copy_to.2`` → 1), else None."""
    kind, _, n = op_id.removeprefix("actions.").rpartition(".")
    if kind in ("copy_to", "move_to") and n.isdigit() and 1 <= int(n) <= MAX_PINNED:
        return int(n) - 1
    return None


def params_for(op_id: str) -> dict:
    """The params a ring Op needs: its folder."""
    slot = slot_of(op_id)
    return {"folder": str(_folders[slot])} if slot is not None and slot < len(_folders) else {}


def pin_folders(folders: Sequence[str]) -> None:
    """Set the pinned folders (config ``folders.pinned``) and label the rings with their names.
    Slots past the last folder are unavailable, so they are not shown."""
    global _folders
    _folders = tuple(Path(os.path.expandvars(f)).expanduser() for f in folders[:MAX_PINNED])
    for kind, func in (("copy_to", copy_to), ("move_to", move_to)):
        for slot, op_id in enumerate(_slots(kind)):
            pinned = slot < len(_folders)
            register(
                Op(
                    id=op_id,
                    wheel="actions",
                    label=folder_label(_folders[slot]) if pinned else "",
                    kinds=ANY,
                    func=f"{__name__}:{func.__name__}",
                    available=(lambda: True) if pinned else (lambda: False),
                )
            )


def _target(params: dict) -> Path:
    if not params.get("folder"):
        raise ValueError("No pinned folder: add one in Settings")
    folder = Path(params["folder"])
    if not folder.is_dir():
        raise FileNotFoundError(f"The pinned folder {folder} is not there any more")
    return folder


def _check_into(src: Path, folder: Path) -> None:
    if src.is_dir() and folder.resolve().is_relative_to(src.resolve()):
        raise ValueError(f"Can't put {src.name} inside itself")


def _size(path: Path) -> int:
    if not path.is_dir():
        return path.stat().st_size
    return sum((Path(d) / f).stat().st_size for d, _, files in os.walk(path) for f in files)


class _Copier:
    """Copies files in chunks, reporting progress by bytes and stopping on a cancel."""

    def __init__(self, ctx: "JobContext", total: int, text: str) -> None:
        self.ctx, self.total, self.text, self.done = ctx, max(total, 1), text, 0

    def copy(self, src: str | Path, dst: str | Path) -> None:
        with open(src, "rb") as f, open(dst, "wb") as out:
            while chunk := f.read(CHUNK):
                if self.ctx.cancelled():
                    raise Cancelled
                out.write(chunk)
                self.done += len(chunk)
                self.ctx.progress(self.done / self.total, self.text)
        shutil.copystat(src, dst)


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def _copy_into(src: Path, folder: Path, ctx: "JobContext") -> Path:
    """Copy ``src`` into ``folder`` under a free name, through a ``.part``."""
    part = ctx.reserve(folder / src.name)
    copier = _Copier(ctx, _size(src), src.name)
    try:
        if src.is_dir():
            part.unlink()  # copytree makes the folder itself
            shutil.copytree(src, part, symlinks=True, copy_function=copier.copy)
        else:
            copier.copy(src, part)
    except BaseException:
        _remove(part)
        raise
    final = finalize(part)
    ctx.final.append(final)
    return final


def _rename_into(src: Path, folder: Path) -> Path | None:
    """Move ``src`` into ``folder`` by a rename, under a free name. None across drives."""
    while True:
        dst = free_path(folder / src.name)
        try:
            os.rename(src, dst)  # fails rather than replace on Windows
            return dst
        except FileExistsError:
            continue  # taken since free_path looked
        except OSError as e:
            if getattr(e, "winerror", None) == ERROR_NOT_SAME_DEVICE:
                return None
            raise


def _delete(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


@op(id="actions.copy_to", wheel="actions", label="Copy to ▸", kinds=ANY, children=_slots("copy_to"))
def copy_to(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    folder = _target(params)
    outputs = []
    for src in paths:
        _check_into(src, folder)
        outputs.append(_copy_into(src, folder, ctx))
    return outputs


@op(id="actions.move_to", wheel="actions", label="Move to ▸", kinds=ANY, children=_slots("move_to"))
def move_to(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    folder = _target(params)
    outputs = []
    for src in paths:
        _check_into(src, folder)
        if src.parent.resolve() == folder.resolve():
            raise ValueError(f"{src.name} is in {folder_label(folder)} already")
        moved = _rename_into(src, folder)
        if moved is None:  # another drive: copy, then delete the original
            moved = _copy_into(src, folder, ctx)
            try:
                _delete(src)
            except OSError as e:
                raise OSError(f"Copied, but the original could not be deleted: {e}") from e
        else:
            ctx.final.append(moved)
        ctx.moves.append((src, moved))
        outputs.append(moved)
    return outputs


pin_folders(())  # the ring Ops exist from the start, unavailable until folders are pinned


# --- Zip and SHA-256 -----------------------------------------------------------------------------
@op(id="actions.zip", wheel="actions", label="Zip", kinds=ANY, multi="all")
def zip_items(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    from dragshift.engine.converters import archives

    root = Path(os.path.commonpath([p.parent for p in paths]))  # ValueError across drives
    name = paths[0] if len(paths) == 1 else paths[0].with_name("Archive")
    items: list[Path] = []
    for p in paths:
        items += (archives.files_under(p) or [p]) if p.is_dir() else [p]
    dst = ctx.output_path(name, ext="zip")
    try:
        archives.write_zip(items, root, dst, ctx, name.name)
    except archives.Cancelled:
        return []
    return [dst]


@op(id="actions.sha256", wheel="actions", label="SHA-256", kinds=FILES, multi="all")
def sha256(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    total = max(sum(p.stat().st_size for p in paths), 1)
    done = 0
    lines = []
    for p in paths:
        digest = hashlib.sha256()
        with p.open("rb") as f:
            while chunk := f.read(CHUNK):
                if ctx.cancelled():
                    return []
                digest.update(chunk)
                done += len(chunk)
                ctx.progress(done / total, p.name)
        lines.append(f"{digest.hexdigest()}  {p.name}")
    ctx.copy_text("\n".join(lines))
    return []
