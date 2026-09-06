#!/usr/bin/env python3
"""
musicbrainz_enrich.py - fills gaps in audio tags using the MusicBrainz API.

Requires: mutagen, musicbrainzngs
  pip3 install mutagen musicbrainzngs --break-system-packages

Companion to fix_music_tags.py: that script infers tags from folder/file
naming conventions; this one looks them up against MusicBrainz for files
where the filename/path itself isn't informative enough to infer from,
or to correct/confirm album + release-date info that path-inference
can't provide.

For each supported audio file (mp3, flac, m4a/mp4, ogg) missing an
artist or album tag:
  1. Uses whatever title/artist is already present (or a path-inferred
     title as fallback) to query MusicBrainz's recording search.
  2. Only accepts a match at or above --min-score confidence (0-100,
     MusicBrainz's own match score).
  3. Fills ONLY missing fields (artist, album, date). Never overwrites
     an existing non-empty tag -- same rule as fix_music_tags.py.

Default mode is dry-run. Pass --execute to write changes. Every run
writes a JSON manifest for audit.

Respects MusicBrainz's rate-limit and requires a descriptive User-Agent
per their API terms of service -- both handled below.
"""

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from mutagen import File as MutagenFile
except ImportError:
    print("ERROR: mutagen not installed. Run:", file=sys.stderr)
    print("  pip3 install mutagen musicbrainzngs --break-system-packages", file=sys.stderr)
    sys.exit(1)

try:
    import musicbrainzngs as mb
except ImportError:
    print("ERROR: musicbrainzngs not installed. Run:", file=sys.stderr)
    print("  pip3 install mutagen musicbrainzngs --break-system-packages", file=sys.stderr)
    sys.exit(1)

SUPPORTED_EXTS = {".mp3", ".flac", ".m4a", ".mp4", ".ogg"}
TAG_TITLE = "title"
TAG_ARTIST = "artist"
TAG_ALBUM = "album"
TAG_DATE = "date"

# Same placeholder convention as fix_music_tags.py -- treat these as blank.
PLACEHOLDER_VALUES = {
    TAG_ARTIST: {"unknown artist"},
    TAG_ALBUM: {"unknown album"},
}


def is_placeholder(field: str, value: str | None) -> bool:
    if not value:
        return False
    return value.strip().lower() in PLACEHOLDER_VALUES.get(field, set())

TRACK_PREFIX_RE = re.compile(r"^\s*(\d{1,3})\s*[-._)]+\s*(.*)$")

mb.set_useragent(
    "ez_tagger",
    "0.1",
    "https://github.com/tangleroot013/ez_tagger",
)


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


def title_from_filename(path: Path) -> str:
    stem = path.stem
    m = TRACK_PREFIX_RE.match(stem)
    return m.group(2).strip() if m else stem.strip()


def lookup_musicbrainz(title: str, artist_hint: str | None, min_score: int):
    """Query MusicBrainz recording search. Returns dict with artist/album/date
    or None if no confident match found."""
    query_parts = [f'recording:"{title}"']
    if artist_hint:
        query_parts.append(f'artist:"{artist_hint}"')
    query = " AND ".join(query_parts)

    try:
        result = mb.search_recordings(query=query, limit=5)
    except mb.WebServiceError as e:
        logging.warning("MusicBrainz lookup failed for %r: %s", title, e)
        return None
    except Exception as e:
        logging.warning("Unexpected error querying MusicBrainz for %r: %s", title, e)
        return None

    recordings = result.get("recording-list", [])
    if not recordings:
        return None

    best = max(recordings, key=lambda r: int(r.get("ext:score", 0)))
    score = int(best.get("ext:score", 0))
    if score < min_score:
        return None

    artist = None
    if best.get("artist-credit"):
        artist = "".join(
            part if isinstance(part, str) else part.get("artist", {}).get("name", "")
            for part in best["artist-credit"]
        ).strip()

    album = None
    date = None
    release_list = best.get("release-list", [])
    if release_list:
        album = release_list[0].get("title")
        date = release_list[0].get("date")

    return {"artist": artist, "album": album, "date": date, "score": score}


