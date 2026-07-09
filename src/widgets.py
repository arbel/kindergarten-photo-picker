"""Reusable Qt widgets: PhotoView and the dynamic Kids/Events panels."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional, Union

from PySide6.QtCore import Qt
from PySide6.QtGui import QImageReader, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .db import Event, Kid

Entity = Union[Kid, Event]


class PhotoView(QLabel):
    """Displays one photo, scaled to fit, EXIF-aware."""

    def __init__(self) -> None:
        super().__init__()
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("background:#111;")
        self.setMinimumSize(400, 300)
        self._pixmap: Optional[QPixmap] = None

    def set_photo(self, abs_path: Optional[Path]) -> None:
        if abs_path is None or not abs_path.exists():
            self._pixmap = None
            self.setText("No photo")
            self.setStyleSheet("background:#111; color:#888; font-size:20px;")
            return
        reader = QImageReader(str(abs_path))
        reader.setAutoTransform(True)
        image = reader.read()
        if image.isNull():
            self._pixmap = None
            self.setText(f"Failed to load:\n{abs_path.name}")
            self.setStyleSheet("background:#111; color:#c66; font-size:16px;")
            return
        self._pixmap = QPixmap.fromImage(image)
        self.setStyleSheet("background:#111;")
        self._rescale()

    def _rescale(self) -> None:
        if self._pixmap is None:
            return
        scaled = self._pixmap.scaled(
            self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        self.setPixmap(scaled)

    def resizeEvent(self, event) -> None:  # noqa: N802 — Qt API
        super().resizeEvent(event)
        self._rescale()


class EntityButton(QPushButton):
    """Compact button representing one kid or one event.

    Shows: [hotkey]  name  ·  N   — single line, small, so all bindings fit
    on-screen as a live hotkey cheatsheet. Highlights when the entity is
    active on the current photo.
    """

    def __init__(self, entity: Entity, on_activate: Callable[[Entity], None]) -> None:
        super().__init__()
        self.entity = entity
        self._on_activate = on_activate
        self._count = 0
        self._active = False
        self.setMinimumHeight(30)
        self.setMaximumHeight(32)
        self.setSizePolicy(QSizePolicy.MinimumExpanding, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)
        self.clicked.connect(lambda: self._on_activate(self.entity))
        self._refresh()

    def set_count(self, count: int) -> None:
        self._count = count
        self._refresh()

    def set_active(self, active: bool) -> None:
        if self._active != active:
            self._active = active
            self._refresh()

    def _refresh(self) -> None:
        hk = f"[{self.entity.hotkey}]" if self.entity.hotkey else "[·]"
        self.setText(f"{hk}  {self.entity.name}  ·  {self._count}")
        if self._active:
            self.setStyleSheet(
                "QPushButton{background:#2c7be5;color:white;border:1px solid #1858b0;"
                "border-radius:5px;text-align:left;font-size:12px;padding:2px 8px;"
                "font-weight:600;}"
            )
        else:
            self.setStyleSheet(
                "QPushButton{background:#242424;color:#ddd;border:1px solid #3a3a3a;"
                "border-radius:5px;text-align:left;font-size:12px;padding:2px 8px;}"
                "QPushButton:hover{background:#2f2f2f;border-color:#555;}"
            )


DEFAULT_COLUMNS = 3


class EntityPanel(QFrame):
    """Panel with a title and a grid of compact EntityButtons.

    Multi-column grid so all kid or event hotkeys are visible at a glance —
    the panel doubles as a live hotkey cheatsheet.
    """

    def __init__(
        self,
        title: str,
        on_activate: Callable[[Entity], None],
        on_manage: Callable[[], None],
        columns: int = DEFAULT_COLUMNS,
    ) -> None:
        super().__init__()
        self.setFrameShape(QFrame.NoFrame)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(3)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        self._title = QLabel(title)
        self._title.setStyleSheet(
            "color:#ddd;font-size:11px;font-weight:700;letter-spacing:1px;padding:2px 4px;"
        )
        manage_btn = QPushButton("Manage…")
        manage_btn.setCursor(Qt.PointingHandCursor)
        manage_btn.setStyleSheet(
            "QPushButton{background:transparent;color:#8bc;border:none;"
            "font-size:11px;padding:2px 4px;} QPushButton:hover{color:#fff;}"
        )
        manage_btn.clicked.connect(on_manage)
        header.addWidget(self._title)
        header.addStretch(1)
        header.addWidget(manage_btn)
        outer.addLayout(header)

        # Grid inside a scroll area (scroll only kicks in for >~20 entities).
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._content = QWidget()
        self._grid = QGridLayout(self._content)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(4)
        self._grid.setVerticalSpacing(3)
        self._scroll.setWidget(self._content)
        outer.addWidget(self._scroll, 1)

        self._columns = columns
        self._on_activate = on_activate
        self._buttons: dict[int, EntityButton] = {}
        self._empty_hint: Optional[QLabel] = None

    def set_entities(self, entities: list[Entity]) -> None:
        # Wipe the grid.
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
        self._buttons.clear()
        self._empty_hint = None

        if not entities:
            self._empty_hint = QLabel("Nothing here yet — click Manage… to add.")
            self._empty_hint.setStyleSheet("color:#777;padding:12px;font-style:italic;")
            self._grid.addWidget(self._empty_hint, 0, 0, 1, self._columns)
            return

        for i, entity in enumerate(entities):
            btn = EntityButton(entity, self._on_activate)
            self._buttons[_entity_key(entity)] = btn
            self._grid.addWidget(btn, i // self._columns, i % self._columns)
        # Make columns share width evenly.
        for col in range(self._columns):
            self._grid.setColumnStretch(col, 1)

    def set_counts(self, counts: dict[int, int]) -> None:
        for key, count in counts.items():
            if key in self._buttons:
                self._buttons[key].set_count(count)

    def set_active(self, active_keys: set[int]) -> None:
        for key, btn in self._buttons.items():
            btn.set_active(key in active_keys)


def _entity_key(entity: Entity) -> int:
    return entity.bit if isinstance(entity, Kid) else entity.id
