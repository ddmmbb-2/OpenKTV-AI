import tkinter as tk
from tkinter import messagebox
import subprocess
import os
import shutil
import threading
import socket
import sys
import json
import time
import webbrowser  # <--- 新增：用來開啟網頁
from flask import Flask, render_template, request, send_from_directory
from flask_socketio import SocketIO, emit
from spleeter.separator import Separator

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

if os.path.exists(FFMPEG_DIR):
    os.environ["PATH"] += os.pathsep + FFMPEG_DIR
os.environ["PATH"] += os.pathsep + BASE_DIR

SONGS_DIR = os.path.join(BASE_DIR, "ktv_songs")
TEMP_BASE_DIR = os.path.join(BASE_DIR, "temp_processing") # 改名為 BASE

if not os.path.exists(SONGS_DIR): os.makedirs(SONGS_DIR)
if not os.path.exists(TEMP_BASE_DIR): os.makedirs(TEMP_BASE_DIR)

# ==========================================
# Flask + SocketIO 伺服器
# ==========================================
app = Flask(__name__, template_folder=TEMPLATES_DIR)
app.config['SECRET_KEY'] = 'ktv_secret'
socketio = SocketIO(app, cors_allowed_origins="*")

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

def broadcast_log(msg):
    print(msg)
    socketio.emit('admin_log', {'msg': msg})

# ------------------------------------------
# Flask 路由
# ------------------------------------------
@app.route('/player')
def page_player():
    return render_template('player.html')

@app.route('/remote')
def page_remote():
    return render_template('remote.html')

@app.route('/admin')
def page_admin():
    return render_template('admin.html')

@app.route('/combo')  # <--- 新增：一體機路由
def page_combo():
    return render_template('combo.html')

@app.route('/')
def page_index():
    return render_template('remote.html')

@app.route('/songs/<path:filename>')
def serve_song(filename):
    return send_from_directory(SONGS_DIR, filename)

@app.route('/api/list')
def get_song_list():
    songs = [f for f in os.listdir(SONGS_DIR) if f.lower().endswith('.mp4')]
    return json.dumps(songs) 

# ------------------------------------------
# SocketIO 事件處理
# ------------------------------------------
@socketio.on('request_play')
def handle_play(data):
    emit('play_video', {'filename': data['filename'], 'title': data['filename']}, broadcast=True)

@socketio.on('control')
def handle_control(action):
    emit('command', action, broadcast=True)

@socketio.on('change_track')
def handle_track(mode):
    emit('set_audio', mode, broadcast=True)

is_processing = False

@socketio.on('start_download')
def handle_start_download(data):
    global is_processing
    if is_processing:
        broadcast_log("⚠️ 系統正在處理其他歌曲，請稍候。")
        return
    
    url = data.get('url')
    title = data.get('title')
    
    def run_process():
        global is_processing
        is_processing = True
        socketio.emit('task_status', {'status': 'busy'})
        
        processor = KTVProcessor(log_cb=broadcast_log)
        success = processor.process_song(url, title)
        
        if success:
            socketio.emit('refresh_list', broadcast=True)
        
        is_processing = False
        socketio.emit('task_status', {'status': 'idle'})

    broadcast_log("=== 開始新任務 ===")
    threading.Thread(target=run_process, daemon=True).start()

