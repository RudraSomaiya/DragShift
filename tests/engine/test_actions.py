"""The Actions wheel (PRD §6.3): clipboard, terminal, Copy to / Move to with Undo, Zip, SHA-256."""

import hashlib
import os
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from dragshift.engine import registry
from dragshift.engine.actions import files as actions
from dragshift.engine.jobs import JobManager, JobResult, apply_undo
from dragshift.engine.worker import ERROR_NOT_SAME_DEVICE, JobContext, final_path, run_job

MODULE = "dragshift.engine.actions.files"
POOL_TIMEOUT_MS = 60_000


@pytest.fixture
def pinned() -> Iterator[None]:
    actions.pin_folders(["C:/Work/Projects", "D:/"])
    yield
    actions.pin_folders(())


@pytest.fixture
def jm(qtbot) -> Iterator[JobManager]:
    manager = JobManager()
    yield manager
    manager.shutdown()


def run(func: str, paths: list[Path], params: dict | None = None, ctx: JobContext | None = None):
    return run_job(f"{MODULE}:{func}", [paths], params or {}, ctx or JobContext("job"))


def make(path: Path, data: bytes = b"data") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def folders(tmp_path: Path) -> tuple[Path, Path]:
    src, dest = tmp_path / "src", tmp_path / "dest"
    src.mkdir()
    dest.mkdir()
    return src, dest


# --- registry -------------------------------------------------------------------------------
def test_the_wheel_takes_any_mix_of_files_and_folders(tmp_path: Path) -> None:
    ids = [o.id for o in registry.ops_for([tmp_path / "a.png", tmp_path / "b.mp3"], "actions")]
    assert ids == [
        "actions.copy_path",
        "actions.copy_name",
        "actions.terminal",
        "actions.zip",
        "actions.sha256",
    ]
    with_folder = [o.id for o in registry.ops_for([tmp_path, tmp_path / "a.png"], "actions")]
    assert "actions.sha256" not in with_folder  # a folder has no hash
    assert "actions.zip" in with_folder


def test_copy_to_and_move_to_appear_with_pinned_folders_and_ring_them(pinned: None) -> None:
    ops = registry.ops_for([Path("a.png")], "actions")
    labels = [o.label for o in ops]
    assert "Copy to ▸" in labels and "Move to ▸" in labels
    assert not any(o.id.startswith("actions.copy_to.") for o in ops)  # only in the ring
    copy_to = registry.get("actions.copy_to")
    ring = registry.children_for(copy_to, [Path("a.png")])
    assert [(o.id, o.label) for o in ring] == [
        ("actions.copy_to.1", "Projects"),
        ("actions.copy_to.2", "D:"),
    ]
    assert actions.params_for("actions.move_to.2") == {"folder": str(Path("D:/"))}
    assert actions.params_for("actions.copy_path") == {}


def test_only_four_folders_are_pinned() -> None:
    try:
        actions.pin_folders([f"C:/f{i}" for i in range(6)])
        ring = registry.children_for(registry.get("actions.move_to"), [Path("a.txt")])
        assert [o.label for o in ring] == ["f0", "f1", "f2", "f3"]
    finally:
        actions.pin_folders(())


# --- clipboard and terminal -----------------------------------------------------------------
def test_copy_path_and_copy_name_put_one_line_per_item(tmp_path: Path) -> None:
    paths = [tmp_path / "a.png", tmp_path / "sub"]
    assert run("copy_path", paths)["copied"] == f"{paths[0]}\n{paths[1]}"
    result = run("copy_name", paths)
    assert result["copied"] == "a.png\nsub"
    assert result["ok"] and result["outputs"] == []


def test_the_terminal_opens_in_the_folder_or_the_files_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[tuple] = []
    monkeypatch.setattr(actions.subprocess, "Popen", lambda args, **kw: started.append((args, kw)))
    monkeypatch.setattr(actions.shutil, "which", lambda _name: "C:/wt.exe")
    run("open_terminal", [make(tmp_path / "a.txt")])
    run("open_terminal", [tmp_path])
    assert [s[1]["cwd"] for s in started] == [tmp_path, tmp_path]
    assert started[0][0] == ["C:/wt.exe", "-d", str(tmp_path)]


def test_without_windows_terminal_it_is_powershell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(actions.shutil, "which", lambda _name: None)
    args, flags = actions.terminal_command(Path("C:/x"))
    assert args == ["powershell.exe", "-NoExit"]
    assert flags == actions.subprocess.CREATE_NEW_CONSOLE


def test_wt_gets_semicolons_escaped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(actions.shutil, "which", lambda _name: "wt.exe")
    assert actions.terminal_command(Path("C:/a;b"))[0][-1] == r"C:\a\;b"


