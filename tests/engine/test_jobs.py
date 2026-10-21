"""JobManager: the real spawn pool end to end, idle shutdown, multi modes, cancel, progress."""

import hashlib
import multiprocessing
import os
import time
from collections.abc import Iterator
from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import pytest
from PIL import Image

from dragshift.engine import jobs as jobs_module
from dragshift.engine import registry
from dragshift.engine.jobs import (
    CANCELLED,
    REDIRECTED,
    JobManager,
    JobResult,
    _Job,
    apply_undo,
    stamp,
)
from dragshift.engine.kinds import Kind
from dragshift.engine.registry import Op, register

POOL_TIMEOUT_MS = 60_000  # first job spawns Python workers; CI runners can be slow


@pytest.fixture
def jm(qtbot) -> Iterator[JobManager]:
    manager = JobManager()
    yield manager
    manager.shutdown()


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def run(qtbot, jm: JobManager, op_id: str, paths: list[Path], **params) -> JobResult:
    with qtbot.waitSignal(jm.finished, timeout=POOL_TIMEOUT_MS) as blocker:
        job_id = jm.submit(op_id, paths, params)
    assert blocker.args[0] == job_id
    return blocker.args[1]


def test_png_to_jpg_end_to_end_then_pool_shuts_down_when_idle(
    qtbot, jm: JobManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(JobManager, "IDLE_SHUTDOWN_MS", 300)
    src = tmp_path / "photo.png"
    im = Image.new("RGBA", (64, 48), (255, 0, 0, 255))
    im.paste((0, 0, 0, 0), (0, 0, 16, 16))  # transparent corner
    im.save(src)
    before = sha256(src)
    assert not jm.pool_active  # lazy: no workers before the first job

    t0 = time.perf_counter()
    first = run(qtbot, jm, "convert.image.jpg", [src])
    print(f"first job incl. pool start: {(time.perf_counter() - t0) * 1e3:.0f} ms")
    second = run(qtbot, jm, "convert.image.jpg", [src])

    assert first.ok, first.message
    assert first.outputs == [tmp_path / "photo.jpg"]
    assert first.undo == {
        "type": "trash",
        "paths": first.outputs,
        "stamps": [stamp(first.outputs[0])],  # Undo recognises the file it wrote
    }
    assert second.outputs == [tmp_path / "photo (2).jpg"]  # never overwrites (FR-5.2)
    with Image.open(first.outputs[0]) as out:
        assert out.format == "JPEG" and out.size == (64, 48)
        r, g, b = out.convert("RGB").getpixel((4, 4))
        assert min(r, g, b) > 240  # alpha flattened on white
    assert sha256(src) == before  # source untouched (rule 4)
    assert not list(tmp_path.glob("*.part"))

    qtbot.waitUntil(lambda: not jm.pool_active, timeout=5_000)
    qtbot.waitUntil(lambda: not multiprocessing.active_children(), timeout=10_000)


def test_worker_failure_reports_and_leaves_no_part(qtbot, jm: JobManager, tmp_path: Path) -> None:
    bad = tmp_path / "broken.png"
    bad.write_bytes(b"not an image")
    res = run(qtbot, jm, "convert.image.jpg", [bad])
    assert not res.ok and "UnidentifiedImageError" in res.message
    assert res.outputs == [] and res.undo == {}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["broken.png"]


def crash(paths, params, ctx):
    import os

    os._exit(1)  # simulate a hard worker crash (segfault in a native lib, OOM kill, …)


def test_worker_crash_fails_the_job_and_next_job_gets_a_fresh_pool(
    qtbot, jm: JobManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "a.png"
    Image.new("RGB", (8, 8)).save(src)
    crash_op = Op("t.crash", "tools", "Crash", frozenset({Kind.IMAGE}), f"{__name__}:crash")
    monkeypatch.setitem(registry._OPS, crash_op.id, crash_op)

    res = run(qtbot, jm, "t.crash", [src])
    assert not res.ok and res.message == "The worker process crashed"
    assert run(qtbot, jm, "convert.image.jpg", [src]).ok


def test_submit_blocks_the_qt_thread_only_briefly(qtbot, jm: JobManager, tmp_path: Path) -> None:
    src = tmp_path / "a.png"
    Image.new("RGB", (8, 8)).save(src)
    with qtbot.waitSignal(jm.finished, timeout=POOL_TIMEOUT_MS):  # don't abandon the job
        t0 = time.perf_counter()
        jm.submit("convert.image.jpg", [src])
        blocked = time.perf_counter() - t0
    # Starting the pool must not block the UI. With a Manager process it was ~600 ms; locally
    # it is now ~10-30 ms. The limit leaves room for slow CI runners.
    assert blocked < 0.5, f"submit() blocked {blocked * 1e3:.0f} ms"


def test_stale_crash_does_not_stop_the_new_pool(jm: JobManager, tmp_path: Path) -> None:
    jm._ensure_pool()
    stale: Future = Future()
    stale.set_exception(BrokenProcessPool("old pool died"))
    res = jm._result(_Job("convert.image.jpg", [], stale, pool=object()))
    assert res.message == "The worker process crashed"
    assert jm.pool_active  # the current pool was left alone


def test_a_failed_rename_keeps_the_other_outputs(jm: JobManager, tmp_path: Path) -> None:
    good = tmp_path / "a.jpg.part"
    good.write_bytes(b"x")
    missing = tmp_path / "gone" / "b.jpg.part"  # rename fails: the part is not there
    done: Future = Future()
    done.set_result({"ok": True, "outputs": [good, missing], "message": ""})
    res = jm._result(_Job("convert.image.jpg", [], done, pool=None))
    assert res.outputs == [tmp_path / "a.jpg"]
    assert not res.ok and res.message.startswith("Could not save b.jpg")
    assert res.undo["paths"] == [tmp_path / "a.jpg"]


def report_progress(paths, params, ctx):
    for i in range(1, 21):
        ctx.progress(i / 20, f"step {i}")
    return []


def wait_for_cancel(paths, params, ctx):
    ctx.output_path(paths[0], ext="txt")  # a .part next to the source
    deadline = time.monotonic() + 30
    while not ctx.cancelled() and time.monotonic() < deadline:
        time.sleep(0.02)
    return []


def worker_op(monkeypatch: pytest.MonkeyPatch, name: str) -> str:
    o = Op(f"t.{name}", "tools", name, frozenset({Kind.IMAGE}), f"{__name__}:{name}")
    monkeypatch.setitem(registry._OPS, o.id, o)
    return o.id


def test_pool_progress_all_arrives_before_finished(
    qtbot, jm: JobManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "a.png"
    Image.new("RGB", (8, 8)).save(src)
    events: list[tuple] = []
    jm.progressed.connect(lambda _id, fraction, _text: events.append(("p", fraction)))
    jm.finished.connect(lambda *_: events.append(("done",)))
    run(qtbot, jm, worker_op(monkeypatch, "report_progress"), [src])
    assert events[-1] == ("done",)
    assert [e[1] for e in events[:-1]] == [i / 20 for i in range(1, 21)]


def test_shutdown_cancels_running_jobs_so_no_part_is_left(
    qtbot, jm: JobManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "a.png"
    Image.new("RGB", (8, 8)).save(src)
    jm.submit(worker_op(monkeypatch, "wait_for_cancel"), [src])
    part = tmp_path / "a.txt.part"
    qtbot.waitUntil(part.exists, timeout=POOL_TIMEOUT_MS)
    jm.shutdown()  # app quit while the job is running
    qtbot.waitUntil(lambda: not part.exists(), timeout=10_000)
    qtbot.waitUntil(lambda: not multiprocessing.active_children(), timeout=10_000)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.png"]


def test_stale_cancel_flags_are_pruned_on_pool_start(jm: JobManager, cancel_dir: Path) -> None:
    old, fresh = cancel_dir / "old-job", cancel_dir / "fresh-job"
    old.touch()
    fresh.touch()
    stale = time.time() - JobManager.STALE_FLAG_S - 60
    os.utime(old, (stale, stale))
    jm._ensure_pool()
    assert not old.exists() and fresh.exists()


# --- "ui" ops run inline, which tests grouping, cancel and progress without spawning ----------
CALLS: list[list[str]] = []
PATHS = [Path("a.png"), Path("b.png")]


def record(paths, params, ctx):
    CALLS.append([p.name for p in paths])
    ctx.progress(1.0, "done")
    return []


@pytest.fixture
def ui_ops(clean_registry) -> None:
    CALLS.clear()
    func = f"{__name__}:record"
    kinds = frozenset({Kind.IMAGE})
    register(Op("t.each", "tools", "Each", kinds, func, multi="each", where="ui"))
    register(Op("t.all", "tools", "All", kinds, func, multi="all", where="ui"))


@pytest.mark.usefixtures("ui_ops")
class TestInlineJobs:
    def test_multi_each_runs_once_per_file(self, qtbot, jm: JobManager) -> None:
        assert run(qtbot, jm, "t.each", PATHS).ok
        assert CALLS == [["a.png"], ["b.png"]]
        assert not jm.pool_active  # ui ops never start the pool

    def test_multi_all_runs_once_over_all_files(self, qtbot, jm: JobManager) -> None:
        assert run(qtbot, jm, "t.all", PATHS).ok
        assert CALLS == [["a.png", "b.png"]]

    def test_progress_is_signalled(self, qtbot, jm: JobManager) -> None:
        seen: list[tuple] = []
        jm.progressed.connect(lambda *a: seen.append(a))
        with qtbot.waitSignal(jm.finished):
            job_id = jm.submit("t.each", PATHS)
        assert seen == [(job_id, 0.5, "done"), (job_id, 1.0, "done")]

    def test_cancel_before_start(self, qtbot, jm: JobManager) -> None:
        with qtbot.waitSignal(jm.finished) as blocker:
            jm.cancel(jm.submit("t.each", PATHS))
        assert blocker.args[1].message == "Cancelled"
        assert CALLS == []

    def test_unknown_op(self, jm: JobManager) -> None:
        with pytest.raises(KeyError):
            jm.submit("nope", PATHS)

    def test_files_the_op_does_not_accept(self, jm: JobManager) -> None:
        with pytest.raises(ValueError, match="does not accept"):
            jm.submit("t.each", [Path("a.pdf")])
        with pytest.raises(ValueError, match="does not accept"):
            jm.submit("t.each", [])


# --- FR-5.3 Downloads fallback and FR-5.6 Undo -------------------------------------------------
def test_read_only_source_folder_saves_to_downloads(
    qtbot, jm: JobManager, tmp_path: Path, make_read_only, downloads: Path
) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    src = locked / "photo.png"
    Image.new("RGB", (8, 8)).save(src)
    make_read_only(locked)
    res = run(qtbot, jm, "convert.image.jpg", [src])
    assert res.ok, res.message  # a note, not a failure
    assert res.message == REDIRECTED
    assert res.outputs == [downloads / "photo.jpg"]
    assert sorted(p.name for p in locked.iterdir()) == ["photo.png"]


def test_a_note_does_not_fail_the_job(jm: JobManager, tmp_path: Path) -> None:
    part = tmp_path / "a.jpg.part"
    part.write_bytes(b"x")
    done: Future = Future()
    done.set_result({"ok": True, "outputs": [part], "message": "", "redirected": True})
    res = jm._result(_Job("convert.image.jpg", [], done, pool=None))
    assert res.ok and res.message == REDIRECTED


def test_undo_lists_the_output_folders(jm: JobManager, tmp_path: Path) -> None:
    pages = tmp_path / "doc pages"
    pages.mkdir()
    part = pages / "doc 001.png.part"
    part.write_bytes(b"x")
    done: Future = Future()
    done.set_result({"ok": True, "outputs": [part], "message": "", "dirs": [pages]})
    res = jm._result(_Job("convert.pdf.png", [], done, pool=None))
    assert res.undo["paths"] == [pages / "doc 001.png"] and res.undo["dirs"] == [pages]


@pytest.fixture
def trashed(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """What Undo sends to the Recycle Bin; the files are deleted instead of really recycled."""
    got: list[Path] = []

    def fake(paths: list[Path]) -> None:
        got.extend(paths)
        for p in paths:
            p.unlink()

    monkeypatch.setattr(jobs_module, "_trash", fake)
    return got


def test_apply_undo_trashes_outputs_and_removes_empty_folders(
    tmp_path: Path, trashed: list[Path]
) -> None:
    pages = tmp_path / "doc pages"
    pages.mkdir()
    out = pages / "doc 001.png"
    out.write_bytes(b"x")
    gone = tmp_path / "already deleted.png"
    apply_undo({"type": "trash", "paths": [str(out), str(gone)], "dirs": [str(pages)]})
    assert trashed == [out]  # the missing one is skipped
    assert not pages.exists()


def test_apply_undo_keeps_a_folder_the_user_added_to(tmp_path: Path, trashed: list[Path]) -> None:
    pages = tmp_path / "doc pages"
    pages.mkdir()
    (pages / "doc 001.png").write_bytes(b"x")
    (pages / "notes.txt").write_bytes(b"mine")
    apply_undo({"type": "trash", "paths": [pages / "doc 001.png"], "dirs": [pages]})
    assert sorted(p.name for p in pages.iterdir()) == ["notes.txt"]


def test_apply_undo_rejects_an_unknown_type() -> None:
    with pytest.raises(ValueError, match="cannot undo"):
        apply_undo({"type": "move"})


def test_undo_a_finished_job(qtbot, jm: JobManager, tmp_path: Path, trashed: list[Path]) -> None:
    src = tmp_path / "photo.png"
    Image.new("RGB", (8, 8)).save(src)
    res = run(qtbot, jm, "convert.image.jpg", [src])
    assert apply_undo(res.undo) == []
    assert trashed == [tmp_path / "photo.jpg"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["photo.png"]  # the source stays


def test_undo_keeps_a_file_saved_later_under_the_output_s_name(
    tmp_path: Path, trashed: list[Path]
) -> None:
    out = tmp_path / "photo.jpg"
    out.write_bytes(b"what the job wrote")
    undo = {"type": "trash", "paths": [out], "stamps": [stamp(out)]}
    out.unlink()  # the user deleted it ...
    out.write_bytes(b"... and later saved their own photo.jpg")
    assert apply_undo(undo) == [out]
    assert trashed == [] and out.exists()


def test_undo_refuses_drives_without_a_recycle_bin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dragshift.platform import files

    monkeypatch.setattr(files, "has_recycle_bin", lambda _p: False)  # a network share
    out = tmp_path / "a.jpg"
    out.write_bytes(b"x")
    with pytest.raises(OSError, match="no Recycle Bin"):
        apply_undo({"type": "trash", "paths": [out]})
    assert out.exists()  # never deleted for good


def test_a_local_drive_has_a_recycle_bin(tmp_path: Path) -> None:
    from dragshift.platform.files import has_recycle_bin

    assert has_recycle_bin(tmp_path)


def test_a_cancelled_job_says_so_in_a_field(qtbot, jm: JobManager, tmp_path: Path) -> None:
    done: Future = Future()
    done.set_result({"ok": False, "outputs": [], "message": CANCELLED, "cancelled": True})
    assert jm._result(_Job("convert.image.jpg", [], done, pool=None)).cancelled
    never: Future = Future()
    never.cancel()
    assert jm._result(_Job("convert.image.jpg", [], never, pool=None)).cancelled
    failed: Future = Future()
    failed.set_result({"ok": False, "outputs": [], "message": "Cancelled"})  # just the text
    assert not jm._result(_Job("convert.image.jpg", [], failed, pool=None)).cancelled
