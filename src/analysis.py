"""Local image analysis — blur score and near-duplicate detection.

Runs on the CPU with numpy + Pillow + imagehash. No models, no network, no
photos leave the machine.

Blur: variance of the Laplacian (Pech-Pacheco et al.), a well-known cheap
proxy for focus. Higher = sharper. Values are scale-dependent, so we always
downsize to a fixed max dimension before measuring — otherwise a big JPEG
and a small crop of the same scene wouldn't be comparable.

Duplicates: 64-bit perceptual hash (imagehash.phash) + Hamming distance.
Pairs within `DUP_THRESHOLD` bits are considered near-duplicates and merged
into connected components with union-find.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import imagehash
import numpy as np
from PIL import Image, ImageOps

BLUR_MAX_DIM = 512
DUP_THRESHOLD = 5  # Hamming distance on 64-bit pHash

# EXIF tag numbers we look at (stable — same across all Pillow versions).
_EXIF_DATETIME_ORIGINAL = 0x9003
_EXIF_DATETIME_DIGITIZED = 0x9004
_EXIF_DATETIME = 0x0132
_EXIF_SUB_IFD = 0x8769


@dataclass
class PhotoAnalysis:
    blur_score: float
    phash: int              # 64-bit unsigned
    taken_at: Optional[datetime]  # local wall-clock from EXIF; None if unknown


def _load_gray_downsized(path: Path, max_dim: int = BLUR_MAX_DIM) -> np.ndarray:
    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img)
        img = img.convert("L")
        img.thumbnail((max_dim, max_dim), Image.Resampling.BILINEAR)
        return np.asarray(img, dtype=np.float32)


def _laplacian_variance(arr: np.ndarray) -> float:
    if arr.shape[0] < 3 or arr.shape[1] < 3:
        return 0.0
    lap = (
        arr[:-2, 1:-1]
        + arr[2:, 1:-1]
        + arr[1:-1, :-2]
        + arr[1:-1, 2:]
        - 4.0 * arr[1:-1, 1:-1]
    )
    return float(lap.var())


def _phash_int(path: Path) -> int:
    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img)
        h = imagehash.phash(img)
    return int(str(h), 16)


def extract_taken_at(path: Path) -> Optional[datetime]:
    """Read the photo's original capture time from EXIF, if present.

    Tries the SubIFD DateTimeOriginal first (what the camera set when the
    shutter fired), then DateTimeDigitized, then the top-level DateTime as a
    last-ditch fallback. Returns None if nothing usable is found — we do NOT
    fall back to file mtime because copies through OS pickers frequently
    reset it and would produce misleading matches."""
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            if not exif:
                return None
            candidates: list[Optional[str]] = []
            try:
                sub = exif.get_ifd(_EXIF_SUB_IFD)
            except Exception:  # noqa: BLE001
                sub = {}
            candidates.append(sub.get(_EXIF_DATETIME_ORIGINAL) if sub else None)
            candidates.append(sub.get(_EXIF_DATETIME_DIGITIZED) if sub else None)
            candidates.append(exif.get(_EXIF_DATETIME_ORIGINAL))
            candidates.append(exif.get(_EXIF_DATETIME_DIGITIZED))
            candidates.append(exif.get(_EXIF_DATETIME))
            for raw in candidates:
                if not raw:
                    continue
                s = raw.strip("\x00 ").strip() if isinstance(raw, str) else str(raw)
                for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                    try:
                        return datetime.strptime(s, fmt)
                    except ValueError:
                        continue
    except Exception:  # noqa: BLE001 — corrupt image, unreadable EXIF, etc.
        return None
    return None


def analyze_one(path: Path) -> PhotoAnalysis:
    """Compute blur + phash + taken_at for a single image. Raises on read failure
    for the blur/phash pipeline; missing EXIF is not an error (taken_at=None)."""
    gray = _load_gray_downsized(path)
    return PhotoAnalysis(
        blur_score=_laplacian_variance(gray),
        phash=_phash_int(path),
        taken_at=extract_taken_at(path),
    )


def group_duplicates(
    hashes: dict[str, int], threshold: int = DUP_THRESHOLD
) -> dict[str, int]:
    """Cluster paths by pHash Hamming distance using union-find.

    Returns {path: group_id}. Singletons get group_id = -1 (i.e., no
    duplicates). Group ids are assigned densely from 0 for groups with ≥ 2
    members, so users see stable, small numbers in the UI.
    """
    paths = list(hashes.keys())
    n = len(paths)
    if n < 2:
        return {p: -1 for p in paths}

    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    hs = [hashes[p] for p in paths]
    # Naive O(n²); fine up to ~15k photos. Popcount via int.bit_count() (3.10+).
    for i in range(n):
        hi = hs[i]
        for j in range(i + 1, n):
            if (hi ^ hs[j]).bit_count() <= threshold:
                union(i, j)

    from collections import Counter

    roots = [find(i) for i in range(n)]
    root_size = Counter(roots)

    result: dict[str, int] = {}
    root_to_gid: dict[int, int] = {}
    next_id = 0
    for i, path in enumerate(paths):
        root = roots[i]
        if root_size[root] < 2:
            result[path] = -1
        else:
            if root not in root_to_gid:
                root_to_gid[root] = next_id
                next_id += 1
            result[path] = root_to_gid[root]
    return result
