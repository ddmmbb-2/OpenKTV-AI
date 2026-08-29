import sys
import os
import queue

# ==========================================
# 【終極修復】修正 Bad file descriptor 崩潰問題
# ==========================================
system_log_queue = queue.Queue()

class GUIWriter:
    def __init__(self):
        # 【關鍵】開啟系統底層的空裝置 (devnull)，取得真實合法的檔案描述符
        self.null_file = open(os.devnull, 'w')

    def write(self, data):
        # 攔截所有 print 和系統報錯，丟進佇列中
        if data and data.strip():
            system_log_queue.put(data.strip())

    def flush(self):
        pass

    def isatty(self):
        return False

    def fileno(self):
        # 【關鍵】回傳真實合法的空裝置描述符，徹底騙過 Flask 的 click 模組！
        return self.null_file.fileno()

if getattr(sys, 'frozen', False):
    # 打包成 EXE 後，強制把所有輸出導向我們的攔截器
    sys_writer = GUIWriter()
    sys.stdout = sys_writer
    sys.stderr = sys_writer

# ==========================================
# 正常 Import 區
# ==========================================
import tkinter as tk
from tkinter import filedialog, messagebox
# ... 下面的 import 保留原樣 ...
import subprocess
import shutil
import threading
import socket
import json
import re
import time
import webbrowser
import urllib.request
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from flask import Flask, render_template, request, send_from_directory
from flask_socketio import SocketIO, emit
import multiprocessing

# ==========================================
# 設定區
# ==========================================
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable) 
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
FFMPEG_DIR = os.path.join(BASE_DIR, "ffmpeg", "bin")
YT_DLP_PATH = os.path.join(BASE_DIR, "yt-dlp.exe")
APP_CONFIG_PATH = os.path.join(BASE_DIR, "_app_config.json")
LOCAL_STATE_DIR = os.path.join(BASE_DIR, "_state")
VALID_DEMUCS_MODELS = {"htdemucs", "mdx_q", "auto"}
DEFAULT_DEMUCS_MODEL = "htdemucs"


def normalize_library_path(path_value):
    text = str(path_value or "").strip()
    if not text:
        return ""
    expanded = os.path.expandvars(os.path.expanduser(text))
    if not os.path.isabs(expanded):
        expanded = os.path.join(BASE_DIR, expanded)
    return os.path.abspath(expanded)


