"""
Promote CNN training clips from data/training/unlabeled/ using human labels.

Labels come from data/transit_labels.csv (same timestamps as transit_events logs
and det_YYYYMMDD_HHMMSS_*.npz filenames from TransitDetector._save_training_clip).

- tp  → data/training/positives/
- fp  → data/training/negatives/

fn / tn are skipped for file promotion (no clear clip semantics).
"""

from __future__ import annotations

import csv
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple

DET_NPZ_RE = re.compile(r"^det_(\d{8})_(\d{6})_", re.IGNORECASE)


def _iso_to_second_key(ts: str) -> Optional[str]:
    """Map CSV/ISO timestamp to YYYYMMDD_HHMMSS key matching det_* npz names."""
    if not ts or not str(ts).strip():
        return None
    s = str(ts).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    dt = dt.replace(microsecond=0)
    # Match det_YYYYMMDD_HHMMSS_* filenames (underscore between date and time).
    return dt.strftime("%Y%m%d") + "_" + dt.strftime("%H%M%S")


def _npz_time_key(name: str) -> Optional[str]:
    m = DET_NPZ_RE.match(name)
    if not m:
        return None
    return f"{m.group(1)}_{m.group(2)}"


def load_labels_csv(path: Path) -> Dict[str, str]:
    """timestamp string -> label (last row wins)."""
    out: Dict[str, str] = {}
    if not path.is_file():
        return out
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                ts = (row.get("timestamp") or "").strip()
                lbl = (row.get("label") or "").strip().lower()
                if ts and lbl:
                    out[ts] = lbl
    except OSError:
        pass
    return out


def promote_labeled_unlabeled(repo_root: Path) -> Dict[str, int]:
    """
    Move matching unlabeled .npz files to positives/negatives.

    Returns counts: promoted_pos, promoted_neg, skipped_no_label, skipped_unknown_label.
    """
    labels_path = repo_root / "data" / "transit_labels.csv"
    unlabeled = repo_root / "data" / "training" / "unlabeled"
    pos_dir = repo_root / "data" / "training" / "positives"
    neg_dir = repo_root / "data" / "training" / "negatives"
    pos_dir.mkdir(parents=True, exist_ok=True)
    neg_dir.mkdir(parents=True, exist_ok=True)

    raw_labels = load_labels_csv(labels_path)
    # Map second-key -> label (prefer exact timestamp string resolution)
    key_to_label: Dict[str, str] = {}
    for ts_str, lbl in raw_labels.items():
        k = _iso_to_second_key(ts_str)
        if k:
            key_to_label[k] = lbl

    stats = {
        "promoted_pos": 0,
        "promoted_neg": 0,
        "skipped_no_label": 0,
        "skipped_unknown_label": 0,
    }

    if not unlabeled.is_dir():
        return stats

    for f in sorted(unlabeled.glob("det_*.npz")):
        key = _npz_time_key(f.name)
        if not key:
            stats["skipped_no_label"] += 1
            continue
        lbl = key_to_label.get(key)
        if not lbl:
            stats["skipped_no_label"] += 1
            continue

        if lbl == "tp":
            dest_dir = pos_dir
        elif lbl == "fp":
            dest_dir = neg_dir
        else:
            stats["skipped_unknown_label"] += 1
            continue

        dest = dest_dir / f.name
        if dest.exists():
            dest = dest_dir / f"{f.stem}_{uuid.uuid4().hex[:6]}{f.suffix}"
        try:
            shutil.move(str(f), str(dest))
            if lbl == "tp":
                stats["promoted_pos"] += 1
            else:
                stats["promoted_neg"] += 1
        except OSError:
            stats["skipped_no_label"] += 1

    return stats


def promote_and_summarize(repo_root: Path) -> Tuple[Dict[str, int], str]:
    unlabeled = repo_root / "data" / "training" / "unlabeled"
    n_unlabeled_npz = (
        len(list(unlabeled.glob("det_*.npz"))) if unlabeled.is_dir() else 0
    )

    stats = promote_labeled_unlabeled(repo_root)
    msg = (
        f"+{stats['promoted_pos']} pos, +{stats['promoted_neg']} neg; "
        f"unlabeled skip {stats['skipped_no_label']}, "
        f"label fn/tn/skip {stats['skipped_unknown_label']}"
    )
    # All zeros usually means no folder or no clips — not a failed match pass.
    hints: list[str] = []
    if not unlabeled.is_dir():
        hints.append("create data/training/unlabeled for new clips")
    elif n_unlabeled_npz == 0:
        hints.append("no det_*.npz in unlabeled (clips save when detection runs)")
    elif (
        stats["promoted_pos"] == 0
        and stats["promoted_neg"] == 0
        and stats["skipped_unknown_label"] == 0
        and stats["skipped_no_label"] > 0
    ):
        hints.append(
            "clips present but no tp/fp label for that second in data/transit_labels.csv"
        )
    if hints:
        msg = f"{msg} — {'; '.join(hints)}"
    return stats, msg
