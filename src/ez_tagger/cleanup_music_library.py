#!/usr/bin/env python3
"""
cleanup_music_library.py - idempotent, self-verifying music library cleanup.

Scans a library root for:
  - exact duplicate audio files (SHA-256 content hash)
  - broken/unreadable audio (zero-byte, OR truncated/corrupt per mutagen)
  - OS junk files (.DS_Store, Thumbs.db, .part, .tmp, .crdownload)

Default mode is dry-run (report only, nothing deleted).
Pass --execute to actually delete. Every run writes a JSON manifest
so actions are auditable, and re-running after --execute is a no-op
(idempotent) since duplicates/broken files are already gone.

Corruption detection uses mutagen if available (recommended --
pip3 install mutagen --break-system-packages) to catch truncated or
malformed files beyond simple zero-byte checks. If mutagen isn't
installed, this falls back to zero-byte-only detection rather than
guessing, to avoid false positives.
"""

import argparse
import hashlib
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from mutagen import File as MutagenFile
    HAS_MUTAGEN = True
except ImportError:
    HAS_MUTAGEN = False

AUDIO_EXTS = {".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".wma", ".alac", ".aiff"}
JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}
JUNK_SUFFIXES = {".part", ".tmp", ".crdownload"}


def sha256_of(path: Path, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def is_junk(path: Path) -> bool:
    name = path.name.lower()
    return name in JUNK_NAMES or path.suffix.lower() in JUNK_SUFFIXES


def is_broken_audio(path: Path) -> bool:
    """Zero-byte is always broken. If mutagen is available, also catch
    truncated/corrupt files it can't parse. Without mutagen, only
    zero-byte is flagged -- never guess, to avoid false positives."""
    try:
        if path.stat().st_size == 0:
            return True
    except OSError:
        return True
    if not HAS_MUTAGEN:
        return False
    try:
        audio = MutagenFile(path)
        return audio is None
    except Exception:
        return True


def scan(root: Path):
    audio_files, junk_files, broken_files = [], [], []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if is_junk(p):
            junk_files.append(p)
            continue
        if p.suffix.lower() not in AUDIO_EXTS:
            continue
        if is_broken_audio(p):
            broken_files.append(p)
        else:
            audio_files.append(p)
    return audio_files, junk_files, broken_files


def find_duplicates(audio_files):
    seen, dupes = {}, []
    for p in audio_files:
        try:
            h = sha256_of(p)
        except OSError as e:
            logging.warning("Could not hash %s: %s", p, e)
            continue
        if h in seen:
            dupes.append((p, seen[h]))
        else:
            seen[h] = p
    return dupes


def main():
    ap = argparse.ArgumentParser(description="Idempotent music library cleanup")
    ap.add_argument("--root", default=str(Path.home() / "Music"), help="Library root (default: ~/Music)")
    ap.add_argument("--execute", action="store_true", help="Actually delete; default is dry-run")
    ap.add_argument("--manifest", default=str(Path.home() / "cleanup_music_manifest.json"))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    root = Path(args.root).expanduser()
    if not root.is_dir():
        logging.error("Root does not exist: %s", root)
        sys.exit(1)

    if not HAS_MUTAGEN:
        logging.warning("mutagen not installed -- corruption check limited to zero-byte files. "
                         "Run: pip3 install mutagen --break-system-packages for full detection.")

    logging.info("Scanning %s (%s)", root, "EXECUTE" if args.execute else "DRY-RUN")
    audio_files, junk_files, broken_files = scan(root)
    logging.info("Found %d audio files, %d junk files, %d broken files",
                 len(audio_files), len(junk_files), len(broken_files))

    dupes = find_duplicates(audio_files)
    logging.info("Found %d duplicate audio files", len(dupes))

    actions = []
    for dupe, original in dupes:
        actions.append({"action": "delete_duplicate", "path": str(dupe), "kept": str(original)})
    for j in junk_files:
        actions.append({"action": "delete_junk", "path": str(j)})
    for b in broken_files:
        actions.append({"action": "delete_broken", "path": str(b)})

    if args.execute:
        for a in actions:
            p = Path(a["path"])
            try:
                p.unlink()
                a["status"] = "deleted"
            except OSError as e:
                a["status"] = f"error: {e}"
                logging.warning("Failed to delete %s: %s", p, e)
    else:
        for a in actions:
            a["status"] = "dry-run (not deleted)"

    manifest = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "root": str(root),
        "mode": "execute" if args.execute else "dry-run",
        "mutagen_available": HAS_MUTAGEN,
        "summary": {
            "audio_files_scanned": len(audio_files),
            "duplicates_found": len(dupes),
            "junk_found": len(junk_files),
            "broken_found": len(broken_files),
        },
        "actions": actions,
    }
    Path(args.manifest).write_text(json.dumps(manifest, indent=2))
    logging.info("Manifest written to %s", args.manifest)
    if not args.execute and actions:
        logging.info("Dry-run complete. Re-run with --execute to apply %d action(s).", len(actions))


if __name__ == "__main__":
    main()
