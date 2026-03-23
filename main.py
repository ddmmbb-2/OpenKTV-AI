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
from tkinter import messagebox
# ... 下面的 import 保留原樣 ...
import subprocess
import shutil
import threading
import socket
import json
import time
import webbrowser
from flask import Flask, render_template, request, send_from_directory
from flask_socketio import SocketIO, emit
from spleeter.separator import Separator
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

if os.path.exists(FFMPEG_DIR):
    os.environ["PATH"] += os.pathsep + FFMPEG_DIR
os.environ["PATH"] += os.pathsep + BASE_DIR

SONGS_DIR = os.path.join(BASE_DIR, "ktv_songs")
TEMP_BASE_DIR = os.path.join(BASE_DIR, "temp_processing") 

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
    # 用 print 就會自動被我們的 GUIWriter 抓走並顯示在介面上
    print(msg)
    socketio.emit('admin_log', {'msg': msg})

# ------------------------------------------
# Flask 路由
# ------------------------------------------
@app.route('/player')
def page_player(): return render_template('player.html')

@app.route('/remote')
def page_remote(): return render_template('remote.html')

@app.route('/admin')
def page_admin(): return render_template('admin.html')

@app.route('/combo')  
def page_combo(): return render_template('combo.html')

@app.route('/')
def page_index(): return render_template('remote.html')

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
    try:
        print("🚀 準備啟動 Flask 伺服器...")
        
        # 【關鍵防護】強制關閉 Flask 雞婆的啟動橫幅 (Banner) 與日誌，從根本拔除報錯源頭
        import logging
        from flask import cli
        cli.show_server_banner = lambda *args, **kwargs: None  # 暴力閹割橫幅印出功能
        logging.getLogger('werkzeug').setLevel(logging.ERROR)  # 只允許印出重大錯誤
        
        socketio.run(app, host='0.0.0.0', port=PORT, debug=False, allow_unsafe_werkzeug=True)
    except Exception as e:
        import traceback
        print(f"❌ 伺服器啟動失敗: {e}")
        print(traceback.format_exc())

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
            if job_temp_dir and os.path.exists(job_temp_dir):
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
        self.title("KTV 伺服器狀態")
        self.geometry("450x500") # 稍微拉高一點放日誌框
        self.configure(bg="#f4f4f9")
        
        tk.Label(self, text="🎤 KTV 系統運作中", font=("Microsoft JhengHei", 20, "bold"), fg="#4CAF50", bg="#f4f4f9").pack(pady=10)
        
        info_frame = tk.Frame(self, bg="white", bd=1, relief="solid")
        info_frame.pack(fill="x", padx=20, pady=5)
        
        self.create_clickable_link(info_frame, "📺 播放端 (電視用)", f"http://{LOCAL_IP}:{PORT}/player", "blue")
        self.create_clickable_link(info_frame, "📱 遙控端 (手機用)", f"http://{LOCAL_IP}:{PORT}/remote", "#d32f2f")
        self.create_clickable_link(info_frame, "🕹️ 一體機 (單機用)", f"http://{LOCAL_IP}:{PORT}/combo", "#9C27B0")
        self.create_clickable_link(info_frame, "⚙️ 管理端 (加歌用)", f"http://{LOCAL_IP}:{PORT}/admin", "#F57C00")

        stat_frame = tk.Frame(self, bg="#f4f4f9")
        stat_frame.pack(fill="x", padx=20, pady=5)
        
        self.lbl_count = tk.Label(stat_frame, text="總歌曲數: 載入中...", font=("Microsoft JhengHei", 12, "bold"), bg="#f4f4f9")
        self.lbl_count.pack(anchor="w")
        
        self.lbl_size = tk.Label(stat_frame, text="佔用空間: 載入中...", font=("Microsoft JhengHei", 12, "bold"), bg="#f4f4f9")
        self.lbl_size.pack(anchor="w", pady=5)

        # 增加一個實體的 GUI 日誌框，用來接聽攔截到的錯誤訊息
        self.log_txt = tk.Text(self, height=8, state="disabled", bg="#222", fg="#0f0", font=("Consolas", 9))
        self.log_txt.pack(fill="both", expand=True, padx=20, pady=10)
        
        self.update_stats()
        
        # 啟動背景佇列監聽器
        self.check_log_queue()

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
            
            self.lbl_count.config(text=f"🎵 總歌曲數: {count} 首")
            self.lbl_size.config(text=f"💾 佔用空間: {size_mb:.2f} MB")
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

    if shutil.which("ffmpeg") is None and not os.path.exists(FFMPEG_DIR):
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
