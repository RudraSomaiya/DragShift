"""Job history and usage counts: ``%APPDATA%\\DragShift\\history.json`` (PRD §10.2).

- The last MAX_JOBS jobs, newest first, each with what Undo needs (FR-5.6, FR-5.7). Cancelled
  jobs are not kept.
- Usage counts per input kind and Op feed smart petal ordering (FR-2.2). Only successful jobs
  count, and only when all their inputs share one kind (the wheel's ordering key).
- Saves go through ``history.json.part`` and an atomic rename. An unreadable file is moved to
  ``history.bad.json`` and the history starts empty.

UI-process module: the stdlib, QtCore and the light engine modules only.
"""

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal

from dragshift.config import write_json
from dragshift.engine.jobs import JobResult, apply_undo
from dragshift.engine.kinds import input_kind

__all__ = ["MAX_JOBS", "History", "HistoryEntry"]

log = logging.getLogger(__name__)

MAX_JOBS = 20


@dataclass(frozen=True)
class HistoryEntry:
    id: str  # the JobManager job id
    op: str
    inputs: tuple[Path, ...]
    outputs: tuple[Path, ...]
    ok: bool
    message: str
    undo: dict  # JobResult.undo with its paths as strings; {} when there is nothing to undo
    ts: str  # local time, ISO 8601 to the second
    undone: bool = False

    @property
    def can_undo(self) -> bool:
        return bool(self.undo) and not self.undone

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "op": self.op,
            "inputs": [str(p) for p in self.inputs],
            "outputs": [str(p) for p in self.outputs],
            "ok": self.ok,
            "message": self.message,
            "undo": self.undo,
            "ts": self.ts,
            "undone": self.undone,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "HistoryEntry":
        """Raises KeyError, TypeError or ValueError for data that is not an entry."""
        return cls(
            id=str(data["id"]),
            op=str(data["op"]),
            inputs=tuple(Path(p) for p in data["inputs"]),
            outputs=tuple(Path(p) for p in data["outputs"]),
            ok=bool(data["ok"]),
            message=str(data.get("message", "")),
            undo=dict(data.get("undo") or {}),
            ts=str(data.get("ts", "")),
            undone=bool(data.get("undone", False)),
        )


def _jsonable(undo: Mapping[str, Any]) -> dict[str, Any]:
    """``undo`` with its Paths as strings (``paths``, ``dirs``); ``stamps`` stay int lists."""

    def plain(v: Any) -> Any:
        return (
            [plain(x) for x in v]
            if isinstance(v, list | tuple)
            else (str(v) if isinstance(v, Path) else v)
        )

    return {k: plain(v) for k, v in undo.items()}


class History(QObject):
    """The live history. Emits ``changed`` after every change, once it is saved."""

    changed = Signal()

    def __init__(self, path: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._path = Path(path)
        self._jobs: list[HistoryEntry] = []  # newest first
        self._usage: dict[str, dict[str, int]] = {}
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def entries(self) -> tuple[HistoryEntry, ...]:
        """The last MAX_JOBS jobs, newest first (the tray's History menu, FR-5.7)."""
        return tuple(self._jobs)

    @property
    def usage(self) -> Mapping[str, Mapping[str, int]]:
        """Input kind -> op id -> successful runs (FR-2.2)."""
        return self._usage

    def get(self, job_id: str) -> HistoryEntry | None:
        return next((e for e in self._jobs if e.id == job_id), None)

    def record(self, job_id: str, result: JobResult) -> None:
        if result.cancelled:
            return
        entry = HistoryEntry(
            id=job_id,
            op=result.op_id,
            inputs=tuple(result.inputs),
            outputs=tuple(result.outputs),
            ok=result.ok,
            message=result.message,
            undo=_jsonable(result.undo),
            ts=datetime.now().isoformat(timespec="seconds"),
        )
        self._jobs.insert(0, entry)
        del self._jobs[MAX_JOBS:]
        key = input_kind(result.inputs) if result.ok else None
        if key is not None:
            counts = self._usage.setdefault(key, {})
            counts[result.op_id] = counts.get(result.op_id, 0) + 1
        self._save()

    def undo(self, job_id: str) -> list[Path]:
        """Undo a job once (FR-5.6), from its toast or the tray. Returns the outputs kept
        because they changed since the job. Raises ``KeyError`` if the job is not here or was
        undone already, ``ValueError`` for an undo type the engine does not know, and
        ``OSError`` if the Recycle Bin refuses."""
        entry = self.get(job_id)
        if entry is None or not entry.can_undo:
            raise KeyError(job_id)
        kept = apply_undo(entry.undo)
        self._jobs[self._jobs.index(entry)] = replace(entry, undone=True)
        self._save()
        return kept

    def reset_usage(self) -> None:
        """Forget the usage counts (Settings > Wheels, FR-8.1): petals go back to the pinned and
        default order. The jobs stay, so Undo still works."""
        if self._usage:
            self._usage = {}
            self._save()

    # --- file --------------------------------------------------------------------------------
    def _load(self) -> None:
        try:
            text = self._path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return
        except OSError as e:
            log.warning("could not read %s: %s", self._path, e)
            return
        try:
            data = json.loads(text)
            jobs = [HistoryEntry.from_json(d) for d in data["jobs"]][:MAX_JOBS]
            usage = {
                str(kind): {str(op): int(n) for op, n in counts.items()}
                for kind, counts in data.get("usage", {}).items()
            }
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            log.warning("invalid history %s (%s); starting a new one", self._path, e)
            try:
                os.replace(self._path, self._path.with_name("history.bad.json"))
            except OSError:
                pass
            return
        self._jobs, self._usage = jobs, usage

    def _save(self) -> None:
        data = {"jobs": [e.to_json() for e in self._jobs], "usage": self._usage}
        try:
            write_json(self._path, data)
        except OSError as e:
            log.warning("could not save %s: %s", self._path, e)  # kept in memory
        self.changed.emit()
