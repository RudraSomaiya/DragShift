"""Application wiring (PRD §9.2): HookThread -> GestureController -> WheelWindow -> JobManager.

``build`` creates the object graph without starting the hooks, so tests can drive it. ``run`` is
what ``dragshift`` does: single instance (FR-6.3), logs to the console when there is one and
always to a rotating file (FR-6.1, FR-6.5), autostart kept in step with the config (FR-7.2).
``--background`` is handled in ``__main__`` (FR-6.2). The tray comes with DS-25.

UI-process module: it must never import an engine library (CLAUDE.md rule 2).
"""

import logging
import os
import signal
import sys
import tempfile
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, Qt, QTimer, Slot
from PySide6.QtWidgets import QApplication

from dragshift.config import Config, ConfigStore
from dragshift.engine import registry
from dragshift.engine.jobs import JobManager, JobResult
from dragshift.gesture import GestureController
from dragshift.launcher import apps, sites
from dragshift.launcher.sites import FaviconCache
from dragshift.platform import autostart
from dragshift.platform.hooks import HookInstallError, HookThread
from dragshift.platform.single_instance import SingleInstance
from dragshift.ui.launcher_wheel import LauncherWheel
from dragshift.ui.wheel import WheelWindow

__all__ = ["App", "build", "run"]

log = logging.getLogger(__name__)


class App(QObject):
    """Owns the long-lived objects and connects them."""

    def __init__(
        self,
        store: ConfigStore | None = None,
        autostart: Callable[[bool], None] | None = None,
    ) -> None:
        super().__init__()
        self.store = store or ConfigStore()
        self._autostart = autostart  # writes the Run key; None in tests, which must not
        self.jobs = JobManager(self)
        self.wheel = WheelWindow()
        icons = self.store.path.parent / "icons"  # FR-3.4: %APPDATA%\DragShift\icons
        self.launcher = LauncherWheel(icons)
        self.favicons = FaviconCache(icons, self)
        self.gesture = GestureController(
            self.wheel, self.store.config, launcher_open=self.launcher.isVisible, parent=self
        )
        self.hooks = HookThread(self)

        queued = Qt.ConnectionType.QueuedConnection
        self.hooks.raw_input.connect(self.gesture.on_input, queued)
        self.hooks.raw_input.connect(self.launcher.on_input, queued)
        self.hooks.reinstalled.connect(self.gesture.on_hooks_reinstalled)
        self.store.changed.connect(self.gesture.apply_config)
        self.store.changed.connect(self.on_config_changed)
        self.wheel.dropped.connect(self.on_dropped)
        self.jobs.finished.connect(self.on_finished)
        self.gesture.quick_launch.connect(self.on_quick_launch)
        self.launcher.picked.connect(self.on_picked)
        self.launcher.add_requested.connect(self.on_add_requested)
        self.favicons.updated.connect(self.launcher.reload_icons)
        self.on_config_changed(self.store.config)

    @Slot(object)
    def on_config_changed(self, config: Config) -> None:
        self.wheel.set_appearance(config.appearance)  # theme, accent, reduced motion (§8.2)
        self.launcher.set_appearance(config.appearance)
        self.launcher.set_launcher(config.launcher)
        self.favicons.ensure(config.launcher.sites)  # once per newly added site (FR-3.4)
        if self._autostart is not None:
            try:
                self._autostart(config.general.autostart)  # FR-7.2
            except OSError as e:
                log.warning("could not change autostart: %s", e)

    @Slot()
    def on_show_settings(self) -> None:
        """A second launch asked for Settings (FR-6.3). The Settings window comes with DS-25."""
        log.info("DragShift is already running; Settings comes with DS-25")

    @Slot(str, object)
    def on_dropped(self, op_id: str, paths: list[Path]) -> None:
        """A drop on an enabled petal. The sources are only read: outputs are new files."""
        if registry.get(op_id).dialog:
            log.warning("%s needs a dialog, which is not built yet; nothing was run", op_id)
            return
        # Settings > Output > JPEG/WebP quality (FR-8.1), read at drop time so it applies live.
        quality = self.store.config.output.jpeg_quality
        self.jobs.submit(op_id, list(paths), {"quality": quality})

    @Slot(str, QPoint)
    def on_quick_launch(self, kind: str, at: QPoint) -> None:
        """A tap passed every gate (FR-3.1, FR-3.2): open that launcher wheel (FR-3.5). With one
        already open, the other key swaps it in place and the same key closes it."""
        if not self.launcher.isVisible():
            self.launcher.open(kind, at)
        elif kind == self.launcher.kind:
            self.launcher.close()
        else:
            self.launcher.swap(kind)

    @Slot(str, int)
    def on_picked(self, kind: str, index: int) -> None:
        launcher = self.store.config.launcher
        if kind == "apps":
            slot = launcher.apps[index]
            name, error = apps.label_of(slot), apps.launch_app(slot)
        else:
            slot = launcher.sites[index]
            name, error = sites.label_of(slot), sites.launch_site(slot)
        if error:
            log.warning("could not open %s: %s", name, error)  # a toast once DS-18 lands
        else:
            log.info("Quick Launch: opened %s", name)

    @Slot(str, int)
    def on_add_requested(self, kind: str, index: int) -> None:
        # FR-3.6: Settings > Launcher comes with DS-25.
        log.info("empty %s slot %d: add it in config.json until Settings exists", kind, index + 1)

    @Slot(str, object)
    def on_finished(self, _job_id: str, result: JobResult) -> None:
        if result.ok:
            log.info("%s: %s -> %s", result.op_id, _names(result.inputs), _names(result.outputs))
        else:
            log.warning("%s failed: %s", result.op_id, result.message)

    def shutdown(self) -> None:
        self.hooks.stop_hooks()
        self.wheel.close()
        self.launcher.close()
        self.jobs.shutdown()


def _names(paths: list[Path]) -> str:
    return ", ".join(p.name for p in paths)


def build(store: ConfigStore | None = None, autostart: Callable[[bool], None] | None = None) -> App:
    return App(store, autostart)


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_BYTES, LOG_BACKUPS = 1_000_000, 3  # FR-6.5: 1 MB x 3


def log_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(), "DragShift", "logs")


def setup_logging(folder: Path | None = None) -> Path:
    """Console logs when there is a console (FR-6.1), and always the rotating file (FR-6.5).
    Under pythonw there is no stderr, and a console handler would fail on every message."""
    folder = folder or log_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "dragshift.log"
    handlers: list[logging.Handler] = [
        RotatingFileHandler(path, maxBytes=LOG_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8")
    ]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, handlers=handlers, force=True)
    return path


def run() -> int:
    setup_logging()
    instance = SingleInstance()
    if not instance.first:  # FR-6.3: hand over to the running one and leave, before any Qt app
        instance.notify_first()
        log.info("DragShift is already running; asked it to open Settings")
        return 0
    # Qt's GDI font engine: DirectWrite's font database costs ~28 MB private (DS-5, NFR-1).
    qt = QApplication([*sys.argv, "-platform", "windows:fontengine=gdi"])
    qt.setQuitOnLastWindowClosed(False)  # the wheel is a tool window that comes and goes
    app = build(autostart=autostart.sync)
    instance.show_settings.connect(app.on_show_settings)
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
    instance.release()
    return code
