"""Worker-side code run in-process: output naming (FR-5.1, FR-5.2) and run_job cleanup."""

import queue
from pathlib import Path

import pytest

from dragshift.engine.worker import CancelDir, JobContext, final_path, free_path, run_job


def src_file(tmp_path: Path, name: str = "Page 001.png") -> Path:
    p = tmp_path / name
    p.write_bytes(b"source")
    return p


class TestOutputPath:
    def test_convert_name_is_stem_dot_ext_part(self, tmp_path: Path) -> None:
        part = JobContext("j").output_path(src_file(tmp_path), ext="jpg")
        assert part == tmp_path / "Page 001.jpg.part"
        assert part.exists()  # reserved
        assert final_path(part) == tmp_path / "Page 001.jpg"

    def test_tool_name_has_label(self, tmp_path: Path) -> None:
        part = JobContext("j").output_path(src_file(tmp_path), ext=".jpg", label="Cropped")
        assert part.name == "Page 001 Cropped.jpg.part"

    def test_collisions_append_numbers(self, tmp_path: Path) -> None:
        src = src_file(tmp_path)
        (tmp_path / "Page 001.jpg").write_bytes(b"existing")
        ctx = JobContext("j")
        first = ctx.output_path(src, ext="jpg")
        second = ctx.output_path(src, ext="jpg")  # the first .part is still reserved
        assert first.name == "Page 001 (2).jpg.part"
        assert second.name == "Page 001 (3).jpg.part"
        assert (tmp_path / "Page 001.jpg").read_bytes() == b"existing"

    def test_folder_is_created(self, tmp_path: Path) -> None:
        pages = tmp_path / "doc pages"
        part = JobContext("j").output_path(tmp_path / "doc 001.pdf", ext="png", folder=pages)
        assert part == pages / "doc 001.png.part"

    def test_folder_source_keeps_full_name(self, tmp_path: Path) -> None:
        folder = tmp_path / "v1.2 release"
        folder.mkdir()
        part = JobContext("j").output_path(folder, ext="zip")
        assert part.name == "v1.2 release.zip.part"


def test_free_path(tmp_path: Path) -> None:
    target = tmp_path / "a.jpg"
    assert free_path(target) == target
    target.touch()
    (tmp_path / "a (2).jpg").touch()
    assert free_path(target) == tmp_path / "a (3).jpg"


# --- run_job ----------------------------------------------------------------------------------
def write_copy(paths, params, ctx):
    outs = []
    for p in paths:
        ctx.progress(0.5, "half")
        out = ctx.output_path(p, ext="txt", label=params.get("label"))
        out.write_bytes(p.read_bytes())
        outs.append(out)
    return outs


def write_then_fail(paths, params, ctx):
    ctx.output_path(paths[0], ext="txt").write_bytes(b"partial")
    raise RuntimeError("boom")


def cancel_midway(paths, params, ctx):
    out = ctx.output_path(paths[0], ext="txt")
    ctx._cancel[ctx.job_id] = True  # as if the user pressed Cancel now
    return [out]


def fail_on_bad(paths, params, ctx):
    out = ctx.output_path(paths[0], ext="txt")
    if paths[0].stem == "bad":
        raise ValueError("corrupt")
    out.write_bytes(b"ok")
    return [out]


def fail_in_new_folder(paths, params, ctx):
    ctx.output_path(paths[0], ext="png", folder=paths[0].parent / "export" / "doc pages")
    raise RuntimeError("page 3 failed")


def hard_exit(paths, params, ctx):
    ctx.output_path(paths[0], ext="txt")
    raise SystemExit(1)


def fn(name: str) -> str:
    return f"{__name__}:{name}"