# --- Copy to --------------------------------------------------------------------------------
def test_copy_to_copies_under_a_free_name_and_leaves_the_source(tmp_path: Path) -> None:
    src, dest = folders(tmp_path)
    a = make(src / "a.txt", b"new")
    make(dest / "a.txt", b"old")
    before = a.stat().st_mtime_ns
    result = run("copy_to", [a], {"folder": str(dest)})
    assert result["ok"], result["message"]
    assert result["outputs"] == result["final"] == [dest / "a (2).txt"]
    assert (dest / "a (2).txt").read_bytes() == b"new"
    assert (dest / "a.txt").read_bytes() == b"old"  # never overwritten (FR-5.2)
    assert a.read_bytes() == b"new" and a.stat().st_mtime_ns == before
    assert not list(dest.glob("*.part"))


def test_copy_to_copies_a_folder_with_everything_in_it(tmp_path: Path) -> None:
    src, dest = folders(tmp_path)
    make(src / "album" / "x.jpg", b"x")
    make(src / "album" / "deep" / "y.jpg", b"y")
    (src / "album" / "empty").mkdir()
    result = run("copy_to", [src / "album"], {"folder": str(dest)})
    assert result["outputs"] == [dest / "album"]
    assert (dest / "album" / "deep" / "y.jpg").read_bytes() == b"y"
    assert (dest / "album" / "empty").is_dir()
    assert (src / "album" / "x.jpg").exists()


def test_a_folder_is_never_put_inside_itself(tmp_path: Path) -> None:
    inner = tmp_path / "album" / "inner"
    inner.mkdir(parents=True)
    for func in ("copy_to", "move_to"):
        result = run(func, [tmp_path / "album"], {"folder": str(inner)})
        assert not result["ok"] and "inside itself" in result["message"]
    assert list(inner.iterdir()) == []


def test_a_pinned_folder_that_is_gone_is_not_recreated(tmp_path: Path) -> None:
    a = make(tmp_path / "a.txt")
    result = run("copy_to", [a], {"folder": str(tmp_path / "gone")})
    assert not result["ok"] and "not there any more" in result["message"]
    assert not (tmp_path / "gone").exists()
    assert "No pinned folder" in run("copy_to", [a])["message"]


def test_a_cancelled_copy_leaves_nothing(tmp_path: Path) -> None:
    src, dest = folders(tmp_path)
    a = make(src / "big.bin", os.urandom(3 * actions.CHUNK))
    ctx = JobContext("job", None, {"job"})  # cancelled from the start
    result = run("copy_to", [a], {"folder": str(dest)}, ctx)
    assert result.get("cancelled")
    assert list(dest.iterdir()) == []
    assert a.stat().st_size == 3 * actions.CHUNK


def other_drive(*_args: object) -> None:
    """os.rename across drives."""
    raise OSError(18, "not the same device", None, ERROR_NOT_SAME_DEVICE)


# --- Move to --------------------------------------------------------------------------------
def test_move_to_renames_into_the_folder_and_records_the_move(tmp_path: Path) -> None:
    src, dest = folders(tmp_path)
    a = make(src / "a.txt", b"mine")
    make(dest / "a.txt", b"other")
    result = run("move_to", [a], {"folder": str(dest)})
    assert result["ok"], result["message"]
    assert result["outputs"] == result["final"] == [dest / "a (2).txt"]
    assert result["moves"] == [[a, dest / "a (2).txt"]]
    assert not a.exists()
    assert (dest / "a (2).txt").read_bytes() == b"mine"
    assert (dest / "a.txt").read_bytes() == b"other"


def test_a_rename_across_drives_is_reported_as_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(actions.os, "rename", other_drive)
    assert actions._rename_into(make(tmp_path / "a.txt"), tmp_path / "x") is None


def test_moving_into_the_folder_it_is_in_does_nothing(tmp_path: Path) -> None:
    a = make(tmp_path / "a.txt")
    result = run("move_to", [a], {"folder": str(tmp_path)})
    assert not result["ok"] and "already" in result["message"]
    assert a.exists() and not (tmp_path / "a (2).txt").exists()


def test_across_drives_move_copies_then_deletes_the_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src, dest = folders(tmp_path)
    a = make(src / "a.txt", b"mine")
    make(src / "album" / "x.jpg", b"x")

    monkeypatch.setattr(actions, "_rename_into", lambda *_: None)  # another drive
    result = run("move_to", [a], {"folder": str(dest)})
    assert result["ok"], result["message"]
    assert not a.exists() and (dest / "a.txt").read_bytes() == b"mine"
    result = run("move_to", [src / "album"], {"folder": str(dest)})
    assert result["ok"], result["message"]
    assert not (src / "album").exists() and (dest / "album" / "x.jpg").read_bytes() == b"x"


def test_a_cancelled_move_across_drives_keeps_the_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src, dest = folders(tmp_path)
    a = make(src / "a.txt", b"mine")
    monkeypatch.setattr(actions, "_rename_into", lambda *_: None)  # another drive
    result = run("move_to", [a], {"folder": str(dest)}, JobContext("job", None, {"job"}))
    assert result.get("cancelled")
    assert a.read_bytes() == b"mine" and list(dest.iterdir()) == []


