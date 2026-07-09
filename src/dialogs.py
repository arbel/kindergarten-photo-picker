"""Modal dialogs for managing kids and events.

Both edit the same shape of data (name + hotkey) so a single dialog class handles
both. The caller passes the current list of entities and a set of *reserved*
hotkeys — keys already bound to navigation etc. — so the dialog can flag
conflicts inline. On accept, `result()` returns the new list of rows the caller
can diff against its persisted state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from PySide6.QtCore import Qt, QTime
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTimeEdit,
    QVBoxLayout,
)

DAYS_OF_WEEK = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass
class EntityRow:
    """A row in the manage dialog. `key` is the persisted identifier (kid bit
    or event id) — None for newly-added rows.

    Schedule fields are only used by the events variant of the dialog."""

    key: Optional[int]
    name: str
    hotkey: Optional[str]
    day_of_week: Optional[int] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None


class HotkeyEdit(QLineEdit):
    """Single-character hotkey field. Captures the next key you press instead
    of relying on typing — makes it easy to bind function keys or symbols later
    if we want, and rejects modifier-only presses."""

    def __init__(self) -> None:
        super().__init__()
        self.setMaxLength(1)
        self.setPlaceholderText("(none)")
        self.setAlignment(Qt.AlignCenter)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        key = event.key()
        if key in (Qt.Key_Backspace, Qt.Key_Delete):
            self.clear()
            return
        text = event.text()
        if text and text.isprintable() and not text.isspace():
            self.setText(text.lower())
            return
        # ignore modifier-only or non-printable presses


class ManageEntitiesDialog(QDialog):
    """Add/edit/remove kids or events.

    If `show_schedule` is True (events variant), three extra columns appear:
    Day (dropdown), Start (HH:MM), End (HH:MM). All three must be set for the
    row to participate in auto-tagging; leaving any of them empty means the
    event has no schedule and won't be auto-assigned.
    """

    def __init__(
        self,
        parent,
        *,
        title: str,
        singular_noun: str,
        rows: list[EntityRow],
        reserved_hotkeys: set[str],
        show_schedule: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(720 if show_schedule else 520, 420)
        self._reserved = {k.lower() for k in reserved_hotkeys}
        self._singular = singular_noun
        self._show_schedule = show_schedule
        self._deleted_keys: list[int] = []

        v = QVBoxLayout(self)

        help_text = (
            f"Add, rename, or remove {singular_noun}s. Click a Hotkey cell and "
            "press the key you want to bind. Conflicting hotkeys are highlighted."
        )
        if show_schedule:
            help_text += (
                "  For each event, set Day + Start + End if you want photos to "
                "auto-tag by their EXIF time. Leave any field blank to skip "
                "auto-tagging for that event."
            )
        help_lbl = QLabel(help_text)
        help_lbl.setWordWrap(True)
        help_lbl.setStyleSheet("color:#aaa;padding:4px;")
        v.addWidget(help_lbl)

        if show_schedule:
            headers = ["Name", "Hotkey", "Day", "Start", "End", ""]
            widths = {1: 80, 2: 80, 3: 90, 4: 90, 5: 90}
        else:
            headers = ["Name", "Hotkey", ""]
            widths = {1: 90, 2: 90}

        self._table = QTableWidget(0, len(headers))
        self._table.setHorizontalHeaderLabels(headers)
        self._table.horizontalHeader().setStretchLastSection(False)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for col in range(1, len(headers)):
            self._table.horizontalHeader().setSectionResizeMode(col, QHeaderView.Fixed)
            if col in widths:
                self._table.setColumnWidth(col, widths[col])
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.verticalHeader().setVisible(False)
        v.addWidget(self._table, 1)

        for row in rows:
            self._append_row(row)

        button_row = QHBoxLayout()
        add_btn = QPushButton(f"+ Add {singular_noun}")
        add_btn.clicked.connect(self._add_blank_row)
        button_row.addWidget(add_btn)
        button_row.addStretch(1)
        v.addLayout(button_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        v.addWidget(buttons)

        self._refresh_conflict_highlight()

    # ---------- table helpers ----------

    def _append_row(self, row: EntityRow) -> None:
        r = self._table.rowCount()
        self._table.insertRow(r)

        name_item = QTableWidgetItem(row.name)
        name_item.setData(Qt.UserRole, row.key)  # persisted key or None
        self._table.setItem(r, 0, name_item)

        hotkey_edit = HotkeyEdit()
        if row.hotkey:
            hotkey_edit.setText(row.hotkey)
        hotkey_edit.textChanged.connect(self._refresh_conflict_highlight)
        self._table.setCellWidget(r, 1, hotkey_edit)

        if self._show_schedule:
            day_combo = QComboBox()
            day_combo.addItem("—", None)
            for i, d in enumerate(DAYS_OF_WEEK):
                day_combo.addItem(d, i)
            if row.day_of_week is not None:
                day_combo.setCurrentIndex(row.day_of_week + 1)
            self._table.setCellWidget(r, 2, day_combo)

            start_edit = QTimeEdit()
            start_edit.setDisplayFormat("HH:mm")
            if row.start_time:
                start_edit.setTime(QTime.fromString(row.start_time, "HH:mm"))
            else:
                start_edit.setTime(QTime(0, 0))
            start_edit.setSpecialValueText("—")
            self._table.setCellWidget(r, 3, start_edit)

            end_edit = QTimeEdit()
            end_edit.setDisplayFormat("HH:mm")
            if row.end_time:
                end_edit.setTime(QTime.fromString(row.end_time, "HH:mm"))
            else:
                end_edit.setTime(QTime(0, 0))
            self._table.setCellWidget(r, 4, end_edit)

            del_col = 5
        else:
            del_col = 2

        del_btn = QPushButton("Remove")
        del_btn.clicked.connect(lambda _=False: self._remove_row_by_button())
        self._table.setCellWidget(r, del_col, del_btn)

    def _add_blank_row(self) -> None:
        self._append_row(EntityRow(key=None, name=f"New {self._singular}", hotkey=None))
        self._table.editItem(self._table.item(self._table.rowCount() - 1, 0))

    def _remove_row_by_button(self) -> None:
        sender = self.sender()
        del_col = 5 if self._show_schedule else 2
        for r in range(self._table.rowCount()):
            if self._table.cellWidget(r, del_col) is sender:
                key = self._table.item(r, 0).data(Qt.UserRole)
                if key is not None:
                    self._deleted_keys.append(int(key))
                self._table.removeRow(r)
                self._refresh_conflict_highlight()
                return

    def _refresh_conflict_highlight(self) -> None:
        # Collect all in-dialog hotkeys and highlight duplicates or reserved ones.
        counts: dict[str, int] = {}
        for r in range(self._table.rowCount()):
            hk = self._table.cellWidget(r, 1).text().strip().lower()
            if hk:
                counts[hk] = counts.get(hk, 0) + 1
        for r in range(self._table.rowCount()):
            edit = self._table.cellWidget(r, 1)
            hk = edit.text().strip().lower()
            conflict = bool(hk) and (counts.get(hk, 0) > 1 or hk in self._reserved)
            edit.setStyleSheet(
                "background:#7a2a2a;color:#fff;" if conflict else ""
            )
            edit.setToolTip(
                f"Conflicts with another {self._singular} or a reserved key" if conflict else ""
            )

    # ---------- accept ----------

    def _on_accept(self) -> None:
        rows = self.result_rows()
        if any(not r.name.strip() for r in rows):
            QMessageBox.warning(self, "Missing name", "Every row needs a non-empty name.")
            return
        self.accept()

    def result_rows(self) -> list[EntityRow]:
        out: list[EntityRow] = []
        for r in range(self._table.rowCount()):
            key = self._table.item(r, 0).data(Qt.UserRole)
            name = self._table.item(r, 0).text().strip()
            hotkey = self._table.cellWidget(r, 1).text().strip().lower() or None
            day: Optional[int] = None
            start: Optional[str] = None
            end: Optional[str] = None
            if self._show_schedule:
                day = self._table.cellWidget(r, 2).currentData()
                s_time = self._table.cellWidget(r, 3).time()
                e_time = self._table.cellWidget(r, 4).time()
                # A zero-width interval (00:00 → 00:00) is treated as "no
                # schedule set" — the user is meant to edit both fields.
                if s_time != e_time:
                    start = s_time.toString("HH:mm")
                    end = e_time.toString("HH:mm")
                if day is None or start is None or end is None:
                    day = None
                    start = None
                    end = None
            out.append(
                EntityRow(
                    key=int(key) if key is not None else None,
                    name=name,
                    hotkey=hotkey,
                    day_of_week=day,
                    start_time=start,
                    end_time=end,
                )
            )
        return out

    def deleted_keys(self) -> list[int]:
        return list(self._deleted_keys)
