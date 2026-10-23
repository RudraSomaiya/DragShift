"""The Settings window (FR-8.1, FR-8.2, §8.8): 760 × 520, a left nav over six tabs.

Every change is saved to ``config.json`` at once through ``ConfigStore.update`` and applies live:
the App listens to ``ConfigStore.changed``. A change made elsewhere while the window is open (the
tray's Pause, a hand edit) is shown in it too.

- **Keys:** the modifier of each wheel and launcher, tap or double tap. The three wheels always
  use three different keys, and the two launchers two: picking a key in use swaps the two.
- **Wheels:** pin petals per input kind and wheel, in order (FR-2.2); reset the usage counts.
- **Launcher:** 8 app and 8 site slots. An app is added by dropping a file or shortcut on its slot,
  by browsing, or from the Start menu's apps; a site by label and URL. Drag a slot to reorder.
- **Appearance**, **Output**, **General:** as in FR-8.1.

UI-process module: it must never import an engine library (CONTRIBUTING.md rule 2).
"""

import dataclasses
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QProcess, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QDragEnterEvent, QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from dragshift.config import Config, ConfigStore, LauncherApp, LauncherSite
from dragshift.engine import registry
from dragshift.engine.kinds import EXTENSIONS, Kind
from dragshift.launcher import apps, sites
from dragshift.ui.dialogs.base_glass import (
    GlassDialog,
    Segmented,
    primary_button,
    secondary_button,
    stylesheet,
)
from dragshift.ui.theme import Theme, css

__all__ = ["TABS", "SettingsWindow", "StartAppsPicker"]

log = logging.getLogger(__name__)

TABS = ("Keys", "Wheels", "Launcher", "Appearance", "Output", "General")
SIZE = QSize(760, 520)  # §8.8
SLOTS = 8  # FR-3.3, FR-3.4
KEY_NAMES = {"shift": "Shift", "ctrl": "Ctrl", "alt": "Alt"}
KEY_OF = {v: k for k, v in KEY_NAMES.items()}
WHEELS = {"convert": "Convert", "tools": "Tools", "actions": "Actions"}
KIND_NAMES = {k: k.value.capitalize() for k in Kind if k is not Kind.OTHER}
KIND_NAMES[Kind.PDF] = "PDF"
KIND_NAMES[Kind.OTHER] = "Other files"
EMPTY = "Empty"
_ROWS_MIME = "application/x-qabstractitemmodeldatalist"  # a QListWidget's own drag


def _extra_css(theme: Theme) -> str:
    text, field, stroke = css(theme.text), css(theme.petal), css(theme.stroke)
    muted, accent = css(theme.text_secondary), theme.accent.name()
    return f"""
    QCheckBox {{ color: {text}; background: transparent; spacing: 8px; }}
    QComboBox {{ color: {text}; background: {field}; border: 1px solid {stroke};
        border-radius: 8px; padding: 4px 8px; }}
    QListWidget {{ color: {text}; background: {field}; border: 1px solid {stroke};
        border-radius: 8px; padding: 4px; outline: none; }}
    QListWidget::item {{ padding: 5px 6px; border-radius: 6px; }}
    QListWidget::item:selected {{ background: {accent}; color: {theme.on_accent(1.0).name()}; }}
    QListWidget#nav {{ background: transparent; border: none; }}
    QCheckBox::indicator, QListWidget::indicator {{ width: 14px; height: 14px;
        border: 1px solid {css(theme.text_secondary)}; border-radius: 4px; background: {field}; }}
    QCheckBox::indicator:checked, QListWidget::indicator:checked {{
        border-color: {accent}; background: {accent}; }}
    QLabel[hint="true"] {{ color: {muted}; }}
    QLabel[error="true"] {{ color: #E5484D; }}
    """


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("hint", True)
    label.setWordWrap(True)
    return label


def _row(*widgets: QWidget, stretch: bool = True) -> QHBoxLayout:
    row = QHBoxLayout()
    for w in widgets:
        row.addWidget(w)
    if stretch:
        row.addStretch()
    return row