def load_app_config():
    if not os.path.exists(APP_CONFIG_PATH):
        return {}
    try:
        with open(APP_CONFIG_PATH, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def save_app_config(data):
    payload = data if isinstance(data, dict) else {}
    with open(APP_CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def normalize_demucs_model(value, default=DEFAULT_DEMUCS_MODEL):
    text = str(value or "").strip().lower()
    return text if text in VALID_DEMUCS_MODELS else default


def get_demucs_model_setting():
    cfg = load_app_config()
    return normalize_demucs_model(cfg.get("demucs_model"), DEFAULT_DEMUCS_MODEL)


def set_demucs_model_setting(model_name):
    normalized = normalize_demucs_model(model_name, "")
    if normalized not in VALID_DEMUCS_MODELS:
        raise ValueError("Invalid Demucs model option")
    cfg = load_app_config()
    cfg["demucs_model"] = normalized
    save_app_config(cfg)
    return normalized

def get_runtime_python_executable():
    candidates = [
        os.path.join(BASE_DIR, ".venv", "Scripts", "python.exe"),
        os.path.join(BASE_DIR, ".venv", "bin", "python"),
        sys.executable,
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return sys.executable

def get_ffmpeg_executable():
    local_ffmpeg = os.path.join(FFMPEG_DIR, "ffmpeg.exe")
    if os.path.exists(local_ffmpeg):
        return local_ffmpeg

    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg

    winget_ffmpeg = os.path.join(
        os.environ.get("LOCALAPPDATA", ""),
        "Microsoft",
        "WinGet",
        "Packages",
        "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe",
        "ffmpeg-9.0-full_build",
        "bin",
        "ffmpeg.exe",
    )
    if os.path.exists(winget_ffmpeg):
        return winget_ffmpeg

    return None

def get_deno_executable():
    system_deno = shutil.which("deno")
    if system_deno:
        return system_deno

    winget_deno = os.path.join(
        os.environ.get("LOCALAPPDATA", ""),
        "Microsoft",
        "WinGet",
        "Packages",
        "DenoLand.Deno_Microsoft.Winget.Source_8wekyb3d8bbwe",
        "deno.exe",
    )
    if os.path.exists(winget_deno):
        return winget_deno

    return None

def get_ytdlp_command():
    if os.path.exists(YT_DLP_PATH):
        return [YT_DLP_PATH]
    runtime_python = get_runtime_python_executable()
    return [runtime_python, "-m", "yt_dlp"]

RESOLVED_FFMPEG = get_ffmpeg_executable()
if RESOLVED_FFMPEG:
    resolved_ffmpeg_dir = os.path.dirname(RESOLVED_FFMPEG)
    if resolved_ffmpeg_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] += os.pathsep + resolved_ffmpeg_dir

if os.path.exists(FFMPEG_DIR) and FFMPEG_DIR not in os.environ.get("PATH", ""):
    os.environ["PATH"] += os.pathsep + FFMPEG_DIR
os.environ["PATH"] += os.pathsep + BASE_DIR

if not os.path.exists(LOCAL_STATE_DIR):
    os.makedirs(LOCAL_STATE_DIR, exist_ok=True)


_boot_cfg = load_app_config()
_configured_library = normalize_library_path(_boot_cfg.get("songs_dir", ""))
SONGS_DIR = _configured_library or os.path.join(BASE_DIR, "ktv_songs")
TEMP_BASE_DIR = os.path.join(BASE_DIR, "temp_processing") 
SONG_METADATA_PATH = os.path.join(SONGS_DIR, "_song_metadata.json")
SONG_METADATA_BACKUP_PATH = os.path.join(LOCAL_STATE_DIR, "_song_metadata_backup.json")
PLAYBACK_STATE_PATH = os.path.join(LOCAL_STATE_DIR, "_playback_state.json")
PLAYBACK_HISTORY_DAYS = 14
LAST_KNOWN_SONGS = []

try:
    if not os.path.exists(SONGS_DIR):
        os.makedirs(SONGS_DIR)
except Exception:
    # Network library can be temporarily offline during boot; keep running with local cache.
    pass
if not os.path.exists(TEMP_BASE_DIR): os.makedirs(TEMP_BASE_DIR)


def set_songs_dir(new_dir, persist=True, allow_unavailable=True):
    global SONGS_DIR, SONG_METADATA_PATH
    normalized = normalize_library_path(new_dir)
    if not normalized:
        raise ValueError("Library path is required")
    available = True
    warning = ""
    try:
        os.makedirs(normalized, exist_ok=True)
    except Exception as exc:
        if not allow_unavailable:
            raise
        available = False
        warning = str(exc)

    SONGS_DIR = normalized
    SONG_METADATA_PATH = os.path.join(SONGS_DIR, "_song_metadata.json")
    if persist:
        cfg = load_app_config()
        cfg["songs_dir"] = SONGS_DIR
        save_app_config(cfg)
    return {
        "path": SONGS_DIR,
        "available": available,
        "warning": warning,
    }


def migrate_song_library(new_dir, move_existing=True, allow_unavailable=True):
    current = os.path.abspath(SONGS_DIR)
    target = normalize_library_path(new_dir)
    if not target:
        raise ValueError("Library path is required")
    target = os.path.abspath(target)

    target_available = True
    target_warning = ""
    try:
        os.makedirs(target, exist_ok=True)
    except Exception as exc:
        if not allow_unavailable:
            raise
        target_available = False
        target_warning = str(exc)

    moved = 0
    skipped = 0
    failures = []

    if move_existing and current != target and os.path.exists(current) and target_available:
        for name in os.listdir(current):
            src = os.path.join(current, name)
            dst = os.path.join(target, name)
            if os.path.exists(dst):
                skipped += 1
                failures.append(f"Skip existing: {name}")
                continue
            try:
                shutil.move(src, dst)
                moved += 1
            except Exception as exc:
                skipped += 1
                failures.append(f"{name}: {exc}")

    if move_existing and current != target and not target_available:
        failures.append("Target currently unavailable; skipped moving existing files.")

    set_result = set_songs_dir(target, persist=True, allow_unavailable=allow_unavailable)

    return {
        "old_path": current,
        "new_path": target,
        "moved": moved,
        "skipped": skipped,
        "move_existing": bool(move_existing),
        "failures": failures[:20],
        "available": bool(set_result.get("available", False)),
        "warning": str(set_result.get("warning") or target_warning or ""),
    }


def get_library_availability(path=None):
    candidate = os.path.abspath(path or SONGS_DIR)
    try:
        if not os.path.isdir(candidate):
            return False, "Library path is not accessible"
        with os.scandir(candidate) as iterator:
            for _ in iterator:
                break
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _load_backup_blob():
    if not os.path.exists(SONG_METADATA_BACKUP_PATH):
        return {"libraries": {}}
    try:
        with open(SONG_METADATA_BACKUP_PATH, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if isinstance(payload, dict) and isinstance(payload.get("libraries"), dict):
            return payload
    except Exception:
        pass
    return {"libraries": {}}


def _save_backup_blob(blob):
    safe = blob if isinstance(blob, dict) else {"libraries": {}}
    if "libraries" not in safe or not isinstance(safe.get("libraries"), dict):
        safe = {"libraries": {}}
    with open(SONG_METADATA_BACKUP_PATH, "w", encoding="utf-8") as fh:
        json.dump(safe, fh, ensure_ascii=False, indent=2)


def load_song_metadata_backup():
    blob = _load_backup_blob()
    libraries = blob.get("libraries") if isinstance(blob, dict) else {}
    if not isinstance(libraries, dict):
        return {}
    key = os.path.abspath(SONGS_DIR)
    entry = libraries.get(key, {})
    return entry if isinstance(entry, dict) else {}


def save_song_metadata_backup(store):
    blob = _load_backup_blob()
    key = os.path.abspath(SONGS_DIR)
    libraries = blob.get("libraries") if isinstance(blob, dict) else {}
    if not isinstance(libraries, dict):
        libraries = {}
    libraries[key] = store if isinstance(store, dict) else {}
    blob["libraries"] = libraries
    _save_backup_blob(blob)

# ==========================================
# Flask + SocketIO 伺服器
# ==========================================
app = Flask(__name__, template_folder=TEMPLATES_DIR)
app.config['SECRET_KEY'] = 'ktv_secret'
socketio = SocketIO(app, cors_allowed_origins="*")
EXT_REQUEST_TTL_SECONDS = 300
extension_request_cache = {}
extension_request_lock = threading.Lock()
playback_state_lock = threading.Lock()


def _prune_extension_request_cache(now_ts=None):
    now_ts = now_ts if now_ts is not None else time.time()
    stale_keys = [
        key for key, row in extension_request_cache.items()
        if now_ts - float(row.get('ts', 0.0)) > EXT_REQUEST_TTL_SECONDS
    ]
    for key in stale_keys:
        extension_request_cache.pop(key, None)


def get_extension_request_result(request_id):
    if not request_id:
        return None
    with extension_request_lock:
        _prune_extension_request_cache()
        row = extension_request_cache.get(request_id)
        if not row:
            return None
        payload = row.get('payload', {})
        return payload if isinstance(payload, dict) else None


def save_extension_request_result(request_id, payload):
    if not request_id:
        return
    with extension_request_lock:
        _prune_extension_request_cache()
        extension_request_cache[request_id] = {
            'ts': time.time(),
            'payload': payload if isinstance(payload, dict) else {},
        }

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "127.0.0.1"

LOCAL_IP = get_local_ip()
PORT = 5000
APP_VERSION = "1.1.1"

def _push_gui_log(msg):
    text = str(msg or "").strip()
    if not text:
        return
    # In frozen EXE mode, print output is already intercepted by GUIWriter.
    if getattr(sys, 'frozen', False):
        return
    system_log_queue.put(text)


def broadcast_log(msg):
    text = str(msg or "").strip()
    if not text:
        return
    _push_gui_log(text)
    print(text)
    socketio.emit('admin_log', {'msg': text})


def broadcast_song_list():
    songs = list_song_files()
    socketio.emit('refresh_list')
    socketio.emit('update_list', songs)
    return songs


def _today_key():
    return time.strftime("%Y-%m-%d")


def load_playback_state():
    if not os.path.exists(PLAYBACK_STATE_PATH):
        return {"by_date": {}}
    try:
        with open(PLAYBACK_STATE_PATH, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if isinstance(payload, dict) and isinstance(payload.get("by_date"), dict):
            return payload
    except Exception:
        pass
    return {"by_date": {}}


def save_playback_state(state):
    payload = state if isinstance(state, dict) else {"by_date": {}}
    with open(PLAYBACK_STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def _prune_playback_state(by_date):
    if not isinstance(by_date, dict):
        return {}
    keys = sorted([k for k in by_date.keys() if isinstance(k, str)])
    if len(keys) <= PLAYBACK_HISTORY_DAYS:
        return by_date
    keep = set(keys[-PLAYBACK_HISTORY_DAYS:])
    return {k: v for k, v in by_date.items() if k in keep}


def get_today_played_map():
    state = load_playback_state()
    by_date = state.get("by_date") if isinstance(state, dict) else {}
    today_state = by_date.get(_today_key(), {}) if isinstance(by_date, dict) else {}
    played = today_state.get("played") if isinstance(today_state, dict) else {}
    return played if isinstance(played, dict) else {}


def mark_song_played(filename):
    safe_name = os.path.basename(str(filename or "").strip())
    if not safe_name:
        return
    now_text = time.strftime("%Y-%m-%d %H:%M:%S")
    today = _today_key()
    with playback_state_lock:
        state = load_playback_state()
        by_date = state.get("by_date") if isinstance(state, dict) else {}
        if not isinstance(by_date, dict):
            by_date = {}
        today_state = by_date.get(today, {})
        if not isinstance(today_state, dict):
            today_state = {}
        played = today_state.get("played")
        if not isinstance(played, dict):
            played = {}
        played[safe_name] = now_text
        today_state["played"] = played
        by_date[today] = today_state
        state["by_date"] = _prune_playback_state(by_date)
        save_playback_state(state)


def get_playback_snapshot():
    played = get_today_played_map()
    return {
        "today": _today_key(),
        "played_today": played,
        "played_count": len(played),
    }


def emit_play_video(filename, title=None):
    safe_name = os.path.basename(str(filename or "").strip())
    if not safe_name:
        return
    mark_song_played(safe_name)
    socketio.emit('play_video', {'filename': safe_name, 'title': title or safe_name})


def remove_song_from_queue(filename):
    removed_current = False
    while filename in playlist_queue:
        index = playlist_queue.index(filename)
        if index == 0:
            removed_current = True
        playlist_queue.pop(index)
    return removed_current


def delete_song_file(filename):
    filename = os.path.basename(str(filename).strip())
    if not filename or not filename.lower().endswith('.mp4'):
        return False, False, "Invalid filename"

    song_path = os.path.abspath(os.path.join(SONGS_DIR, filename))
    songs_root = os.path.abspath(SONGS_DIR)
    if not song_path.startswith(songs_root + os.sep) and song_path != songs_root:
        return False, False, "Invalid song path"

    if not os.path.exists(song_path):
        return False, False, f"Song not found: {filename}"

    os.remove(song_path)

    metadata = load_song_metadata_store()
    if filename in metadata:
        metadata.pop(filename, None)
        save_song_metadata_store(metadata)

    removed_current = remove_song_from_queue(filename)
    return True, removed_current, filename


def list_song_files(strict=False):
    global LAST_KNOWN_SONGS
    available, reason = get_library_availability()
    if not available:
        if strict:
            raise RuntimeError(f"Library unavailable: {reason}")
        if LAST_KNOWN_SONGS:
            return list(LAST_KNOWN_SONGS)
        backup = load_song_metadata_backup()
        return sorted([name for name in backup.keys() if str(name).lower().endswith('.mp4')])

    songs = sorted([f.name for f in os.scandir(SONGS_DIR) if f.is_file() and f.name.lower().endswith('.mp4')])
    LAST_KNOWN_SONGS = list(songs)
    return songs


def rescan_library_files(sync_metadata=True):
    available, reason = get_library_availability()
    if not available:
        return {
            "songs": len(list_song_files(strict=False)),
            "metadata_added": 0,
            "metadata_removed": 0,
            "queue_removed": 0,
            "removed_current": False,
            "library_available": False,
            "library_reason": reason,
            "dry_run_only": True,
        }

    songs = list_song_files(strict=True)
    song_set = set(songs)

    store = load_song_metadata_store()
    added = 0
    removed = 0

    if sync_metadata:
        for filename in songs:
            if filename not in store:
                entry = normalize_song_metadata_entry(filename, {})
                entry["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                store[filename] = entry
                added += 1

        stale_keys = [k for k in store.keys() if k not in song_set]
        for key in stale_keys:
            store.pop(key, None)
            removed += 1

        if added or removed:
            save_song_metadata_store(store)

    queue_before = list(playlist_queue)
    playlist_queue[:] = [name for name in playlist_queue if name in song_set]
    queue_removed = len(queue_before) - len(playlist_queue)
    removed_current = bool(queue_before and queue_before[0] not in song_set)

    if removed_current:
        if len(playlist_queue) > 0:
            emit_play_video(playlist_queue[0], playlist_queue[0])
        else:
            socketio.emit('stop_video')

    socketio.emit('update_queue', playlist_queue)
    broadcast_song_list()

    return {
        "songs": len(songs),
        "metadata_added": added,
        "metadata_removed": removed,
        "queue_removed": queue_removed,
        "removed_current": removed_current,
        "library_available": True,
        "library_reason": "",
        "dry_run_only": False,
    }


def load_song_metadata_store():
    if not os.path.exists(SONG_METADATA_PATH):
        return load_song_metadata_backup()
    try:
        with open(SONG_METADATA_PATH, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        store = payload if isinstance(payload, dict) else {}
        save_song_metadata_backup(store)
        return store
    except Exception:
        return load_song_metadata_backup()


def save_song_metadata_store(data):
    safe = data if isinstance(data, dict) else {}
    save_song_metadata_backup(safe)
    try:
        with open(SONG_METADATA_PATH, "w", encoding="utf-8") as fh:
            json.dump(safe, fh, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def infer_genre_from_text(title="", artist="", album=""):
    text = f"{title} {artist} {album}".lower()
    genre_rules = [
        ("k-pop|kpop|bts|blackpink|twice|ive|newjeans", "K-Pop"),
        ("metal|slayer|iron maiden|metallica", "Metal"),
        ("jazz|blues|sax|bossa", "Jazz/Blues"),
        ("edm|house|trance|dubstep|techno", "EDM"),
        ("hip hop|hip-hop|rap|drill", "Hip-Hop"),
        ("rock|queen|rhcp|red hot chili peppers|nirvana", "Rock"),
        ("acoustic|folk|ballad", "Acoustic/Folk"),
        ("classical|orchestra|symphony|sonata", "Classical"),
    ]
    for pattern, genre in genre_rules:
        if re.search(pattern, text):
            return genre
    return "Unknown"


def parse_filename_metadata(filename):
    raw = os.path.splitext(os.path.basename(filename))[0]
    parts = [p.strip() for p in raw.split(" - ") if p.strip()]
    if len(parts) >= 3:
        return {"title": " - ".join(parts[2:]), "artist": parts[0], "album": parts[1]}
    if len(parts) == 2:
        return {"title": parts[1], "artist": parts[0], "album": ""}
    return {"title": raw, "artist": "", "album": ""}


def is_metadata_completed(entry):
    # Completion rule (option 1): title + artist are enough.
    if not isinstance(entry, dict):
        return False
    return bool(str(entry.get("title") or "").strip() and str(entry.get("artist") or "").strip())


def normalize_song_metadata_entry(filename, existing=None):
    existing = existing if isinstance(existing, dict) else {}
    parsed = parse_filename_metadata(filename)
    safe_name = os.path.basename(str(filename or "").strip())

    added_ts_value = existing.get("added_ts", 0)
    try:
        added_ts = int(added_ts_value or 0)
    except Exception:
        added_ts = 0

    if added_ts <= 0 and safe_name:
        song_path = os.path.join(SONGS_DIR, safe_name)
        try:
            stat = os.stat(song_path)
            added_ts = int(getattr(stat, "st_ctime", 0) or getattr(stat, "st_mtime", 0) or 0)
        except Exception:
            added_ts = 0

    added_at = str(existing.get("added_at") or "").strip()
    if not added_at and added_ts > 0:
        try:
            added_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(added_ts))
        except Exception:
            added_at = ""

    title = str(existing.get("title") or parsed["title"] or "").strip()
    artist = str(existing.get("artist") or parsed["artist"] or "").strip()
    album = str(existing.get("album") or parsed["album"] or "").strip()
    genre = str(existing.get("genre") or "").strip()
    artwork_url = str(existing.get("artwork_url") or "").strip()
    artist_image_url = str(existing.get("artist_image_url") or "").strip()
    normalized = {
        "filename": filename,
        "title": title,
        "artist": artist,
        "album": album,
        "genre": genre,
        "artwork_url": artwork_url,
        "artist_image_url": artist_image_url,
        "added_at": added_at,
        "added_ts": added_ts,
        "updated_at": existing.get("updated_at") or "",
    }
    normalized["completed"] = is_metadata_completed(normalized)
    return normalized


def persist_song_metadata_terms(filename, title="", artist="", album=""):
    safe_name = os.path.basename(str(filename or "")).strip()
    if not safe_name:
        return None

    store = load_song_metadata_store()
    entry = normalize_song_metadata_entry(safe_name, store.get(safe_name, {}))

    incoming_title = str(title or "").strip()
    incoming_artist = str(artist or "").strip()
    incoming_album = str(album or "").strip()

    if incoming_title:
        entry["title"] = incoming_title
    if incoming_artist:
        entry["artist"] = incoming_artist
    if incoming_album:
        entry["album"] = incoming_album

    if not str(entry.get("genre") or "").strip():
        entry["genre"] = infer_genre_from_text(entry.get("title", ""), entry.get("artist", ""), entry.get("album", ""))

    entry["completed"] = is_metadata_completed(entry)
    entry["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    store[safe_name] = entry
    save_song_metadata_store(store)
    return entry


def update_song_metadata_entry(filename, title=None, artist=None, album=None):
    safe_name = os.path.basename(str(filename or "")).strip()
    if not safe_name:
        raise ValueError("Invalid filename")

    store = load_song_metadata_store()
    entry = normalize_song_metadata_entry(safe_name, store.get(safe_name, {}))

    if title is not None:
        entry["title"] = str(title).strip()
    if artist is not None:
        entry["artist"] = str(artist).strip()
    if album is not None:
        entry["album"] = str(album).strip()

    if not str(entry.get("genre") or "").strip():
        entry["genre"] = infer_genre_from_text(entry.get("title", ""), entry.get("artist", ""), entry.get("album", ""))

    entry["completed"] = is_metadata_completed(entry)
    entry["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    store[safe_name] = entry
    save_song_metadata_store(store)
    return entry


def rename_artist_metadata_batch(source_artist, target_artist):
    source = str(source_artist or "").strip()
    target = str(target_artist or "").strip()
    if not source:
        raise ValueError("Source artist is required")
    if not target:
        raise ValueError("Target artist is required")

    store = load_song_metadata_store()
    songs = list_song_files(strict=False)
    updated_files = []
    source_key = source.casefold()

    for filename in songs:
        entry = normalize_song_metadata_entry(filename, store.get(filename, {}))
        current_artist = str(entry.get("artist") or "").strip()
        if current_artist.casefold() != source_key:
            continue

        entry["artist"] = target
        if not str(entry.get("genre") or "").strip():
            entry["genre"] = infer_genre_from_text(entry.get("title", ""), entry.get("artist", ""), entry.get("album", ""))
        entry["completed"] = is_metadata_completed(entry)
        entry["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        store[filename] = entry
        updated_files.append(filename)

    if updated_files:
        save_song_metadata_store(store)

    return {
        "source_artist": source,
        "target_artist": target,
        "updated": len(updated_files),
        "songs": updated_files,
    }


def _artwork_to_large(url):
    if not url:
        return ""
    # Apple artwork URLs usually contain /100x100bb.jpg; upscale for gallery display.
    return re.sub(r"/\d+x\d+bb", "/600x600bb", url)


def fetch_artwork_metadata(title="", artist="", album=""):
    query_parts = [p.strip() for p in [artist, album, title] if p and str(p).strip()]
    if not query_parts:
        return {"artwork_url": "", "artist_image_url": ""}

    term = " ".join(query_parts)
    params = urlencode({"term": term, "entity": "song", "limit": 8})
    url = f"https://itunes.apple.com/search?{params}"

    try:
        with urllib.request.urlopen(url, timeout=6) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
    except Exception:
        return {"artwork_url": "", "artist_image_url": ""}

    results = payload.get("results") if isinstance(payload, dict) else []
    if not isinstance(results, list) or not results:
        return {"artwork_url": "", "artist_image_url": ""}

    artist_l = str(artist or "").lower()
    album_l = str(album or "").lower()
    title_l = str(title or "").lower()

    best = None
    best_score = -1
    for row in results:
        if not isinstance(row, dict):
            continue
        score = 0
        r_artist = str(row.get("artistName") or "").lower()
        r_album = str(row.get("collectionName") or "").lower()
        r_title = str(row.get("trackName") or "").lower()
        if artist_l and artist_l in r_artist:
            score += 5
        if album_l and album_l in r_album:
            score += 4
        if title_l and title_l in r_title:
            score += 6
        if score > best_score:
            best = row
            best_score = score

    best = best or results[0]
    raw_art = str(best.get("artworkUrl100") or best.get("artworkUrl60") or "").strip()
    artwork = _artwork_to_large(raw_art)
    return {"artwork_url": artwork, "artist_image_url": artwork}


def build_song_metadata_snapshot():
    songs = list_song_files()
    store = load_song_metadata_store()
    snapshot = {}
    for filename in songs:
        snapshot[filename] = normalize_song_metadata_entry(filename, store.get(filename, {}))
    return snapshot


def autofill_song_metadata():
    songs = list_song_files()
    store = load_song_metadata_store()
    processor = KTVProcessor(log_cb=broadcast_log)
    updated = 0
    completed_before = 0
    completed_after = 0

    for filename in songs:
        entry = normalize_song_metadata_entry(filename, store.get(filename, {}))
        original = dict(entry)

        terms = processor.extract_lyrics_search_terms(entry["title"], entry["artist"], entry["album"])
        if not entry["title"] and terms.get("title"):
            entry["title"] = terms["title"]
        if not entry["artist"] and terms.get("artist"):
            entry["artist"] = terms["artist"]
        if not entry["album"] and terms.get("album"):
            entry["album"] = terms["album"]

        if not entry["artist"] or not entry["album"]:
            try:
                candidates = processor.search_lyrics_candidates(entry["title"], entry["artist"], entry["album"])
                best = processor.rank_lyrics_candidates(entry["artist"], entry["title"], candidates) if candidates else None
                if best:
                    if not entry["artist"]:
                        entry["artist"] = processor.normalize_space(str(best.get("artistName") or ""))
                    if not entry["album"]:
                        entry["album"] = processor.normalize_space(str(best.get("albumName") or ""))
                    if entry["title"] in {"", os.path.splitext(filename)[0]}:
                        candidate_title = processor.normalize_space(str(best.get("trackName") or ""))
                        if candidate_title:
                            entry["title"] = candidate_title
            except Exception:
                pass

        if not entry["genre"]:
            entry["genre"] = infer_genre_from_text(entry["title"], entry["artist"], entry["album"])

        if not entry["artwork_url"] or not entry["artist_image_url"]:
            art = fetch_artwork_metadata(entry["title"], entry["artist"], entry["album"])
            if not entry["artwork_url"]:
                entry["artwork_url"] = art.get("artwork_url", "")
            if not entry["artist_image_url"]:
                entry["artist_image_url"] = art.get("artist_image_url", "")

        entry["completed"] = is_metadata_completed(entry)
        entry["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

        if original.get("completed"):
            completed_before += 1
        if entry["completed"]:
            completed_after += 1

        if entry != original:
            updated += 1
        store[filename] = entry

    stale_keys = [k for k in store.keys() if k not in songs]
    for key in stale_keys:
        store.pop(key, None)

    save_song_metadata_store(store)
    return {
        "songs": len(songs),
        "updated": updated,
        "completed_before": completed_before,
        "completed_after": completed_after,
        "metadata": store,
    }

# ------------------------------------------
# Flask 路由
# ------------------------------------------
@app.route('/player')
def page_player(): return render_template('player.html')

@app.route('/remote')
def page_remote(): return render_template('remote.html')

@app.route('/admin')
def page_admin(): return render_template('admin.html', local_ip=LOCAL_IP, local_port=PORT, app_version=APP_VERSION)

@app.route('/combo')  
def page_combo(): return render_template('combo.html')

@app.route('/')
def page_index(): return render_template('remote.html')

@app.route('/songs/<path:filename>')
def serve_song(filename):
    return send_from_directory(SONGS_DIR, filename)

@app.route('/lyrics/<path:filename>')
def serve_lyrics(filename):
    safe_name = os.path.basename(filename)
    if not safe_name.lower().endswith('.lrc'):
        safe_name = f"{safe_name}.lrc"
    lyric_path = os.path.abspath(os.path.join(SONGS_DIR, safe_name))
    songs_root = os.path.abspath(SONGS_DIR)
    if not lyric_path.startswith(songs_root + os.sep) and lyric_path != songs_root:
        return "", 404

    if not os.path.exists(lyric_path):
        song_base = os.path.splitext(safe_name)[0]
        song_path = os.path.join(SONGS_DIR, f"{song_base}.mp4")
        if os.path.exists(song_path):
            processor = KTVProcessor(log_cb=broadcast_log)
            generated = processor.save_lyrics_for_song(song_path, song_base, "", "")
            if generated and os.path.exists(generated):
                lyric_path = generated

    if not os.path.exists(lyric_path):
        return "", 404
    return send_from_directory(SONGS_DIR, os.path.basename(lyric_path), mimetype='text/plain; charset=utf-8')

@app.route('/api/lyrics/<path:filename>')
def api_song_lyrics(filename):
    safe_name = os.path.basename(filename)
    base_name = os.path.splitext(safe_name)[0]
    lyric_path = os.path.join(SONGS_DIR, f"{base_name}.lrc")
    if not os.path.exists(lyric_path):
        return json.dumps({"ok": False, "error": "No local lyrics found"})
    with open(lyric_path, "r", encoding="utf-8", errors="replace") as fh:
        return json.dumps({"ok": True, "filename": f"{base_name}.lrc", "content": fh.read()})

@app.route('/api/lyrics/match/<path:filename>', methods=['POST'])
def api_match_song_lyrics(filename):
    try:
        safe_name = os.path.basename(filename)
        payload = request.get_json(silent=True) or {}
        title = str(payload.get('title', '')).strip()
        artist = str(payload.get('artist', '')).strip()
        album = str(payload.get('album', '')).strip()

        saved_entry = persist_song_metadata_terms(safe_name, title, artist, album) or {}
        search_title = title or str(saved_entry.get('title') or '')
        search_artist = artist or str(saved_entry.get('artist') or '')
        search_album = album or str(saved_entry.get('album') or '')

        processor = KTVProcessor(log_cb=broadcast_log)
        matches = processor.search_lyrics_candidates(search_title, search_artist, search_album)
        return json.dumps({"ok": True, "matches": matches, "terms": {"title": search_title, "artist": search_artist, "album": search_album}})
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)}), 500

@app.route('/api/lyrics/terms/<path:filename>')
def api_lyrics_terms(filename):
    try:
        safe_name = os.path.basename(filename)
        store = load_song_metadata_store()
        entry = normalize_song_metadata_entry(safe_name, store.get(safe_name, {}))

        processor = KTVProcessor(log_cb=broadcast_log)
        terms = processor.extract_lyrics_search_terms(entry.get('title', ''), entry.get('artist', ''), entry.get('album', ''))
        return json.dumps({"ok": True, "terms": terms})
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)}), 500

@app.route('/api/lyrics/save/<path:filename>', methods=['POST'])
def api_save_lyrics(filename):
    try:
        payload = request.get_json(silent=True) or {}
        safe_name = os.path.basename(filename)
        base_name = os.path.splitext(safe_name)[0]
        if not base_name:
            return json.dumps({"ok": False, "error": "Invalid filename"}), 400

        candidate = payload.get('candidate') if isinstance(payload.get('candidate'), dict) else {}
        lyrics_text = str(payload.get('lyrics') or "").strip()
        if not lyrics_text:
            lyrics_text = str(candidate.get('syncedLyrics') or candidate.get('plainLyrics') or "").strip()
        if not lyrics_text:
            return json.dumps({"ok": False, "error": "No lyrics content to save"}), 400

        processor = KTVProcessor(log_cb=broadcast_log)
        lrc_data = processor.generate_lrc_from_lyrics(lyrics_text)
        if not lrc_data:
            return json.dumps({"ok": False, "error": "Could not generate valid .lrc content"}), 400

        lyric_path = os.path.join(SONGS_DIR, f"{base_name}.lrc")
        with open(lyric_path, "w", encoding="utf-8") as fh:
            fh.write(lrc_data)

        persist_song_metadata_terms(
            safe_name,
            str(payload.get('title') or '').strip(),
            str(payload.get('artist') or '').strip(),
            str(payload.get('album') or '').strip(),
        )

        return json.dumps({"ok": True, "saved": f"{base_name}.lrc"})
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)}), 500

@app.route('/api/lyrics/delete/<path:filename>', methods=['POST'])
def api_delete_lyrics(filename):
    base_name = os.path.splitext(os.path.basename(filename))[0]
    lyric_path = os.path.join(SONGS_DIR, f"{base_name}.lrc")
    if not os.path.exists(lyric_path):
        return json.dumps({"ok": False, "error": "Lyrics file not found"})
    os.remove(lyric_path)
    return json.dumps({"ok": True, "deleted": f"{base_name}.lrc"})

@app.route('/api/list')
def get_song_list():
    songs = list_song_files(strict=False)
    return json.dumps(songs) 


@app.route('/api/metadata')
def api_song_metadata():
    snapshot = build_song_metadata_snapshot()
    playback = get_playback_snapshot()
    return json.dumps({"ok": True, "metadata": snapshot, "playback": playback})


@app.route('/api/metadata/<path:filename>', methods=['POST'])
def api_song_metadata_update(filename):
    try:
        safe_name = os.path.basename(str(filename or "").strip())
        if safe_name == 'artist-batch':
            return api_song_metadata_artist_batch()
        if not safe_name.lower().endswith('.mp4'):
            return json.dumps({"ok": False, "error": "Only .mp4 songs can be edited"}), 400

        payload = request.get_json(silent=True) or {}
        if 'title' not in payload and 'artist' not in payload and 'album' not in payload:
            return json.dumps({"ok": False, "error": "No editable fields supplied"}), 400

        title = payload.get('title', None)
        artist = payload.get('artist', None)
        album = payload.get('album', None)

        if title is not None and not str(title).strip():
            return json.dumps({"ok": False, "error": "Song title cannot be empty"}), 400

        updated = update_song_metadata_entry(safe_name, title=title, artist=artist, album=album)
        broadcast_log(f"✍️ Metadata updated: {safe_name} -> {updated.get('title', '')} / {updated.get('artist', '')}")
        socketio.emit('refresh_list')
        return json.dumps({"ok": True, "filename": safe_name, "metadata": updated})
    except ValueError as exc:
        return json.dumps({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)}), 500


@app.route('/api/metadata/artist-batch', methods=['POST'])
def api_song_metadata_artist_batch():
    try:
        payload = request.get_json(silent=True) or {}
        source_artist = str(payload.get('source_artist') or '').strip()
        target_artist = str(payload.get('target_artist') or '').strip()
        if not source_artist:
            return json.dumps({"ok": False, "error": "source_artist is required"}), 400
        if not target_artist:
            return json.dumps({"ok": False, "error": "target_artist is required"}), 400

        result = rename_artist_metadata_batch(source_artist, target_artist)
        if result.get("updated", 0) > 0:
            broadcast_log(
                f"✍️ Artist renamed: {result.get('source_artist', '')} -> "
                f"{result.get('target_artist', '')} ({result.get('updated', 0)} songs)"
            )
            socketio.emit('refresh_list')
        return json.dumps({"ok": True, **result})
    except ValueError as exc:
        return json.dumps({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)}), 500


