"""Archive and folder converters (PRD §6.1 archive and folder rows).

- **Extract here:** one item inside goes next to the archive as it is. Several items go into a
  folder named after the archive, so nothing spills over the folder. If the archive's folder
  cannot be written to, both go to Downloads instead (FR-5.3).
  - ZIP: ``zipfile``. TAR, TAR.GZ, TGZ: ``tarfile`` with the ``data`` filter, which refuses
    absolute paths, ``..`` and links that leave the folder. A ``.gz`` that is not a tar is one
    gzipped file.
  - RAR and 7z: Windows' own ``tar.exe`` (bsdtar/libarchive), by its full path: a GNU tar from
    Git or conda on PATH cannot read them. bsdtar refuses ``..`` and absolute paths by default.
  - It is extracted into a ``<name>.part`` folder that is renamed when complete, or deleted on a
    failure or a cancel. A half-extracted folder is never left behind.
- **ZIP / TAR.GZ:** an archive is extracted to a temporary folder and packed again.
- **Folder -> ZIP:** ``<folder>.zip`` next to it, with the folder itself as the root inside.

All stdlib: nothing heavy, but it runs in the worker like every converter.
"""

import gzip
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

from dragshift.engine.kinds import Kind, normalize_ext
from dragshift.engine.registry import op
from dragshift.engine.worker import PART, free_path

if TYPE_CHECKING:
    from dragshift.engine.worker import JobContext

ARCHIVE = frozenset({Kind.ARCHIVE})
FOLDER = frozenset({Kind.FOLDER})
POLL_S = 0.1  # tar.exe: how often to check for cancel
_TAR_SUFFIXES = (".tar.gz", ".tgz", ".tar", ".gz", ".zip", ".rar", ".7z")


class Cancelled(Exception):
    """The job was cancelled part-way; the caller cleans up."""


def bsdtar() -> Path:
    """Windows' tar.exe (bsdtar), never another tar on PATH."""
    return Path(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "tar.exe")


def base_name(src: Path) -> str:
    """``x.tar.gz`` -> ``x``, ``x.zip`` -> ``x``, ``log.txt.gz`` -> ``log.txt``."""
    name = src.name
    for suffix in _TAR_SUFFIXES:
        if name.lower().endswith(suffix):
            return name[: -len(suffix)] or name
    return src.stem


def _named(src: Path) -> Path:
    """A path whose ``.stem`` is the archive's base name, for ``ctx.output_path``."""
    return src.with_name(base_name(src) + ".x")


# --- extracting ------------------------------------------------------------------------------
def _check(ctx: "JobContext", done: int, total: int, text: str) -> None:
    if ctx.cancelled():
        raise Cancelled
    ctx.progress(done / max(total, 1), text)


def _extract_zip(src: Path, dest: Path, ctx: "JobContext") -> None:
    with zipfile.ZipFile(src) as zf:
        members = zf.infolist()
        for i, member in enumerate(members):
            _check(ctx, i, len(members), member.filename)
            zf.extract(member, dest)  # zipfile drops "..", drive letters and leading slashes


def _extract_tar(src: Path, dest: Path, ctx: "JobContext") -> None:
    if normalize_ext(src.suffix) == "gz" and not tarfile.is_tarfile(src):
        with gzip.open(src) as f_in, (dest / base_name(src)).open("wb") as f_out:  # one file
            shutil.copyfileobj(f_in, f_out)
        return
    with tarfile.open(src) as tf:  # any compression
        members = tf.getmembers()
        for i, member in enumerate(members):
            _check(ctx, i, len(members), member.name)
            tf.extract(member, dest, filter="data")  # refuses paths and links that escape