class _Page(QWidget):
    """One tab. ``load`` shows a config; ``save`` replaces one section of it."""

    FILLS = False  # True: a list takes the spare height; else the form keeps to the top

    def __init__(self, window: "SettingsWindow") -> None:
        super().__init__()
        self.window_ = window
        self.loading = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 4, 8, 4)
        self.form = QFormLayout()
        self.form.setContentsMargins(0, 0, 0, 0)
        outer.addLayout(self.form, 1 if self.FILLS else 0)
        if not self.FILLS:
            outer.addStretch(1)
        self.form.setVerticalSpacing(12)
        self.form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

    @property
    def config(self) -> Config:
        return self.window_.store.config

    def save(self, section: str, **changes: Any) -> None:
        if not self.loading:
            self.window_.save(section, **changes)

    def load(self, config: Config) -> None:
        self.loading = True
        try:
            self._load(config)
        finally:
            self.loading = False

    def _load(self, config: Config) -> None:
        raise NotImplementedError


# --- Keys ------------------------------------------------------------------------------------
class KeysPage(_Page):
    WHEEL_FIELDS = ("convert", "tools", "actions")
    LAUNCH_FIELDS = ("launch_apps", "launch_sites")

    def __init__(self, window: "SettingsWindow") -> None:
        super().__init__(window)
        self.keys: dict[str, Segmented] = {}
        titles = {
            "convert": "Convert wheel",
            "tools": "Tools wheel",
            "actions": "Actions wheel",
            "launch_apps": "Apps launcher",
            "launch_sites": "Sites launcher",
        }
        self.form.addRow(_hint("While dragging a file, tap a key to open its wheel."))
        for field in (*self.WHEEL_FIELDS, *self.LAUNCH_FIELDS):
            if field == "launch_apps":
                self.form.addRow(_hint("On an empty desktop, tap a key to open a launcher."))
            seg = Segmented(list(KEY_NAMES.values()))
            seg.changed.connect(lambda name, f=field: self._pick(f, KEY_OF[name]))
            self.keys[field] = seg
            self.form.addRow(titles[field], _row(seg))
        self.tap = Segmented(["Single tap", "Double tap"])
        self.tap.changed.connect(
            lambda name: self.save("keys", tap_mode="double" if name == "Double tap" else "single")
        )
        self.form.addRow("Launcher tap", _row(self.tap))

    def _load(self, config: Config) -> None:
        for field, seg in self.keys.items():
            seg.set_value(KEY_NAMES[getattr(config.keys, field)])
        self.tap.set_value("Double tap" if config.keys.tap_mode == "double" else "Single tap")

    def _pick(self, field: str, key: str) -> None:
        if self.loading:
            return
        keys = self.config.keys
        group = self.WHEEL_FIELDS if field in self.WHEEL_FIELDS else self.LAUNCH_FIELDS
        changes = {field: key}
        for other in group:  # the key is taken: give that one this row's old key
            if other != field and getattr(keys, other) == key:
                changes[other] = getattr(keys, field)
        self.save("keys", **changes)


# --- Wheels ----------------------------------------------------------------------------------
def sample_paths(kind: Kind) -> list[Path]:
    """Paths that stand for ``kind``, to ask the registry which petals it has."""
    if kind is Kind.FOLDER:
        return [Path.home()]
    if kind is Kind.OTHER:
        return [Path("sample.bin")]
    return [Path(f"sample.{ext}") for ext in sorted(EXTENSIONS[kind])]


def ops_of(kind: Kind, wheel: str) -> list[registry.Op]:
    """Every petal ``wheel`` can show for ``kind``, in default order. The union over the kind's
    extensions: a PNG has no PNG petal, but an image does."""
    seen: dict[str, registry.Op] = {}
    for path in sample_paths(kind):
        for op in registry.ops_for([path], wheel):  # type: ignore[arg-type]
            seen.setdefault(op.id, op)
    order = {op.id: i for i, op in enumerate(registry.ops_for(sample_paths(kind), wheel))}
    return sorted(seen.values(), key=lambda o: order.get(o.id, len(order)))


