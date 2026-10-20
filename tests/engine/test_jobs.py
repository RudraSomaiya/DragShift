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

from dragshift.engine import registry
from dragshift.engine.jobs import JobManager, JobResult, _Job
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
    assert first.undo == {"type": "trash", "paths": first.outputs}
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
    assert res.undo == {"type": "trash", "paths": [tmp_path / "a.jpg"]}


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
