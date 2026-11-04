from __future__ import annotations

import json
import hmac
import os
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

from flask import Blueprint, Flask, current_app, jsonify, redirect, render_template, request, send_from_directory
from flask_socketio import SocketIO, emit

from .config import AppSettings, load_settings
from .library import (
    cleanup_orphan_files,
    delete_catalog_entry,
    find_intro_skip_seconds,
    get_catalog_health,
    get_db_path,
    init_library_db,
    save_lyrics_for_song,
    update_catalog_entry,
    update_library_index,
)
from .metadata import ai_parse_youtube_title, parse_youtube_title

CONTROL_ROLES = {"remote", "queue", "admin", "combo"}


def get_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def _is_playlist_url(url: str) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    return "list" in query or "/playlist" in parsed.path


def _validate_youtube_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("只允許 http/https 連結")
    host = (parsed.netloc or "").lower()
    allowed_hosts = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}
    if host not in allowed_hosts:
        raise ValueError("僅支援 YouTube 連結")
    return url.strip()


def _extract_youtube_title(url: str) -> dict[str, str]:
    safe_url = _validate_youtube_url(url)
    try:
        from yt_dlp import YoutubeDL  # pylint: disable=import-outside-toplevel
    except Exception as error:
        raise RuntimeError(f"無法載入 yt-dlp 模組: {error}") from error

    with YoutubeDL({"quiet": True, "skip_download": True, "extract_flat": True}) as ydl:
        payload = ydl.extract_info(safe_url, download=False)
    title = (payload or {}).get("title") or ""
    uploader = (payload or {}).get("uploader") or ""
    parsed = ai_parse_youtube_title(title, uploader)
    if parsed:
        parsed = {"title": title, "song": parsed["title"], "singer": parsed["artist"]}
    else:
        metadata = parse_youtube_title(title, uploader)
        parsed = {"title": title, "song": metadata["title"], "singer": metadata["artist"]}
    parsed["raw_title"] = title
    parsed["uploader"] = uploader
    return parsed


def _playlist_entries(url: str, settings: AppSettings) -> list[dict]:
    safe_url = _validate_youtube_url(url)
    try:
        from yt_dlp import YoutubeDL  # pylint: disable=import-outside-toplevel
    except Exception as error:
        raise RuntimeError(f"無法載入 yt-dlp 模組: {error}") from error

    with YoutubeDL({"quiet": True, "extract_flat": True, "skip_download": True}) as ydl:
        payload = ydl.extract_info(safe_url, download=False)
    entries = []
    for item in payload.get("entries", []) or []:
        video_id = item.get("id")
        if not video_id:
            continue
        entries.append(
            {
                "id": video_id,
                "title": (item.get("title") or video_id).strip(),
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "singer": (item.get("uploader") or "").strip(),
            }
        )
    return entries


