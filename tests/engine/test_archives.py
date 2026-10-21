"""Archive and folder converters (PRD §6.1). Every archive is made here, 7z with tar.exe."""

import gzip
import io
import subprocess
import tarfile
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from dragshift.engine import registry
from dragshift.engine.converters import archives
from dragshift.engine.jobs import JobManager
from dragshift.engine.worker import JobContext, final_path, run_job

FILES = {"a.txt": b"alpha", "sub/b.txt": b"beta", "sub/deeper/c.bin": bytes(range(256))}
MODULE = "dragshift.engine.converters.archives"


def finish(outputs: list[Path]) -> list[Path]:
    """What JobManager does: rename ``.part`` outputs; anything else is already final."""
    return [p.rename(final_path(p)) if p.suffix == ".part" else p for p in outputs]


def tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def make_zip(path: Path, files: dict[str, bytes] = FILES) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return path


def make_tar(path: Path, mode: str = "w:gz", files: dict[str, bytes] = FILES) -> Path:
    with tarfile.open(path, mode) as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


def make_7z(path: Path, tmp: Path, files: dict[str, bytes] = FILES) -> Path:
    src = tmp / "7z-src"
    for name, data in files.items():
        (src / name).parent.mkdir(parents=True, exist_ok=True)
        (src / name).write_bytes(data)
    subprocess.run([str(archives.bsdtar()), "--format", "7zip", "-cf", str(path), "-C", str(src), "."],
                   check=True)  # fmt: skip
    return path


def extract(src: Path) -> list[Path]:
    return finish(archives.extract_archive([src], {}, JobContext("t")))


# --- Extract here ----------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["zip", "tar.gz", "tgz", "tar", "7z"])
def test_several_items_go_into_a_folder_named_after_the_archive(tmp_path: Path, kind: str) -> None:
    folder = tmp_path / "in"
    folder.mkdir()
    src = folder / f"pack.{kind}"
    if kind == "zip":
        make_zip(src)
    elif kind == "7z":
        make_7z(src, tmp_path)
    else:
        make_tar(src, "w" if kind == "tar" else "w:gz")
    before = src.read_bytes()
    (out,) = extract(src)
    assert out == folder / "pack"
    assert tree(out) == FILES
    assert src.read_bytes() == before  # the source is never touched (rule 4)
    assert sorted(p.name for p in folder.iterdir()) == ["pack", f"pack.{kind}"]  # no .part left


def test_one_item_goes_straight_next_to_the_archive(tmp_path: Path) -> None:
    src = make_zip(tmp_path / "report.zip", {"report/x.txt": b"x", "report/y.txt": b"y"})
    (out,) = extract(src)
    assert out == tmp_path / "report"  # not report\report
    assert tree(out) == {"x.txt": b"x", "y.txt": b"y"}


def test_a_plain_gz_is_one_file(tmp_path: Path) -> None:
    src = tmp_path / "log.txt.gz"
    src.write_bytes(gzip.compress(b"lines"))
    (out,) = extract(src)
    assert out == tmp_path / "log.txt"
    assert out.read_bytes() == b"lines"


def test_names_already_taken_are_not_overwritten(tmp_path: Path) -> None:
    src = make_zip(tmp_path / "pack.zip")
    (tmp_path / "pack").mkdir()
    (tmp_path / "pack" / "mine.txt").write_bytes(b"keep")
    (out,) = extract(src)
    assert out.name == "pack (2)"
    assert (tmp_path / "pack" / "mine.txt").read_bytes() == b"keep"


def test_zip_slip_stays_inside(tmp_path: Path) -> None:
    folder = tmp_path / "in"
    folder.mkdir()
    src = make_zip(folder / "evil.zip", {"../../escaped.txt": b"x", "ok.txt": b"y"})
    (out,) = extract(src)
    assert not (tmp_path / "escaped.txt").exists()
    assert sorted(tree(out)) == ["escaped.txt", "ok.txt"]  # zipfile keeps it inside


def test_tar_with_an_escaping_path_is_refused_and_leaves_nothing(tmp_path: Path) -> None:
    folder = tmp_path / "in"
    folder.mkdir()
    src = make_tar(folder / "evil.tar.gz", files={"ok.txt": b"y", "../escaped.txt": b"x"})
    result = run_job(f"{MODULE}:extract_archive", [[src]], {}, JobContext("t"))
    assert not result["ok"] and "outside" in result["message"].lower()
    assert not (tmp_path / "escaped.txt").exists()
    assert sorted(p.name for p in folder.iterdir()) == ["evil.tar.gz"]  # no half folder


