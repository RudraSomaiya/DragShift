"""history.json (PRD §10.2): the last 20 jobs for Undo and the tray (FR-5.7), and usage counts
for smart petal ordering (FR-2.2)."""

import json
from pathlib import Path

import pytest

from dragshift.engine import jobs as jobs_module
from dragshift.engine.jobs import CANCELLED, JobResult
from dragshift.history import MAX_JOBS, History

PNG, PDF = Path("C:/x/a.png"), Path("C:/x/b.pdf")


def done(op: str = "convert.image.jpg", inputs=(PNG,), outputs=(Path("C:/x/a.jpg"),), ok=True):
    undo = {"type": "trash", "paths": list(outputs)} if outputs else {}
    return JobResult(op, list(inputs), list(outputs), ok, "" if ok else "boom", undo)


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "DragShift" / "history.json"


def test_a_job_is_saved_newest_first(path: Path) -> None:
    history = History(path)
    history.record("j1", done())
    history.record("j2", done("convert.image.webp"))
    assert [e.id for e in history.entries] == ["j2", "j1"]
    data = json.loads(path.read_text(encoding="utf-8"))
    first = data["jobs"][1]
    assert first["op"] == "convert.image.jpg"
    assert first["inputs"] == [str(PNG)] and first["outputs"] == [str(Path("C:/x/a.jpg"))]
    assert first["undo"] == {"type": "trash", "paths": [str(Path("C:/x/a.jpg"))]}
    assert first["ok"] is True and first["ts"]
    assert not path.with_name("history.json.part").exists()  # written atomically


def test_only_the_last_20_jobs_are_kept(path: Path) -> None:
    history = History(path)
    for i in range(MAX_JOBS + 5):
        history.record(f"j{i}", done())
    assert len(history.entries) == MAX_JOBS == 20
    assert history.entries[0].id == "j24" and history.entries[-1].id == "j5"


def test_cancelled_jobs_are_not_kept(path: Path) -> None:
    history = History(path)
    cancelled = JobResult("convert.image.jpg", [PNG], [], False, CANCELLED, {}, cancelled=True)
    history.record("j", cancelled)
    assert history.entries == ()


def test_stamps_survive_the_json_round_trip(path: Path) -> None:
    out = Path("C:/x/a.jpg")
    undo = {"type": "trash", "paths": [out], "stamps": [[5, 1729504931000000000]]}
    History(path).record("j", JobResult("convert.image.jpg", [PNG], [out], True, "", undo))
    assert History(path).get("j").undo == {
        "type": "trash",
        "paths": [str(out)],
        "stamps": [[5, 1729504931000000000]],  # numbers, not strings
    }


def test_failures_are_kept_but_not_counted(path: Path) -> None:
    history = History(path)
    history.record("j", done(ok=False, outputs=()))
    assert history.entries[0].ok is False and history.entries[0].message == "boom"
    assert history.usage == {}


def test_usage_counts_successful_jobs_per_input_kind(path: Path) -> None:
    history = History(path)
    history.record("1", done("convert.image.jpg"))
    history.record("2", done("convert.image.jpg"))
    history.record("3", done("convert.image.webp"))
    history.record("4", done("convert.pdf.docx", inputs=(PDF,)))
    history.record("5", done("tools.merge", inputs=(PNG, PDF)))  # mixed kinds: no wheel key
    assert history.usage == {
        "image": {"convert.image.jpg": 2, "convert.image.webp": 1},
        "pdf": {"convert.pdf.docx": 1},
    }


def test_history_survives_a_restart(path: Path) -> None:
    first = History(path)
    first.record("j", done())
    again = History(path)
    assert again.entries == first.entries
    assert again.usage == {"image": {"convert.image.jpg": 1}}


def test_changed_is_emitted_after_each_save(path: Path) -> None:
    history = History(path)
    seen: list[bool] = []
    history.changed.connect(lambda: seen.append(path.exists()))
    history.record("j", done())
    assert seen == [True]


def test_an_invalid_file_is_backed_up_and_history_starts_empty(path: Path) -> None:
    path.parent.mkdir(parents=True)
    path.write_text('{"jobs": [{"id": 1}]}', encoding="utf-8")
    history = History(path)
    assert history.entries == () and history.usage == {}
    assert path.with_name("history.bad.json").exists() and not path.exists()


def test_a_missing_file_is_an_empty_history(path: Path) -> None:
    assert History(path).entries == ()
    assert not path.exists()


def test_undo_sends_the_outputs_to_the_recycle_bin_once(
    path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trashed: list[Path] = []
    monkeypatch.setattr(jobs_module, "_trash", trashed.extend)
    out = tmp_path / "a.jpg"
    out.write_bytes(b"x")
    history = History(path)
    history.record("j", done(outputs=(out,)))
    assert history.get("j").can_undo
    assert history.undo("j") == []  # nothing kept
    assert trashed == [out]
    assert History(path).get("j").undone  # remembered across a restart
    with pytest.raises(KeyError):
        history.undo("j")


def test_undo_of_an_unknown_job(path: Path) -> None:
    with pytest.raises(KeyError):
        History(path).undo("nope")