@app.route('/api/metadata/autofill', methods=['POST'])
def api_metadata_autofill():
    try:
        result = autofill_song_metadata()
        return json.dumps({"ok": True, **result})
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)}), 500


@app.route('/api/library/path')
def api_library_path_get():
    try:
        available, reason = get_library_availability()
        songs = list_song_files(strict=False)
        return json.dumps({
            "ok": True,
            "path": SONGS_DIR,
            "song_count": len(songs),
            "available": available,
            "warning": reason if not available else "",
        })
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)}), 500


@app.route('/api/library/pick-path', methods=['POST'])
def api_library_pick_path():
    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        selected = filedialog.askdirectory(initialdir=SONGS_DIR or BASE_DIR, title='Select Song Library Folder')
        if not selected:
            return json.dumps({"ok": False, "cancelled": True})
        return json.dumps({"ok": True, "path": normalize_library_path(selected)})
    except Exception as exc:
        return json.dumps({"ok": False, "error": f"Folder picker unavailable: {exc}"}), 500
    finally:
        try:
            if root is not None:
                root.destroy()
        except Exception:
            pass


@app.route('/api/library/path', methods=['POST'])
def api_library_path_set():
    try:
        payload = request.get_json(silent=True) or {}
        new_path = str(payload.get('path') or '').strip()
        move_existing = bool(payload.get('move_existing', True))
        allow_unavailable = bool(payload.get('allow_unavailable', True))
        if not new_path:
            return json.dumps({"ok": False, "error": "Path is required"}), 400

        result = migrate_song_library(new_path, move_existing=move_existing, allow_unavailable=allow_unavailable)
        broadcast_log(f"📁 Song library path updated: {result['new_path']} (moved: {result['moved']}, skipped: {result['skipped']})")
        broadcast_song_list()
        return json.dumps({"ok": True, **result})
    except ValueError as exc:
        return json.dumps({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)}), 500


