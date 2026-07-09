"""SQLite persistence for a kindergarten-photo-picker project.

The database lives inside the photo folder as `.kpp-state.db` so a project is
fully described by its folder — copy the folder, keep the state.

Data model
----------
- kids(bit, name, hotkey, order_index)
    `bit` doubles as the kid's ID and its bit position in `photos.kids_mask`.
    Bits are never reused after a kid is deleted (safer than reclaiming).
- events(id, name, hotkey, order_index)
    One event per photo, stored as a nullable FK on `photos.event_id`.
- photos(path PK, kids_mask, event_id, skipped, blur_score, phash, dup_group_id)

Review status is derived from the *source* of the event tag: automatic tags
are pending user review, manual tags count as reviewed.

    reviewed   = skipped=1 OR event_source='manual'
    unreviewed = NOT reviewed  (untouched OR auto-tagged pending approval)
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional

DB_FILENAME = ".kpp-state.db"
MAX_KID_BITS = 64  # kids_mask is a signed 64-bit int → 63 usable bits, but the
# sign bit works too if we mask before compare. We cap at 63 to stay safe.
MAX_KIDS = 63


@dataclass(frozen=True)
class Kid:
    bit: int
    name: str
    hotkey: Optional[str]
    order_index: int


@dataclass(frozen=True)
class Event:
    id: int
    name: str
    hotkey: Optional[str]
    order_index: int
    day_of_week: Optional[int] = None   # 0=Mon .. 6=Sun; NULL = no schedule
    start_time: Optional[str] = None    # "HH:MM"
    end_time: Optional[str] = None      # "HH:MM"

    def has_schedule(self) -> bool:
        return (
            self.day_of_week is not None
            and self.start_time is not None
            and self.end_time is not None
        )


@dataclass(frozen=True)
class PhotoState:
    path: str
    kids_mask: int
    event_id: Optional[int]
    event_source: Optional[str]  # 'auto' | 'manual' | None
    skipped: bool
    taken_at: Optional[str]      # ISO 8601 local time from EXIF, or None
    blur_score: Optional[float]
    phash: Optional[int]
    dup_group_id: int

    def reviewed(self) -> bool:
        return self.skipped or self.event_source == "manual"

    def pending_auto_review(self) -> bool:
        return self.event_source == "auto" and not self.skipped


class ProjectDB:
    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.db_path = self.folder / DB_FILENAME
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()
        self._migrate_v1_kid_names()

    # ---------- schema ----------

    def _init_schema(self) -> None:
        c = self._conn
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS photos (
                path         TEXT PRIMARY KEY,
                kids_mask    INTEGER NOT NULL DEFAULT 0,
                event_id     INTEGER,
                event_source TEXT,
                skipped      INTEGER NOT NULL DEFAULT 0,
                taken_at     TEXT,
                blur_score   REAL,
                phash        BLOB,
                dup_group_id INTEGER NOT NULL DEFAULT -1
            );
            CREATE INDEX IF NOT EXISTS idx_photos_event    ON photos(event_id);
            CREATE INDEX IF NOT EXISTS idx_photos_skipped  ON photos(skipped);
            CREATE INDEX IF NOT EXISTS idx_photos_dupgroup ON photos(dup_group_id);
            CREATE INDEX IF NOT EXISTS idx_photos_source   ON photos(event_source);
            CREATE INDEX IF NOT EXISTS idx_photos_taken_at ON photos(taken_at);

            CREATE TABLE IF NOT EXISTS kids (
                bit         INTEGER PRIMARY KEY,
                name        TEXT NOT NULL,
                hotkey      TEXT,
                order_index INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS events (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT NOT NULL,
                hotkey       TEXT,
                order_index  INTEGER NOT NULL DEFAULT 0,
                day_of_week  INTEGER,
                start_time   TEXT,
                end_time     TEXT
            );

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        # Column migrations for pre-existing DBs.
        photo_cols = {row[1] for row in c.execute("PRAGMA table_info(photos)")}
        for col, sql in {
            "kids_mask":    "ALTER TABLE photos ADD COLUMN kids_mask INTEGER NOT NULL DEFAULT 0",
            "event_id":     "ALTER TABLE photos ADD COLUMN event_id INTEGER",
            "event_source": "ALTER TABLE photos ADD COLUMN event_source TEXT",
            "skipped":      "ALTER TABLE photos ADD COLUMN skipped INTEGER NOT NULL DEFAULT 0",
            "taken_at":     "ALTER TABLE photos ADD COLUMN taken_at TEXT",
            "blur_score":   "ALTER TABLE photos ADD COLUMN blur_score REAL",
            "phash":        "ALTER TABLE photos ADD COLUMN phash BLOB",
            "dup_group_id": "ALTER TABLE photos ADD COLUMN dup_group_id INTEGER NOT NULL DEFAULT -1",
        }.items():
            if col not in photo_cols:
                c.execute(sql)
        event_cols = {row[1] for row in c.execute("PRAGMA table_info(events)")}
        for col, sql in {
            "day_of_week": "ALTER TABLE events ADD COLUMN day_of_week INTEGER",
            "start_time":  "ALTER TABLE events ADD COLUMN start_time TEXT",
            "end_time":    "ALTER TABLE events ADD COLUMN end_time TEXT",
        }.items():
            if col not in event_cols:
                c.execute(sql)
        # Back-fill event_source for any photo that has an event_id but no
        # source recorded — assume it was manual (V2 behavior).
        c.execute(
            "UPDATE photos SET event_source='manual' "
            "WHERE event_id IS NOT NULL AND event_source IS NULL"
        )
        c.commit()

    def _migrate_v1_kid_names(self) -> None:
        """Seed the kids table from V1's kid_name_0..8 settings, if present."""
        already = self._conn.execute("SELECT COUNT(*) FROM kids").fetchone()[0]
        if already > 0:
            return
        rows = self._conn.execute(
            "SELECT key, value FROM settings WHERE key LIKE 'kid_name_%'"
        ).fetchall()
        if not rows:
            return
        with self._tx() as c:
            for key, name in rows:
                try:
                    bit = int(key.split("_")[-1])
                except ValueError:
                    continue
                c.execute(
                    "INSERT INTO kids(bit, name, hotkey, order_index) VALUES(?,?,?,?)",
                    (bit, name, str(bit + 1) if bit < 9 else None, bit),
                )
                c.execute("DELETE FROM settings WHERE key=?", (key,))

    # ---------- settings ----------

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self._conn.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self._conn.commit()

    # ---------- kids ----------

    def list_kids(self) -> list[Kid]:
        return [
            Kid(row[0], row[1], row[2], row[3])
            for row in self._conn.execute(
                "SELECT bit, name, hotkey, order_index FROM kids ORDER BY order_index, bit"
            )
        ]

    def _next_kid_bit(self) -> int:
        used = {row[0] for row in self._conn.execute("SELECT bit FROM kids")}
        for b in range(MAX_KIDS):
            if b not in used:
                return b
        raise RuntimeError(f"Maximum {MAX_KIDS} kids supported")

    def add_kid(self, name: str, hotkey: Optional[str] = None) -> Kid:
        bit = self._next_kid_bit()
        order = self._conn.execute(
            "SELECT COALESCE(MAX(order_index), -1) + 1 FROM kids"
        ).fetchone()[0]
        self._conn.execute(
            "INSERT INTO kids(bit, name, hotkey, order_index) VALUES(?,?,?,?)",
            (bit, name, hotkey, order),
        )
        self._conn.commit()
        return Kid(bit, name, hotkey, order)

    def update_kid(self, bit: int, name: str, hotkey: Optional[str]) -> None:
        self._conn.execute(
            "UPDATE kids SET name=?, hotkey=? WHERE bit=?", (name, hotkey, bit)
        )
        self._conn.commit()

    def delete_kid(self, bit: int) -> None:
        # Clear this kid's bit from all photos so counters stay honest.
        clear_mask = ~(1 << bit)
        with self._tx() as c:
            c.execute(
                "UPDATE photos SET kids_mask = kids_mask & ? WHERE (kids_mask & ?) != 0",
                (clear_mask, 1 << bit),
            )
            c.execute("DELETE FROM kids WHERE bit=?", (bit,))

    def reorder_kids(self, bits_in_order: list[int]) -> None:
        with self._tx() as c:
            for i, bit in enumerate(bits_in_order):
                c.execute("UPDATE kids SET order_index=? WHERE bit=?", (i, bit))

    # ---------- events ----------

    def list_events(self) -> list[Event]:
        return [
            Event(
                id=row[0],
                name=row[1],
                hotkey=row[2],
                order_index=row[3],
                day_of_week=row[4],
                start_time=row[5],
                end_time=row[6],
            )
            for row in self._conn.execute(
                "SELECT id, name, hotkey, order_index, day_of_week, start_time, end_time "
                "FROM events ORDER BY order_index, id"
            )
        ]

    def add_event(
        self,
        name: str,
        hotkey: Optional[str] = None,
        *,
        day_of_week: Optional[int] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> Event:
        order = self._conn.execute(
            "SELECT COALESCE(MAX(order_index), -1) + 1 FROM events"
        ).fetchone()[0]
        cur = self._conn.execute(
            "INSERT INTO events(name, hotkey, order_index, day_of_week, start_time, end_time) "
            "VALUES(?,?,?,?,?,?)",
            (name, hotkey, order, day_of_week, start_time, end_time),
        )
        self._conn.commit()
        return Event(
            id=cur.lastrowid,
            name=name,
            hotkey=hotkey,
            order_index=order,
            day_of_week=day_of_week,
            start_time=start_time,
            end_time=end_time,
        )

    def update_event(
        self,
        event_id: int,
        name: str,
        hotkey: Optional[str],
        *,
        day_of_week: Optional[int] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> None:
        self._conn.execute(
            "UPDATE events "
            "SET name=?, hotkey=?, day_of_week=?, start_time=?, end_time=? "
            "WHERE id=?",
            (name, hotkey, day_of_week, start_time, end_time, event_id),
        )
        self._conn.commit()

    def delete_event(self, event_id: int) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE photos SET event_id=NULL WHERE event_id=?", (event_id,)
            )
            c.execute("DELETE FROM events WHERE id=?", (event_id,))

    def reorder_events(self, ids_in_order: list[int]) -> None:
        with self._tx() as c:
            for i, event_id in enumerate(ids_in_order):
                c.execute("UPDATE events SET order_index=? WHERE id=?", (i, event_id))

    # ---------- photos ----------

    def sync_paths(self, relative_paths: Iterable[str]) -> None:
        """Insert any new photo rows. Never deletes stale rows — we don't want
        to lose tags if a file is temporarily unavailable."""
        with self._tx() as c:
            c.executemany(
                "INSERT OR IGNORE INTO photos(path) VALUES(?)",
                ((p,) for p in relative_paths),
            )

    def get_photo(self, path: str) -> Optional[PhotoState]:
        row = self._conn.execute(
            "SELECT path, kids_mask, event_id, event_source, skipped, taken_at, "
            "       blur_score, phash, dup_group_id "
            "FROM photos WHERE path=?",
            (path,),
        ).fetchone()
        if row is None:
            return None
        return PhotoState(
            path=row[0],
            kids_mask=int(row[1]),
            event_id=row[2],
            event_source=row[3],
            skipped=bool(row[4]),
            taken_at=row[5],
            blur_score=row[6],
            phash=self._blob_to_phash(row[7]),
            dup_group_id=row[8] if row[8] is not None else -1,
        )

    def toggle_kid(self, path: str, bit: int) -> int:
        """Flip the bit for `kid_bit` on this photo. Returns the new kids_mask."""
        row = self._conn.execute(
            "SELECT kids_mask FROM photos WHERE path=?", (path,)
        ).fetchone()
        mask = int(row[0]) if row else 0
        new_mask = mask ^ (1 << bit)
        self._conn.execute(
            "UPDATE photos SET kids_mask=? WHERE path=?", (new_mask, path)
        )
        self._conn.commit()
        return new_mask

    def set_event(self, path: str, event_id: Optional[int]) -> Optional[int]:
        """Manually assign (or toggle-clear) an event on a photo.

        Any user-triggered assignment counts as *manual* — that's the whole
        point of this method vs `auto_tag_events`. Assigning also clears the
        skipped flag. If the photo already has this event, we clear it.
        Returns the new event_id (or None)."""
        row = self._conn.execute(
            "SELECT event_id FROM photos WHERE path=?", (path,)
        ).fetchone()
        current = row[0] if row else None
        new = None if current == event_id else event_id
        source = "manual" if new is not None else None
        self._conn.execute(
            "UPDATE photos SET event_id=?, event_source=?, skipped=0 WHERE path=?",
            (new, source, path),
        )
        self._conn.commit()
        return new

    def approve_current_event(self, path: str) -> bool:
        """Promote an auto-tagged event to manual (user reviewed it and said OK).
        Returns True if we actually promoted something, False if there was
        nothing to approve."""
        row = self._conn.execute(
            "SELECT event_id, event_source FROM photos WHERE path=?", (path,)
        ).fetchone()
        if row is None or row[0] is None or row[1] != "auto":
            return False
        self._conn.execute(
            "UPDATE photos SET event_source='manual' WHERE path=?", (path,)
        )
        self._conn.commit()
        return True

    def set_taken_at_many(self, rows: list[tuple[str, Optional[str]]]) -> None:
        with self._tx() as c:
            c.executemany(
                "UPDATE photos SET taken_at=? WHERE path=?",
                ((iso, p) for (p, iso) in rows),
            )

    def toggle_skipped(self, path: str) -> bool:
        """Flip the skipped flag. Skipping clears any assigned event.
        Returns the new skipped value."""
        row = self._conn.execute(
            "SELECT skipped FROM photos WHERE path=?", (path,)
        ).fetchone()
        cur = bool(row[0]) if row else False
        new = not cur
        if new:
            self._conn.execute(
                "UPDATE photos SET skipped=1, event_id=NULL WHERE path=?", (path,)
            )
        else:
            self._conn.execute(
                "UPDATE photos SET skipped=0 WHERE path=?", (path,)
            )
        self._conn.commit()
        return new

    # ---------- counters ----------

    def count_by_event(self, event_id: int) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM photos WHERE event_id=?", (event_id,)
        ).fetchone()[0]

    def count_by_kid(self, bit: int) -> int:
        """Total photos tagged with this kid (regardless of review state)."""
        return self._conn.execute(
            "SELECT COUNT(*) FROM photos WHERE (kids_mask & ?) != 0", (1 << bit,)
        ).fetchone()[0]

    def review_counts(self) -> tuple[int, int, int, int, int]:
        """(total, reviewed, skipped, pending_auto, unreviewed_untouched).

        - reviewed              = skipped OR event_source='manual'
        - pending_auto          = event_source='auto' (needs approval)
        - unreviewed_untouched  = no event, not skipped
        """
        row = self._conn.execute(
            """
            SELECT
              COUNT(*),
              COALESCE(SUM(CASE WHEN skipped=1 OR event_source='manual'
                                THEN 1 ELSE 0 END), 0),
              COALESCE(SUM(skipped), 0),
              COALESCE(SUM(CASE WHEN event_source='auto' AND skipped=0
                                THEN 1 ELSE 0 END), 0),
              COALESCE(SUM(CASE WHEN event_id IS NULL AND skipped=0
                                THEN 1 ELSE 0 END), 0)
            FROM photos
            """
        ).fetchone()
        total, reviewed, skipped, pending, untouched = row
        return total, reviewed, skipped, pending, untouched

    def event_photos(self, event_id: int, only_manual: bool = False) -> list[str]:
        """Photos assigned to an event. If only_manual=True, restrict to
        user-approved (event_source='manual') tags — used at export time so
        pending auto-tags don't leak out."""
        if only_manual:
            sql = (
                "SELECT path FROM photos "
                "WHERE event_id=? AND event_source='manual' AND skipped=0 "
                "ORDER BY path"
            )
        else:
            sql = "SELECT path FROM photos WHERE event_id=? ORDER BY path"
        return [row[0] for row in self._conn.execute(sql, (event_id,))]

    # ---------- filtered navigation ----------

    def filtered_paths(
        self,
        *,
        hide_blurry: bool = False,
        blur_threshold: float = 60.0,
        hide_duplicates: bool = False,
        hide_reviewed: bool = False,
    ) -> list[str]:
        """Return paths visible under the given filter combination, sorted."""
        clauses: list[str] = []
        params: list[object] = []
        if hide_blurry:
            clauses.append("(blur_score IS NULL OR blur_score >= ?)")
            params.append(blur_threshold)
        if hide_duplicates:
            # Show at most one photo per dup group — the sharpest. Photos not
            # in any dup group (gid=-1) are always shown.
            clauses.append(
                "(dup_group_id = -1 OR path IN ("
                "  SELECT path FROM photos p2 WHERE p2.dup_group_id = photos.dup_group_id "
                "  ORDER BY COALESCE(p2.blur_score, -1) DESC, p2.path LIMIT 1"
                "))"
            )
        if hide_reviewed:
            clauses.append("NOT (skipped=1 OR event_source='manual')")
        sql = "SELECT path FROM photos"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY path"
        return [row[0] for row in self._conn.execute(sql, params)]

    # ---------- analysis (unchanged from v1) ----------

    @staticmethod
    def _phash_to_blob(phash: int) -> bytes:
        return phash.to_bytes(8, "big")

    @staticmethod
    def _blob_to_phash(blob: Optional[bytes]) -> Optional[int]:
        return int.from_bytes(blob, "big") if blob is not None else None

    def set_analysis_many(
        self, rows: list[tuple[str, float, int, Optional[str]]]
    ) -> None:
        """rows: (path, blur_score, phash, taken_at_iso)."""
        with self._tx() as c:
            c.executemany(
                "UPDATE photos SET blur_score=?, phash=?, taken_at=? WHERE path=?",
                ((blur, self._phash_to_blob(ph), taken, p) for (p, blur, ph, taken) in rows),
            )

    def set_dup_groups(self, groups: dict[str, int]) -> None:
        with self._tx() as c:
            c.executemany(
                "UPDATE photos SET dup_group_id=? WHERE path=?",
                ((gid, p) for p, gid in groups.items()),
            )

    def paths_needing_analysis(self) -> list[str]:
        return [
            row[0]
            for row in self._conn.execute(
                "SELECT path FROM photos "
                "WHERE blur_score IS NULL OR phash IS NULL OR taken_at IS NULL"
            )
        ]

    # ---------- auto-tag by schedule ----------

    def auto_tag_events(self) -> tuple[int, int, int]:
        """Match each candidate photo to a scheduled event by weekday +
        time-of-day. Returns (matched, ambiguous, unmatched).

        - Never overwrites manual event tags.
        - Overwrites existing auto tags (so re-running after a schedule edit
          picks up the new mapping).
        - Skipped photos are left alone."""
        events = [e for e in self.list_events() if e.has_schedule()]
        if not events:
            return 0, 0, 0

        # Fetch candidate photos: have a taken_at, not skipped, not manual.
        candidates = self._conn.execute(
            """
            SELECT path, taken_at FROM photos
             WHERE taken_at IS NOT NULL AND skipped=0
               AND (event_source IS NULL OR event_source='auto')
            """
        ).fetchall()

        from datetime import datetime as _dt

        matched = ambiguous = unmatched = 0
        updates: list[tuple[Optional[int], Optional[str], str]] = []

        for path, iso in candidates:
            try:
                dt = _dt.fromisoformat(iso)
            except (TypeError, ValueError):
                unmatched += 1
                updates.append((None, None, path))
                continue
            weekday = dt.weekday()
            hhmm = dt.strftime("%H:%M")
            hits = [
                e for e in events
                if e.day_of_week == weekday
                and (e.start_time or "") <= hhmm <= (e.end_time or "")
            ]
            if len(hits) == 1:
                updates.append((hits[0].id, "auto", path))
                matched += 1
            elif len(hits) > 1:
                # Ambiguous — clear any prior auto tag so the user sees it as
                # untagged and picks manually.
                updates.append((None, None, path))
                ambiguous += 1
            else:
                updates.append((None, None, path))
                unmatched += 1

        with self._tx() as c:
            c.executemany(
                "UPDATE photos SET event_id=?, event_source=? WHERE path=?",
                updates,
            )
        return matched, ambiguous, unmatched

    def all_phashes(self) -> dict[str, int]:
        return {
            row[0]: int.from_bytes(row[1], "big")
            for row in self._conn.execute(
                "SELECT path, phash FROM photos WHERE phash IS NOT NULL"
            )
        }

    def dup_group_position(self, path: str) -> tuple[int, int]:
        """Return (1-based index within the group, group size), or (0, 0) if none."""
        row = self._conn.execute(
            "SELECT dup_group_id FROM photos WHERE path=?", (path,)
        ).fetchone()
        gid = row[0] if row else -1
        if gid is None or gid < 0:
            return 0, 0
        members = [
            r[0]
            for r in self._conn.execute(
                "SELECT path FROM photos WHERE dup_group_id=? ORDER BY path", (gid,)
            )
        ]
        try:
            return members.index(path) + 1, len(members)
        except ValueError:
            return 0, len(members)

    # ---------- bulk cleanups ----------

    def skip_blurry_below(self, threshold: float) -> int:
        """Mark blurry, un-reviewed photos as skipped."""
        cur = self._conn.execute(
            """
            UPDATE photos SET skipped=1
             WHERE blur_score IS NOT NULL AND blur_score < ?
               AND skipped = 0
               AND event_id IS NULL
            """,
            (threshold,),
        )
        self._conn.commit()
        return cur.rowcount

    def skip_duplicates_keep_sharpest(self) -> tuple[int, int]:
        """For each duplicate group, keep the sharpest photo untouched and mark
        all other members as skipped (unless they already have an event assigned
        — user intent trumps the automated cleanup).

        Returns (groups_touched, photos_skipped).
        """
        groups = [
            row[0]
            for row in self._conn.execute(
                "SELECT DISTINCT dup_group_id FROM photos WHERE dup_group_id >= 0"
            )
        ]
        groups_touched = 0
        photos_skipped = 0
        with self._tx() as c:
            for gid in groups:
                members = c.execute(
                    """
                    SELECT path, event_id, skipped, COALESCE(blur_score, -1)
                    FROM photos WHERE dup_group_id=?
                    """,
                    (gid,),
                ).fetchall()
                if len(members) < 2:
                    continue
                members.sort(key=lambda r: (-r[3], r[0]))  # sharpest first, ties by path
                sharpest = members[0][0]
                to_skip = [
                    m[0] for m in members
                    if m[0] != sharpest and m[1] is None and m[2] == 0
                ]
                if not to_skip:
                    continue
                c.executemany(
                    "UPDATE photos SET skipped=1 WHERE path=?",
                    ((p,) for p in to_skip),
                )
                groups_touched += 1
                photos_skipped += len(to_skip)
        return groups_touched, photos_skipped

    # ---------- lifecycle ----------

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        self._conn.close()