class WheelsPage(_Page):
    FILLS = True

    def __init__(self, window: "SettingsWindow") -> None:
        super().__init__(window)
        self.kind = QComboBox()
        for kind, name in KIND_NAMES.items():
            self.kind.addItem(name, kind.value)
        self.wheel = Segmented(list(WHEELS.values()))
        self.list = QListWidget()
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list.itemChanged.connect(self._changed)
        self.list.model().rowsMoved.connect(self._changed)
        reset = secondary_button("Reset usage counts")
        reset.clicked.connect(self.window_.reset_usage.emit)
        self.form.addRow("Files", _row(self.kind, self.wheel))
        self.form.addRow(
            _hint(
                "Ticked petals are pinned: they come first, in this order. Drag to reorder. "
                "The rest follow by how often you use them."
            )
        )
        self.form.addRow(self.list)
        self.form.addRow(_row(reset))
        self.kind.currentIndexChanged.connect(lambda _i: self.load(self.config))
        self.wheel.changed.connect(lambda _n: self.load(self.config))

    def _key(self) -> tuple[str, str]:
        wheel = next(k for k, v in WHEELS.items() if v == self.wheel.value)
        return self.kind.currentData(), wheel

    def _load(self, config: Config) -> None:
        kind, wheel = self._key()
        pinned = list(config.wheels.pinned.get(kind, ()))
        ops = ops_of(Kind(kind), wheel)
        ops.sort(key=lambda o: pinned.index(o.id) if o.id in pinned else len(pinned))
        self.list.clear()
        for op in ops:
            item = QListWidgetItem(op.label.rstrip(" ▸"))
            item.setData(Qt.ItemDataRole.UserRole, op.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            pin = Qt.CheckState.Checked if op.id in pinned else Qt.CheckState.Unchecked
            item.setCheckState(pin)
            self.list.addItem(item)

    def _changed(self, *_args: Any) -> None:
        if self.loading:
            return
        kind, _wheel = self._key()
        items = [self.list.item(i) for i in range(self.list.count())]
        here = {it.data(Qt.ItemDataRole.UserRole) for it in items}
        checked = [
            it.data(Qt.ItemDataRole.UserRole)
            for it in items
            if it.checkState() == Qt.CheckState.Checked
        ]
        others = [i for i in self.config.wheels.pinned.get(kind, ()) if i not in here]
        pinned = dict(self.config.wheels.pinned)
        if others or checked:
            pinned[kind] = (*others, *checked)  # pins of the other wheels keep their place
        else:
            pinned.pop(kind, None)
        self.save("wheels", pinned=pinned)


# --- Launcher --------------------------------------------------------------------------------
class SlotList(QListWidget):
    """Eight slots: drag one to reorder, or drop a file or shortcut on one (apps)."""

    file_dropped = Signal(int, str)  # slot, path
    reordered = Signal(list)  # the old slot index of each row, in the new order

    def __init__(self, accept_files: bool) -> None:
        super().__init__()
        self.accept_files = accept_files
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setAcceptDrops(True)
        self.model().rowsMoved.connect(self._moved)

    def _external(self, event: QDragEnterEvent | QDragMoveEvent | QDropEvent) -> bool:
        # A reorder carries the model's own rows; a file from Explorer carries URLs only.
        mime = event.mimeData()
        return mime.hasUrls() and not mime.hasFormat(_ROWS_MIME)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._external(event):
            if self.accept_files:
                event.setDropAction(Qt.DropAction.CopyAction)  # never Move (FR-1.8)
                event.accept()
            else:
                event.ignore()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if self._external(event):
            ok = self.accept_files and self.itemAt(event.position().toPoint()) is not None
            if ok:
                event.setDropAction(Qt.DropAction.CopyAction)
                event.accept()
            else:
                event.ignore()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        if self._external(event):
            item = self.itemAt(event.position().toPoint())
            urls = [u for u in event.mimeData().urls() if u.isLocalFile()]
            if not (self.accept_files and item is not None and urls):
                event.ignore()
                return
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            self.file_dropped.emit(self.row(item), urls[0].toLocalFile())
            return
        super().dropEvent(event)

    def _moved(self, *_args: Any) -> None:
        order = [self.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.count())]
        self.reordered.emit(order)