@app.route('/api/library/rescan', methods=['POST'])
def api_library_rescan():
    try:
        payload = request.get_json(silent=True) or {}
        sync_metadata = bool(payload.get('sync_metadata', True))
        result = rescan_library_files(sync_metadata=sync_metadata)
        if not result.get('library_available', True):
            broadcast_log(
                f"⚠️ Library unavailable, skipped destructive sync: {result.get('library_reason', 'unknown reason')}"
            )
            return json.dumps({"ok": True, **result})

        broadcast_log(
            f"🔄 Library rescan finished: songs={result['songs']}, "
            f"metadata+={result['metadata_added']}, metadata-={result['metadata_removed']}, "
            f"queue_removed={result['queue_removed']}"
        )
        return json.dumps({"ok": True, **result})
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)}), 500


@app.route('/api/settings/demucs')
def api_demucs_settings_get():
    try:
        configured_model = get_demucs_model_setting()
        env_override = str(os.environ.get("KTV_DEMUCS_MODEL", "")).strip().lower()
        effective_model = env_override if env_override in {"htdemucs", "mdx_q"} else configured_model
        return json.dumps({
            "ok": True,
            "model": configured_model,
            "effective_model": effective_model,
            "env_override": bool(env_override),
        })
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)}), 500


@app.route('/api/settings/demucs', methods=['POST'])
def api_demucs_settings_set():
    try:
        payload = request.get_json(silent=True) or {}
        model_name = str(payload.get('model') or '').strip().lower()
        if model_name not in VALID_DEMUCS_MODELS:
            return json.dumps({"ok": False, "error": "Invalid model option"}), 400
        saved = set_demucs_model_setting(model_name)
        broadcast_log(f"⚙️ Demucs model preference updated: {saved}")
        return json.dumps({"ok": True, "model": saved})
    except ValueError as exc:
        return json.dumps({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)}), 500