def _create_blueprint() -> Blueprint:
    bp = Blueprint("web", __name__)

    def node_page(page: str):
        settings: AppSettings = current_app.config["APP_SETTINGS"]
        host = urlparse(request.host_url).hostname or "127.0.0.1"
        if ":" in host:
            host = f"[{host}]"
        return redirect(f"http://{host}:{settings.node_port}/{page}")

    @bp.route("/player")
    def page_player():
        return node_page("player.html")

    @bp.route("/lyrics-editor")
    def page_lyrics_editor():
        return render_template("lyrics_editor.html")

    @bp.route("/remote")
    def page_remote():
        return node_page("remote.html")

    @bp.route("/queue")
    def page_queue():
        return node_page("remote.html")

    @bp.route("/admin")
    def page_admin():
        return render_template("admin.html")

    @bp.route("/combo")
    def page_combo():
        return node_page("remote.html")

    @bp.route("/")
    def page_index():
        return render_template("remote.html")

    @bp.route("/songs/<path:filename>")
    def serve_song(filename: str):
        settings: AppSettings = current_app.config["APP_SETTINGS"]
        return send_from_directory(settings.songs_dir, filename)

    @bp.route("/api/list")
    def get_song_list():
        settings: AppSettings = current_app.config["APP_SETTINGS"]
        songs = [
            file_name
            for file_name in os.listdir(settings.songs_dir)
            if file_name.lower().endswith(".mp4")
        ]
        return json.dumps(sorted(songs))

    @bp.route("/health")
    def health():
        settings: AppSettings = current_app.config["APP_SETTINGS"]
        return jsonify(
            ok=True,
            service="python",
        )

    def can_edit_catalog() -> bool:
        expected = (os.getenv("KTV_LIBRARY_ADMIN_TOKEN") or os.getenv("KTV_SECRET_KEY") or "").strip()
        supplied = request.headers.get("Authorization", "")
        scheme, separator, token = supplied.partition(" ")
        return bool(
            expected
            and separator
            and scheme.lower() == "bearer"
            and len(token) == len(expected)
            and hmac.compare_digest(token, expected)
        )

    @bp.route("/api/catalog/<song_id>", methods=["PATCH"])
    def edit_catalog_song(song_id: str):
        if not can_edit_catalog():
            return jsonify(ok=False, error="曲庫管理權杖無效或尚未設定"), 401
        if len(song_id) > 128 or not request.is_json:
            return jsonify(ok=False, error="歌曲識別碼或更新內容無效"), 400
        updates = request.get_json(silent=True)
        editable_fields = {
            "artist", "title", "album", "release_year", "artist_gender", "genre", "language", "lyrics",
        }
        if not isinstance(updates, dict) or not updates or set(updates) - editable_fields:
            return jsonify(ok=False, error="更新欄位不受支援"), 400
        if not str(updates.get("artist", "")).strip() and "artist" in updates:
            return jsonify(ok=False, error="歌手名稱不可為空"), 400
        if not str(updates.get("title", "")).strip() and "title" in updates:
            return jsonify(ok=False, error="歌曲名稱不可為空"), 400
        settings: AppSettings = current_app.config["APP_SETTINGS"]
        try:
            row = update_catalog_entry(settings.library_index_path, song_id, updates)
        except ValueError:
            return jsonify(ok=False, error="相同歌手與歌名已存在於曲庫"), 409
        if row is None:
            return jsonify(ok=False, error="找不到歌曲"), 404
        return jsonify(ok=True, song=row)

    @bp.route("/api/catalog/<song_id>", methods=["DELETE"])
    def remove_catalog_song(song_id: str):
        if not can_edit_catalog():
            return jsonify(ok=False, error="曲庫管理權杖無效或尚未設定"), 401
        if len(song_id) > 128:
            return jsonify(ok=False, error="歌曲識別碼無效"), 400
        delete_files = request.args.get("delete_files", "").lower() in ("true", "1")
        if request.is_json:
            body = request.get_json(silent=True) or {}
            if "delete_files" in body:
                delete_files = bool(body["delete_files"])
        settings: AppSettings = current_app.config["APP_SETTINGS"]
        row = delete_catalog_entry(
            settings.library_index_path,
            song_id,
            delete_media_files=delete_files,
            songs_dir=settings.songs_dir
        )
        if row is None:
            return jsonify(ok=False, error="找不到歌曲"), 404
        return jsonify(
            ok=True,
            song=row,
            media_files_deleted=bool(delete_files),
            deleted_files=row.get("deleted_media_files", [])
        )

    @bp.route("/api/catalog/health", methods=["GET"])
    def catalog_health():
        settings: AppSettings = current_app.config["APP_SETTINGS"]
        db_p = get_db_path(settings.library_index_path)
        init_library_db(db_p, settings.library_index_path)
        health = get_catalog_health(settings.songs_dir, db_p)
        return jsonify(ok=True, **health)

    @bp.route("/api/catalog/cleanup-orphans", methods=["POST"])
    def cleanup_orphans():
        if not can_edit_catalog():
            return jsonify(ok=False, error="曲庫管理權杖無效或尚未設定"), 401
        settings: AppSettings = current_app.config["APP_SETTINGS"]
        db_p = get_db_path(settings.library_index_path)
        init_library_db(db_p, settings.library_index_path)
        res = cleanup_orphan_files(settings.songs_dir, db_p)
        return jsonify(ok=True, **res)

    @bp.route("/api/catalog/<song_id>/lyrics", methods=["PUT"])
    def commit_lyrics(song_id: str):
        if not can_edit_catalog():
            return jsonify(ok=False, error="曲庫管理權杖無效或尚未設定"), 401
        if not request.is_json:
            return jsonify(ok=False, error="格式錯誤，需為 JSON"), 400
        data = request.get_json(silent=True) or {}
        lrc_content = (data.get("lrc_content") or "").strip()
        if not lrc_content:
            return jsonify(ok=False, error="歌詞內容不可為空"), 400
        settings: AppSettings = current_app.config["APP_SETTINGS"]
        result = save_lyrics_for_song(settings.songs_dir, settings.library_index_path, song_id, lrc_content)
        if not result:
            return jsonify(ok=False, error="找不到歌曲"), 404
        return jsonify(ok=True, **result)

    @bp.route("/api/playlist_preview")
    def playlist_preview():
        settings: AppSettings = current_app.config["APP_SETTINGS"]
        url = request.args.get("url", "").strip()
        if not _is_playlist_url(url):
            return json.dumps({"ok": False, "error": "不是 playlist 連結"}), 400
        try:
            entries = _playlist_entries(url, settings)
            return json.dumps({"ok": True, "entries": entries})
        except ValueError:
            return json.dumps({"ok": False, "error": "playlist 參數無效"}), 400
        except Exception:
            return json.dumps({"ok": False, "error": "playlist 讀取失敗"}), 500

    @bp.route("/api/resolve_yt_title")
    def resolve_yt_title():
        url = request.args.get("url", "").strip()
        if not url:
            return json.dumps({"ok": False, "error": "缺少連結"}), 400
        try:
            data = _extract_youtube_title(url)
            return json.dumps({"ok": True, **data})
        except ValueError:
            return json.dumps({"ok": False, "error": "YouTube 連結格式不正確"}), 400
        except Exception:
            return json.dumps({"ok": False, "error": "無法解析 YouTube 標題"}), 500

    return bp