def _padded(slots: tuple, empty: Any) -> list:
    return [*slots[:SLOTS], *[empty] * (SLOTS - len(slots[:SLOTS]))]


def _trimmed(slots: list, is_empty: Callable[[Any], bool]) -> tuple:
    while slots and is_empty(slots[-1]):
        slots.pop()
    return tuple(slots)


def _app_empty(app: LauncherApp) -> bool:
    return not app.target.strip()


def _site_empty(site: LauncherSite) -> bool:
    return not site.url.strip()


class LauncherPage(_Page):
    FILLS = True

    def __init__(self, window: "SettingsWindow") -> None:
        super().__init__(window)
        self.which = Segmented(["Apps", "Sites"])
        self.form.addRow(_row(self.which))
        self.stack = QStackedWidget()
        self.form.addRow(self.stack)
        self.which.changed.connect(lambda n: self.stack.setCurrentIndex(0 if n == "Apps" else 1))

        # apps
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        self.apps = SlotList(accept_files=True)
        self.apps.file_dropped.connect(lambda slot, path: self.set_app(slot, str(Path(path))))
        self.apps.reordered.connect(lambda order: self._reorder("apps", order))
        self.apps.currentRowChanged.connect(self._app_selected)
        self.app_label = QLineEdit()
        self.app_label.setPlaceholderText("Label (optional)")
        self.app_label.editingFinished.connect(self._app_label_edited)
        browse, start, clear = (
            secondary_button("Browse…"),
            secondary_button("Start apps…"),
            secondary_button("Clear"),
        )
        browse.clicked.connect(self._browse_app)
        start.clicked.connect(self._pick_start_app)
        clear.clicked.connect(lambda: self._clear("apps", self.apps.currentRow()))
        box.addWidget(_hint("Drop a file, folder or shortcut on a slot, or pick one below."))
        box.addWidget(self.apps)
        box.addLayout(_row(self.app_label, browse, start, clear, stretch=False))
        self.stack.addWidget(page)

        # sites
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        self.sites = SlotList(accept_files=False)
        self.sites.reordered.connect(lambda order: self._reorder("sites", order))
        self.sites.currentRowChanged.connect(self._site_selected)
        self.site_label = QLineEdit()
        self.site_label.setPlaceholderText("Label (optional)")
        self.site_url = QLineEdit()
        self.site_url.setPlaceholderText("github.com")
        self.site_error = QLabel()
        self.site_error.setProperty("error", True)
        save_site, clear_site = primary_button("Save"), secondary_button("Clear")
        save_site.setDefault(False)
        save_site.setAutoDefault(False)
        save_site.clicked.connect(self._save_site)
        self.site_url.returnPressed.connect(self._save_site)
        clear_site.clicked.connect(lambda: self._clear("sites", self.sites.currentRow()))
        box.addWidget(_hint("Pick a slot, then type a label and an address."))
        box.addWidget(self.sites)
        box.addLayout(_row(self.site_label, self.site_url, save_site, clear_site, stretch=False))
        box.addWidget(self.site_error)
        self.stack.addWidget(page)
        self.picker: StartAppsPicker | None = None

    # --- showing ---
    def _load(self, config: Config) -> None:
        for widget, slots, text in (
            (self.apps, _padded(config.launcher.apps, LauncherApp()), self._app_text),
            (self.sites, _padded(config.launcher.sites, LauncherSite()), self._site_text),
        ):
            row = max(widget.currentRow(), 0)
            widget.clear()
            for i, slot in enumerate(slots):
                item = QListWidgetItem(f"{i + 1}   {text(slot)}")
                item.setData(Qt.ItemDataRole.UserRole, i)
                widget.addItem(item)
            widget.setCurrentRow(row)
        self._app_selected(self.apps.currentRow())
        self._site_selected(self.sites.currentRow())

    @staticmethod
    def _app_text(app: LauncherApp) -> str:
        if _app_empty(app):
            return EMPTY
        target = app.target if not apps.aumid_of(app.target) else "Start menu app"
        return f"{apps.label_of(app)}  —  {target}"

    @staticmethod
    def _site_text(site: LauncherSite) -> str:
        return EMPTY if _site_empty(site) else f"{sites.label_of(site)}  —  {site.url}"

    def show_slot(self, kind: str, index: int) -> None:
        """Open on one slot: the launcher wheel's "+ Add" petal (FR-3.6)."""
        self.which.set_value("Apps" if kind == "apps" else "Sites")
        (self.apps if kind == "apps" else self.sites).setCurrentRow(index)

    def _app_selected(self, row: int) -> None:
        slots = _padded(self.config.launcher.apps, LauncherApp())
        self.app_label.setText(slots[row].label if 0 <= row < SLOTS else "")

    def _site_selected(self, row: int) -> None:
        slots = _padded(self.config.launcher.sites, LauncherSite())
        site = slots[row] if 0 <= row < SLOTS else LauncherSite()
        self.site_label.setText(site.label)
        self.site_url.setText(site.url)
        self.site_error.setText("")

    # --- changing ---
    def _store(self, kind: str, slots: list) -> None:
        empty = _app_empty if kind == "apps" else _site_empty
        self.save("launcher", **{kind: _trimmed(slots, empty)})

    def set_app(self, slot: int, target: str, label: str = "") -> None:
        slots = _padded(self.config.launcher.apps, LauncherApp())
        slots[slot] = LauncherApp(label=label, target=target)
        self.apps.setCurrentRow(slot)
        self._store("apps", slots)

    def _app_label_edited(self) -> None:
        row = self.apps.currentRow()
        slots = _padded(self.config.launcher.apps, LauncherApp())
        if 0 <= row < SLOTS and not _app_empty(slots[row]):
            slots[row] = dataclasses.replace(slots[row], label=self.app_label.text().strip())
            self._store("apps", slots)

    def _browse_app(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Pick an app", str(Path.home()), "Apps and shortcuts (*.exe *.lnk);;All files (*)"
        )
        if path:
            self.set_app(max(self.apps.currentRow(), 0), str(Path(path)))

    def _pick_start_app(self) -> None:
        slot = max(self.apps.currentRow(), 0)
        self.picker = StartAppsPicker(self.window_.theme)
        self.picker.picked.connect(
            lambda name, app_id: self.set_app(slot, apps.APPS_FOLDER + app_id, name)
        )
        self.picker.show()

    def _save_site(self) -> None:
        row = max(self.sites.currentRow(), 0)
        url = self.site_url.text().strip()
        if sites.normalize_url(url) is None:
            self.site_error.setText("That is not a web address (http or https).")
            return
        slots = _padded(self.config.launcher.sites, LauncherSite())
        slots[row] = LauncherSite(label=self.site_label.text().strip(), url=url)
        self._store("sites", slots)

    def _clear(self, kind: str, row: int) -> None:
        if not 0 <= row < SLOTS:
            return
        empty = LauncherApp() if kind == "apps" else LauncherSite()
        slots = _padded(getattr(self.config.launcher, kind), empty)
        slots[row] = empty
        self._store(kind, slots)

    def _reorder(self, kind: str, order: list[int]) -> None:
        empty = LauncherApp() if kind == "apps" else LauncherSite()
        slots = _padded(getattr(self.config.launcher, kind), empty)
        self._store(kind, [slots[i] for i in order])


