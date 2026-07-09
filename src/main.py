"""Kindergarten Photo Picker — main window and entry point.

Workflow
--------
1. Open a folder of photos.
2. Analyze (Ctrl+Shift+A) → blur + duplicate detection + EXIF timestamps.
3. (optional) Analyze → Skip blurry / Skip duplicates to prune obvious cruft.
4. Manage your events (Ctrl+G) and give each a day + start + end time.
5. Analyze → Auto-tag events by schedule. Photos get tagged with 'auto'
   provenance and wait for your approval.
6. Review each photo:
     - Space           approve the auto-tag as-is
     - event hotkey    reassign to a different event (counts as manual)
     - kid hotkey      toggle kid presence
     - Backspace       skip (not to use)
7. Turn on View → Hide already-reviewed to keep the queue tight.
8. Export by event → one folder per event, only APPROVED photos.

Reserved keybindings (do not use these as kid/event hotkeys)
------------------------------------------------------------
    Left / Right          previous / next photo (in the current filter view)
    PageUp / PageDown     jump 20
    Home / End            first / last
    Space                 approve current auto-tag
    Backspace             toggle skip
    Ctrl+O                open folder
    Ctrl+K                manage kids
    Ctrl+G                manage events
    Ctrl+Shift+A          analyze folder
    Ctrl+Shift+E          export by event
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt, QObject, QThread, Signal
from PySide6.QtGui import QAction, QColor, QKeySequence, QPalette, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QVBoxLayout,
    QWidget,
)

from .analysis import analyze_one, group_duplicates
from .db import Event, Kid, PhotoState, ProjectDB
from .dialogs import EntityRow, ManageEntitiesDialog
from .widgets import EntityPanel, PhotoView

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".bmp", ".tif", ".tiff"}
BLUR_DEFAULT_THRESHOLD = 60.0
JUMP = 20


# --------------------------------------------------------------------------
# Folder scanning
# --------------------------------------------------------------------------

def scan_folder(folder: Path) -> list[str]:
    results: list[str] = []
    for p in folder.rglob("*"):
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS and not p.name.startswith("."):
            results.append(str(p.relative_to(folder)))
    results.sort()
    return results


# --------------------------------------------------------------------------
# Background analysis worker (blur + pHash + dedup grouping)
# --------------------------------------------------------------------------

class AnalysisWorker(QObject):
    progress = Signal(int, int)  # done, total
    finished = Signal(int, int)  # analyzed_count, dup_group_count
    failed = Signal(str)

    def __init__(self, folder: Path) -> None:
        super().__init__()
        self.folder = folder
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            db = ProjectDB(self.folder)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"Could not open project DB: {e}")
            return
        try:
            todo = db.paths_needing_analysis()
            total = len(todo)
            batch: list[tuple[str, float, int, Optional[str]]] = []
            for i, rel in enumerate(todo):
                if self._cancel:
                    break
                try:
                    r = analyze_one(self.folder / rel)
                    taken_iso = r.taken_at.isoformat() if r.taken_at else None
                    batch.append((rel, r.blur_score, r.phash, taken_iso))
                except Exception:
                    pass  # unreadable image — skip, retry next run
                if len(batch) >= 50:
                    db.set_analysis_many(batch)
                    batch = []
                self.progress.emit(i + 1, total)
            if batch:
                db.set_analysis_many(batch)

            phashes = db.all_phashes()
            groups = group_duplicates(phashes)
            db.set_dup_groups(groups)
            n_groups = len({g for g in groups.values() if g >= 0})
            self.finished.emit(total, n_groups)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))
        finally:
            db.close()


# --------------------------------------------------------------------------
# Export helper — one folder per event
# --------------------------------------------------------------------------

_UNSAFE_FS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_folder_name(name: str) -> str:
    cleaned = _UNSAFE_FS_CHARS.sub("_", name).strip().rstrip(".")
    return cleaned or "unnamed"


def export_by_event(
    photo_root: Path, target_root: Path, db: ProjectDB
) -> tuple[dict[str, int], list[str]]:
    """Copy each *approved* photo into a subfolder named after its event.

    Only photos with `event_source='manual'` are exported — pending auto-tags
    that the user hasn't confirmed stay behind, so nothing unreviewed leaks
    out. Returns a dict `{event_name: photos_copied}` and a list of errors."""
    per_event: dict[str, int] = {}
    errors: list[str] = []
    for event in db.list_events():
        rels = db.event_photos(event.id, only_manual=True)
        if not rels:
            continue
        subdir = target_root / sanitize_folder_name(event.name)
        subdir.mkdir(parents=True, exist_ok=True)
        copied = 0
        for rel in rels:
            src = photo_root / rel
            if not src.exists():
                errors.append(f"missing: {rel}")
                continue
            dst = subdir / Path(rel).name
            n = 1
            while dst.exists():
                dst = subdir / f"{Path(rel).stem}_{n}{Path(rel).suffix}"
                n += 1
            try:
                shutil.copy2(src, dst)
                copied += 1
            except OSError as e:
                errors.append(f"{rel}: {e}")
        per_event[event.name] = copied
    return per_event, errors


# --------------------------------------------------------------------------
# MainWindow
# --------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Kindergarten Photo Picker")
        self.resize(1360, 900)

        self.db: Optional[ProjectDB] = None
        self.folder: Optional[Path] = None
        self.all_paths: list[str] = []
        self.view_paths: list[str] = []
        self.view_index = 0
        self.kids: list[Kid] = []
        self.events: list[Event] = []
        self._dynamic_shortcuts: list[QShortcut] = []
        self._analysis_dlg: Optional[QProgressDialog] = None
        self._analysis_thread: Optional[QThread] = None
        self._analysis_worker: Optional[AnalysisWorker] = None

        # Filter state
        self.hide_blurry = False
        self.hide_duplicates = False
        self.hide_reviewed = False
        self.blur_threshold = BLUR_DEFAULT_THRESHOLD

        self._build_ui()
        self._build_menu()
        self._install_reserved_shortcuts()
        self._render_empty()

    # ---------- UI ----------

    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        self.header = QLabel()
        self.header.setStyleSheet(
            "background:#1e1e1e;color:#eee;padding:8px 12px;border-radius:6px;font-size:13px;"
        )
        self.header.setWordWrap(True)
        root.addWidget(self.header)

        self.view = PhotoView()
        root.addWidget(self.view, 1)

        info_row = QHBoxLayout()
        info_row.setSpacing(8)
        self.filename_label = QLabel("—")
        self.filename_label.setStyleSheet(
            "color:#ccc;padding:6px 10px;background:#1e1e1e;border-radius:6px;font-size:13px;"
        )
        self.blur_badge = QLabel("")
        self.blur_badge.setAlignment(Qt.AlignCenter)
        self.blur_badge.setFixedWidth(120)
        self.dup_badge = QLabel("")
        self.dup_badge.setAlignment(Qt.AlignCenter)
        self.dup_badge.setFixedWidth(170)
        self.status_badge = QLabel("")
        self.status_badge.setAlignment(Qt.AlignCenter)
        self.status_badge.setFixedWidth(280)
        info_row.addWidget(self.filename_label, 1)
        info_row.addWidget(self.blur_badge)
        info_row.addWidget(self.dup_badge)
        info_row.addWidget(self.status_badge)
        root.addLayout(info_row)

        entities_row = QHBoxLayout()
        entities_row.setSpacing(12)
        self.kids_panel = EntityPanel(
            "KIDS  (press hotkey to toggle in current photo)",
            on_activate=lambda kid: self._toggle_kid(kid.bit),
            on_manage=self._manage_kids,
        )
        self.events_panel = EntityPanel(
            "EVENTS  (press hotkey to assign current photo)",
            on_activate=lambda ev: self._activate_event(ev.id),
            on_manage=self._manage_events,
        )
        entities_row.addWidget(self.kids_panel, 1)
        divider = QFrame()
        divider.setFrameShape(QFrame.VLine)
        divider.setStyleSheet("color:#333;")
        entities_row.addWidget(divider)
        entities_row.addWidget(self.events_panel, 1)
        entities_wrap = QWidget()
        entities_wrap.setLayout(entities_row)
        entities_wrap.setMinimumHeight(140)
        entities_wrap.setMaximumHeight(360)
        root.addWidget(entities_wrap)

        self.setCentralWidget(central)

    def _build_menu(self) -> None:
        m_file = self.menuBar().addMenu("&File")
        act_open = QAction("&Open folder…", self)
        act_open.setShortcut(QKeySequence("Ctrl+O"))
        act_open.triggered.connect(self._open_folder)
        m_file.addAction(act_open)
        m_file.addSeparator()
        act_export = QAction("&Export by event…", self)
        act_export.setShortcut(QKeySequence("Ctrl+Shift+E"))
        act_export.triggered.connect(self._export_by_event)
        m_file.addAction(act_export)
        m_file.addSeparator()
        act_quit = QAction("&Quit", self)
        act_quit.setShortcut(QKeySequence("Ctrl+Q"))
        act_quit.triggered.connect(self.close)
        m_file.addAction(act_quit)

        m_edit = self.menuBar().addMenu("&Edit")
        act_kids = QAction("Manage &kids…", self)
        act_kids.setShortcut(QKeySequence("Ctrl+K"))
        act_kids.triggered.connect(self._manage_kids)
        m_edit.addAction(act_kids)
        act_events = QAction("Manage e&vents…", self)
        act_events.setShortcut(QKeySequence("Ctrl+G"))
        act_events.triggered.connect(self._manage_events)
        m_edit.addAction(act_events)

        m_view = self.menuBar().addMenu("&View")
        self.act_hide_blurry = QAction("Hide &blurry photos", self, checkable=True)
        self.act_hide_blurry.toggled.connect(self._on_filter_toggle)
        m_view.addAction(self.act_hide_blurry)
        self.act_hide_dups = QAction("Hide &duplicate photos", self, checkable=True)
        self.act_hide_dups.toggled.connect(self._on_filter_toggle)
        m_view.addAction(self.act_hide_dups)
        self.act_hide_reviewed = QAction(
            "Hide already-&reviewed photos", self, checkable=True
        )
        self.act_hide_reviewed.toggled.connect(self._on_filter_toggle)
        m_view.addAction(self.act_hide_reviewed)
        m_view.addSeparator()
        act_thresh = QAction("Set blur &threshold…", self)
        act_thresh.triggered.connect(self._set_blur_threshold)
        m_view.addAction(act_thresh)

        m_analyze = self.menuBar().addMenu("&Analyze")
        act_scan = QAction("&Scan folder (blur + duplicates + EXIF)", self)
        act_scan.setShortcut(QKeySequence("Ctrl+Shift+A"))
        act_scan.triggered.connect(self._analyze)
        m_analyze.addAction(act_scan)
        m_analyze.addSeparator()
        act_skip_blurry = QAction("Skip &blurry photos…", self)
        act_skip_blurry.triggered.connect(self._skip_blurry_action)
        m_analyze.addAction(act_skip_blurry)
        act_skip_dups = QAction("Skip &duplicates (keep sharpest)", self)
        act_skip_dups.triggered.connect(self._skip_duplicates_action)
        m_analyze.addAction(act_skip_dups)
        m_analyze.addSeparator()
        act_auto_tag = QAction("Auto-&tag events by schedule", self)
        act_auto_tag.triggered.connect(self._auto_tag_events_action)
        m_analyze.addAction(act_auto_tag)

    def _install_reserved_shortcuts(self) -> None:
        def sc(seq: str, slot: Callable[[], None]) -> None:
            s = QShortcut(QKeySequence(seq), self)
            s.activated.connect(slot)

        sc("Right", lambda: self._step(1))
        sc("Left", lambda: self._step(-1))
        sc("PgDown", lambda: self._step(JUMP))
        sc("PgUp", lambda: self._step(-JUMP))
        sc("Home", lambda: self._goto(0))
        sc("End", lambda: self._goto(len(self.view_paths) - 1))
        sc("Backspace", self._toggle_skip)
        sc("Space", self._approve_current)

    # ---------- dynamic hotkeys ----------

    def _reinstall_dynamic_shortcuts(self) -> None:
        for s in self._dynamic_shortcuts:
            s.setEnabled(False)
            s.deleteLater()
        self._dynamic_shortcuts.clear()

        def add(seq: str, slot: Callable[[], None]) -> None:
            s = QShortcut(QKeySequence(seq), self)
            s.activated.connect(slot)
            self._dynamic_shortcuts.append(s)

        for kid in self.kids:
            if kid.hotkey:
                add(kid.hotkey, lambda k=kid: self._toggle_kid(k.bit))
        for event in self.events:
            if event.hotkey:
                add(event.hotkey, lambda e=event: self._activate_event(e.id))

    # ---------- folder loading ----------

    def _open_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose photo folder")
        if not folder:
            return
        self._load_folder(Path(folder))

    def _load_folder(self, folder: Path) -> None:
        if self.db is not None:
            self.db.close()
            self.db = None
        self.folder = folder
        self.db = ProjectDB(folder)

        self.header.setText(f"Scanning {folder}…")
        QApplication.processEvents()
        self.all_paths = scan_folder(folder)
        self.db.sync_paths(self.all_paths)

        last = self.db.get_setting("last_index")
        try:
            initial_index = int(last) if last else 0
        except ValueError:
            initial_index = 0

        self._refresh_entities()
        self._refresh_view_paths()

        # Try to preserve position — find the previously-viewed path in the view.
        if 0 <= initial_index < len(self.all_paths):
            desired = self.all_paths[initial_index]
            self.view_index = self.view_paths.index(desired) if desired in self.view_paths else 0
        else:
            self.view_index = 0

        if not self.all_paths:
            QMessageBox.information(
                self, "No photos", f"No image files were found under:\n{folder}"
            )
        self._render_all()

    def _refresh_entities(self) -> None:
        if self.db is None:
            self.kids = []
            self.events = []
        else:
            self.kids = self.db.list_kids()
            self.events = self.db.list_events()
        self.kids_panel.set_entities(self.kids)
        self.events_panel.set_entities(self.events)
        self._reinstall_dynamic_shortcuts()

    # ---------- filter / view ----------

    def _on_filter_toggle(self, _checked: bool) -> None:
        self.hide_blurry = self.act_hide_blurry.isChecked()
        self.hide_duplicates = self.act_hide_dups.isChecked()
        self.hide_reviewed = self.act_hide_reviewed.isChecked()
        self._refresh_view_paths(preserve_current=True)
        self._render_all()

    def _set_blur_threshold(self) -> None:
        threshold, ok = QInputDialog.getDouble(
            self,
            "Blur threshold",
            "Photos scoring below this value are considered blurry\n"
            "(higher = stricter — only very sharp photos survive):",
            value=self.blur_threshold,
            minValue=0.0,
            maxValue=100000.0,
            decimals=1,
        )
        if ok:
            self.blur_threshold = threshold
            self._refresh_view_paths(preserve_current=True)
            self._render_all()

    def _refresh_view_paths(self, preserve_current: bool = False) -> None:
        """Rebuild the filtered navigation queue. If preserve_current is set,
        try to keep the current photo in view; otherwise anchor to index 0."""
        current_path = self._current_path() if preserve_current else None
        if self.db is None:
            self.view_paths = []
            self.view_index = 0
            return
        self.view_paths = self.db.filtered_paths(
            hide_blurry=self.hide_blurry,
            blur_threshold=self.blur_threshold,
            hide_duplicates=self.hide_duplicates,
            hide_reviewed=self.hide_reviewed,
        )
        if not self.view_paths:
            self.view_index = 0
            return
        if current_path and current_path in self.view_paths:
            self.view_index = self.view_paths.index(current_path)
        else:
            # Try to land on the first path >= current_path so we don't
            # teleport across the folder.
            self.view_index = 0
            if current_path:
                for i, p in enumerate(self.view_paths):
                    if p >= current_path:
                        self.view_index = i
                        break

    # ---------- navigation ----------

    def _current_path(self) -> Optional[str]:
        if not self.view_paths:
            return None
        if 0 <= self.view_index < len(self.view_paths):
            return self.view_paths[self.view_index]
        return None

    def _current_abs(self) -> Optional[Path]:
        rel = self._current_path()
        if rel is None or self.folder is None:
            return None
        return self.folder / rel

    def _step(self, delta: int) -> None:
        if not self.view_paths:
            return
        self._goto(self.view_index + delta)

    def _goto(self, new_index: int) -> None:
        if not self.view_paths:
            return
        self.view_index = max(0, min(new_index, len(self.view_paths) - 1))
        if self.db is not None:
            rel = self._current_path()
            if rel is not None and rel in self.all_paths:
                self.db.set_setting("last_index", str(self.all_paths.index(rel)))
        self._render_all()

    # ---------- photo actions ----------

    def _toggle_kid(self, bit: int) -> None:
        rel = self._current_path()
        if rel is None or self.db is None:
            return
        self.db.toggle_kid(rel, bit)
        # Toggling a kid may complete or un-complete review; refilter if that
        # matters.
        self._refilter_after_change()
        self._render_all()

    def _activate_event(self, event_id: int) -> None:
        rel = self._current_path()
        if rel is None or self.db is None:
            return
        self.db.set_event(rel, event_id)
        self._refilter_after_change()
        self._render_all()

    def _toggle_skip(self) -> None:
        rel = self._current_path()
        if rel is None or self.db is None:
            return
        self.db.toggle_skipped(rel)
        self._refilter_after_change()
        self._render_all()

    def _refilter_after_change(self) -> None:
        """If hide_reviewed is on and the current photo just became reviewed,
        drop it from the view queue and advance to the next photo."""
        if not self.hide_reviewed:
            return
        current = self._current_path()
        self._refresh_view_paths(preserve_current=False)
        # After a change with hide_reviewed on, the current photo is likely
        # gone. Land on the same slot in the new list so review flows forward.
        if current and current not in self.view_paths:
            # view_index stays; if it's past the end after removal, clamp.
            if self.view_paths:
                self.view_index = min(self.view_index, len(self.view_paths) - 1)
            else:
                self.view_index = 0

    # ---------- manage kids/events ----------

    def _manage_kids(self) -> None:
        if self.db is None:
            QMessageBox.information(self, "Manage kids", "Open a folder first.")
            return
        rows = [
            EntityRow(key=k.bit, name=k.name, hotkey=k.hotkey) for k in self.db.list_kids()
        ]
        reserved = {e.hotkey for e in self.db.list_events() if e.hotkey}
        dlg = ManageEntitiesDialog(
            self,
            title="Manage kids",
            singular_noun="kid",
            rows=rows,
            reserved_hotkeys=reserved,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._apply_kid_changes(dlg.result_rows(), dlg.deleted_keys())
        self._refresh_entities()
        self._render_all()

    def _manage_events(self) -> None:
        if self.db is None:
            QMessageBox.information(self, "Manage events", "Open a folder first.")
            return
        rows = [
            EntityRow(
                key=e.id,
                name=e.name,
                hotkey=e.hotkey,
                day_of_week=e.day_of_week,
                start_time=e.start_time,
                end_time=e.end_time,
            )
            for e in self.db.list_events()
        ]
        reserved = {k.hotkey for k in self.db.list_kids() if k.hotkey}
        dlg = ManageEntitiesDialog(
            self,
            title="Manage events",
            singular_noun="event",
            rows=rows,
            reserved_hotkeys=reserved,
            show_schedule=True,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._apply_event_changes(dlg.result_rows(), dlg.deleted_keys())
        self._refresh_entities()
        self._render_all()

    def _apply_kid_changes(self, rows: list[EntityRow], deleted_keys: list[int]) -> None:
        assert self.db is not None
        for key in deleted_keys:
            self.db.delete_kid(key)
        surviving_bits: list[int] = []
        for row in rows:
            if row.key is None:
                new_kid = self.db.add_kid(row.name, row.hotkey)
                surviving_bits.append(new_kid.bit)
            else:
                self.db.update_kid(row.key, row.name, row.hotkey)
                surviving_bits.append(row.key)
        self.db.reorder_kids(surviving_bits)

    def _apply_event_changes(self, rows: list[EntityRow], deleted_keys: list[int]) -> None:
        assert self.db is not None
        for key in deleted_keys:
            self.db.delete_event(key)
        surviving_ids: list[int] = []
        for row in rows:
            if row.key is None:
                new_ev = self.db.add_event(
                    row.name,
                    row.hotkey,
                    day_of_week=row.day_of_week,
                    start_time=row.start_time,
                    end_time=row.end_time,
                )
                surviving_ids.append(new_ev.id)
            else:
                self.db.update_event(
                    row.key,
                    row.name,
                    row.hotkey,
                    day_of_week=row.day_of_week,
                    start_time=row.start_time,
                    end_time=row.end_time,
                )
                surviving_ids.append(row.key)
        self.db.reorder_events(surviving_ids)

    # ---------- analysis + bulk cleanups ----------

    def _analyze(self) -> None:
        if self.db is None or self.folder is None or not self.all_paths:
            QMessageBox.information(self, "Analyze", "Open a folder first.")
            return
        todo_count = len(self.db.paths_needing_analysis())
        dlg = QProgressDialog(
            f"Analyzing {todo_count} new photo(s)…" if todo_count else "Regrouping duplicates…",
            "Cancel",
            0,
            max(todo_count, 1),
            self,
        )
        dlg.setWindowTitle("Analyze folder")
        dlg.setWindowModality(Qt.ApplicationModal)
        dlg.setMinimumDuration(0)
        dlg.setValue(0)

        thread = QThread(self)
        worker = AnalysisWorker(self.folder)
        worker.moveToThread(thread)

        # Store handles as attrs so the signal handlers below (bound methods
        # on MainWindow — which lives on the main thread) can reach them
        # without needing a receiver-less lambda. A lambda without a receiver
        # is invoked on the emitter (worker) thread, and touching a QWidget
        # from a non-main thread hard-crashes macOS.
        self._analysis_dlg = dlg
        self._analysis_thread = thread
        self._analysis_worker = worker

        worker.progress.connect(self._on_analysis_progress)
        worker.finished.connect(self._on_analysis_finished)
        worker.failed.connect(self._on_analysis_failed)
        dlg.canceled.connect(worker.cancel)
        thread.started.connect(worker.run)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        thread.start()
        dlg.exec()

    def _on_analysis_progress(self, done: int, total: int) -> None:
        if self._analysis_dlg is None:
            return
        self._analysis_dlg.setMaximum(max(total, 1))
        self._analysis_dlg.setValue(done)

    def _on_analysis_finished(self, analyzed: int, groups: int) -> None:
        dlg = self._analysis_dlg
        thread = self._analysis_thread
        if dlg is not None:
            dlg.setValue(dlg.maximum())
            dlg.close()
        if thread is not None:
            thread.quit()
        self._analysis_dlg = None
        QMessageBox.information(
            self,
            "Analyze complete",
            f"Analyzed {analyzed} photo(s).\n"
            f"{groups} duplicate group(s) found across the folder.",
        )
        self._refresh_view_paths(preserve_current=True)
        self._render_all()

    def _on_analysis_failed(self, msg: str) -> None:
        dlg = self._analysis_dlg
        thread = self._analysis_thread
        if dlg is not None:
            dlg.close()
        if thread is not None:
            thread.quit()
        self._analysis_dlg = None
        QMessageBox.warning(self, "Analyze failed", msg)

    def _skip_blurry_action(self) -> None:
        if self.db is None:
            QMessageBox.information(self, "Bulk action", "Open a folder first.")
            return
        threshold, ok = QInputDialog.getDouble(
            self,
            "Skip blurry photos",
            "Photos scoring below this blur value will be marked skipped\n"
            "(only untouched photos are affected — anything already tagged is left alone):",
            value=self.blur_threshold,
            minValue=0.0,
            maxValue=100000.0,
            decimals=1,
        )
        if not ok:
            return
        n = self.db.skip_blurry_below(threshold)
        QMessageBox.information(
            self, "Skip blurry", f"Marked {n} photo(s) as skipped."
        )
        self._refresh_view_paths(preserve_current=True)
        self._render_all()

    def _skip_duplicates_action(self) -> None:
        if self.db is None:
            QMessageBox.information(self, "Bulk action", "Open a folder first.")
            return
        touched, skipped = self.db.skip_duplicates_keep_sharpest()
        QMessageBox.information(
            self,
            "Skip duplicates",
            f"Cleaned {touched} duplicate group(s); "
            f"marked {skipped} photo(s) as skipped (kept sharpest untouched).",
        )
        self._refresh_view_paths(preserve_current=True)
        self._render_all()

    def _auto_tag_events_action(self) -> None:
        if self.db is None:
            QMessageBox.information(self, "Auto-tag", "Open a folder first.")
            return
        scheduled = [e for e in self.db.list_events() if e.has_schedule()]
        if not scheduled:
            QMessageBox.information(
                self,
                "Auto-tag",
                "None of your events have a day + start + end set. Add a "
                "schedule in Edit → Manage events first.",
            )
            return
        matched, ambiguous, unmatched = self.db.auto_tag_events()
        QMessageBox.information(
            self,
            "Auto-tag events",
            f"Assigned {matched} photo(s) to an event automatically.\n"
            f"{ambiguous} photo(s) matched multiple events (left untagged for you).\n"
            f"{unmatched} photo(s) matched no scheduled event.\n\n"
            "Auto-tags are pending your review — press Space on each to approve, "
            "or a different event hotkey to reassign.",
        )
        self._refresh_view_paths(preserve_current=True)
        self._render_all()

    # ---------- approve ----------

    def _approve_current(self) -> None:
        rel = self._current_path()
        if rel is None or self.db is None:
            return
        promoted = self.db.approve_current_event(rel)
        if not promoted:
            return
        self._refilter_after_change()
        self._render_all()

    # ---------- export ----------

    def _export_by_event(self) -> None:
        if self.db is None or self.folder is None:
            QMessageBox.information(self, "Export", "Open a folder first.")
            return
        events = self.db.list_events()
        if not events:
            QMessageBox.information(self, "Export", "You haven't defined any events yet.")
            return
        target = QFileDialog.getExistingDirectory(self, "Choose export target (parent folder)")
        if not target:
            return
        target_root = Path(target)

        per_event, errors = export_by_event(self.folder, target_root, self.db)
        total_copied = sum(per_event.values())
        summary = f"Exported {total_copied} photo(s) to:\n{target_root}\n\n"
        if per_event:
            summary += "By event:\n"
            for name, n in per_event.items():
                summary += f"  • {name}: {n}\n"
        else:
            summary += "No events had any photos assigned."
        if errors:
            summary += f"\nErrors ({len(errors)}):\n" + "\n".join(errors[:20])
            if len(errors) > 20:
                summary += f"\n… and {len(errors) - 20} more"
        QMessageBox.information(self, "Export complete", summary)

    # ---------- rendering ----------

    def _render_empty(self) -> None:
        self.header.setText("Open a folder to begin — File → Open folder (Ctrl+O)")
        self.filename_label.setText("—")
        self.view.set_photo(None)
        self._render_badges(None)
        self.kids_panel.set_active(set())
        self.events_panel.set_active(set())

    def _render_all(self) -> None:
        self._render_header()
        self._render_current_photo()
        self._render_counts()

    def _render_header(self) -> None:
        if self.db is None or self.folder is None:
            self.header.setText("Open a folder to begin — File → Open folder (Ctrl+O)")
            return
        total, reviewed, skipped, pending_auto, untouched = self.db.review_counts()
        view_total = len(self.view_paths)
        pos = self.view_index + 1 if self.view_paths else 0
        filters = []
        if self.hide_blurry:
            filters.append(f"blurry<{self.blur_threshold:g}")
        if self.hide_duplicates:
            filters.append("duplicates")
        if self.hide_reviewed:
            filters.append("reviewed")
        filter_bit = f"   [filter: hiding {', '.join(filters)}]" if filters else ""
        hidden = total - view_total
        hidden_bit = f"   ({hidden} hidden)" if hidden else ""
        self.header.setText(
            f"📁 {self.folder}\n"
            f"Photo {pos} / {view_total}{hidden_bit}   "
            f"|   ✅ Reviewed: {reviewed}   ⏭ Skipped: {skipped}   "
            f"🤖 Auto-pending: {pending_auto}   ◻ Untouched: {untouched}   "
            f"/   Total: {total}"
            f"{filter_bit}"
        )

    def _render_current_photo(self) -> None:
        abs_path = self._current_abs()
        rel = self._current_path()
        self.view.set_photo(abs_path)
        self.filename_label.setText(rel or "—")
        state = self.db.get_photo(rel) if (self.db and rel) else None
        self._render_badges(state)
        if state is not None:
            active_kids = {kid.bit for kid in self.kids if state.kids_mask & (1 << kid.bit)}
            self.kids_panel.set_active(active_kids)
            self.events_panel.set_active({state.event_id} if state.event_id is not None else set())
        else:
            self.kids_panel.set_active(set())
            self.events_panel.set_active(set())

    def _render_badges(self, state: Optional[PhotoState]) -> None:
        # blur badge
        if state is None or state.blur_score is None:
            self.blur_badge.setText("blur: —")
            self.blur_badge.setStyleSheet(
                "background:#2a2a2a;color:#666;padding:6px;border-radius:6px;font-size:12px;"
            )
        elif state.blur_score < self.blur_threshold:
            self.blur_badge.setText(f"⚠ BLURRY\n{state.blur_score:.0f}")
            self.blur_badge.setStyleSheet(
                "background:#7a2a2a;color:#fff;padding:6px;border-radius:6px;"
                "font-size:12px;font-weight:700;"
            )
        else:
            self.blur_badge.setText(f"blur: {state.blur_score:.0f}")
            self.blur_badge.setStyleSheet(
                "background:#2a2a2a;color:#8bc;padding:6px;border-radius:6px;font-size:12px;"
            )

        # dup badge
        if state is None or state.dup_group_id < 0 or self.db is None:
            self.dup_badge.setText("no duplicates")
            self.dup_badge.setStyleSheet(
                "background:#2a2a2a;color:#666;padding:6px;border-radius:6px;font-size:12px;"
            )
        else:
            pos, size = self.db.dup_group_position(state.path)
            self.dup_badge.setText(f"⧉ DUP GROUP #{state.dup_group_id}\n{pos} of {size}")
            self.dup_badge.setStyleSheet(
                "background:#7a5a2a;color:#fff;padding:6px;border-radius:6px;"
                "font-size:12px;font-weight:700;"
            )

        # status badge (unreviewed / auto-pending / manual / skipped)
        if state is None:
            self.status_badge.setText("")
            self.status_badge.setStyleSheet("")
            return
        if state.skipped:
            self.status_badge.setText("⏭ SKIPPED   (Backspace to undo)")
            self.status_badge.setStyleSheet(
                "background:#555;color:white;font-weight:700;font-size:13px;"
                "border-radius:6px;padding:8px;"
            )
        elif state.event_id is not None:
            event_name = next(
                (e.name for e in self.events if e.id == state.event_id), "?"
            )
            if state.event_source == "auto":
                self.status_badge.setText(f"🤖 AUTO → {event_name}\nSpace to approve")
                self.status_badge.setStyleSheet(
                    "background:#a86a1a;color:white;font-weight:700;font-size:12px;"
                    "border-radius:6px;padding:6px;"
                )
            else:
                self.status_badge.setText(f"✅ → {event_name}")
                self.status_badge.setStyleSheet(
                    "background:#2ea043;color:white;font-weight:700;font-size:13px;"
                    "border-radius:6px;padding:8px;"
                )
        else:
            self.status_badge.setText("◻ untouched")
            self.status_badge.setStyleSheet(
                "background:#333;color:#aaa;font-size:13px;border-radius:6px;padding:8px;"
            )

    def _render_counts(self) -> None:
        if self.db is None:
            return
        self.kids_panel.set_counts({k.bit: self.db.count_by_kid(k.bit) for k in self.kids})
        self.events_panel.set_counts(
            {e.id: self.db.count_by_event(e.id) for e in self.events}
        )

    # ---------- lifecycle ----------

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.db is not None:
            self.db.close()
        super().closeEvent(event)


def _apply_dark_palette(app: QApplication) -> None:
    """Force a consistent dark theme regardless of the host OS's system theme.

    Without this, macOS light-mode users get an unstyled window+panel background
    that clashes with our explicitly-dark headers/badges and hides the panel
    title text ('KIDS', 'EVENTS') by rendering light-gray on light-gray."""
    app.setStyle("Fusion")
    p = QPalette()
    p.setColor(QPalette.Window,          QColor(30, 30, 30))
    p.setColor(QPalette.WindowText,      QColor(220, 220, 220))
    p.setColor(QPalette.Base,            QColor(20, 20, 20))
    p.setColor(QPalette.AlternateBase,   QColor(40, 40, 40))
    p.setColor(QPalette.Text,            QColor(220, 220, 220))
    p.setColor(QPalette.Button,          QColor(42, 42, 42))
    p.setColor(QPalette.ButtonText,      QColor(220, 220, 220))
    p.setColor(QPalette.BrightText,      QColor(255, 80, 80))
    p.setColor(QPalette.Highlight,       QColor(44, 123, 229))
    p.setColor(QPalette.HighlightedText, QColor(255, 255, 255))
    p.setColor(QPalette.ToolTipBase,     QColor(50, 50, 50))
    p.setColor(QPalette.ToolTipText,     QColor(220, 220, 220))
    p.setColor(QPalette.PlaceholderText, QColor(120, 120, 120))
    p.setColor(QPalette.Link,            QColor(120, 180, 240))
    # Disabled states.
    dim = QColor(120, 120, 120)
    p.setColor(QPalette.Disabled, QPalette.WindowText, dim)
    p.setColor(QPalette.Disabled, QPalette.Text,       dim)
    p.setColor(QPalette.Disabled, QPalette.ButtonText, dim)
    app.setPalette(p)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Kindergarten Photo Picker")
    _apply_dark_palette(app)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
