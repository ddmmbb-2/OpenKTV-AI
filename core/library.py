from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

TIMESTAMP_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?\]|^[%$&](\d+(?:\.\d+)?)")
LRC_INLINE_TIMESTAMP_RE = re.compile(r"\[\d{1,2}:\d{2}(?:[.:]\d{1,3})?\]")
SYMBOL_NOISE_LINE_RE = re.compile(r"^[^0-9A-Za-z\u3400-\u9fff]+$")
REPEATED_SYMBOL_RE = re.compile(r"([^\w\s\u3400-\u9fff])\1{2,}")
CATALOG_FIELDS = [
    "id", "artist", "title", "album", "release_year", "duration_seconds",
    "artist_gender", "genre", "lyrics", "separator_model", "separator_mode",
    "alignment_model", "video_filename", "instrumental_filename", "lyrics_filename",
    "source_url", "youtube_title", "vocals_filename", "dereverbed_vocals_filename",
    "alignment_results_filename", "processing_status", "created_at",
    "language", "char_count", "pinyin_abbr", "zhuyin_abbr",
]

def _clean_bilingual_name(text: str) -> str:
    """
    移除「中文名 英文名」格式中的英文部分。
    例如: "簡單愛 Simple Love" -> "簡單愛", "周杰倫 Jay Chou" -> "周杰倫"
    "K歌之王 King of KTV" -> "K歌之王"
    """
    if not text:
        return ""
    if re.search(r'[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]', text):
        cleaned = re.sub(r'\s+[A-Za-z0-9\s\.\-\'’]+$', '', text).strip()
        if cleaned:
            return cleaned
    return text.strip()

def stable_song_id(artist: str, title: str, source_url: str = "") -> str:
    identity = "|".join((artist.strip().casefold(), title.strip().casefold()))
    if not artist.strip() or not title.strip():
        identity = source_url.strip()
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()

def plain_lyrics_from_lrc(lrc_path: Path) -> str:
    if not lrc_path.is_file():
        return ""
    contents = lrc_path.read_text(encoding="utf-8", errors="ignore")
    is_ktv_lrc = "@format ktv-lrc" in contents
    lines = []
    for raw_line in contents.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("@"):
            continue
        if is_ktv_lrc and not line.startswith("%"):
            continue
        line = re.sub(r"^%\d+(?:\.\d+)?\s+\d+(?:\.\d+)?\s+", "", line)
        line = re.sub(r"^\$\d+(?:\.\d+)?\s+\d+(?:\.\d+)?\s+", "", line)
        line = LRC_INLINE_TIMESTAMP_RE.sub("", line)
        line = re.sub(r"\[(?:ar|al|ti|by|re|ve|offset):[^\]]*\]", "", line, flags=re.IGNORECASE)
        line = _strip_weird_unicode(line).strip()
        if line:
            lines.append(line)
    return " ".join(lines)

def _read_catalog(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))