@app.route('/api/extension/add-song', methods=['POST', 'OPTIONS'])
def api_extension_add_song():
    if request.method == 'OPTIONS':
        resp = app.response_class(response='')
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        resp.headers['Access-Control-Allow-Private-Network'] = 'true'
        return resp

    try:
        payload = request.get_json(silent=True) or {}
        url = str(payload.get('url') or '').strip()
        title = str(payload.get('title') or '').strip()
        request_id = str(payload.get('request_id') or '').strip()

        previous = get_extension_request_result(request_id)
        if previous is not None:
            resp = app.response_class(
                response=json.dumps({**previous, 'duplicate': True}),
                mimetype='application/json'
            )
            resp.headers['Access-Control-Allow-Origin'] = '*'
            resp.headers['Access-Control-Allow-Private-Network'] = 'true'
            return resp

        if not url:
            resp = app.response_class(
                response=json.dumps({'ok': False, 'error': 'url is required'}),
                status=400,
                mimetype='application/json'
            )
            resp.headers['Access-Control-Allow-Origin'] = '*'
            resp.headers['Access-Control-Allow-Private-Network'] = 'true'
            return resp

        with download_queue_lock:
            download_queue.append({'url': url, 'title': title})
            waiting_count = len(download_queue)

        display_name = title or 'auto-detected metadata'
        broadcast_log(f"🧩 Extension queued: {display_name} (waiting in queue: {waiting_count})")
        warning = ""
        try:
            try_start_next_download()
        except Exception as exc:
            # Queue append already succeeded; report warning but keep successful response.
            warning = str(exc)
            broadcast_log(f"⚠️ Queue starter warning: {warning}")

        result_payload = {'ok': True, 'queued': waiting_count, 'title': title, 'warning': warning, 'request_id': request_id}
        save_extension_request_result(request_id, result_payload)

        resp = app.response_class(
            response=json.dumps(result_payload),
            mimetype='application/json'
        )
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Private-Network'] = 'true'
        return resp
    except Exception as exc:
        resp = app.response_class(
            response=json.dumps({'ok': False, 'error': str(exc)}),
            status=500,
            mimetype='application/json'
        )
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Private-Network'] = 'true'
        return resp


@app.route('/api/delete/<path:filename>', methods=['POST'])
def delete_song_api(filename):
    try:
        success, removed_current, result = delete_song_file(filename)
        if not success:
            return json.dumps({'ok': False, 'error': result}), 404
        return json.dumps({'ok': True, 'removed_current': removed_current, 'result': result})
    except Exception as exc:
        return json.dumps({'ok': False, 'error': str(exc)}), 500


# ------------------------------------------
# SocketIO 事件處理 & 待播清單
# ------------------------------------------
playlist_queue = []
current_playback_state = {'filename': '', 'seconds': 0.0}

@socketio.on('song_status')
def handle_song_status(data):
    filename = str((data or {}).get('filename') or '').strip()
    seconds = float((data or {}).get('seconds', 0) or 0)
    if not filename:
        return
    current_playback_state['filename'] = filename
    current_playback_state['seconds'] = max(0.0, seconds)
    socketio.emit('song_status', {'filename': filename, 'seconds': current_playback_state['seconds']})

@socketio.on('add_to_queue')
def handle_add_queue(data):
    filename = data['filename']
    playlist_queue.append(filename)
    broadcast_log(f"🎶 Queued: {filename} (queue: {len(playlist_queue)})")
    
    # 廣播更新所有設備上的歌單畫面
    socketio.emit('update_queue', playlist_queue)
    
    # 如果清單裡面只有剛點的這首歌，代表目前沒有歌在播，立刻開始播放
    if len(playlist_queue) == 1:
        current_playback_state['filename'] = filename
        current_playback_state['seconds'] = 0.0
        emit_play_video(filename, filename)
        socketio.emit('song_status', {'filename': filename, 'seconds': 0.0})


@socketio.on('remove_from_queue')
def handle_remove_from_queue(data):
    payload = data or {}
    removed_index = None

    try:
        index_value = payload.get('index', None)
        if index_value is not None:
            candidate = int(index_value)
            if 0 <= candidate < len(playlist_queue):
                removed_index = candidate
    except Exception:
        removed_index = None

    if removed_index is None:
        filename = str(payload.get('filename') or '').strip()
        if filename in playlist_queue:
            removed_index = playlist_queue.index(filename)

    if removed_index is None:
        return

    removed_song = playlist_queue[removed_index]
    playlist_queue.pop(removed_index)
    broadcast_log(f"🧹 Removed from queue: {removed_song} (queue: {len(playlist_queue)})")
    socketio.emit('update_queue', playlist_queue)

    if removed_index == 0:
        if len(playlist_queue) > 0:
            next_song = playlist_queue[0]
            current_playback_state['filename'] = next_song
            current_playback_state['seconds'] = 0.0
            broadcast_log(f"▶️ Playing next: {next_song}")
            emit_play_video(next_song, next_song)
            socketio.emit('song_status', {'filename': next_song, 'seconds': 0.0})
        else:
            current_playback_state['filename'] = ''
            current_playback_state['seconds'] = 0.0
            broadcast_log("⏹️ Queue empty. Playback stopped.")
            socketio.emit('stop_video')

@socketio.on('request_play')
def handle_request_play(data):
    # 舊版遙控器送出的事件名稱，沿用同一套排隊/播放邏輯
    handle_add_queue(data)


@socketio.on('delete_song')
def handle_delete_song(data):
    try:
        success, removed_current, result = delete_song_file(data.get('filename', ''))
        if not success:
            broadcast_log(f"⚠️ {result}")
            return

        broadcast_log(f"🗑️ Deleted song: {result}")
        broadcast_song_list()
        socketio.emit('update_queue', playlist_queue)

        if removed_current:
            if len(playlist_queue) > 0:
                emit_play_video(playlist_queue[0], playlist_queue[0])
            else:
                socketio.emit('stop_video')
    except Exception as exc:
        broadcast_log(f"❌ Failed to delete song: {exc}")

@socketio.on('song_ended')
def handle_song_ended():
    if len(playlist_queue) > 0:
        # 移除剛剛唱完的那首歌
        finished_song = playlist_queue.pop(0)
        broadcast_log(f"✅ Finished: {finished_song}")
        socketio.emit('update_queue', playlist_queue)
        
        # 檢查是否還有下一首
        if len(playlist_queue) > 0:
            next_song = playlist_queue[0]
            broadcast_log(f"▶️ Playing next: {next_song}")
            emit_play_video(next_song, next_song)
        else:
            # 沒歌了，停止畫面並回到待機狀態
            broadcast_log("⏹️ Queue empty. Playback stopped.")
            socketio.emit('stop_video')

@socketio.on('control')
def handle_control(action):
    normalized = str(action or '').strip().lower()
    broadcast_log(f"🎛️ Control: {normalized or 'unknown'}")
    if normalized in {'cut', 'stop', 'skip', 'next'}:
        # 多端按鈕命名可能不同，這些都視為「切到下一首」
        handle_song_ended()
    else:
        # 其他指令 (例如 pause) 照常發送
        socketio.emit('command', normalized)

# ------------------------------------------
# (以下原本的音效與下載事件保留不動)
@socketio.on('control_effect')
def handle_effect(data):
    socketio.emit('apply_effect', data)

@socketio.on('change_track')
def handle_track(mode):
    socketio.emit('set_audio', mode)

@socketio.on('seek_to')
def handle_seek_to(data):
    try:
        seconds = float((data or {}).get('seconds', 0))
    except Exception:
        return

    if seconds < 0:
        seconds = 0
    socketio.emit('seek_to', {'seconds': seconds})

is_processing = False
download_queue = []
download_queue_lock = threading.Lock()
DEMUCS_HEARTBEAT_SECONDS = 20
DEMUCS_TIMEOUT_SECONDS = 30 * 60


def emit_download_status():
    with download_queue_lock:
        processing = is_processing
        queued = len(download_queue)

    status = 'busy' if processing or queued > 0 else 'idle'
    socketio.emit('task_status', {'status': status, 'queued': queued, 'active': processing})


def try_start_next_download():
    global is_processing
    next_job = None

    with download_queue_lock:
        if (not is_processing) and download_queue:
            next_job = download_queue.pop(0)
            is_processing = True

    if next_job is not None:
        threading.Thread(target=run_download_job, args=(next_job,), daemon=True).start()

    emit_download_status()


def run_download_job(job):
    global is_processing

    url = job.get('url', '')
    title = job.get('title', '')

    with download_queue_lock:
        pending_after_start = len(download_queue)

    broadcast_log("=== Starting new job ===")
    broadcast_log(f"Queue status: {pending_after_start} job(s) waiting behind current task")

    try:
        processor = KTVProcessor(log_cb=broadcast_log)
        final_filename = processor.process_song(url, title)

        if final_filename:
            socketio.emit('refresh_list')
    except Exception as exc:
        broadcast_log(f"❌ Process failed: {exc}")
    finally:
        with download_queue_lock:
            is_processing = False

        try_start_next_download()