def test_a_damaged_archive_fails_and_leaves_nothing(tmp_path: Path) -> None:
    src = tmp_path / "broken.7z"
    src.write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\x00" * 40)
    result = run_job(f"{MODULE}:extract_archive", [[src]], {}, JobContext("t"))
    assert not result["ok"] and result["message"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["broken.7z"]


class CancelAfter:
    """A cancel flag that turns on after ``n`` checks."""

    def __init__(self, n: int) -> None:
        self.n = n

    def __contains__(self, _job: object) -> bool:
        self.n -= 1
        return self.n < 0


def test_cancel_part_way_leaves_nothing(tmp_path: Path) -> None:
    src = make_zip(tmp_path / "pack.zip")
    result = run_job(
        f"{MODULE}:extract_archive", [[src]], {}, JobContext("t", None, CancelAfter(2))
    )
    assert result["message"] == "Cancelled"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["pack.zip"]


# --- repacking -------------------------------------------------------------------------------
def test_tar_gz_to_zip_and_back(tmp_path: Path) -> None:
    src = make_tar(tmp_path / "pack.tar.gz")
    (zipped,) = finish(archives.archive_to_zip([src], {}, JobContext("t")))
    assert zipped.name == "pack.zip"
    with zipfile.ZipFile(zipped) as zf:
        assert {n: zf.read(n) for n in zf.namelist() if not n.endswith("/")} == FILES
    zipped.rename(tmp_path / "again.zip")
    (tgz,) = finish(archives.archive_to_targz([tmp_path / "again.zip"], {}, JobContext("t")))
    assert tgz.name == "again.tar.gz"
    out = tmp_path / "check"
    with tarfile.open(tgz) as tf:
        tf.extractall(out, filter="data")
    assert tree(out) == FILES


def test_7z_to_zip(tmp_path: Path) -> None:
    src = make_7z(tmp_path / "pack.7z", tmp_path)
    (zipped,) = finish(archives.archive_to_zip([src], {}, JobContext("t")))
    with zipfile.ZipFile(zipped) as zf:
        assert {n for n in zf.namelist() if not n.endswith("/")} == set(FILES)


# --- folder -> ZIP ---------------------------------------------------------------------------
def test_a_folder_zips_with_itself_as_the_root(tmp_path: Path) -> None:
    folder = tmp_path / "Holiday"
    for name, data in FILES.items():
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(data)
    (folder / "empty").mkdir()
    (out,) = finish(archives.folder_to_zip([folder], {}, JobContext("t")))
    assert out == tmp_path / "Holiday.zip"
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        assert "Holiday/empty/" in names
        assert {
            n.removeprefix("Holiday/"): zf.read(n) for n in names if not n.endswith("/")
        } == FILES


def test_an_empty_folder_still_zips(tmp_path: Path) -> None:
    (folder := tmp_path / "Nothing").mkdir()
    (out,) = finish(archives.folder_to_zip([folder], {}, JobContext("t")))
    with zipfile.ZipFile(out) as zf:
        assert zf.namelist() == ["Nothing/"]


def test_base_names() -> None:
    assert archives.base_name(Path("x.tar.gz")) == "x"
    assert archives.base_name(Path("x.TGZ")) == "x"
    assert archives.base_name(Path("my.data.zip")) == "my.data"
    assert archives.base_name(Path("log.txt.gz")) == "log.txt"


def test_petals(tmp_path: Path) -> None:
    def labels(*paths: Path) -> list[str]:
        return [o.label for o in registry.ops_for(list(paths), "convert")]

    assert labels(Path("a.zip")) == ["Extract", "TAR.GZ"]
    assert labels(Path("a.tgz")) == ["Extract", "ZIP"]
    assert labels(Path("a.rar")) == ["Extract", "ZIP", "TAR.GZ"]
    assert labels(tmp_path) == ["ZIP"]


# --- through the real worker pool ------------------------------------------------------------
@pytest.fixture
def jm(qtbot) -> Iterator[JobManager]:
    manager = JobManager()
    yield manager
    manager.shutdown()


def test_extract_through_the_pool_renames_the_part_folder(
    qtbot, jm: JobManager, tmp_path: Path
) -> None:
    src = make_zip(tmp_path / "pack.zip")
    with qtbot.waitSignal(jm.finished, timeout=60_000) as blocker:
        jm.submit("convert.archive.extract", [src])
    result = blocker.args[1]
    assert result.ok, result.message
    assert result.outputs == [tmp_path / "pack"]
    assert tree(tmp_path / "pack") == FILES