def test_run_job_success_reports_progress_per_group(tmp_path: Path) -> None:
    a, b = src_file(tmp_path, "a.png"), src_file(tmp_path, "b.png")
    q: queue.SimpleQueue = queue.SimpleQueue()
    res = run_job(fn("write_copy"), [[a], [b]], {"label": "Copy"}, JobContext("j", q))
    assert res["ok"] and res["message"] == ""
    assert [p.name for p in res["outputs"]] == ["a Copy.txt.part", "b Copy.txt.part"]
    assert [q.get_nowait() for _ in range(2)] == [("j", 0.25, "half"), ("j", 0.75, "half")]


def test_run_job_failure_removes_parts_and_keeps_source(tmp_path: Path) -> None:
    src = src_file(tmp_path)
    res = run_job(fn("write_then_fail"), [[src]], {}, JobContext("j"))
    assert res == {"ok": False, "outputs": [], "message": "RuntimeError: boom"}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["Page 001.png"]
    assert src.read_bytes() == b"source"


def test_run_job_cancelled_before_start_runs_nothing(tmp_path: Path) -> None:
    src = src_file(tmp_path)
    res = run_job(fn("write_then_fail"), [[src]], {}, JobContext("j", cancel_flags={"j": True}))
    assert res == {"ok": False, "outputs": [], "message": "Cancelled"}
    assert list(tmp_path.iterdir()) == [src]


def test_run_job_cancelled_midway_removes_parts(tmp_path: Path) -> None:
    src = src_file(tmp_path)
    res = run_job(fn("cancel_midway"), [[src], [src]], {}, JobContext("j"))
    assert res["message"] == "Cancelled"
    assert list(tmp_path.iterdir()) == [src]


def test_run_job_bad_func_path(tmp_path: Path) -> None:
    res = run_job("dragshift.nope:fn", [[src_file(tmp_path)]], {}, JobContext("j"))
    assert not res["ok"] and "ModuleNotFoundError" in res["message"]


def test_one_bad_file_keeps_the_other_outputs(tmp_path: Path) -> None:
    good1, bad, good2 = (src_file(tmp_path, n) for n in ("a.png", "bad.png", "c.png"))
    res = run_job(fn("fail_on_bad"), [[good1], [bad], [good2]], {}, JobContext("j"))
    assert not res["ok"]
    assert [p.name for p in res["outputs"]] == ["a.txt.part", "c.txt.part"]
    assert res["message"] == "2 of 3 done; bad.png: ValueError: corrupt"
    assert not (tmp_path / "bad.txt.part").exists()


def test_failure_removes_the_output_folders_it_created(tmp_path: Path) -> None:
    src = src_file(tmp_path, "doc.pdf")
    res = run_job(fn("fail_in_new_folder"), [[src]], {}, JobContext("j"))
    assert res["message"] == "RuntimeError: page 3 failed"
    assert list(tmp_path.iterdir()) == [src]


def test_system_exit_still_cleans_up(tmp_path: Path) -> None:
    src = src_file(tmp_path)
    with pytest.raises(SystemExit):
        run_job(fn("hard_exit"), [[src]], {}, JobContext("j"))
    assert list(tmp_path.iterdir()) == [src]


def test_cancel_dir_flags(tmp_path: Path) -> None:
    flags = CancelDir(tmp_path)
    ctx = JobContext("j1", cancel_flags=flags)
    assert not ctx.cancelled()
    flags.set("j1")
    assert ctx.cancelled() and "j2" not in flags
    flags.clear("j1")
    assert not ctx.cancelled()


def test_folder_removed_by_another_jobs_cleanup_is_recreated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pages = tmp_path / "doc pages"
    pages.mkdir()  # exists, so this job does not own it
    real_open = Path.open
    raced: list[bool] = []

    def open_after_race(self: Path, *args, **kwargs):
        if not raced:  # another job's failure cleanup rmdirs the empty folder right now
            raced.append(True)
            pages.rmdir()
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_after_race)
    part = JobContext("j").output_path(tmp_path / "doc 001.pdf", ext="png", folder=pages)
    assert part == pages / "doc 001.png.part" and part.exists()
