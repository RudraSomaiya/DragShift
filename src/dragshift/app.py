"""Application wiring (PRD §9.2): HookThread -> GestureController -> WheelWindow -> JobManager.

``build`` creates the object graph without starting the hooks, so tests can drive it. ``run`` is
what ``dragshift`` does: single instance (FR-6.3), logs to the console when there is one and
always to a rotating file (FR-6.1, FR-6.5), autostart kept in step with the config (FR-7.2).
``--background`` is handled in ``__main__`` (FR-6.2). Finished jobs go to ``history.json``
and a toast (FR-5.5, FR-5.7); their usage counts reorder the petals (FR-2.2). The tray (FR-7.1)
opens Settings (FR-8.1), the History menu, Pause and the log folder.

UI-process module: it must never import an engine library (CONTRIBUTING.md rule 2).
"""

import dataclasses
import logging
import os
import signal
import sys
import tempfile
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, Qt, QTimer, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QWidget

from dragshift.config import Config, ConfigStore
from dragshift.engine import presets, registry, variants
from dragshift.engine.actions import files as actions
from dragshift.engine.jobs import OUTPUT_FOLDER, JobManager, JobResult
from dragshift.gesture import GestureController
from dragshift.history import History
from dragshift.launcher import apps, sites
from dragshift.launcher.sites import FaviconCache
from dragshift.platform import autostart, files, memory
from dragshift.platform.hooks import HookInstallError, HookThread
from dragshift.platform.single_instance import SingleInstance
from dragshift.ui import theme
from dragshift.ui.dialogs.add_bg import AddBgDialog
from dragshift.ui.dialogs.base_glass import GlassDialog
from dragshift.ui.dialogs.compress import CompressDialog
from dragshift.ui.dialogs.crop import CropDialog
from dragshift.ui.dialogs.settings import SettingsWindow
from dragshift.ui.job_toasts import JobToasts, primary_output
from dragshift.ui.launcher_wheel import LauncherWheel
from dragshift.ui.tray import Tray
from dragshift.ui.wheel import WheelWindow

__all__ = ["App", "build", "run"]

log = logging.getLogger(__name__)

# Op.dialog -> the window that collects its params (§8.6). Extract pages comes with its own story.
DIALOGS = {"add_bg": AddBgDialog, "compress": CompressDialog, "crop": CropDialog}