def process_file(path: Path, execute: bool, min_score: int, throttle_seconds: float):
    try:
        audio = MutagenFile(path, easy=True)
    except Exception as e:
        return [{"path": str(path), "field": "*", "error": f"unreadable: {e}"}]
    if audio is None:
        return [{"path": str(path), "field": "*", "error": "unrecognized audio format"}]

    current_title = get_tag(audio, TAG_TITLE)
    current_artist = get_tag(audio, TAG_ARTIST)
    current_album = get_tag(audio, TAG_ALBUM)
    current_date = get_tag(audio, TAG_DATE)

    artist_missing = not current_artist or is_placeholder(TAG_ARTIST, current_artist)
    album_missing = not current_album or is_placeholder(TAG_ALBUM, current_album)

    # Only bother MusicBrainz for files actually missing something
    if not artist_missing and not album_missing:
        return []

    title = current_title or title_from_filename(path)
    if not title:
        return []

    time.sleep(throttle_seconds)
    match = lookup_musicbrainz(title, current_artist, min_score)
    if not match:
        return []

    changes = []

    def plan(field, current, new_value):
        if new_value and new_value != current:
            changes.append({
                "path": str(path),
                "field": field,
                "old": current,
                "new": new_value,
                "mb_score": match["score"],
            })

    if artist_missing and match["artist"]:
        plan(TAG_ARTIST, current_artist, match["artist"])
    if album_missing and match["album"]:
        plan(TAG_ALBUM, current_album, match["album"])
    if not current_date and match["date"]:
        plan(TAG_DATE, current_date, match["date"])

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
    ap = argparse.ArgumentParser(description="MusicBrainz-backed tag enrichment (gap-fill only)")
    ap.add_argument("--root", default=str(Path.home() / "Music"), help="Library root (default: ~/Music)")
    ap.add_argument("--execute", action="store_true", help="Actually write tags; default is dry-run")
    ap.add_argument("--min-score", type=int, default=90, help="Minimum MusicBrainz match confidence 0-100 (default: 90)")
    ap.add_argument("--throttle", type=float, default=1.1, help="Seconds to wait between API calls (default: 1.1)")
    ap.add_argument("--manifest", default=str(Path.home() / "musicbrainz_enrich_manifest.json"))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    root = Path(args.root).expanduser()
    if not root.is_dir():
        logging.error("Root does not exist: %s", root)
        sys.exit(1)

    logging.info("Scanning %s (%s), min-score=%d", root, "EXECUTE" if args.execute else "DRY-RUN", args.min_score)

    all_changes, errors, scanned, queried = [], [], 0, 0
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in SUPPORTED_EXTS:
            continue
        scanned += 1
        result = process_file(p, args.execute, args.min_score, args.throttle)
        if result:
            queried += 1
        for r in result:
            (errors if "error" in r else all_changes).append(r)

    logging.info("Scanned %d files, queried MusicBrainz for %d, %d changes proposed/applied",
                 scanned, queried, len(all_changes))
    if errors:
        logging.warning("%d files had errors", len(errors))

    manifest = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "root": str(root),
        "mode": "execute" if args.execute else "dry-run",
        "min_score": args.min_score,
        "summary": {
            "files_scanned": scanned,
            "files_queried": queried,
            "files_changed": len(}ath(a["path"])
            t): quee. }        "broken_totaldio,: quee. audio_figes))
    i       "broken_, len(e:rors))

       },
        "actions quee. audnges, errors
 "broken_, len(e:r scanne
 "brath(args.manifest).write_text(json.dumps(manifest, indent=2))
    logging.info("Manifest written to %s", args.manifest)
    if not args.execute and actions:ges, error   logging.info("Dry-run complete. Re-run with --execute to apply %d action(, errolen(actions)ges))
    if name__ == "__main__":
    main()