class StartAppsPicker(GlassDialog):
    """The Start menu's apps (``Get-StartApps``), with a search box. Any of them, Store apps
    included, opens through ``shell:AppsFolder\\<AppID>``."""

    picked = Signal(str, str)  # name, AppID
    COMMAND = "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress"

    def __init__(self, theme: Theme) -> None:
        super().__init__("Start apps", theme)
        self.resize(420, 480)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search")
        self.search.textChanged.connect(self._filter)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _item: self._accept())
        self.status = _hint("Loading the Start menu…")
        cancel, add = secondary_button("Cancel"), primary_button("Add")
        cancel.clicked.connect(self.reject)
        add.clicked.connect(self._accept)
        self.body.addWidget(self.search)
        self.body.addWidget(self.status)
        self.body.addWidget(self.list)
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(cancel)
        buttons.addWidget(add)
        self.body.addLayout(buttons)
        self.setStyleSheet(stylesheet(theme) + _extra_css(theme))
        self.process = QProcess(self)
        self.process.finished.connect(self._loaded)
        self.process.start(
            "powershell.exe", ["-NoProfile", "-NonInteractive", "-Command", self.COMMAND]
        )

    def set_apps(self, found: list[tuple[str, str]]) -> None:
        self.list.clear()
        for name, app_id in sorted(found, key=lambda a: a[0].lower()):
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, app_id)
            self.list.addItem(item)
        self.status.setText(f"{len(found)} apps")
        self._filter(self.search.text())

    def _loaded(self, *_args: Any) -> None:
        raw = bytes(self.process.readAllStandardOutput().data()).decode("utf-8", "replace")
        try:
            data = json.loads(raw or "[]")
        except json.JSONDecodeError:
            log.warning("Get-StartApps gave no list: %s", raw[:200])
            data = []
        if isinstance(data, dict):  # one app: PowerShell writes an object, not a list
            data = [data]
        self.set_apps([(d["Name"], d["AppID"]) for d in data if d.get("Name") and d.get("AppID")])

    def _filter(self, text: str) -> None:
        for i in range(self.list.count()):
            item = self.list.item(i)
            item.setHidden(text.lower() not in item.text().lower())

    def _accept(self) -> None:
        item = self.list.currentItem()
        if item is not None and not item.isHidden():
            self.picked.emit(item.text(), item.data(Qt.ItemDataRole.UserRole))
            self.accept()


