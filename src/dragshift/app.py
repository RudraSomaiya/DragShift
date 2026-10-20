"""Application wiring (PRD §9.2): HookThread -> GestureController -> WheelWindow -> JobManager.

``build`` creates the object graph without starting the hooks, so tests can drive it. ``run`` is
what ``dragshift`` does (FR-6.1: foreground, console logs). Tray, single instance and
``--background`` come with DS-25 and DS-26.

UI-process module: it must never import an engine library (CLAUDE.md rule 2).
"""

import logging
import signal
import sys
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Slot
from PySide6.QtWidgets import QApplication

from dragshift.config import Config, ConfigStore
from dragshift.engine import registry
from dragshift.engine.jobs import JobManager, JobResult
from dragshift.gesture import GestureController
from dragshift.platform.hooks import HookInstallError, HookThread
from dragshift.ui.wheel import WheelWindow

__all__ = ["App", "build", "run"]

log = logging.getLogger(__name__)


class App(QObject):
    """Owns the long-lived objects and connects them."""

    def __init__(self, store: ConfigStore | None = None) -> None:
        super().__init__()
        self.store = store or ConfigStore()
        self.jobs = JobManager(self)
        self.wheel = WheelWindow()
        self.gesture = GestureController(self.wheel, self.store.config, parent=self)
        self.hooks = HookThread(self)
        self.wheel.set_appearance(self.store.config.appearance)

        self.hooks.raw_input.connect(self.gesture.on_input, Qt.ConnectionType.QueuedConnection)
        self.hooks.reinstalled.connect(self.gesture.on_hooks_reinstalled)
        self.store.changed.connect(self.gesture.apply_config)
        self.store.changed.connect(self.on_config_changed)
        self.wheel.dropped.connect(self.on_dropped)
        self.jobs.finished.connect(self.on_finished)

    @Slot(object)
    def on_config_changed(self, config: Config) -> None:
        self.wheel.set_appearance(config.appearance)  # theme, accent, reduced motion (§8.2)

    @Slot(str, object)
    def on_dropped(self, op_id: str, paths: list[Path]) -> None:
        """A drop on an enabled petal. The sources are only read: outputs are new files."""
        if registry.get(op_id).dialog:
            log.warning("%s needs a dialog, which is not built yet; nothing was run", op_id)
            return
        # Settings > Output > JPEG/WebP quality (FR-8.1), read at drop time so it applies live.
        quality = self.store.config.output.jpeg_quality
        self.jobs.submit(op_id, list(paths), {"quality": quality})

    @Slot(str, object)
    def on_finished(self, _job_id: str, result: JobResult) -> None:
        if result.ok:
            log.info("%s: %s -> %s", result.op_id, _names(result.inputs), _names(result.outputs))
        else:
            log.warning("%s failed: %s", result.op_id, result.message)

    def shutdown(self) -> None:
        self.hooks.stop_hooks()
        self.wheel.close()
        self.jobs.shutdown()


def _names(paths: list[Path]) -> str:
    return ", ".join(p.name for p in paths)


def build(store: ConfigStore | None = None) -> App:
    return App(store)


def run() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # Qt's GDI font engine: DirectWrite's font database costs ~28 MB private (DS-5, NFR-1).
    qt = QApplication([*sys.argv, "-platform", "windows:fontengine=gdi"])
    qt.setQuitOnLastWindowClosed(False)  # the wheel is a tool window that comes and goes
    app = build()
    try:
        app.hooks.start_hooks()
    except HookInstallError as exc:
        log.error("could not install the input hooks: %s", exc)
        return 1
    signal.signal(signal.SIGINT, lambda *_: qt.quit())  # Ctrl+C
    keepalive = QTimer()  # lets Python see the signal while Qt's loop runs
    keepalive.timeout.connect(lambda: None)
    keepalive.start(200)
    log.info("DragShift is running. Drag a file and tap Shift, Ctrl or Alt. Ctrl+C quits.")
    code = qt.exec()
    app.shutdown()
    return code