def _write_catalog(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            writer = csv.DictWriter(handle, fieldnames=CATALOG_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows({field: row.get(field, "") or "" for field in CATALOG_FIELDS} for row in rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()

def get_db_path(index_path: Path) -> Path:
    return index_path.with_name("library.db")

def init_library_db(db_path: Path, csv_path: Path | None = None) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    with conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS songs (
                id TEXT PRIMARY KEY,
                artist TEXT,
                title TEXT,
                album TEXT,
                release_year TEXT,
                duration_seconds REAL,
                artist_gender TEXT,
                genre TEXT,
                lyrics TEXT,
                separator_model TEXT,
                separator_mode TEXT,
                alignment_model TEXT,
                video_filename TEXT,
                instrumental_filename TEXT,
                lyrics_filename TEXT,
                source_url TEXT,
                youtube_title TEXT,
                vocals_filename TEXT,
                dereverbed_vocals_filename TEXT,
                alignment_results_filename TEXT,
                processing_status TEXT,
                created_at TEXT,
                language TEXT,
                char_count INTEGER,
                pinyin_abbr TEXT,
                zhuyin_abbr TEXT,
                updated_at TEXT
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_songs_artist ON songs(artist);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_songs_lang ON songs(language);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_songs_created ON songs(created_at);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_songs_title ON songs(title);")

        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM songs")
        count = cur.fetchone()[0]
        if count == 0 and csv_path and csv_path.is_file():
            csv_rows = _read_catalog(csv_path)
            now_str = datetime.now(timezone.utc).isoformat()
            for r in csv_rows:
                cur.execute("""
                    INSERT OR REPLACE INTO songs (
                        id, artist, title, album, release_year, duration_seconds,
                        artist_gender, genre, lyrics, separator_model, separator_mode,
                        alignment_model, video_filename, instrumental_filename, lyrics_filename,
                        source_url, youtube_title, vocals_filename, dereverbed_vocals_filename,
                        alignment_results_filename, processing_status, created_at,
                        language, char_count, pinyin_abbr, zhuyin_abbr, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    r.get("id") or stable_song_id(r.get("artist", ""), r.get("title", ""), r.get("source_url", "")),
                    r.get("artist", ""),
                    r.get("title", ""),
                    r.get("album", ""),
                    r.get("release_year", ""),
                    float(r.get("duration_seconds") or 0.0),
                    r.get("artist_gender", ""),
                    r.get("genre", ""),
                    r.get("lyrics", ""),
                    r.get("separator_model", ""),
                    r.get("separator_mode", ""),
                    r.get("alignment_model", ""),
                    r.get("video_filename", ""),
                    r.get("instrumental_filename", ""),
                    r.get("lyrics_filename", ""),
                    r.get("source_url", ""),
                    r.get("youtube_title", ""),
                    r.get("vocals_filename", ""),
                    r.get("dereverbed_vocals_filename", ""),
                    r.get("alignment_results_filename", ""),
                    r.get("processing_status", "complete"),
                    r.get("created_at") or now_str,
                    r.get("language", ""),
                    int(r.get("char_count") or len(r.get("title", ""))),
                    r.get("pinyin_abbr", ""),
                    r.get("zhuyin_abbr", ""),
                    now_str,
                ))
    return conn

def sync_db_to_csv(db_path: Path, csv_path: Path) -> None:
    conn = sqlite3.connect(str(db_path), timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    with conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM songs ORDER BY artist, title")
        rows = [dict(r) for r in cur.fetchall()]
    _write_catalog(csv_path, rows)

def upsert_catalog_entry(index_path: Path, entry: dict) -> None:
    db_path = get_db_path(index_path)
    conn = init_library_db(db_path, index_path)
    row = {field: str(entry.get(field, "") or "") for field in CATALOG_FIELDS}
    if not row["id"]:
        row["id"] = stable_song_id(row["artist"], row["title"], row["source_url"])
    now_str = datetime.now(timezone.utc).isoformat()
    if not row["created_at"]:
        row["created_at"] = now_str

    with conn:
        cur = conn.cursor()
        cur.execute("SELECT id, created_at FROM songs WHERE id = ? OR (artist = ? AND title = ?)",
                    (row["id"], row["artist"], row["title"]))
        existing = cur.fetchone()
        if existing:
            row["id"] = existing["id"]
            if existing["created_at"]:
                row["created_at"] = existing["created_at"]

        conn.execute("""
            INSERT OR REPLACE INTO songs (
                id, artist, title, album, release_year, duration_seconds,
                artist_gender, genre, lyrics, separator_model, separator_mode,
                alignment_model, video_filename, instrumental_filename, lyrics_filename,
                source_url, youtube_title, vocals_filename, dereverbed_vocals_filename,
                alignment_results_filename, processing_status, created_at,
                language, char_count, pinyin_abbr, zhuyin_abbr, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            row["id"], row["artist"], row["title"], row.get("album", ""), row.get("release_year", ""),
            float(row.get("duration_seconds") or 0.0), row.get("artist_gender", ""), row.get("genre", ""),
            row.get("lyrics", ""), row.get("separator_model", ""), row.get("separator_mode", ""),
            row.get("alignment_model", ""), row.get("video_filename", ""), row.get("instrumental_filename", ""),
            row.get("lyrics_filename", ""), row.get("source_url", ""), row.get("youtube_title", ""),
            row.get("vocals_filename", ""), row.get("dereverbed_vocals_filename", ""),
            row.get("alignment_results_filename", ""), row.get("processing_status", "complete"),
            row["created_at"], row.get("language", ""), int(row.get("char_count") or len(row.get("title", ""))),
            row.get("pinyin_abbr", ""), row.get("zhuyin_abbr", ""), now_str,
        ))
    sync_db_to_csv(db_path, index_path)

def update_catalog_entry(index_path: Path, song_id: str, updates: dict) -> dict | None:
    editable_fields = {
        "artist", "title", "album", "release_year", "artist_gender", "genre", "language", "lyrics",
    }
    db_path = get_db_path(index_path)
    conn = init_library_db(db_path, index_path)
    with conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM songs WHERE id = ?", (song_id,))
        existing = cur.fetchone()
        if not existing:
            return None
        existing_dict = dict(existing)
        new_artist = str(updates.get("artist", existing_dict["artist"])).strip()
        new_title = str(updates.get("title", existing_dict["title"])).strip()

        cur.execute("SELECT id FROM songs WHERE id != ? AND LOWER(TRIM(artist)) = ? AND LOWER(TRIM(title)) = ?",
                    (song_id, new_artist.casefold(), new_title.casefold()))
        dup = cur.fetchone()
        if dup:
            raise ValueError("相同歌手與歌名已存在於曲庫")

        set_clauses = []
        params = []
        for field, value in updates.items():
            if field in editable_fields and value is not None:
                set_clauses.append(f"{field} = ?")
                params.append(str(value).strip())
                existing_dict[field] = str(value).strip()

        now_str = datetime.now(timezone.utc).isoformat()
        set_clauses.append("updated_at = ?")
        params.append(now_str)
        existing_dict["updated_at"] = now_str
        params.append(song_id)

        if set_clauses:
            conn.execute(f"UPDATE songs SET {', '.join(set_clauses)} WHERE id = ?", params)

    sync_db_to_csv(db_path, index_path)
    return existing_dict

def delete_catalog_entry(
    index_path: Path,
    song_id: str,
    delete_media_files: bool = False,
    songs_dir: Path | None = None
) -> dict | None:
    db_path = get_db_path(index_path)
    conn = init_library_db(db_path, index_path)
    with conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM songs WHERE id = ?", (song_id,))
        row = cur.fetchone()
        if not row:
            return None
        deleted = dict(row)
        cur.execute("DELETE FROM songs WHERE id = ?", (song_id,))

    sync_db_to_csv(db_path, index_path)

    deleted_files = []
    if delete_media_files and songs_dir and songs_dir.is_dir():
        file_fields = [
            "video_filename", "instrumental_filename", "lyrics_filename",
            "vocals_filename", "dereverbed_vocals_filename", "alignment_results_filename"
        ]
        for fld in file_fields:
            fname = deleted.get(fld)
            if fname:
                target = songs_dir / fname
                if target.is_file():
                    try:
                        target.unlink()
                        deleted_files.append(fname)
                    except Exception:
                        pass
        if deleted.get("video_filename"):
            stem = Path(deleted["video_filename"]).stem
            for extra_cand in (f"{stem}.original.instrumental.m4a", f"{stem}.dialogue.vocals.wav", f"{stem}.pitch.json"):
                target = songs_dir / extra_cand
                if target.is_file():
                    try:
                        target.unlink()
                        deleted_files.append(extra_cand)
                    except Exception:
                        pass

    deleted["deleted_media_files"] = deleted_files
    return deleted

def get_catalog_health(songs_dir: Path, db_path: Path) -> dict:
    conn = sqlite3.connect(str(db_path), timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    with conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM songs")
        songs = [dict(r) for r in cur.fetchall()]

    referenced_files = set()
    missing_media_list = []
    word_synced_count = 0
    line_synced_count = 0
    plain_lyrics_count = 0
    missing_instrumental_count = 0

    for s in songs:
        missing_for_song = []
        if s.get("video_filename"):
            vf = s["video_filename"]
            referenced_files.add(vf)
            if not (songs_dir / vf).is_file():
                missing_for_song.append(vf)
        if s.get("instrumental_filename"):
            inf = s["instrumental_filename"]
            referenced_files.add(inf)
            if not (songs_dir / inf).is_file():
                missing_for_song.append(inf)
                missing_instrumental_count += 1
        else:
            missing_instrumental_count += 1

        if s.get("lyrics_filename"):
            lf = s["lyrics_filename"]
            referenced_files.add(lf)
            lrc_file = songs_dir / lf
            if not lrc_file.is_file():
                missing_for_song.append(lf)
            else:
                try:
                    content = lrc_file.read_text(encoding="utf-8", errors="ignore")
                    if "@format ktv-lrc" in content or "$" in content:
                        word_synced_count += 1
                    elif "%" in content or re.search(r"\[\d+:\d+", content):
                        line_synced_count += 1
                    else:
                        plain_lyrics_count += 1
                except Exception:
                    pass
        elif s.get("lyrics"):
            plain_lyrics_count += 1

        for voc_field in ("vocals_filename", "dereverbed_vocals_filename", "alignment_results_filename"):
            if s.get(voc_field):
                referenced_files.add(s[voc_field])
        if s.get("video_filename"):
            stem = Path(s["video_filename"]).stem
            referenced_files.add(f"{stem}.pitch.json")

        if missing_for_song:
            missing_media_list.append({
                "id": s["id"],
                "title": s["title"],
                "artist": s["artist"],
                "missing": missing_for_song
            })

    total_disk_bytes = 0
    total_file_count = 0
    orphaned_files = []
    orphan_bytes = 0

    system_filenames = {
        "library.db", "library.db-wal", "library.db-shm",
        "library_index.csv", "song_stats.json"
    }

    if songs_dir.is_dir():
        for item in songs_dir.iterdir():
            if not item.is_file():
                continue
            size = item.stat().st_size
            total_disk_bytes += size
            total_file_count += 1
            name = item.name
            if name in system_filenames or name.startswith("."):
                continue
            if name not in referenced_files:
                orphaned_files.append({
                    "filename": name,
                    "size": size,
                    "mtime": item.stat().st_mtime
                })
                orphan_bytes += size

    return {
        "total_songs": len(songs),
        "total_disk_bytes": total_disk_bytes,
        "total_file_count": total_file_count,
        "missing_media": missing_media_list,
        "missing_media_count": len(missing_media_list),
        "missing_instrumental_count": missing_instrumental_count,
        "orphaned_files": orphaned_files,
        "orphan_count": len(orphaned_files),
        "orphan_bytes": orphan_bytes,
        "word_synced_count": word_synced_count,
        "line_synced_count": line_synced_count,
        "plain_lyrics_count": plain_lyrics_count,
    }

def cleanup_orphan_files(songs_dir: Path, db_path: Path) -> dict:
    health = get_catalog_health(songs_dir, db_path)
    orphans = health.get("orphaned_files", [])
    deleted_count = 0
    freed_bytes = 0
    errors = []
    for item in orphans:
        fname = item["filename"]
        target = songs_dir / fname
        try:
            if target.is_file():
                size = target.stat().st_size
                target.unlink()
                deleted_count += 1
                freed_bytes += size
        except Exception as err:
            errors.append(f"{fname}: {err}")
    return {
        "ok": True,
        "deleted_count": deleted_count,
        "freed_bytes": freed_bytes,
        "errors": errors
    }

def save_lyrics_for_song(
    songs_dir: Path,
    index_path: Path,
    song_id: str,
    lrc_content: str
) -> dict | None:
    db_path = get_db_path(index_path)
    conn = init_library_db(db_path, index_path)
    with conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM songs WHERE id = ?", (song_id,))
        row = cur.fetchone()
        if not row:
            return None
        song = dict(row)

    lrc_filename = song.get("lyrics_filename")
    if not lrc_filename:
        video_fn = song.get("video_filename") or f"{song_id}.mp4"
        stem = Path(video_fn).stem
        lrc_filename = f"{stem}.lrc"

    target_lrc = songs_dir / lrc_filename
    target_lrc.parent.mkdir(parents=True, exist_ok=True)
    target_lrc.write_text(lrc_content, encoding="utf-8")

    plain_text = plain_lyrics_from_lrc(target_lrc)
    now_str = datetime.now(timezone.utc).isoformat()

    with conn:
        conn.execute("""
            UPDATE songs
            SET lyrics_filename = ?, lyrics = ?, updated_at = ?
            WHERE id = ?
        """, (lrc_filename, plain_text, now_str, song_id))

    sync_db_to_csv(db_path, index_path)
    return {
        "id": song_id,
        "lyrics_filename": lrc_filename,
        "lyrics": plain_text,
        "updated_at": now_str
    }

def parse_first_lyric_time(lrc_path: Path) -> float | None:
    if not lrc_path.is_file():
        return None
    for raw_line in lrc_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = TIMESTAMP_RE.search(line)
        if not match:
            continue
        if match.group(4):
            return float(match.group(4))
        minutes = int(match.group(1) or 0)
        seconds = int(match.group(2) or 0)
        millis = int((match.group(3) or "0").ljust(3, "0"))
        return minutes * 60 + seconds + millis / 1000.0
    return None

def find_intro_skip_seconds(songs_dir: Path, song_filename: str, lead_seconds: float) -> tuple[float | None, float | None]:
    song_name = Path(song_filename).name
    song_stem = song_name[:-4] if song_name.lower().endswith(".mp4") else song_name
    lrc_candidates = [
        songs_dir / f"{song_stem}.ktv.lrc",
        songs_dir / f"{song_stem}.lrc",
    ]
    first_line_time = None
    for candidate in lrc_candidates:
        first_line_time = parse_first_lyric_time(candidate)
        if first_line_time is not None:
            break

    if first_line_time is None:
        return None, None

    return max(0.0, first_line_time - max(0.0, lead_seconds)), first_line_time

def update_library_index(songs_dir: Path, index_path: Path) -> None:
    db_path = get_db_path(index_path)
    conn = init_library_db(db_path, index_path)
    with conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM songs")
        previous_rows = [dict(r) for r in cur.fetchall()]
    existing_by_video = {row.get("video_filename", ""): row for row in previous_rows}
    seen_ids = set()
    timestamp = int(time.time())
    now_str = datetime.now(timezone.utc).isoformat()

    for mp4_file in sorted(songs_dir.glob("*.mp4")):
        stem = mp4_file.stem
        previous = existing_by_video.get(mp4_file.name, {})
        lrc_name = f"{stem}.lrc" if (songs_dir / f"{stem}.lrc").is_file() else ""
        entry = {
            **previous,
            "id": previous.get("id") or stable_song_id(previous.get("artist", ""), previous.get("title", ""), previous.get("source_url", mp4_file.name)),
            "title": previous.get("title") or stem,
            "video_filename": mp4_file.name,
            "instrumental_filename": f"{stem}.instrumental.m4a" if (songs_dir / f"{stem}.instrumental.m4a").is_file() else "",
            "lyrics_filename": lrc_name,
            "vocals_filename": f"{stem}.vocals.wav" if (songs_dir / f"{stem}.vocals.wav").is_file() else "",
            "dereverbed_vocals_filename": f"{stem}.vocals.dereverbed.wav" if (songs_dir / f"{stem}.vocals.dereverbed.wav").is_file() else "",
            "lyrics": plain_lyrics_from_lrc(songs_dir / lrc_name) if lrc_name else "",
            "processing_status": "complete",
            "created_at": previous.get("created_at") or str(timestamp),
        }
        if entry["id"] in seen_ids:
            continue
        seen_ids.add(entry["id"])

        with conn:
            conn.execute("""
                INSERT OR REPLACE INTO songs (
                    id, artist, title, album, release_year, duration_seconds,
                    artist_gender, genre, lyrics, separator_model, separator_mode,
                    alignment_model, video_filename, instrumental_filename, lyrics_filename,
                    source_url, youtube_title, vocals_filename, dereverbed_vocals_filename,
                    alignment_results_filename, processing_status, created_at,
                    language, char_count, pinyin_abbr, zhuyin_abbr, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                entry["id"], entry.get("artist", ""), entry.get("title", ""), entry.get("album", ""), entry.get("release_year", ""),
                float(entry.get("duration_seconds") or 0.0), entry.get("artist_gender", ""), entry.get("genre", ""),
                entry.get("lyrics", ""), entry.get("separator_model", ""), entry.get("separator_mode", ""),
                entry.get("alignment_model", ""), entry.get("video_filename", ""), entry.get("instrumental_filename", ""),
                entry.get("lyrics_filename", ""), entry.get("source_url", ""), entry.get("youtube_title", ""),
                entry.get("vocals_filename", ""), entry.get("dereverbed_vocals_filename", ""),
                entry.get("alignment_results_filename", ""), entry.get("processing_status", "complete"),
                entry["created_at"], entry.get("language", ""), int(entry.get("char_count") or len(entry.get("title", ""))),
                entry.get("pinyin_abbr", ""), entry.get("zhuyin_abbr", ""), now_str,
            ))

    sync_db_to_csv(db_path, index_path)

def fetch_lrclib_lyrics(song_name: str, singer: str) -> str | None:
    if not song_name:
        return None

    clean_song = _clean_bilingual_name(song_name)
    clean_singer = _clean_bilingual_name(singer)
    candidates = []
    candidates.append((song_name, singer))
    if clean_song != song_name or clean_singer != singer:
        candidates.append((clean_song, clean_singer))
    candidates.append((clean_song, ""))
    if clean_song != song_name:
        candidates.append((song_name, ""))

    seen_queries = set()
    for track, artist in candidates:
        query_dict = {"track_name": track}
        if artist:
            query_dict["artist_name"] = artist
        query_string = urllib.parse.urlencode(query_dict)
        if query_string in seen_queries:
            continue
        seen_queries.add(query_string)

        url = f"https://lrclib.net/api/search?{query_string}"
        request = urllib.request.Request(url, headers={"User-Agent": "OpenKTV-AI/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=12) as response:  # nosec B310
                payload = json.loads(response.read().decode("utf-8"))
        except Exception:
            continue

        if not isinstance(payload, list) or not payload:
            continue

        for item in payload:
            if not isinstance(item, dict):
                continue
            cleaned = sanitize_lrclib_lyrics(item.get("plainLyrics"))
            if cleaned:
                return cleaned

    return None

def fetch_lrclib_lyrics_by_url(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url.strip())
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"lrclib.net", "www.lrclib.net"}
        or parsed.port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("歌詞網址必須是 HTTPS LRCLIB 網址")
    if not re.fullmatch(r"/api/get/\d+", parsed.path):
        raise ValueError("LRCLIB 歌詞網址必須是 /api/get/{數字 ID} API 網址")

    request = urllib.request.Request(url, headers={"User-Agent": "OpenKTV-AI/1.0"})
    with urllib.request.urlopen(request, timeout=12) as response:  # nosec B310
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        return None
    lyrics = payload.get("plainLyrics") or payload.get("syncedLyrics")
    return sanitize_lrclib_lyrics(lyrics)

def sanitize_lrclib_lyrics(lyrics: str | None) -> str | None:
    if not lyrics:
        return None

    cleaned_lines: list[str] = []
    for raw_line in lyrics.splitlines():
        line = _strip_weird_unicode(raw_line).strip()
        if not line:
            continue
        line = LRC_INLINE_TIMESTAMP_RE.sub("", line).strip()
        line = REPEATED_SYMBOL_RE.sub(r"\1\1", line).strip()
        if not line or SYMBOL_NOISE_LINE_RE.fullmatch(line):
            continue
        cleaned_lines.append(line)

    if not cleaned_lines:
        return None
    return "\n".join(cleaned_lines)

def _strip_weird_unicode(text: str) -> str:
    allowed = []
    for char in text:
        category = unicodedata.category(char)
        if char in {"\n", "\r", "\t"}:
            allowed.append(char)
            continue
        if category in {"Cc", "Cf", "Cs", "Co", "Cn"}:
            continue
        if char in {"\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"}:
            continue
        allowed.append(char)
    return "".join(allowed)

def write_ktv_lrc_template(output_path: Path, song_name: str, singer: str, lyrics: str | None) -> None:
    lines = [line.strip() for line in (lyrics or "").splitlines() if line.strip()]
    output = [
        "@format ktv-lrc",
        "@version 1",
        "",
        "@id 000000",
        "",
        f"@song_name {song_name}, zh-tw",
        f"@singer {singer or 'Unknown'}, zh-tw",
        "@duration 0",
        "",
        "@lyrics zh-tw",
        "@lyric_synced line",
        "",
        "@lyric zh-tw",
    ]

    cursor = 0.0
    if lines:
        for line in lines:
            end = cursor + 2.0
            output.append(f"%{cursor:.3f} {end:.3f} {line}")
            cursor = end
    else:
        output.append("%0.000 0.000 (暫無歌詞)")

    output.extend(["", "@dialogue"])
    output_path.write_text("\n".join(output) + "\n", encoding="utf-8")