# --- Appearance ------------------------------------------------------------------------------
class AppearancePage(_Page):
    def __init__(self, window: "SettingsWindow") -> None:
        super().__init__(window)
        self.theme_ = Segmented(["System", "Light", "Dark"])
        self.theme_.changed.connect(lambda n: self.save("appearance", theme=n.lower()))
        self.accent = Segmented(["Windows", "Custom"])
        self.accent.changed.connect(self._accent_mode)
        self.swatch = QPushButton()
        self.swatch.setFixedSize(28, 28)
        self.swatch.setToolTip("Choose the accent colour")
        self.swatch.clicked.connect(self._choose)
        self.motion = Segmented(["System", "On", "Off"])
        self.motion.changed.connect(lambda n: self.save("appearance", reduced_motion=n.lower()))
        self.form.addRow("Theme", _row(self.theme_))
        self.form.addRow("Accent", _row(self.accent, self.swatch))
        self.form.addRow("Reduced motion", _row(self.motion))
        self.form.addRow(_hint("System follows the Windows settings."))

    def _load(self, config: Config) -> None:
        a = config.appearance
        self.theme_.set_value(a.theme.capitalize() if a.theme in ("light", "dark") else "System")
        custom = a.accent != "system" and QColor.isValidColorName(a.accent)
        self.accent.set_value("Custom" if custom else "Windows")
        self.swatch.setVisible(custom)
        colour = QColor(a.accent) if custom else self.window_.theme.accent
        self.swatch.setStyleSheet(f"background: {colour.name()}; border-radius: 14px;")
        motion = a.reduced_motion if a.reduced_motion in ("on", "off") else "system"
        self.motion.set_value(motion.capitalize())

    def _accent_mode(self, name: str) -> None:
        if self.loading:
            return
        if name == "Windows":
            self.save("appearance", accent="system")
        else:
            self.save("appearance", accent=self.window_.theme.accent.name())

    def _choose(self) -> None:
        start = QColor(self.config.appearance.accent)
        colour = QColorDialog.getColor(start if start.isValid() else self.window_.theme.accent)
        if colour.isValid():
            self.save("appearance", accent=colour.name())