# ==========================================
# Demucs 獨立進程處理函式
# ==========================================
def _run_demucs_process(input_path, output_dir, log_path):
    """
    這個函式會在一個完全獨立的 Python 進程中執行。
    結束時作業系統會強制清空此進程佔用的 PyTorch 記憶體。
    """
    runtime_python = get_runtime_python_executable()
    model_marker_path = os.path.join(output_dir, "demucs_model.txt")
    device_marker_path = os.path.join(output_dir, "demucs_device.txt")
    configured_model_env = str(os.environ.get("KTV_DEMUCS_MODEL", "")).strip().lower()
    configured_model_cfg = get_demucs_model_setting()
    if configured_model_env in {"htdemucs", "mdx_q"}:
        configured_model = configured_model_env
    elif configured_model_cfg in {"htdemucs", "mdx_q"}:
        configured_model = configured_model_cfg
    else:
        configured_model = ""
    configured_device = str(os.environ.get("KTV_DEMUCS_DEVICE", "")).strip()
    device_candidates = [configured_device] if configured_device else ["cuda", "cpu"]

    failure_logs = []
    last_error = None

    for device_name in device_candidates:
        if configured_model:
            model_candidates = [configured_model]
        else:
            model_candidates = ["htdemucs", "mdx_q"] if device_name == "cuda" else ["mdx_q", "htdemucs"]

        for model_name in model_candidates:
            cmd = [
                runtime_python,
                "-m",
                "demucs",
                "--two-stems",
                "vocals",
                "-n",
                model_name,
                "--device",
                device_name,
                "-o",
                output_dir,
                input_path,
            ]

            try:
                result = subprocess.run(cmd, check=True, capture_output=True, text=True)
                with open(log_path, "w", encoding="utf-8") as log_file:
                    log_file.write(f"[Demucs device] {device_name}\n")
                    log_file.write(f"[Demucs model] {model_name}\n")
                    if result.stdout:
                        log_file.write(result.stdout)
                    if result.stderr:
                        if result.stdout:
                            log_file.write("\n")
                        log_file.write(result.stderr)
                with open(model_marker_path, "w", encoding="utf-8") as model_file:
                    model_file.write(model_name)
                with open(device_marker_path, "w", encoding="utf-8") as device_file:
                    device_file.write(device_name)
                return
            except subprocess.CalledProcessError as exc:
                last_error = exc
                out = "\n".join(part for part in [exc.stdout, exc.stderr] if part).strip()
                failure_logs.append(f"[Demucs failed] device={device_name}, model={model_name}\n{out}")

    with open(log_path, "w", encoding="utf-8") as log_file:
        if failure_logs:
            log_file.write("\n\n".join(failure_logs))

    if last_error is not None:
        raise last_error
    raise RuntimeError("Demucs separation failed.")




@socketio.on('start_download')
def handle_start_download(data):
    url = str((data or {}).get('url', '')).strip()
    title = str((data or {}).get('title', '')).strip()

    if not url:
        broadcast_log("⚠️ Upload skipped: URL is required.")
        return

    with download_queue_lock:
        download_queue.append({'url': url, 'title': title})
        waiting_count = len(download_queue)

    display_name = title or "auto-detected metadata"
    broadcast_log(f"📥 Queued: {display_name} (waiting in queue: {waiting_count})")
    try_start_next_download()