TRIM_AFTER_MS = 30_000  # quiet this long -> trim the working set (NFR-1)


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
        self.history = History(self.store.path.parent / "history.json", self)  # §10.2
        self.toasts = JobToasts(self.jobs, self.history, log_dir() / "dragshift.log", parent=self)
        self.wheel = WheelWindow()
        icons = self.store.path.parent / "icons"  # FR-3.4: %APPDATA%\DragShift\icons
        self.launcher = LauncherWheel(icons)
        self.favicons = FaviconCache(icons, self)
        self.gesture = GestureController(
            self.wheel, self.store.config, launcher_open=self.launcher.isVisible, parent=self
        )
        self.hooks = HookThread(self)
        self.tray = Tray(self.history, self)  # shown by run(), not in tests
        self._dialog: GlassDialog | None = None  # the open op dialog (FR-2.5)
        self.settings: SettingsWindow | None = None  # made when opened, gone when closed (NFR-1)
        self._trim_timer = QTimer(self)  # quiet for TRIM_AFTER_MS -> give the idle pages back
        self._trim_timer.setSingleShot(True)
        self._trim_timer.setInterval(TRIM_AFTER_MS)
        self._trim_timer.timeout.connect(self._trim_memory)

        queued = Qt.ConnectionType.QueuedConnection
        self.hooks.raw_input.connect(self.gesture.on_input, queued)
        self.hooks.raw_input.connect(self.launcher.on_input, queued)
        self.hooks.reinstalled.connect(self.gesture.on_hooks_reinstalled)
        self.store.changed.connect(self.gesture.apply_config)
        self.store.changed.connect(self.on_config_changed)
        self.wheel.dropped.connect(self.on_dropped)
        self.wheel.closed.connect(self.trim_soon)
        self.jobs.finished.connect(self.trim_soon)
        self.jobs.finished.connect(self.on_finished)
        self.gesture.quick_launch.connect(self.on_quick_launch)
        self.launcher.picked.connect(self.on_picked)
        self.launcher.closed.connect(self.trim_soon)
        self.launcher.add_requested.connect(self.on_add_requested)
        self.favicons.updated.connect(self.launcher.reload_icons)
        self.history.changed.connect(self.apply_ordering)
        self.tray.settings_requested.connect(self.on_show_settings)
        self.tray.pause_toggled.connect(self.set_paused)
        self.tray.logs_requested.connect(lambda: files.open_path(log_dir()))
        self.tray.quit_requested.connect(QApplication.quit)
        self.tray.history_action.connect(self.on_history_action)
        self.on_config_changed(self.store.config)  # also starts the idle clock

    @Slot()
    def trim_soon(self) -> None:
        """Restart the quiet-period clock; it fires once nothing has used the UI for a while."""
        self._trim_timer.start()

    def _trim_memory(self) -> None:
        """Settings > General > Trim memory when idle (NFR-1). Not while a window is up: its
        close restarts the clock. Not while the worker pool is alive either (it lingers
        IDLE_SHUTDOWN_MS after a job); that has no signal, so look again in TRIM_AFTER_MS."""
        if not self.store.config.general.trim_memory_when_idle:
            return
        windows = (self.wheel, self.launcher, self._dialog, self.settings)
        if any(w is not None and w.isVisible() for w in windows):
            return
        if self.jobs.pool_active:
            self.trim_soon()
            return
        before = memory.usage()
        memory.trim()
        after = memory.usage()
        log.info(
            "trimmed memory: working set %.1f -> %.1f MB, private %.1f MB",
            before.working_set_mb,
            after.working_set_mb,
            after.private_mb,
        )

    @Slot()
    def apply_ordering(self) -> None:
        """FR-2.2: pinned petals from the config, then usage counts from the history."""
        self.wheel.set_ordering(self.store.config.wheels.pinned, self.history.usage)

    @Slot(object)
    def on_config_changed(self, config: Config) -> None:
        self.trim_soon()  # a setting changed, or start-up: the pages in use are not needed again
        self.wheel.set_appearance(config.appearance)  # theme, accent, reduced motion (§8.2)
        self.launcher.set_appearance(config.appearance)
        self.toasts.stack.set_appearance(config.appearance)
        current = theme.resolve(config.appearance)
        for window in (self._dialog, self.settings):
            if window is not None:
                window.set_theme(current)
        self.tray.set_accent(current.accent)
        self.tray.set_paused(config.general.paused)
        actions.pin_folders(config.folders.pinned)  # the Copy to / Move to rings (§6.3)
        self.apply_ordering()
        self.launcher.set_launcher(config.launcher)
        self.favicons.ensure(config.launcher.sites)  # once per newly added site (FR-3.4)
        if self._autostart is not None:
            try:
                self._autostart(config.general.autostart)  # FR-7.2
            except OSError as e:
                log.warning("could not change autostart: %s", e)

    @Slot()
    def on_show_settings(self) -> SettingsWindow:
        """Open Settings, or bring it to the front (the tray, a second launch: FR-6.3)."""
        if self.settings is None:
            window = SettingsWindow(self.store, theme.resolve(self.store.config.appearance))
            window.reset_usage.connect(self.history.reset_usage)
            window.finished.connect(lambda _result: self._settings_closed(window))
            self.settings = window
        self.settings.show()
        self.settings.raise_()
        self.settings.activateWindow()
        return self.settings

    def _settings_closed(self, window: SettingsWindow) -> None:
        if self.settings is window:
            self.settings = None
        window.deleteLater()
        self.trim_soon()

    @Slot(bool)
    def set_paused(self, paused: bool) -> None:
        """The tray's Pause gestures (FR-7.1, FR-1.12), saved like any setting."""
        general = dataclasses.replace(self.store.config.general, paused=paused)
        self.store.update(dataclasses.replace(self.store.config, general=general))

    @Slot(str, str)
    def on_history_action(self, job_id: str, action: str) -> None:
        """Open, Show in folder or Undo from the tray's History (FR-5.7)."""
        entry = self.history.get(job_id)
        if entry is None:
            return
        if action == "undo":
            self.toasts.undo(job_id)
            return
        result = JobResult(entry.op, list(entry.inputs), list(entry.outputs), True, "", entry.undo)
        target = primary_output(result)
        if target is None:
            return
        try:
            (files.open_path if action == "open" else files.show_in_folder)(target)
        except OSError as e:
            self.toasts.error(f"Couldn't open {target.name}", str(e))

    @Slot(str, object)
    def on_dropped(self, op_id: str, paths: list[Path]) -> None:
        """A drop on an enabled petal. The sources are only read: outputs are new files."""
        dialog = registry.get(op_id).dialog
        if dialog is not None and dialog not in DIALOGS:
            log.warning("%s needs a dialog, which is not built yet; nothing was run", op_id)
            return
        # Settings > Output > JPEG/WebP quality and PDF raster DPI (FR-8.1), read at drop time so
        # they apply live.
        output = self.store.config.output
        params = {"quality": output.jpeg_quality, "dpi": output.pdf_dpi}
        if output.location == "fixed_folder" and output.fixed_folder:
            params[OUTPUT_FOLDER] = output.fixed_folder  # Settings > Output > one folder
        params |= actions.params_for(op_id)  # a Copy to / Move to ring: its folder
        params |= variants.params_for(op_id)  # a ring Op: Compress preset (FR-4.3), Resize...
        if presets.is_compress(op_id):
            params["allow_format_change"] = output.allow_format_change_for_size  # §6.2 PNG→WebP
        if dialog is None:
            self.submit(op_id, list(paths), params)
            return
        # FR-2.5: the dialog opens after the drop and runs the job when accepted. It is shown,
        # not exec()'d: the drop must return to Explorer at once.
        if self._dialog is not None:  # one at a time: a new drop replaces an open dialog
            self._dialog.reject()
        window = DIALOGS[dialog](list(paths), theme.resolve(self.store.config.appearance))
        window.accepted.connect(lambda: self.submit(op_id, list(paths), params | window.params()))
        window.finished.connect(lambda _result: self._dialog_closed(window))
        self._dialog = window
        window.show()
        window.raise_()
        window.activateWindow()

    def _dialog_closed(self, window: QWidget) -> None:
        if self._dialog is window:
            self._dialog = None
        window.deleteLater()  # after accepted has run the job with its params
        self.trim_soon()

    def submit(self, op_id: str, paths: list[Path], params: dict) -> None:
        job_id = self.jobs.submit(op_id, paths, params)
        self.toasts.started(job_id, op_id, paths)  # a progress toast after 1 s (FR-4.5)

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
            log.warning("could not open %s: %s", name, error)
            self.toasts.error(f"Couldn't open {name}", error)
        else:
            log.info("Quick Launch: opened %s", name)

    @Slot(str, int)
    def on_add_requested(self, kind: str, index: int) -> None:
        """An empty slot's "+ Add" petal opens Settings > Launcher on that slot (FR-3.6)."""
        window = self.on_show_settings()
        window.show_tab("Launcher")
        window.launcher.show_slot(kind, index)

    @Slot(str, object)
    def on_finished(self, job_id: str, result: JobResult) -> None:
        if result.copied:  # Copy path, Copy name, SHA-256: a worker owns no clipboard
            QGuiApplication.clipboard().setText(result.copied)
        if result.ok:
            log.info("%s: %s -> %s", result.op_id, _names(result.inputs), _names(result.outputs))
        else:
            log.warning("%s failed: %s", result.op_id, result.message)
        self.history.record(job_id, result)  # first, so the toast's Undo finds it (FR-5.7)
        self.toasts.finished(job_id, result)  # FR-5.5

    def shutdown(self) -> None:
        self._trim_timer.stop()
        self.tray.hide()
        if self.settings is not None:
            self.settings.close()
        self.hooks.stop_hooks()
        self.wheel.close()
        self.launcher.close()
        self.toasts.shutdown()
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
    app.tray.show()  # once the hooks are in: the icon means DragShift is ready
    signal.signal(signal.SIGINT, lambda *_: qt.quit())  # Ctrl+C
    keepalive = QTimer()  # lets Python see the signal while Qt's loop runs
    keepalive.timeout.connect(lambda: None)
    keepalive.start(200)
    log.info("DragShift is running. Drag a file and tap Shift, Ctrl or Alt. Ctrl+C quits.")
    code = qt.exec()
    app.shutdown()
    instance.release()
    return code