@socketio.on('update_ytdlp')
def handle_update_ytdlp():
    def run_update():
        socketio.emit('task_status', {'status': 'busy'})
        broadcast_log("開始更新 yt-dlp 核心...")
        try:
            cmd = ["yt-dlp", "-U"]
            if os.path.exists(YT_DLP_PATH):
                cmd = [YT_DLP_PATH, "-U"]
            result = subprocess.run(cmd, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            broadcast_log(result.stdout)
            if result.stderr: broadcast_log(result.stderr)
            broadcast_log("✅ yt-dlp 更新程序結束。")
        except Exception as e:
            broadcast_log(f"❌ 更新失敗: {str(e)}")
        finally:
            socketio.emit('task_status', {'status': 'idle'})

    threading.Thread(target=run_update, daemon=True).start()

def run_server_thread():
    socketio.run(app, host='0.0.0.0', port=PORT, debug=False, allow_unsafe_werkzeug=True)

# ==========================================
# 核心處理類別
# ==========================================
class KTVProcessor:
    def __init__(self, log_cb):
        self.log = log_cb

    def sanitize_filename(self, name):
        return "".join([c for c in name if c not in r'\/:*?"<>|'])

    def process_song(self, url, manual_title):
        job_temp_dir = None
        try:
            safe_title = self.sanitize_filename(manual_title)
            self.log(f"目標歌曲：{safe_title}")

            # 【重要修正】每次都產生一個獨立的、帶有時間戳記的專屬暫存資料夾
            job_id = str(int(time.time()))
            job_temp_dir = os.path.join(TEMP_BASE_DIR, job_id)
            os.makedirs(job_temp_dir, exist_ok=True)

            temp_input = os.path.join(job_temp_dir, "input.mp4")
            temp_output = os.path.join(job_temp_dir, "output.mp4")

            self.log("步驟 1/4: 下載影片...")
            cmd_dl = [
                "yt-dlp", 
                "--ffmpeg-location", FFMPEG_DIR, 
                "--force-overwrites",  
                "--no-playlist",       
                "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best", 
                "-o", temp_input, 
                url
            ]
            
            subprocess.run(
                cmd_dl, check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
            )

            self.log("步驟 2/4: AI 去人聲 (Spleeter)... (這需要一點時間)")
            separator = Separator('spleeter:2stems')
            separator.separate_to_file(temp_input, job_temp_dir)
            
            voc_path = os.path.join(job_temp_dir, "input", "vocals.wav")
            acc_path = os.path.join(job_temp_dir, "input", "accompaniment.wav")

            if not os.path.exists(voc_path) or not os.path.exists(acc_path):
                raise Exception("Spleeter 分離失敗，找不到音軌檔")

            self.log("步驟 3/4: 合成 L/R 聲道 (L:原曲 R:伴奏)...")
            cmd_ffmpeg = (
                f'ffmpeg -y -i "{temp_input}" -i "{voc_path}" -i "{acc_path}" '
                '-filter_complex "[0:a]pan=mono|c0=0.5*FL+0.5*FR[L];[2:a]pan=mono|c0=0.5*FL+0.5*FR[R];[L][R]join=inputs=2:channel_layout=stereo[a]" '
                f'-map 0:v -map "[a]" -c:v copy -c:a aac "{temp_output}"'
            )
            
            subprocess.run(
                cmd_ffmpeg, shell=True, check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
            )

            self.log(f"步驟 4/4: 儲存為 {safe_title}.mp4")
            final = os.path.join(SONGS_DIR, f"{safe_title}.mp4")
            
            if os.path.exists(final):
                final = os.path.join(SONGS_DIR, f"{safe_title}_{job_id}.mp4")

            shutil.move(temp_output, final)
            
            self.log("✅ 製作完成！已自動同步至歌單。")
            return True

        except subprocess.CalledProcessError as e:
            self.log(f"❌ 執行失敗 (Code {e.returncode})")
            return False
        except Exception as e:
            self.log(f"❌ 錯誤: {e}")
            return False
        finally:
            # 【重要修正】執行完畢後（無論成功或失敗），嘗試刪除這個專屬暫存資料夾
            if job_temp_dir and os.path.exists(job_temp_dir):
                try:
                    shutil.rmtree(job_temp_dir, ignore_errors=True)
                except:
                    pass # 如果被鎖住刪不掉也沒關係，不會影響下一次任務

# ==========================================
# 本機 GUI 
# ==========================================
class ServerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("KTV 伺服器狀態")
        self.geometry("450x400")
        self.configure(bg="#f4f4f9")
        
        tk.Label(self, text="🎤 KTV 系統運作中", font=("Microsoft JhengHei", 20, "bold"), fg="#4CAF50", bg="#f4f4f9").pack(pady=15)
        
        info_frame = tk.Frame(self, bg="white", bd=1, relief="solid")
        info_frame.pack(fill="x", padx=20, pady=5)
        
        # 使用自訂的建立連結函數
        self.create_clickable_link(info_frame, "📺 播放端 (電視用)", f"http://{LOCAL_IP}:{PORT}/player", "blue")
        self.create_clickable_link(info_frame, "📱 遙控端 (手機用)", f"http://{LOCAL_IP}:{PORT}/remote", "#d32f2f")
        self.create_clickable_link(info_frame, "🕹️ 一體機 (單機用)", f"http://{LOCAL_IP}:{PORT}/combo", "#9C27B0")
        self.create_clickable_link(info_frame, "⚙️ 管理端 (加歌用)", f"http://{LOCAL_IP}:{PORT}/admin", "#F57C00")

        stat_frame = tk.Frame(self, bg="#f4f4f9")
        stat_frame.pack(fill="x", padx=20, pady=20)
        
        self.lbl_count = tk.Label(stat_frame, text="總歌曲數: 載入中...", font=("Microsoft JhengHei", 12, "bold"), bg="#f4f4f9")
        self.lbl_count.pack(anchor="w")
        
        self.lbl_size = tk.Label(stat_frame, text="佔用空間: 載入中...", font=("Microsoft JhengHei", 12, "bold"), bg="#f4f4f9")
        self.lbl_size.pack(anchor="w", pady=5)
        
        self.update_stats()

    def create_clickable_link(self, parent, text_prefix, url, color):
        """建立可點擊的超連結 Label"""
        # 容器用來讓文字跟網址排在同一行
        frame = tk.Frame(parent, bg="white")
        frame.pack(pady=5, anchor="w", padx=10)
        
        tk.Label(frame, text=f"{text_prefix}: ", font=("Consolas", 11), bg="white").pack(side="left")
        
        link_lbl = tk.Label(frame, text=url, font=("Consolas", 11, "underline"), fg=color, bg="white", cursor="hand2")
        link_lbl.pack(side="left")
        
        # 綁定點擊事件，使用 webbrowser 開啟
        link_lbl.bind("<Button-1>", lambda e, u=url: webbrowser.open(u))

    def update_stats(self):
        try:
            songs = [f for f in os.listdir(SONGS_DIR) if f.endswith('.mp4')]
            count = len(songs)
            total_size = sum(os.path.getsize(os.path.join(SONGS_DIR, f)) for f in songs)
            size_mb = total_size / (1024 * 1024)
            
            self.lbl_count.config(text=f"🎵 總歌曲數: {count} 首")
            self.lbl_size.config(text=f"💾 佔用空間: {size_mb:.2f} MB")
        except Exception as e:
            pass
        self.after(5000, self.update_stats)

if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()

    if shutil.which("ffmpeg") is None:
        try:
            messagebox.showerror("錯誤", "找不到 FFmpeg\n請將 ffmpeg 資料夾放在程式同一目錄")
        except:
            print("找不到 FFmpeg")
    else:
        t = threading.Thread(target=run_server_thread)
        t.daemon = True
        t.start()
        
        app = ServerApp()
        app.mainloop()