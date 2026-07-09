# Kindergarten Photo Picker

Fast, keyboard-driven tool to whittle ~10,000 photos down to ~480 for a
kindergarten end-of-year presentation — with per-child appearance counts to
keep coverage balanced.

Runs locally. No photos ever leave your machine.

## Status

**V3 (current).** Fully local; no photos leave your machine.

The intended flow, top to bottom:

1. **Analyze** the folder — extracts blur score, pHash, and EXIF
   `DateTimeOriginal` for every photo. Incremental (skips already-analyzed).
2. **Skip blurry / duplicates** in bulk to trim the review queue.
3. **Define events** with a day-of-week + start/end time (Edit → Manage
   events).
4. **Auto-tag events by schedule** — every photo whose EXIF time falls
   inside exactly one scheduled slot is tagged with `source='auto'` and
   waits for your approval.
5. **Review each photo:**
   - `Space` — approve the auto-tag as-is
   - press a different event hotkey — reassigns (counts as manual approval)
   - kid hotkeys — tag which kids are in the photo
   - `Backspace` — skip (not to use)
6. **Export by event** — one subfolder per event; **only approved
   (`event_source='manual'`) photos are copied**, so nothing pending or
   skipped leaks out.

Under the hood:

- **Provenance:** every event tag records whether it was set automatically
  (`event_source='auto'`) or by you (`'manual'`). The header shows both
  counts (`✅ Reviewed`, `🤖 Auto-pending`).
- **Filters:** hide blurry / duplicate / already-reviewed photos to keep
  the queue tight during the review pass.
- **Dynamic kids & events:** any number, each with a user-chosen hotkey.
  Conflicts highlighted in the manage dialog.

Planned:

- Face recognition to auto-suggest kid tags (the DB is shaped for it — kid
  tags will get the same `auto/manual` provenance).
- Best-of-burst scoring beyond blur (aesthetic, framing).

## Install

Requires Python 3.11+ (built and tested against 3.13).

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run

```bash
./run.sh
```

Then **File → Open folder** and pick the folder containing your photos.
Subfolders are scanned recursively. A `.kpp-state.db` SQLite file is written
inside the photo folder — that's where your selections and kid tags live.

## Keybindings

Reserved (do not reuse these as kid/event hotkeys):

| Key                | Action                                       |
| ------------------ | -------------------------------------------- |
| `←` / `→`          | previous / next photo (within filter view)   |
| `PgUp` / `PgDown`  | jump 20 photos                               |
| `Home` / `End`     | first / last photo                           |
| `Space`            | approve current auto-tag                     |
| `Backspace`        | toggle *skip* on current photo               |
| `Ctrl+O`           | open folder                                  |
| `Ctrl+K`           | manage kids                                  |
| `Ctrl+G`           | manage events                                |
| `Ctrl+Shift+A`     | analyze folder (blur + duplicates + EXIF)    |
| `Ctrl+Shift+E`     | export by event                              |

**Kid / event hotkeys are user-defined** — any printable single character.
Set them in **Edit → Manage kids** / **Manage events**. The dialog highlights
conflicts (same key used twice, or a key already reserved).

### AI features

**Analyze → Scan folder (Ctrl+A)** runs blur scoring and duplicate detection
across the folder. The scan is incremental: photos already analyzed are
skipped. Results are cached in the project DB. On a modern Mac, budget
roughly 1–3 minutes per 1,000 photos for the first scan (single-threaded).

After a scan, each photo shows two badges next to its filename:

- **`⚠ BLURRY 42`** — Laplacian variance below the threshold (default 60).
- **`⧉ DUP GROUP #3 · 2 of 5`** — this photo shares a group with N others.

Two one-click cleanups (also under **Analyze**):

- **Deselect blurry photos…** — prompts for a threshold, then deselects any
  currently-selected photos scoring below it.
- **Deselect duplicates (keep sharpest)** — for each duplicate group that
  contains any selected photo, keeps only the sharpest selected and
  deselects the rest.

## Project layout

```
kindergarten-photo-picker/
├── run.sh
├── requirements.txt
└── src/
    ├── main.py     # entry point, MainWindow, analysis worker, export helper
    ├── widgets.py  # PhotoView, EntityButton, EntityPanel (dynamic kid/event lists)
    ├── dialogs.py  # ManageEntitiesDialog (shared by kids & events)
    ├── db.py       # SQLite persistence — kids/events/photos + review counters
    └── analysis.py # Local blur + pHash + duplicate grouping
```