# --- Undo (FR-5.6) --------------------------------------------------------------------------
def test_undo_moves_it_back(tmp_path: Path) -> None:
    src, dest = folders(tmp_path)
    moved = make(dest / "a.txt", b"mine")
    origin = src / "a.txt"
    kept = apply_undo({"type": "move", "moves": [[str(origin), str(moved)]]})
    assert kept == []
    assert origin.read_bytes() == b"mine" and not moved.exists()


def test_undo_keeps_a_move_whose_old_place_is_taken_or_that_changed(tmp_path: Path) -> None:
    from dragshift.engine.jobs import stamp

    src, dest = folders(tmp_path)
    taken = make(dest / "a.txt", b"moved")
    make(src / "a.txt", b"new file in the old place")
    changed = make(dest / "b.txt", b"moved")
    old_stamp = stamp(changed)
    changed.write_bytes(b"edited since the move")
    undo = {
        "type": "move",
        "moves": [[str(src / "a.txt"), str(taken)], [str(src / "b.txt"), str(changed)]],
        "stamps": [stamp(taken), old_stamp],
    }
    assert apply_undo(undo) == [taken, changed]
    assert (src / "a.txt").read_bytes() == b"new file in the old place"
    assert not (src / "b.txt").exists()


# --- through the JobManager -----------------------------------------------------------------
def submit(qtbot, jm: JobManager, op_id: str, paths: list[Path], **params) -> JobResult:
    with qtbot.waitSignal(jm.finished, timeout=POOL_TIMEOUT_MS) as blocker:
        jm.submit(op_id, paths, params)
    return blocker.args[1]


def test_a_move_through_the_pool_is_undone_by_moving_back(
    qtbot, jm: JobManager, tmp_path: Path, pinned: None
) -> None:
    src, dest = folders(tmp_path)
    a = make(src / "draft.part", b"a partial download")  # its name must survive the move
    result = submit(qtbot, jm, "actions.move_to.1", [a], folder=str(dest))
    assert result.ok, result.message
    assert result.outputs == [dest / "draft.part"]
    assert result.undo["type"] == "move"
    assert not a.exists()
    assert apply_undo(result.undo) == []
    assert a.read_bytes() == b"a partial download"
    assert list(dest.iterdir()) == []


def test_a_copy_through_the_pool_is_undone_by_trashing_the_copy(
    qtbot, jm: JobManager, tmp_path: Path, pinned: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dragshift.engine import jobs

    trashed: list[Path] = []
    monkeypatch.setattr(jobs, "_trash", trashed.extend)
    src, dest = folders(tmp_path)
    a = make(src / "a.txt")
    result = submit(qtbot, jm, "actions.copy_to.1", [a], folder=str(dest))
    assert result.ok, result.message
    assert result.undo["type"] == "trash"
    apply_undo(result.undo)
    assert trashed == [dest / "a.txt"]


def test_copy_path_reports_what_it_copied(qtbot, jm: JobManager, tmp_path: Path) -> None:
    result = submit(qtbot, jm, "actions.copy_path", [tmp_path / "a.png"])
    assert result.ok and result.copied == str(tmp_path / "a.png")
    assert result.outputs == [] and result.undo == {}


def test_an_output_without_an_extension_gets_no_trailing_dot(tmp_path: Path) -> None:
    part = JobContext("job").output_path(make(tmp_path / "README"), ext="", folder=tmp_path / "o")
    assert part.name == "README.part"


# --- Zip and SHA-256 ------------------------------------------------------------------------
def test_zip_one_item_is_named_after_it(tmp_path: Path) -> None:
    make(tmp_path / "album" / "x.jpg", b"x")
    result = run("zip_items", [tmp_path / "album"])
    (out,) = result["outputs"]
    zipped = out.rename(final_path(out))
    assert zipped.name == "album.zip"
    assert zipfile.ZipFile(zipped).namelist() == ["album/x.jpg"]


def test_zip_several_items_is_an_archive_next_to_them(tmp_path: Path) -> None:
    a = make(tmp_path / "a.txt", b"a")
    make(tmp_path / "album" / "x.jpg", b"x")
    (out,) = run("zip_items", [a, tmp_path / "album"])["outputs"]
    zipped = out.rename(final_path(out))
    assert zipped == tmp_path / "Archive.zip"
    assert sorted(zipfile.ZipFile(zipped).namelist()) == ["a.txt", "album/x.jpg"]
    assert a.read_bytes() == b"a"


def test_sha256_lines_match_sha256sum(tmp_path: Path) -> None:
    a = make(tmp_path / "a.bin", os.urandom(actions.CHUNK + 5))
    b = make(tmp_path / "b.txt", b"")
    result = run("sha256", [a, b])
    want = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}" for p in (a, b)]
    assert result["copied"] == "\n".join(want)