@socketio.on('update_ytdlp')
def handle_update_ytdlp():
    def run_update():
        broadcast_log("Updating yt-dlp core...")
        try:
            cmd = get_ytdlp_command() + ["-U"]
            result = subprocess.run(cmd, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            broadcast_log(result.stdout)
            if result.stderr: broadcast_log(result.stderr)
            broadcast_log("✅ yt-dlp update finished.")
        except Exception as e:
            broadcast_log(f"❌ Update failed: {str(e)}")

    threading.Thread(target=run_update, daemon=True).start()

def run_server_thread():
    try:
        print("🚀 Starting Flask server...")
        broadcast_log(f"🌐 Server online: http://{LOCAL_IP}:{PORT}")
        broadcast_log(f"📚 Song library: {SONGS_DIR}")
        
        # 【關鍵防護】強制關閉 Flask 雞婆的啟動橫幅 (Banner) 與日誌，從根本拔除報錯源頭
        import logging
        from flask import cli
        cli.show_server_banner = lambda *args, **kwargs: None  # 暴力閹割橫幅印出功能
        logging.getLogger('werkzeug').setLevel(logging.ERROR)  # 只允許印出重大錯誤
        
        socketio.run(app, host='0.0.0.0', port=PORT, debug=False, allow_unsafe_werkzeug=True)
    except Exception as e:
        import traceback
        print(f"❌ Server start failed: {e}")
        print(traceback.format_exc())

# ==========================================
# 核心處理類別
# ==========================================
class KTVProcessor:
    def __init__(self, log_cb):
        self.log = log_cb

    def sanitize_filename(self, name):
        cleaned = "".join([c for c in (name or "") if c not in r'\\/:*?\"<>|'])
        cleaned = cleaned.replace("\r", " ").replace("\n", " ")
        cleaned = " ".join(cleaned.split())
        return cleaned.strip(" .-")

    def normalize_space(self, value):
        return " ".join((value or "").split())

    def tokenize_for_match(self, value):
        words = re.findall(r"[a-z0-9]+", self.normalize_space(str(value or "")).lower())
        stop = {"the", "a", "an", "and", "of", "for", "to", "in", "on", "feat", "ft", "featuring"}
        return [w for w in words if w not in stop]

    def clean_lyrics_metadata_text(self, value, is_artist=False):
        text = self.normalize_space(value or "")
        if not text:
            return ""

        # Remove common auto-generated numeric suffixes, e.g. "_1787176680".
        text = re.sub(r"[_\-]\d{6,}$", "", text).strip()

        noise_words = (
            r"official",
            r"music\s+video",
            r"video",
            r"audio",
            r"lyric(?:s)?",
            r"visualizer",
            r"mv",
            r"hd",
            r"4k",
            r"hq",
            r"remaster(?:ed)?(?:\s*\d{4})?",
            r"live",
        )
        noise_pattern = "|".join(noise_words)

        def strip_noisy_brackets(match):
            inner = self.normalize_space(match.group(1)).lower()
            return " " if re.search(rf"\b(?:{noise_pattern})\b", inner) else match.group(0)

        # Keep meaningful brackets like "[Hey Oh]", but remove noisy ones like "[Official Video]".
        text = re.sub(r"\s*[\[(\{]([^\]\)\}]{1,120})[\]\)\}]", strip_noisy_brackets, text)

        # Remove trailing promo tags outside brackets.
        text = re.sub(rf"\s*[-|:]+\s*(?:{noise_pattern})\b.*$", "", text, flags=re.I)

        # Trim featured artist suffixes for cleaner matching.
        text = re.sub(r"\s*(?:ft\.?|feat\.?|featuring)\s+.+$", "", text, flags=re.I)

        if is_artist:
            # Remove common channel suffixes from uploader names.
            text = re.sub(r"\s*-\s*topic$", "", text, flags=re.I)
            text = re.sub(r"\s*official(?:\s+channel)?$", "", text, flags=re.I)
            text = re.sub(r"\s*vevo$", "", text, flags=re.I)

        return self.normalize_space(text).strip(" -_|")

    def extract_title_parts(self, raw_title):
        title = self.normalize_space(raw_title or "")
        title = re.sub(r"^(?:official\s+)?(?:music\s+video|audio)\s*[:\-]?\s*", "", title, flags=re.I)
        title = re.sub(r"\s*\((?:official\s+)?(?:music\s+video|audio|lyric\s+video)\)\s*$", "", title, flags=re.I)
        if not title:
            return "", "", ""

        if " - " in title:
            parts = [p.strip() for p in title.split(" - ") if p.strip()]
            if len(parts) >= 3:
                artist = parts[0]
                album = parts[1]
                song = " - ".join(parts[2:])
                return artist, album, song
            if len(parts) == 2:
                left, right = parts
                common_title_words = {
                    "hello", "love", "forever", "goodbye", "sorry", "alone", "summer", "winter",
                    "beautiful", "fire", "dream", "song", "track", "music", "memory", "light",
                    "time", "angel", "night", "day", "heart", "party", "star", "rain", "shine",
                    "happier", "together", "dangerous", "broken", "stuck", "perfect", "change",
                }
                left_words = set(left.lower().split())
                right_words = set(right.lower().split())
                left_is_title = bool(left_words & common_title_words) or left.lower() in common_title_words
                if left_is_title and not (right_words & common_title_words):
                    return right, "", left
                return left, "", right

        return "", "", title

    def infer_artist_album_title(self, raw_title):
        artist, album, song = self.extract_title_parts(raw_title)
        return artist, album, song

    def build_song_filename(self, title, artist="", album=""):
        if title:
            inferred_artist, inferred_album, inferred_song = self.infer_artist_album_title(title)
            if not artist:
                artist = inferred_artist
            if not album:
                album = inferred_album
            title = inferred_song or title

        title = self.normalize_space(title) or "Unknown Song"
        artist = self.normalize_space(artist)
        album = self.normalize_space(album)

        if artist and album and title:
            candidate = f"{artist} - {album} - {title}"
        elif artist and title:
            candidate = f"{artist} - {title}"
        elif album and title:
            candidate = f"{album} - {title}"
        else:
            candidate = title

        cleaned = self.sanitize_filename(candidate)
        if not cleaned:
            cleaned = "Unknown Song"
        return cleaned.strip(" -")

    def extract_lyrics_search_terms(self, title="", artist="", album=""):
        title = self.clean_lyrics_metadata_text(title or "")
        artist = self.clean_lyrics_metadata_text(artist or "", is_artist=True)
        album = self.clean_lyrics_metadata_text(album or "")

        inferred_artist, inferred_album, inferred_song = self.infer_artist_album_title(title)
        if not artist and inferred_artist:
            artist = inferred_artist
        if not album and inferred_album:
            album = inferred_album

        if not title and artist and album:
            title = album

        track_name = self.clean_lyrics_metadata_text(inferred_song or title or "Unknown Song")
        title = track_name
        artist = self.clean_lyrics_metadata_text(artist or inferred_artist or "", is_artist=True)
        album = self.clean_lyrics_metadata_text(album or inferred_album or "")

        return {
            "title": title,
            "artist": artist,
            "album": album,
            "track": track_name,
        }

    def rank_lyrics_candidates(self, artist, title, candidates):
        def match_score(candidate):
            artist_key = self.normalize_space(artist or "").lower()
            title_key = self.normalize_space(title or "").lower()
            artist_name = self.normalize_space(str(candidate.get("artistName") or "")).lower()
            track_name = self.normalize_space(str(candidate.get("trackName") or "")).lower()
            score = 0
            if artist_key and artist_name:
                if artist_key == artist_name:
                    score += 30
                elif artist_key in artist_name or artist_name in artist_key:
                    score += 18
            if title_key and track_name:
                if title_key == track_name:
                    score += 50
                elif title_key in track_name or track_name in title_key:
                    score += 30
            if artist_key and title_key and artist_key in track_name and title_key in artist_name:
                score += 10
            return score

        artist_key = self.normalize_space(artist or "").lower()
        title_key = self.normalize_space(title or "").lower()
        ranked = []
        for candidate in candidates:
            artist_name = self.normalize_space(str(candidate.get("artistName") or "")).lower()
            track_name = self.normalize_space(str(candidate.get("trackName") or "")).lower()
            score = match_score(candidate)
            ranked.append((score, candidate))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return ranked[0][1] if ranked else None

    def search_lyrics_candidates(self, title="", artist="", album=""):
        terms = self.extract_lyrics_search_terms(title, artist, album)
        artist_term = terms["artist"]
        track_term = terms["track"]
        album_term = terms["album"]

        candidates = []
        query_variants = []
        if artist_term and track_term:
            query_variants.extend([
                (artist_term, track_term),
                (track_term, artist_term),
                (artist_term, f"{track_term} {album_term}") if album_term else (artist_term, track_term),
            ])
        if track_term:
            query_variants.append(("", track_term))
        if artist_term:
            query_variants.append((artist_term, ""))

        q_variants = []
        if artist_term and track_term:
            q_variants.append(f"{artist_term} {track_term}")
            q_variants.append(f"{track_term} {artist_term}")
        if track_term:
            q_variants.append(track_term)
        if artist_term and album_term:
            q_variants.append(f"{artist_term} {album_term} {track_term}".strip())

        request_param_variants = []
        for artist_value, track_value in query_variants:
            params = {}
            if track_value:
                params["track"] = track_value
            if artist_value:
                params["artist"] = artist_value
            if album_term and not artist_value:
                params["album"] = album_term
            if params:
                request_param_variants.append(params)

        for q_value in q_variants:
            q_value = self.normalize_space(q_value)
            if q_value:
                request_param_variants.append({"q": q_value})

        seen_queries = set()
        for params in request_param_variants:
            if not params:
                continue
            q_key = tuple(sorted((k, self.normalize_space(str(v)).lower()) for k, v in params.items()))
            if q_key in seen_queries:
                continue
            seen_queries.add(q_key)
            try:
                request_url = "https://lrclib.net/api/search?" + urlencode(params)
                req = urllib.request.Request(request_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=12) as response:
                    payload = json.loads(response.read().decode("utf-8", errors="replace"))
                    if isinstance(payload, list):
                        candidates.extend(payload)
            except Exception:
                pass

        unique = []
        seen = set()
        for candidate in candidates:
            key = (str(candidate.get("artistName") or "").lower(), str(candidate.get("trackName") or "").lower(), str(candidate.get("plainLyrics") or candidate.get("syncedLyrics") or "").lower())
            if key not in seen:
                unique.append(candidate)
                seen.add(key)

        if not unique:
            return []

        artist_key = self.normalize_space(artist_term).lower()
        track_key = self.normalize_space(track_term).lower()
        artist_tokens = self.tokenize_for_match(artist_term)
        track_tokens = self.tokenize_for_match(track_term)

        def relevance_score(item):
            artist_name = self.normalize_space(str(item.get("artistName") or "")).lower()
            track_name = self.normalize_space(str(item.get("trackName") or "")).lower()
            score = 0

            if artist_key and artist_name:
                if artist_key == artist_name:
                    score += 45
                elif artist_key in artist_name or artist_name in artist_key:
                    score += 25

            if track_key and track_name:
                if track_key == track_name:
                    score += 60
                elif track_key in track_name or track_name in track_key:
                    score += 35

            if artist_tokens:
                artist_overlap = sum(1 for token in artist_tokens if token in artist_name)
                score += int((artist_overlap / max(len(artist_tokens), 1)) * 30)
                if artist_key and artist_overlap == 0:
                    score -= 20

            if track_tokens:
                title_overlap = sum(1 for token in track_tokens if token in track_name)
                score += int((title_overlap / max(len(track_tokens), 1)) * 35)
                if track_key and title_overlap == 0:
                    score -= 30

            return score

        ranked = sorted(unique, key=relevance_score, reverse=True)
        strong = [item for item in ranked if relevance_score(item) > 0]
        return strong or ranked

    def fetch_lyrics_text(self, title="", artist="", album=""):
        terms = self.extract_lyrics_search_terms(title, artist, album)
        artist_term = terms["artist"]
        track_term = terms["track"]
        album_term = terms["album"]
        candidates = self.search_lyrics_candidates(track_term, artist_term, album_term)
        if not candidates:
            return None
        best_match = self.rank_lyrics_candidates(artist_term, track_term, candidates)
        if not best_match:
            return None
        lyrics_text = (best_match.get("syncedLyrics") or best_match.get("plainLyrics") or "").strip()
        return lyrics_text or None

    def generate_lrc_from_lyrics(self, lyrics_text):
        if lyrics_text is None:
            return ""
        normalized = str(lyrics_text).replace("\r\n", "\n").replace("\r", "\n")
        if re.search(r"\[\d{1,2}:\d{2}(?:\.\d{1,3})?\]", normalized):
            filtered = []
            for raw_line in normalized.split("\n"):
                stripped = raw_line.strip()
                if stripped and re.search(r"\[\d{1,2}:\d{2}(?:\.\d{1,3})?\]", stripped):
                    filtered.append(stripped)
            return ("\n".join(filtered) + "\n") if filtered else ""

        lines = []
        for raw_line in normalized.split("\n"):
            clean_line = raw_line.strip()
            if not clean_line:
                continue
            if re.match(r"^(?:\[.*\]|https?://|\(.*\))", clean_line):
                continue
            lines.append(clean_line)

        if not lines:
            return ""

        lrc_lines = []
        for index, line in enumerate(lines):
            seconds = max(0, index * 4)
            minutes, remaining = divmod(seconds, 60)
            lrc_lines.append(f"[{minutes:02d}:{remaining:02d}.000]{line}")
        return "\n".join(lrc_lines) + "\n"

    def save_lyrics_for_song(self, song_filename, title="", artist="", album="", force=False):
        if not song_filename:
            return None
        base_name = os.path.splitext(os.path.basename(song_filename))[0]
        lyric_output = os.path.join(SONGS_DIR, f"{base_name}.lrc")
        if os.path.exists(lyric_output) and not force:
            return lyric_output
        lyrics_text = self.fetch_lyrics_text(title, artist, album)
        if not lyrics_text:
            return None
        lrc_data = self.generate_lrc_from_lyrics(lyrics_text)
        if not lrc_data:
            return None
        with open(lyric_output, "w", encoding="utf-8") as lyric_file:
            lyric_file.write(lrc_data)
        return lyric_output

    def resolve_song_metadata(self, url, fallback_title=""):
        fallback_title = (fallback_title or "").strip()
        metadata = {"title": fallback_title, "artist": "", "album": ""}
        try:
            cmd = get_ytdlp_command() + [
                "--skip-download",
                "--no-warnings",
                "--dump-single-json",
                url,
            ]
            result = subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            )
            data = json.loads(result.stdout or "{}")
            title = self.normalize_space((data.get("title") or fallback_title or "").strip())
            artist = self.normalize_space((data.get("artist") or data.get("uploader") or "").strip())
            album = self.normalize_space((data.get("album") or "").strip())
            metadata["title"] = title
            metadata["artist"] = artist
            metadata["album"] = album

            inferred_artist, inferred_album, inferred_song = self.infer_artist_album_title(title)
            if not metadata["artist"] and inferred_artist:
                metadata["artist"] = inferred_artist
            if not metadata["album"] and inferred_album:
                metadata["album"] = inferred_album
            if inferred_song and inferred_song != metadata["title"]:
                metadata["title"] = inferred_song
        except Exception:
            pass

        title = self.normalize_space(metadata["title"] or fallback_title or "Unknown Song")
        artist = self.normalize_space(metadata["artist"])
        album = self.normalize_space(metadata["album"])
        return title, artist, album

    def normalize_youtube_url(self, url):
        parsed = urlparse(url)
        if "youtube.com" not in parsed.netloc and "youtu.be" not in parsed.netloc:
            return url

        if "youtu.be" in parsed.netloc:
            video_id = parsed.path.strip("/")
            return f"https://www.youtube.com/watch?v={video_id}" if video_id else url

        query = parse_qs(parsed.query)
        video_id = query.get("v", [""])[0]
        if not video_id:
            return url

        clean_query = urlencode({"v": video_id})
        return urlunparse(("https", "www.youtube.com", "/watch", "", clean_query, ""))

    def process_song(self, url, manual_title):
        job_temp_dir = None
        job_succeeded = False
        try:
            normalized_url = self.normalize_youtube_url(url)
            resolved_title, resolved_artist, resolved_album = self.resolve_song_metadata(normalized_url, manual_title)
            safe_title = self.build_song_filename(resolved_title, resolved_artist, resolved_album)
            self.log(f"Target song: {safe_title}")
            if normalized_url != url:
                self.log("Normalized the YouTube URL to improve download stability")

            job_id = str(int(time.time()))
            job_temp_dir = os.path.join(TEMP_BASE_DIR, job_id)
            os.makedirs(job_temp_dir, exist_ok=True)

            temp_input = os.path.join(job_temp_dir, "input.mp4")
            temp_output = os.path.join(job_temp_dir, "output.mp4")

            ffmpeg_executable = get_ffmpeg_executable()
            if ffmpeg_executable is None:
                raise Exception("FFmpeg not found. Please install it or place ffmpeg in the app folder.")

            deno_executable = get_deno_executable()
            separator_log = os.path.join(job_temp_dir, "separator.log")

            self.log("Step 1/4: Downloading video...")
            cmd_dl = get_ytdlp_command() + [
                "--ffmpeg-location", os.path.dirname(ffmpeg_executable), 
                "--force-overwrites",  
                "--no-playlist",       
                "--extractor-args", "youtube:player_client=android,web",
                "-f", "best[ext=mp4]/18/best", 
                "-o", temp_input, 
                normalized_url
            ]
            if deno_executable:
                cmd_dl += ["--js-runtimes", f"deno:{deno_executable}", "--remote-components", "ejs:github"]
            
            try:
                subprocess.run(
                    cmd_dl, check=True, capture_output=True, text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
                )
            except subprocess.CalledProcessError as exc:
                download_output = "\n".join(part for part in [exc.stdout, exc.stderr] if part).strip()
                if download_output:
                    self.log(download_output)
                lowered_output = download_output.lower()
                if "video unavailable" in lowered_output or "restricted" in lowered_output or "private video" in lowered_output:
                    raise Exception("YouTube is blocking this video for your account, workspace, or network. Try a different video or sign in with an account that has access.")
                raise Exception(f"yt-dlp download failed with code {exc.returncode}")

            self.log("Step 2/4: AI vocal separation (Demucs)... (this may take a while)")
            
            # 【終極修復】改用獨立進程執行 Demucs，避免 PyTorch 記憶體殘留。
            import multiprocessing
            p = multiprocessing.Process(target=_run_demucs_process, args=(temp_input, job_temp_dir, separator_log))
            p.start()
            demucs_started_at = time.time()
            next_heartbeat = demucs_started_at + DEMUCS_HEARTBEAT_SECONDS

            while p.is_alive():
                p.join(timeout=2)
                now = time.time()
                elapsed = int(now - demucs_started_at)

                if now >= next_heartbeat:
                    self.log(f"⏳ Demucs still running... elapsed {elapsed}s")
                    next_heartbeat = now + DEMUCS_HEARTBEAT_SECONDS

                if elapsed >= DEMUCS_TIMEOUT_SECONDS:
                    try:
                        p.terminate()
                    except Exception:
                        pass
                    p.join(timeout=5)
                    raise Exception(
                        f"Demucs timed out after {elapsed}s. "
                        f"Try shorter videos, lower workload, or rerun this song later."
                    )

            if os.path.exists(separator_log):
                with open(separator_log, "r", encoding="utf-8", errors="replace") as log_file:
                    separator_output = log_file.read().strip()
                if separator_output:
                    self.log(separator_output)
            
            if p.exitcode != 0:
                raise Exception(f"Demucs separation failed. Subprocess exited with code {p.exitcode}")
            
            # Demucs 會建立 model/track/stem 的輸出結構
            base_name = os.path.splitext(os.path.basename(temp_input))[0] # 會得到 "input"
            model_name = "htdemucs"
            model_marker_path = os.path.join(job_temp_dir, "demucs_model.txt")
            if os.path.exists(model_marker_path):
                try:
                    with open(model_marker_path, "r", encoding="utf-8") as marker_file:
                        marker_value = str(marker_file.read()).strip()
                    if marker_value:
                        model_name = marker_value
                except Exception:
                    pass
            voc_path = os.path.join(job_temp_dir, model_name, base_name, "vocals.wav")
            acc_path = os.path.join(job_temp_dir, model_name, base_name, "no_vocals.wav")

            if not os.path.exists(voc_path) or not os.path.exists(acc_path):
                raise Exception("Demucs separation failed. Audio stems were not found.")

            self.log("Step 3/4: Merging L/R channels (L: vocal, R: instrumental)...")
            cmd_ffmpeg = [
                ffmpeg_executable,
                "-y",
                "-i", temp_input,
                "-i", voc_path,
                "-i", acc_path,
                "-filter_complex", "[0:a]pan=mono|c0=0.5*FL+0.5*FR[L];[2:a]pan=mono|c0=0.5*FL+0.5*FR[R];[L][R]join=inputs=2:channel_layout=stereo[a]",
                "-map", "0:v",
                "-map", "[a]",
                "-c:v", "copy",
                "-c:a", "aac",
                temp_output,
            ]
            
            subprocess.run(
                cmd_ffmpeg, check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
            )

            self.log(f"Step 4/4: Saving as {safe_title}.mp4")
            final = os.path.join(SONGS_DIR, f"{safe_title}.mp4")
            
            if os.path.exists(final):
                final = os.path.join(SONGS_DIR, f"{safe_title}_{job_id}.mp4")

            shutil.move(temp_output, final)
            lyrics_path = self.save_lyrics_for_song(os.path.basename(final), resolved_title, resolved_artist, resolved_album)
            if lyrics_path:
                self.log(f"🎵 Matched synced lyrics for {os.path.basename(final)}")
            else:
                self.log("🎵 No lyrics match was found for this song; the audio file is still saved.")
            
            self.log("✅ Processing complete. The song has been added to the library.")
            job_succeeded = True
            return os.path.basename(final)

        except subprocess.CalledProcessError as e:
            self.log(f"❌ Process failed (Code {e.returncode})")
            if e.stdout:
                self.log(e.stdout.strip())
            if e.stderr:
                self.log(e.stderr.strip())
            return None
        except Exception as e:
            self.log(f"❌ Error: {e}")
            return None
        finally:
            if job_succeeded and job_temp_dir and os.path.exists(job_temp_dir):
                try:
                    shutil.rmtree(job_temp_dir, ignore_errors=True)
                except:
                    pass 

