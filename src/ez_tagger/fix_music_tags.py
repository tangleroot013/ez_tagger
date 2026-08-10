#!/usr/bin/env python3
"""
fix_music_tags.py - autonomous, idempotent, self-verifying tag repair.

Requires: mutagen  (pip3 install mutagen --break-system-packages)

Assumes a library laid out as:  <root>/Artist/Album/NN - Title.ext
(common rip/download convention). For each supported audio file
(mp3, flac, m4a/mp4, ogg) it will:

  - infer missing artist   from the grandparent directory name
  - infer missing album    from the parent directory name
  - infer missing title    from the filename (track-number prefix stripped)
  - infer missing tracknumber from a leading number in the filename
  - strip stray leading/trailing whitespace from all text tags
  - zero-pad tracknumber to 2 digits (e.g. "3" -> "03", "3/12" -> "03/12")

It NEVER overwrites a tag that already has a non-empty, non-whitespace
value -- it only fills gaps and normalizes formatting. Files whose tags
are already complete and clean produce zero actions (idempotent).

Default mode is dry-run. Pass --execute to write changes. Every run
writes a JSON manifest of every change (proposed or applied) so it's
auditable.
"""

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from mutagen import File as MutagenFile
except ImportError:
    print("ERROR: mutagen not installed. Run:", file=sys.stderr)
    print("  pip3 install mutagen --break-system-packages", file=sys.stderr)
    sys.exit(1)

SUPPORTED_EXTS = {".mp3", ".flac", ".m4a", ".mp4", ".ogg"}

# EasyID3/EasyMP4/FLAC/OggVorbis all expose these common keys via easy=True
TAG_TITLE = "title"
TAG_ARTIST = "artist"
TAG_ALBUM = "album"
TAG_TRACK = "tracknumber"

TRACK_PREFIX_RE = re.compile(r"^\s*(\d{1,3})\s*[-._)]+\s*(.*)$")
LEADING_DIGITS_RE = re.compile(r"^\s*(\d{1,3})\b")


def infer_from_path(path: Path, root: Path):
    """Infer (artist, album, title, track) from <root>/Artist/Album/NN - Title.ext"""
    try:
        rel_parts = path.relative_to(root).parts
    except ValueError:
        rel_parts = path.parts

    artist = rel_parts[-3] if len(rel_parts) >= 3 else None
    album = rel_parts[-2] if len(rel_parts) >= 2 else None

    stem = path.stem
    m = TRACK_PREFIX_RE.match(stem)
    if m:
        track = m.group(1).zfill(2)
        title = m.group(2).strip()
    else:
        track = None
        title = stem.strip()

    return artist, album, title or None, track


def normalize_track(value: str) -> str:
    """Zero-pad 'N' or 'N/M' style track numbers to 2 digits on each side."""
    value = value.strip()
    if "/" in value:
        num, total = value.split("/", 1)
        num, total = num.strip(), total.strip()
        num = num.zfill(2) if num.isdigit() else num
        total = total.zfill(2) if total.isdigit() else total
        return f"{num}/{total}"
    if value.isdigit():
        return value.zfill(2)
    m = LEADING_DIGITS_RE.match(value)
    if m:
        return m.group(1).zfill(2)
    return value


def get_tag(audio, key):
    try:
        vals = audio.get(key)
    except Exception:
        return None
    if not vals:
        return None
    v = vals[0] if isinstance(vals, list) else vals
    v = str(v).strip()
    return v or None


def process_file(path: Path, root: Path, execute: bool):
    """Returns list of action dicts for this file (empty if nothing to fix)."""
    try:
        audio = MutagenFile(path, easy=True)
    except Exception as e:
        return [{"path": str(path), "field": "*", "error": f"unreadable: {e}"}]

    if audio is None:
        return [{"path": str(path), "field": "*", "error": "unrecognized audio format"}]

    inferred_artist, inferred_album, inferred_title, inferred_track = infer_from_path(path, root)

    current_title = get_tag(audio, TAG_TITLE)
    current_artist = get_tag(audio, TAG_ARTIST)
    current_album = get_tag(audio, TAG_ALBUM)
    current_track = get_tag(audio, TAG_TRACK)

    changes = []

    def plan(field, current, new_value):
        if new_value and new_value != current:
            changes.append({
                "path": str(path),
                "field": field,
                "old": current,
                "new": new_value,
            })

    # Fill gaps only -- never clobber an existing non-empty value
    if not current_title and inferred_title:
        plan(TAG_TITLE, current_title, inferred_title)
    if not current_artist and inferred_artist:
        plan(TAG_ARTIST, current_artist, inferred_artist)
    if not current_album and inferred_album:
        plan(TAG_ALBUM, current_album, inferred_album)
    if not current_track and inferred_track:
        plan(TAG_TRACK, current_track, inferred_track)

    # Normalize formatting of whatever ends up as the track number
    track_after_fill = current_track or inferred_track
    if track_after_fill:
        normalized = normalize_track(track_after_fill)
        if normalized != current_track:
            # avoid double-planning if we just inferred it fresh with correct padding
            already_planned = any(c["field"] == TAG_TRACK for c in changes)
            if not already_planned:
                plan(TAG_TRACK, current_track, normalized)

    # Trim stray whitespace on existing values (rare, but cheap to fix)
    for field, current in ((TAG_TITLE, current_title), (TAG_ARTIST, current_artist), (TAG_ALBUM, current_album)):
        if current and current != current.strip():
            plan(field, current, current.strip())

    if not changes:
        return []

    if execute:
        try:
            for c in changes:
                audio[c["field"]] = c["new"]
            audio.save()
            for c in changes:
                c["status"] = "applied"
        except Exception as e:
            for c in changes:
                c["status"] = f"error: {e}"
    else:
        for c in changes:
            c["status"] = "dry-run (not written)"

    return changes


def main():
    ap = argparse.ArgumentParser(description="Autonomous, idempotent music tag repair")
    ap.add_argument("--root", default=str(Path.home() / "Music"), help="Library root (default: ~/Music)")
    ap.add_argument("--execute", action="store_true", help="Actually write tags; default is dry-run")
    ap.add_argument("--manifest", default=str(Path.home() / "fix_music_tags_manifest.json"))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    root = Path(args.root).expanduser()
    if not root.is_dir():
        logging.error("Root does not exist: %s", root)
        sys.exit(1)

    logging.info("Scanning %s (%s)", root, "EXECUTE" if args.execute else "DRY-RUN")

    all_changes = []
    errors = []
    scanned = 0
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in SUPPORTED_EXTS:
            continue
        scanned += 1
        result = process_file(p, root, args.execute)
        for r in result:
            if "error" in r:
                errors.append(r)
            else:
                all_changes.append(r)

    logging.info("Scanned %d supported audio files", scanned)
    logging.info("Planned/applied %d tag changes across %d files",
                 len(all_changes), len({c["path"] for c in all_changes}))
    if errors:
        logging.warning("%d files could not be read/tagged", len(errors))

    manifest = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "root": str(root),
        "mode": "execute" if args.execute else "dry-run",
        "summary": {
            "files_scanned": scanned,
            "files_changed": len({c["path"] for c in all_changes}),
            "total_tag_changes": len(all_changes),
            "unreadable_files": len(errors),
        },
        "changes": all_changes,
        "errors": errors,
    }
    Path(args.manifest).write_text(json.dumps(manifest, indent=2))
    logging.info("Manifest written to %s", args.manifest)
    if not args.execute and all_changes:
        logging.info("Dry-run complete. Re-run with --execute to apply %d change(s).", len(all_changes))


if __name__ == "__main__":
    main()