# --- Output ----------------------------------------------------------------------------------
class OutputPage(_Page):
    def __init__(self, window: "SettingsWindow") -> None:
        super().__init__(window)
        self.location = Segmented(["Next to the file", "One folder"])
        self.location.changed.connect(self._location)
        self.folder = QLabel()
        self.folder.setProperty("hint", True)
        browse = secondary_button("Choose…")
        browse.clicked.connect(self._browse)
        self.browse = browse
        self.quality = QSlider(Qt.Orientation.Horizontal)
        self.quality.setRange(1, 100)
        self.quality_value = QLabel()
        self.quality.valueChanged.connect(lambda v: self.quality_value.setText(str(v)))
        self.quality.sliderReleased.connect(
            lambda: self.save("output", jpeg_quality=self.quality.value())
        )
        self.quality.valueChanged.connect(self._quality_keys)
        self.dpi = QSpinBox()
        self.dpi.setRange(72, 600)
        self.dpi.setSingleStep(25)
        self.dpi.setSuffix(" DPI")
        self.dpi.valueChanged.connect(lambda v: self.save("output", pdf_dpi=v))
        self.format_change = QCheckBox("Compress to a size may turn a PNG into a WebP")
        self.format_change.toggled.connect(
            lambda on: self.save("output", allow_format_change_for_size=on)
        )
        self.form.addRow("Save outputs", _row(self.location))
        self.form.addRow("", _row(self.folder, browse))
        self.form.addRow("JPEG / WebP quality", _row(self.quality, self.quality_value))
        self.form.addRow("PDF pages as images", _row(self.dpi))
        self.form.addRow("", self.format_change)

    def _load(self, config: Config) -> None:
        o = config.output
        fixed = o.location == "fixed_folder"
        self.location.set_value("One folder" if fixed else "Next to the file")
        self.folder.setText(o.fixed_folder or "No folder chosen")
        self.folder.setVisible(fixed)
        self.browse.setVisible(fixed)
        self.quality.setValue(o.jpeg_quality)
        self.quality_value.setText(str(o.jpeg_quality))
        self.dpi.setValue(o.pdf_dpi)
        self.format_change.setChecked(o.allow_format_change_for_size)

    def _quality_keys(self, value: int) -> None:
        if not self.quality.isSliderDown():  # keyboard or a click on the groove
            self.save("output", jpeg_quality=value)

    def _location(self, name: str) -> None:
        if self.loading:
            return
        if name == "Next to the file":
            self.save("output", location="next_to_source")
        elif self.config.output.fixed_folder:
            self.save("output", location="fixed_folder")
        else:
            self._browse()
            if not self.config.output.fixed_folder:  # no folder chosen: stay next to the file
                self.load(self.config)

    def _browse(self) -> None:
        start = self.config.output.fixed_folder or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Save outputs in", start)
        if folder:
            self.save("output", location="fixed_folder", fixed_folder=str(Path(folder)))