def create_app(settings: AppSettings | None = None) -> tuple[Flask, SocketIO, AppSettings]:
    app_settings = settings or load_settings()
    app = Flask(__name__, template_folder=str(app_settings.templates_dir))
    app.config.from_mapping(SECRET_KEY=app_settings.secret_key, APP_SETTINGS=app_settings)
    app.register_blueprint(_create_blueprint())

    socketio = SocketIO(app, cors_allowed_origins="*")
    return app, socketio, app_settings


def register_socket_handlers(socketio: SocketIO, settings: AppSettings, log_cb: Callable[[str], None]):
    state = {
        "is_processing": False,
        "client_roles": {},
        "playlist_queue": [],
        "played_queue": [],
        "next_queue_id": 1,
        "current_song": None,
        "is_playing": False,
        "position": 0.0,
        "started_at": None,
        "audio_mode": "original",
        "effects": {"volume": 1.0, "pitch": 0},
        "intro_skip_to": None,
        "intro_skip_hide_after": None,
        "intro_skip_used": False,
    }

    def current_position() -> float:
        if state["is_playing"] and state["started_at"] is not None:
            return state["position"] + max(0.0, time.time() - state["started_at"])
        return state["position"]

    def role_for_sid(sid: str) -> str:
        return state["client_roles"].get(sid, "player")

    def can_control() -> bool:
        return role_for_sid(request.sid) in CONTROL_ROLES

    def update_intro_skip(song_filename: str | None):
        if not song_filename:
            state["intro_skip_to"] = None
            state["intro_skip_hide_after"] = None
            state["intro_skip_used"] = False
            return
        skip_to, hide_after = find_intro_skip_seconds(
            settings.songs_dir,
            song_filename,
            lead_seconds=settings.intro_skip_lead_seconds,
        )
        state["intro_skip_to"] = skip_to
        state["intro_skip_hide_after"] = hide_after
        state["intro_skip_used"] = False

    def queue_payload() -> list[dict]:
        items = []
        for item in state["playlist_queue"]:
            current = state["current_song"] and item["queue_id"] == state["current_song"]["queue_id"]
            item_state = "playing" if current else "queued"
            items.append({
                "queue_id": item["queue_id"],
                "filename": item["filename"],
                "title": item["title"],
                "state": item_state,
            })

        for item in state["played_queue"][-30:]:
            items.append(
                {
                    "queue_id": item["queue_id"],
                    "filename": item["filename"],
                    "title": item["title"],
                    "state": "sung",
                }
            )
        return items

    def playback_snapshot() -> dict:
        current = state["current_song"]
        filename = current["filename"] if current else None
        instrumental_filename = None
        if filename:
            candidate = f"{Path(filename).stem}.instrumental.m4a"
            if (settings.songs_dir / candidate).is_file():
                instrumental_filename = candidate
        return {
            "filename": filename,
            "title": (current["title"] if current else ""),
            "instrumental_filename": instrumental_filename,
            "is_playing": state["is_playing"],
            "position": current_position(),
            "audio_mode": state["audio_mode"],
            "effects": state["effects"],
            "server_time": time.time(),
            "intro_skip_to": state["intro_skip_to"],
            "intro_skip_hide_after": state["intro_skip_hide_after"],
            "intro_skip_used": state["intro_skip_used"],
            "queue": queue_payload(),
        }

    def emit_all_state():
        payload = playback_snapshot()
        socketio.emit("playback_snapshot", payload)
        socketio.emit("playback_state", payload)
        socketio.emit("update_queue", [item["filename"] for item in state["playlist_queue"]])
        socketio.emit("update_queue_state", queue_payload())

    def playback_sync_loop():
        while True:
            socketio.sleep(1)
            if state["current_song"]:
                emit_all_state()

    socketio.start_background_task(playback_sync_loop)

    def broadcast_log(message: str):
        log_cb(message)
        socketio.emit("admin_log", {"msg": message})

    def enqueue_song(filename: str, title: str | None = None, insert_next: bool = False) -> dict:
        entry = {
            "queue_id": state["next_queue_id"],
            "filename": filename,
            "title": title or filename,
        }
        state["next_queue_id"] += 1

        if insert_next and state["current_song"] and state["playlist_queue"]:
            state["playlist_queue"].insert(1, entry)
        else:
            state["playlist_queue"].append(entry)

        if state["current_song"] is None and state["playlist_queue"]:
            start_current_song(state["playlist_queue"][0])
        emit_all_state()
        return entry

    def start_current_song(entry: dict):
        state["current_song"] = entry
        state["is_playing"] = True
        state["position"] = 0.0
        state["started_at"] = time.time()
        update_intro_skip(entry["filename"])

    def advance_song():
        if state["playlist_queue"]:
            finished = state["playlist_queue"].pop(0)
            state["played_queue"].append(finished)
        if state["playlist_queue"]:
            start_current_song(state["playlist_queue"][0])
        else:
            state["current_song"] = None
            state["is_playing"] = False
            state["position"] = 0.0
            state["started_at"] = None
            update_intro_skip(None)

    def apply_seek(position: float):
        state["position"] = max(0.0, float(position))
        state["started_at"] = time.time() if state["is_playing"] else None

    @socketio.on("connect")
    def handle_connect():
        role = request.args.get("role", "player").lower().strip()
        state["client_roles"][request.sid] = role if role in (CONTROL_ROLES | {"player"}) else "player"
        emit("playback_snapshot", playback_snapshot())
        emit("playback_state", playback_snapshot())
        emit("update_queue_state", queue_payload())
        emit("apply_effect", state["effects"])

    @socketio.on("disconnect")
    def handle_disconnect():
        state["client_roles"].pop(request.sid, None)

    @socketio.on("add_to_queue")
    def handle_add_queue(data):
        if not can_control():
            return
        enqueue_song(data["filename"], data.get("title"))

    @socketio.on("queue_insert_next")
    def handle_insert_next(data):
        if not can_control():
            return
        enqueue_song(data["filename"], data.get("title"), insert_next=True)

    @socketio.on("queue_remove")
    def handle_queue_remove(data):
        if not can_control():
            return
        try:
            queue_id = int(data.get("queue_id", -1))
        except (TypeError, ValueError):
            return
        removed_current = state["current_song"] and state["current_song"]["queue_id"] == queue_id
        state["playlist_queue"] = [item for item in state["playlist_queue"] if item["queue_id"] != queue_id]
        if removed_current:
            if state["playlist_queue"]:
                start_current_song(state["playlist_queue"][0])
            else:
                state["current_song"] = None
                state["is_playing"] = False
                state["position"] = 0.0
                state["started_at"] = None
                update_intro_skip(None)
        emit_all_state()

    @socketio.on("request_play")
    def handle_request_play(data):
        if not can_control():
            return
        enqueue_song(data["filename"], data.get("title"), insert_next=True)

    @socketio.on("song_ended")
    def handle_song_ended(data=None):
        if not state["current_song"]:
            return
        if isinstance(data, dict) and data.get("filename") != state["current_song"]["filename"]:
            return
        advance_song()
        emit_all_state()

    @socketio.on("control")
    def handle_control(action):
        if not can_control():
            return
        if action == "cut":
            advance_song()
        elif action == "pause" and state["current_song"]:
            state["position"] = current_position()
            state["is_playing"] = not state["is_playing"]
            state["started_at"] = time.time() if state["is_playing"] else None
        elif action == "play" and state["current_song"]:
            state["is_playing"] = True
            state["started_at"] = time.time()
        elif action == "stop":
            state["is_playing"] = False
            state["position"] = 0.0
            state["started_at"] = None
        emit_all_state()

    @socketio.on("seek")
    def handle_seek(data):
        if not can_control() or not state["current_song"]:
            return
        try:
            position = float(data.get("position", 0.0))
        except (TypeError, ValueError):
            return
        apply_seek(position)
        emit_all_state()

    @socketio.on("skip_intro")
    def handle_skip_intro():
        if not can_control() or not state["current_song"]:
            return
        skip_to = state["intro_skip_to"]
        hide_after = state["intro_skip_hide_after"]
        if skip_to is None or hide_after is None:
            return
        if current_position() >= hide_after:
            return
        apply_seek(skip_to)
        state["intro_skip_used"] = True
        emit_all_state()

    @socketio.on("control_effect")
    def handle_effect(data):
        if not can_control():
            return
        state["effects"].update({key: value for key, value in data.items() if key in {"volume", "pitch"}})
        socketio.emit("apply_effect", state["effects"])
        emit_all_state()

    @socketio.on("change_track")
    def handle_track(mode):
        if not can_control():
            return
        if mode in {"original", "instrumental"}:
            state["audio_mode"] = mode
            socketio.emit("set_audio", mode)
            emit_all_state()

    ingest_queue = []
    ingest_lock = threading.Lock()
    cancel_requested = threading.Event()
    worker_running = False

    def get_queue_status():
        with ingest_lock:
            return [{
                "id": t["id"],
                "title": t["title"],
                "singer": t["singer"],
                "status": t["status"],
            } for t in ingest_queue if t["status"] in ("queued", "processing")]

    @socketio.on("cancel_download")
    def handle_cancel_download(data=None):
        nonlocal cancel_requested
        task_id = data.get("taskId") if isinstance(data, dict) else None
        with ingest_lock:
            if task_id:
                for t in ingest_queue:
                    if t["id"] == task_id:
                        if t["status"] == "queued":
                            t["status"] = "cancelled"
                        elif t["status"] == "processing":
                            cancel_requested.set()
            else:
                for t in ingest_queue:
                    if t["status"] == "queued":
                        t["status"] = "cancelled"
                cancel_requested.set()
        broadcast_log("🛑 已送出取消下載任務請求")
        socketio.emit("queue_status", {"queue": get_queue_status()})

    @socketio.on("start_download")
    def handle_start_download(data):
        nonlocal worker_running, cancel_requested
        if not can_control():
            return

        raw_url = (data.get("url") or "").strip()
        lines = [u.strip() for u in raw_url.splitlines() if u.strip()]
        if not lines:
            broadcast_log("❌ 請輸入有效的 YouTube 網址")
            return

        manual_title = (data.get("title") or "").strip()
        manual_singer = (data.get("singer") or "").strip()
        manual_lyrics = (data.get("lyrics_text") or "").strip()
        options = {
            k: v for k, v in {
                "stems": data.get("stems"),
                "device": data.get("device"),
                "separator_backend": data.get("separator_backend"),
                "alignment_backend": data.get("alignment_backend"),
            }.items() if v is not None and str(v).strip() != "" and str(v).strip().lower() != "none"
        }

        new_tasks = []
        for line_url in lines:
            try:
                valid_url = _validate_youtube_url(line_url)
                if _is_playlist_url(valid_url):
                    entries = data.get("playlist_entries") or _playlist_entries(valid_url, settings)
                    for item in entries:
                        item_url = item.get("url") or f"https://www.youtube.com/watch?v={item.get('id')}"
                        new_tasks.append({
                            "id": f"job_{int(time.time()*1000)}_{len(new_tasks)}",
                            "url": _validate_youtube_url(item_url),
                            "title": item.get("title") or item.get("id") or manual_title or "YouTube 歌曲",
                            "singer": (item.get("singer") or "").strip(),
                            "singer_is_manual": False,
                            "youtube_title": (item.get("title") or "").strip(),
                            "title_is_auto": True,
                            "lyrics_text": "",
                            "options": options,
                            "status": "queued",
                        })
                else:
                    new_tasks.append({
                        "id": f"job_{int(time.time()*1000)}_{len(new_tasks)}",
                        "url": valid_url,
                        "title": manual_title or "YouTube 歌曲",
                        "singer": manual_singer,
                        "singer_is_manual": bool(data.get("singer_is_manual")),
                        "youtube_title": str(data.get("youtube_title") or ""),
                        "title_is_auto": False,
                        "lyrics_text": manual_lyrics,
                        "options": options,
                        "status": "queued",
                    })
            except Exception as error:
                broadcast_log(f"❌ 連結解析失敗 ({line_url}): {error}")

        if not new_tasks:
            return

        with ingest_lock:
            ingest_queue.extend(new_tasks)

        cancel_requested.clear()
        broadcast_log(f"📥 已加入 {len(new_tasks)} 首新任務至入庫隊列 (待播中共 {len([t for t in ingest_queue if t['status'] == 'queued'])} 首)")
        socketio.emit("queue_status", {"queue": get_queue_status()})

        if worker_running:
            return

        worker_running = True
        state["is_processing"] = True

        def run_queue_worker():
            nonlocal worker_running, cancel_requested
            try:
                from .unified_nightingale import KTVProcessor  # pylint: disable=import-outside-toplevel

                socketio.emit("task_status", {"status": "busy"})
                processor = KTVProcessor(settings=settings, log_cb=broadcast_log)

                while True:
                    current_task = None
                    with ingest_lock:
                        pending = [t for t in ingest_queue if t["status"] == "queued"]
                        if not pending:
                            break
                        current_task = pending[0]
                        current_task["status"] = "processing"

                    if cancel_requested.is_set():
                        current_task["status"] = "cancelled"
                        continue

                    title = current_task["title"]
                    broadcast_log(f"▶ 開始處理：{title}")
                    socketio.emit("task_progress", {
                        "phase": "processing",
                        "current": 1,
                        "total": len(ingest_queue),
                        "title": title,
                        "attempt": 1,
                        "taskId": current_task["id"],
                    })

                    success = False
                    for attempt in range(settings.download_retry_count + 1):
                        if cancel_requested.is_set():
                            current_task["status"] = "cancelled"
                            break

                        socketio.emit("task_progress", {
                            "phase": "processing",
                            "current": 1,
                            "total": len(ingest_queue),
                            "title": title,
                            "attempt": attempt + 1,
                            "taskId": current_task["id"],
                        })

                        try:
                            success = processor.process_song(
                                current_task["url"],
                                title,
                                options={
                                    **current_task["options"],
                                    "singer": current_task["singer"],
                                    "singer_is_manual": current_task.get("singer_is_manual", False),
                                    "youtube_title": current_task.get("youtube_title", ""),
                                    "title_is_auto": current_task.get("title_is_auto", False),
                                    "lyrics_text": current_task.get("lyrics_text", ""),
                                },
                            )
                        except Exception as e:
                            processor.last_error = str(e)
                            broadcast_log(f"處理失敗: {e}")
                            success = False

                        if success:
                            break
                        time.sleep(0.8)

                    error_reason = getattr(processor, "last_error", "") or ""
                    if not success and not error_reason:
                        error_reason = "處理未完成（下載或音訊分離失敗）"
                    current_task["error"] = error_reason
                    current_task["status"] = "completed" if success else ("cancelled" if cancel_requested.is_set() else "failed")
                    if success:
                        update_library_index(settings.songs_dir, settings.library_index_path)
                        socketio.emit("refresh_list")

                    socketio.emit("task_progress", {
                        "phase": "done",
                        "total": 1,
                        "success": 1 if success else 0,
                        "failed": [] if success else [title],
                        "failed_reason": error_reason if not success else "",
                        "taskId": current_task["id"],
                    })
                    socketio.emit("queue_status", {"queue": get_queue_status()})

            finally:
                state["is_processing"] = False
                worker_running = False
                socketio.emit("task_status", {"status": "idle"})
                socketio.emit("queue_status", {"queue": get_queue_status()})

        threading.Thread(target=run_queue_worker, daemon=True).start()

    @socketio.on("update_ytdlp")
    def handle_update_ytdlp():
        if not can_control():
            return

        def run_update():
            socketio.emit("task_status", {"status": "busy"})
            broadcast_log("開始更新 yt-dlp 核心...")
            try:
                command = ["yt-dlp", "-U"]
                if settings.yt_dlp_path.exists():
                    command = [str(settings.yt_dlp_path), "-U"]
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                if result.stdout:
                    broadcast_log(result.stdout)
                if result.stderr:
                    broadcast_log(result.stderr)
                broadcast_log("✅ yt-dlp 更新程序結束。")
            except Exception as error:
                broadcast_log(f"❌ 更新失敗: {error}")
            finally:
                socketio.emit("task_status", {"status": "idle"})

        threading.Thread(target=run_update, daemon=True).start()