# ==========================================
# 本機 GUI 
# ==========================================
class ServerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("KTV Server Status")
        self.geometry("450x500") # 稍微拉高一點放日誌框
        self.configure(bg="#f4f4f9")
        
        tk.Label(self, text="🎤 KTV System Running", font=("Microsoft JhengHei", 20, "bold"), fg="#4CAF50", bg="#f4f4f9").pack(pady=10)
        
        info_frame = tk.Frame(self, bg="white", bd=1, relief="solid")
        info_frame.pack(fill="x", padx=20, pady=5)
        
        self.create_clickable_link(info_frame, "📺 Player (TV)", f"http://{LOCAL_IP}:{PORT}/player", "blue")
        self.create_clickable_link(info_frame, "📱 Remote (Phone)", f"http://{LOCAL_IP}:{PORT}/remote", "#d32f2f")
        self.create_clickable_link(info_frame, "🕹️ Combo (Single PC)", f"http://{LOCAL_IP}:{PORT}/combo", "#9C27B0")
        self.create_clickable_link(info_frame, "⚙️ Admin (Add Songs)", f"http://{LOCAL_IP}:{PORT}/admin", "#F57C00")

        stat_frame = tk.Frame(self, bg="#f4f4f9")
        stat_frame.pack(fill="x", padx=20, pady=5)
        
        self.lbl_count = tk.Label(stat_frame, text="Total songs: loading...", font=("Microsoft JhengHei", 12, "bold"), bg="#f4f4f9")
        self.lbl_count.pack(anchor="w")
        
        self.lbl_size = tk.Label(stat_frame, text="Storage used: loading...", font=("Microsoft JhengHei", 12, "bold"), bg="#f4f4f9")
        self.lbl_size.pack(anchor="w", pady=5)

        # 增加一個實體的 GUI 日誌框，用來接聽攔截到的錯誤訊息
        self.log_txt = tk.Text(self, height=8, state="disabled", bg="#222", fg="#0f0", font=("Consolas", 9))
        self.log_txt.pack(fill="both", expand=True, padx=20, pady=10)
        
        self.update_stats()
        
        # 啟動背景佇列監聽器
        self.check_log_queue()
        _push_gui_log("🟢 GUI ready. Waiting for realtime events...")

    def create_clickable_link(self, parent, text_prefix, url, color):
        frame = tk.Frame(parent, bg="white")
        frame.pack(pady=2, anchor="w", padx=10)
        tk.Label(frame, text=f"{text_prefix}: ", font=("Consolas", 11), bg="white").pack(side="left")
        link_lbl = tk.Label(frame, text=url, font=("Consolas", 11, "underline"), fg=color, bg="white", cursor="hand2")
        link_lbl.pack(side="left")
        link_lbl.bind("<Button-1>", lambda e, u=url: webbrowser.open(u))

    def update_stats(self):
        try:
            songs = [f for f in os.listdir(SONGS_DIR) if f.endswith('.mp4')]
            count = len(songs)
            total_size = sum(os.path.getsize(os.path.join(SONGS_DIR, f)) for f in songs)
            size_mb = total_size / (1024 * 1024)
            
            self.lbl_count.config(text=f"🎵 Total songs: {count}")
            self.lbl_size.config(text=f"💾 Storage used: {size_mb:.2f} MB")
        except Exception as e:
            pass
        self.after(5000, self.update_stats)

    def check_log_queue(self):
        """每 100 毫秒檢查一次佇列，把背景的文字寫進 GUI 日誌框"""
        try:
            while not system_log_queue.empty():
                msg = system_log_queue.get_nowait()
                self.log_txt.config(state="normal")
                self.log_txt.insert("end", msg + "\n")
                self.log_txt.see("end")
                self.log_txt.config(state="disabled")
        except Exception:
            pass
        self.after(100, self.check_log_queue)

if __name__ == "__main__":
    # 【關鍵】多進程保護必須放在 if __name__ == "__main__": 的第一行
    multiprocessing.freeze_support()

    if get_ffmpeg_executable() is None:
        try:
            messagebox.showerror("Error", "FFmpeg not found\nPlease place the ffmpeg folder in the app directory")
        except:
            print("FFmpeg not found")
    else:
        headless = str(os.environ.get("KTV_HEADLESS", "")).strip().lower() in {"1", "true", "yes", "on"}
        if headless:
            run_server_thread()
        else:
            try:
                t = threading.Thread(target=run_server_thread)
                t.daemon = True
                t.start()

                gui_app = ServerApp()
                gui_app.mainloop()
            except Exception as exc:
                print(f"⚠️ GUI mode unavailable ({exc}). Falling back to headless server mode.")
                run_server_thread()