# --- General ---------------------------------------------------------------------------------
class GeneralPage(_Page):
    def __init__(self, window: "SettingsWindow") -> None:
        super().__init__(window)
        self.autostart = QCheckBox("Start DragShift when I sign in")
        self.autostart.toggled.connect(lambda on: self.save("general", autostart=on))
        self.trim = QCheckBox("Trim memory when idle")
        self.trim.toggled.connect(lambda on: self.save("general", trim_memory_when_idle=on))
        self.quick = QCheckBox("Quick Launch (tap a key on an empty desktop)")
        self.quick.toggled.connect(lambda on: self.save("launcher", enabled=on))
        self.blocklist = QListWidget()
        self.blocklist.setFixedHeight(150)
        self.entry = QLineEdit()
        self.entry.setPlaceholderText("game.exe")
        self.entry.returnPressed.connect(self._add)
        add, browse, remove = (
            secondary_button("Add"),
            secondary_button("Browse…"),
            secondary_button("Remove"),
        )
        add.clicked.connect(self._add)
        browse.clicked.connect(self._browse)
        remove.clicked.connect(self._remove)
        self.form.addRow(self.autostart)
        self.form.addRow(self.trim)
        self.form.addRow(self.quick)
        self.form.addRow(_hint("DragShift ignores taps while one of these apps is in front:"))
        self.form.addRow(self.blocklist)
        self.form.addRow(_row(self.entry, add, browse, remove, stretch=False))

    def _load(self, config: Config) -> None:
        g = config.general
        self.autostart.setChecked(g.autostart)
        self.trim.setChecked(g.trim_memory_when_idle)
        self.quick.setChecked(config.launcher.enabled)
        self.blocklist.clear()
        self.blocklist.addItems(list(g.blocklist))

    def add_entry(self, entry: str) -> None:
        entry = entry.strip()
        known = {e.lower() for e in self.config.general.blocklist}
        if entry and entry.lower() not in known:
            self.save("general", blocklist=(*self.config.general.blocklist, entry))

    def _add(self) -> None:
        self.add_entry(self.entry.text())
        self.entry.clear()

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Pick an app", "C:/", "Apps (*.exe)")
        if path:
            self.add_entry(Path(path).name)

    def _remove(self) -> None:
        row = self.blocklist.currentRow()
        entries = list(self.config.general.blocklist)
        if 0 <= row < len(entries):
            del entries[row]
            self.save("general", blocklist=tuple(entries))


# --- the window ------------------------------------------------------------------------------
class SettingsWindow(GlassDialog):
    reset_usage = Signal()

    def __init__(self, store: ConfigStore, theme: Theme) -> None:
        super().__init__("Settings", theme)
        self.store = store
        self.setFixedSize(SIZE)
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setFixedWidth(150)
        self.nav.addItems(list(TABS))
        self.pages = QStackedWidget()
        self.keys = KeysPage(self)
        self.wheels = WheelsPage(self)
        self.launcher = LauncherPage(self)
        self.appearance = AppearancePage(self)
        self.output = OutputPage(self)
        self.general = GeneralPage(self)
        for page in self._pages():
            self.pages.addWidget(page)
        self.nav.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.nav.setCurrentRow(0)
        row = QHBoxLayout()
        row.setSpacing(16)
        row.addWidget(self.nav)
        row.addWidget(self.pages, 1)
        self.body.addLayout(row)
        self._saving = False
        store.changed.connect(self._external_change)
        self._load(store.config)
        # Last: lists filled before they had this window as a parent keep the default style.
        self.setStyleSheet(stylesheet(theme) + _extra_css(theme))

    def _pages(self) -> list[_Page]:
        return [self.keys, self.wheels, self.launcher, self.appearance, self.output, self.general]

    def show_tab(self, name: str) -> None:
        self.nav.setCurrentRow(TABS.index(name))

    def set_theme(self, theme: Theme) -> None:
        super().set_theme(theme)
        self.setStyleSheet(stylesheet(theme) + _extra_css(theme))

    def save(self, section: str, **changes: Any) -> None:
        """Replace fields of one config section and save it now (FR-8.2)."""
        config = self.store.config
        new = dataclasses.replace(
            config, **{section: dataclasses.replace(getattr(config, section), **changes)}
        )
        self._saving = True
        try:
            self.store.update(new)
        finally:
            self._saving = False
        # Show what was saved (a swapped key, slots renumbered), once the event that made the
        # change is over: a list must not be cleared inside its own drop.
        QTimer.singleShot(0, self, lambda: self._load(self.store.config))

    def _external_change(self, config: Config) -> None:
        if not self._saving:
            self._load(config)

    def _load(self, config: Config) -> None:
        for page in self._pages():
            page.load(config)

    def done(self, result: int) -> None:  # ✕ and close() both end here
        try:
            self.store.changed.disconnect(self._external_change)
        except (RuntimeError, TypeError):
            pass
        super().done(result)