def _extract_bsdtar(src: Path, dest: Path, ctx: "JobContext") -> None:
    proc = subprocess.Popen(
        [str(bsdtar()), "-x", "-f", str(src), "-C", str(dest)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        while proc.poll() is None:
            if ctx.cancelled():
                raise Cancelled
            time.sleep(POLL_S)
    finally:
        if proc.poll() is None:
            proc.kill()  # let go of the files before the folder is deleted
            proc.wait()
    err = proc.stderr.read().decode("utf-8", "replace").strip() if proc.stderr else ""
    if proc.returncode != 0:
        raise RuntimeError(err.splitlines()[-1] if err else f"tar.exe failed ({proc.returncode})")


def extract(src: Path, dest: Path, ctx: "JobContext") -> None:
    """Everything in ``src`` into the existing folder ``dest``."""
    ext = normalize_ext(src.suffix)
    if ext == "zip":
        _extract_zip(src, dest, ctx)
    elif ext in ("tar", "gz", "tgz"):
        _extract_tar(src, dest, ctx)
    else:  # rar, 7z
        _extract_bsdtar(src, dest, ctx)


def _reserve_dir(target: Path, ctx: "JobContext") -> Path:
    """Create and return a free ``<target>.part`` folder (``x (2).part`` if ``x`` is taken), in
    Downloads if ``target``'s folder cannot be written to (FR-5.3)."""
    try:
        return _free_part_dir(target)
    except PermissionError:
        return _free_part_dir(ctx.redirect(target))


def _free_part_dir(target: Path) -> Path:
    n = 1
    while True:
        name = target.name if n == 1 else f"{target.name} ({n})"  # "my.data (2)", not "my (2).data"
        if not target.with_name(name).exists():
            try:
                (part := target.with_name(name + PART)).mkdir()
                return part
            except FileExistsError:  # another job is extracting to the same name
                pass
        n += 1


def extract_here(src: Path, ctx: "JobContext") -> Path | None:
    """Extract next to ``src``. Returns the item or the ``.part`` folder (JobManager renames
    it), or None if cancelled."""
    part = _reserve_dir(ctx.out_dir(src) / base_name(src), ctx)
    try:
        extract(src, part, ctx)
        entries = list(part.iterdir())
        if len(entries) == 1:  # one item: it goes next to the archive, no extra folder
            only = entries[0]
            target = free_path(part.parent / only.name)  # part.parent: maybe Downloads
            only.rename(target)
            part.rmdir()
            return target
        return part
    except Cancelled:
        shutil.rmtree(part, ignore_errors=True)
        return None
    except BaseException:
        shutil.rmtree(part, ignore_errors=True)
        raise


# --- packing ---------------------------------------------------------------------------------
def files_under(root: Path) -> list[Path]:
    """Every file and empty folder below ``root``, in a stable order, without following links."""
    found = []
    for folder, dirs, files in os.walk(root, followlinks=False):
        dirs.sort()
        here = Path(folder)
        if not dirs and not files and here != root:
            found.append(here)  # keep empty folders
        found += [here / f for f in sorted(files)]
    return found


def write_zip(items: list[Path], arc_root: Path, dst: Path, ctx: "JobContext", text: str) -> None:
    """Zip ``items`` (files and empty folders) with names relative to ``arc_root``."""
    # strict_timestamps=False: files dated before 1980 (tars often say 1970) are clamped, not refused
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, strict_timestamps=False) as zf:
        for i, path in enumerate(items):
            _check(ctx, i, len(items), text)
            name = path.relative_to(arc_root).as_posix()
            if path.is_dir():
                zf.writestr(name + "/", b"")
            else:
                zf.write(path, name)


def write_targz(root: Path, dst: Path, ctx: "JobContext", text: str) -> None:
    """Everything inside ``root`` into a gzipped tar, without ``root`` itself."""
    entries = sorted(root.iterdir())
    with tarfile.open(dst, "w:gz") as tf:
        for i, entry in enumerate(entries):
            _check(ctx, i, len(entries), text)
            tf.add(entry, arcname=entry.name)


def _repack(paths: list[Path], ctx: "JobContext", ext: str, label_text: str) -> list[Path]:
    outputs = []
    for src in paths:
        dst = ctx.output_path(_named(src), ext=ext)
        try:
            with tempfile.TemporaryDirectory(prefix="dragshift-") as tmp:
                root = Path(tmp)
                extract(src, root, ctx)
                if ext == "zip":
                    write_zip(files_under(root), root, dst, ctx, label_text)
                else:
                    write_targz(root, dst, ctx, label_text)
        except Cancelled:
            return []  # run_job deletes the reserved .part
        outputs.append(dst)
    return outputs


# --- Ops, in default petal order (§6.1) ------------------------------------------------------
@op(
    id="convert.archive.extract", wheel="convert", label="Extract", kinds=ARCHIVE
)  # "Extract here" (§6.1) is cut off at 72 px
def extract_archive(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    outputs = []
    for src in paths:
        out = extract_here(src, ctx)
        if out is None:
            return outputs  # cancelled: run_job reports it
        outputs.append(out)
    return outputs


@op(id="convert.archive.zip", wheel="convert", label="ZIP", kinds=ARCHIVE, hide_if_same_ext="zip")
def archive_to_zip(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _repack(paths, ctx, "zip", "Packing ZIP")


@op(
    id="convert.archive.targz",
    wheel="convert",
    label="TAR.GZ",
    kinds=ARCHIVE,
    hide_if_same_ext="gz",
)
def archive_to_targz(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    return _repack(paths, ctx, "tar.gz", "Packing TAR.GZ")


@op(id="convert.folder.zip", wheel="convert", label="ZIP", kinds=FOLDER)
def folder_to_zip(paths: list[Path], params: dict, ctx: "JobContext") -> list[Path]:
    outputs = []
    for folder in paths:
        dst = ctx.output_path(folder, ext="zip")
        try:
            items = files_under(folder) or [folder]  # an empty folder is still in the zip
            write_zip(items, folder.parent, dst, ctx, folder.name)
        except Cancelled:
            return []
        outputs.append(dst)
    return outputs
