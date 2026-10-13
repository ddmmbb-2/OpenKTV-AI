//! 非同步 mpv IPC 播放器。所有 IPC 請求都在背景執行，不阻塞 egui 介面。
//! 每次切歌等待 mpv 的 file-loaded 事件，再檢查真實音軌並切換。
use crate::library::Song;
use anyhow::{anyhow, bail, Context, Result};
use serde_json::{json, Value};
use std::{
    io::{BufRead, BufReader, Read, Write},
    path::{Path, PathBuf},
    process::{Child, Command},
    sync::mpsc::{self, Receiver, RecvTimeoutError, Sender},
    thread,
    time::{Duration, Instant},
};

#[derive(Clone, Debug)]
pub struct PlaybackInfo {
    pub loaded: bool,
    pub paused: bool,
    pub position: f64,
    pub duration: f64,
    pub volume: u32,
    pub original: Option<bool>,
    pub audio_tracks: usize,
    /// mpv 已確認套用的半音數；0 = 原 KEY。
    pub key_semitones: i8,
    pub mode_message: String,
}
impl Default for PlaybackInfo {
    fn default() -> Self {
        Self { loaded: false, paused: false, position: 0.0, duration: 0.0,
            volume: 75, original: None, audio_tracks: 0, key_semitones: 0, mode_message: "尚未播放".into() }
    }
}

pub enum PlayerEvent {
    State(PlaybackInfo),
    Notice(String),
    Error(String),
    Finished,
    PlaybackFailed(String),
    /// 失敗時 UI 才回滾，不把尚未確認的移調顯示成成功。
    KeyRejected { requested: i8, message: String },
}

enum Action {
    Play { path: PathBuf, format: String, original: bool, video_window: Option<u64> },
    Mode(bool),
    Key(i8),
    TogglePause,
    Stop,
    Seek(f64),
    Volume(u32),
    Shutdown,
}

enum MpvSignal { Loaded, EndOfFile }

struct Active {
    path: PathBuf,
    format: String,
    requested_original: bool,
    requested_key: i8,
    loaded: bool,
}

pub struct Player {
    tx: Sender<Action>,
    rx: Receiver<PlayerEvent>,
    thread: Option<thread::JoinHandle<()>>,
    pub video_window: Option<u64>,
}

impl Player {
    pub fn new(tools: &Path) -> Self {
        let binary = tools.join(if cfg!(windows) { "mpv.exe" } else { "mpv" });
        let executable = if binary.exists() { binary } else { PathBuf::from("mpv") };
        #[cfg(windows)]
        let ipc = format!(r"\\.\pipe\openktv-{}", std::process::id());
        #[cfg(not(windows))]
        let ipc = format!("/tmp/openktv-{}.sock", std::process::id());
        let (tx, commands) = mpsc::channel();
        let (events, rx) = mpsc::channel();
        let thread = thread::spawn(move || controller(commands, events, executable, ipc));
        Self { tx, rx, thread: Some(thread), video_window: None }
    }
    fn send(&self, action: Action) -> Result<()> {
        self.tx.send(action).map_err(|_| anyhow!("播放器控制執行緒已停止"))
    }
    pub fn play(&self, song: &Song, data_dir: &Path, original: bool) -> Result<()> {
        let path = PathBuf::from(&song.path);
        let path = if path.is_absolute() { path } else { data_dir.join(path) };
        if !path.is_file() { bail!("歌曲檔案不存在：{}", path.display()); }
        #[cfg(windows)]
        if self.video_window.is_none() { bail!("內嵌影片視窗尚未建立；請先開啟點歌台"); }
        self.send(Action::Play { path, format: song.format.clone(), original, video_window: self.video_window })
    }
    pub fn set_vocal(&self, original: bool) -> Result<()> { self.send(Action::Mode(original)) }
    pub fn set_key(&self, semitones: i8) -> Result<()> {
        if !(-6..=6).contains(&semitones) { bail!("KEY 必須介於 -6 與 +6 半音"); }
        self.send(Action::Key(semitones))
    }
    pub fn pause(&self) -> Result<()> { self.send(Action::TogglePause) }
    pub fn stop(&self) -> Result<()> { self.send(Action::Stop) }
    pub fn seek(&self, seconds: f64) -> Result<()> { self.send(Action::Seek(seconds)) }
    pub fn volume(&self, value: u32) -> Result<()> { self.send(Action::Volume(value.min(100))) }
    pub fn events(&self) -> Vec<PlayerEvent> { self.rx.try_iter().collect() }
}
impl Drop for Player {
    fn drop(&mut self) {
        let _ = self.tx.send(Action::Shutdown);
        // 不 join：若 Windows mpv IPC 卡住，關閉 GUI 也不能被無限阻塞。
        self.thread.take();
    }
}

fn connect(ipc: &str) -> std::io::Result<Box<dyn Read + Send>> {
    #[cfg(windows)] {
        let pipe = std::fs::OpenOptions::new().read(true).write(true).open(ipc)?;
        Ok(Box::new(pipe))
    }
    #[cfg(unix)] {
        let stream = std::os::unix::net::UnixStream::connect(ipc)?;
        Ok(Box::new(stream))
    }
}

// requests 使用獨立 IPC connection，因此 listener 不會吃掉 request_id 回應。
// 請求在背景執行緒完成，並檢查 mpv 是否實際回傳 success。
fn request(ipc: &str, command: Value) -> Result<Value> {
    let mut packet = serde_json::to_vec(&json!({ "command": command, "request_id": 1 }))?;
    packet.push(b'\n');
    #[cfg(windows)]
    let mut connection = std::fs::OpenOptions::new().read(true).write(true).open(ipc)?;
    #[cfg(unix)]
    let mut connection = {
        let s = std::os::unix::net::UnixStream::connect(ipc)?;
        s.set_read_timeout(Some(Duration::from_secs(2)))?;
        s.set_write_timeout(Some(Duration::from_secs(2)))?;
        s
    };
    connection.write_all(&packet)?;
    let mut reader = BufReader::new(connection);
    loop {
        let mut line = String::new();
        if reader.read_line(&mut line)? == 0 { bail!("mpv IPC 已中斷"); }
        let response: Value = serde_json::from_str(&line)?;
        if response.get("request_id").and_then(Value::as_i64) != Some(1) { continue; }
        let error = response.get("error").and_then(Value::as_str).unwrap_or("unknown");
        if error != "success" { bail!("mpv 回報：{error}；命令：{command}"); }
        return Ok(response.get("data").cloned().unwrap_or(Value::Null));
    }
}
fn property(ipc: &str, name: &str) -> Result<Value> {
    request(ipc, json!(["get_property", name]))
}

fn spawn_listener(ipc: String, signal_tx: Sender<MpvSignal>) -> Result<()> {
    let (ready_tx, ready_rx) = mpsc::sync_channel(1);
    thread::spawn(move || {
        let connected = (0..30).find_map(|_| match connect(&ipc) {
            Ok(stream) => Some(stream),
            Err(_) => { thread::sleep(Duration::from_millis(50)); None }
        });
        let _ = ready_tx.send(connected.is_some());
        if let Some(stream) = connected {
            for line in BufReader::new(stream).lines() {
                let Ok(line) = line else { break };
                let Ok(ev) = serde_json::from_str::<Value>(&line) else { continue };
                match ev.get("event").and_then(Value::as_str) {
                    Some("file-loaded") => { let _ = signal_tx.send(MpvSignal::Loaded); }
                    Some("end-file") if ev.get("reason").and_then(Value::as_str) == Some("eof") => {
                        let _ = signal_tx.send(MpvSignal::EndOfFile);
                    }
                    _ => {}
                }
            }
        }
    });
    match ready_rx.recv_timeout(Duration::from_secs(2)) {
        Ok(true) => Ok(()),
        _ => bail!("無法監聽 mpv 載入事件"),
    }
}

fn ensure_running(
    child: &mut Option<Child>, executable: &Path, ipc: &str, video_window: Option<u64>,
    signals: &Sender<MpvSignal>,
) -> Result<()> {
    if let Some(p) = child.as_mut() {
        if p.try_wait()?.is_none() { return Ok(()); }
    }
    child.take();
    let mut command = Command::new(executable);
    command.arg("--idle=yes").arg("--force-window=yes")
        .arg("--keep-open=no").arg("--terminal=no").arg("--really-quiet")
        .arg(format!("--input-ipc-server={ipc}"));
    if let Some(window) = video_window {
        command.arg(format!("--wid={window}"));
        command.arg("--osc=no");
        command.arg("--input-default-bindings=no");
    }
    let started = command.spawn().with_context(|| format!("找不到 mpv 播放器：{}", executable.display()))?;
    *child = Some(started);
    let mut ok = false;
    for _ in 0..40 {
        if property(ipc, "idle-active").is_ok() { ok = true; break; }
        thread::sleep(Duration::from_millis(80));
    }
    if !ok { bail!("mpv 已啟動，但 IPC 無法連線。確認 tools/mpv.exe 是否正常"); }
    spawn_listener(ipc.to_string(), signals.clone())?;
    request(ipc, json!(["set_property", "volume", 75]))?;
    Ok(())
}

#[derive(Clone, Debug)]
struct ModeReport { original: bool, count: usize, description: String }

fn audio_ids(tracks: &Value) -> Vec<(i64, String)> {
    tracks.as_array().into_iter().flatten()
        .filter(|t| t.get("type").and_then(Value::as_str) == Some("audio"))
        .filter_map(|t| {
            Some((t.get("id")?.as_i64()?, t.get("title").and_then(Value::as_str).unwrap_or("").to_ascii_lowercase()))
        }).collect()
}

fn choose_dual_tracks(ids: &[(i64, String)]) -> Result<(i64, i64)> {
    if ids.len() < 2 { bail!("檔案只偵測到 {} 條音軌，無法切換伴奏；請重新使用 AI 去人聲製作雙音軌", ids.len()); }
    let original = ids.iter().find(|(_, t)| t.contains("original") || t.contains("原唱"))
        .map(|(id, _)| *id).unwrap_or(ids[0].0);
    let instrumental = ids.iter().find(|(_, t)| t.contains("instrumental") || t.contains("no_vocals") || t.contains("karaoke") || t.contains("伴奏"))
        .map(|(id, _)| *id).unwrap_or_else(|| ids.iter().find(|(id, _)| *id != original).map(|(id, _)| *id).unwrap_or(original));
    if original == instrumental { bail!("無法區分原唱與伴奏音軌"); }
    Ok((original, instrumental))
}

fn apply_mode(ipc: &str, format: &str, original: bool) -> Result<ModeReport> {
    let tracks = property(ipc, "track-list")?;
    let ids = audio_ids(&tracks);
    match format {
        "dual" => {
            let (vocal, karaoke) = choose_dual_tracks(&ids)?;
            let target = if original { vocal } else { karaoke };
            request(ipc, json!(["change-list", "af", "clr", ""]))?;
            request(ipc, json!(["set_property", "aid", target]))?;
            let actual = property(ipc, "aid")?;
            if actual.as_i64() != Some(target) {
                bail!("音軌未切換成功，預期 #{target}、實際回傳 {actual}");
            }
            Ok(ModeReport { original, count: ids.len(), description:
                format!("已即時切換為{} · 音軌 #{target}", if original { "原唱" } else { "伴奏" }) })
        }
        "legacy" => {
            if ids.is_empty() { bail!("檔案未偵測到音訊"); }
            // OpenKTV 傳統聲道：左=原唱，右=伴奏。
            let pan = if original { "lavfi=[pan=stereo|c0=c0|c1=c0]" }
                       else { "lavfi=[pan=stereo|c0=c1|c1=c1]" };
            request(ipc, json!(["af", "set", pan]))?;
            Ok(ModeReport { original, count: ids.len(), description:
                format!("已即時切換為{} · 左右聲道", if original { "原唱" } else { "伴奏" }) })
        }
        _ => {
            request(ipc, json!(["change-list", "af", "clr", ""]))?;
            Ok(ModeReport { original: true, count: ids.len(), description:
                "單音軌來源：只有原始聲音，沒有獨立伴奏".into() })
        }
    }
}

/// mpv 原生 pitch 屬性：以倍率表示音高，不改播放速度。
/// 12 半音 = 2 倍頻率，所以每半音倍率 = 2^(1/12)。
fn pitch_factor(semitones: i8) -> f64 { 2.0_f64.powf(f64::from(semitones) / 12.0) }

fn apply_key(ipc: &str, semitones: i8) -> Result<()> {
    if !(-6..=6).contains(&semitones) { bail!("KEY 超出允許範圍"); }
    let ratio = pitch_factor(semitones);
    request(ipc, json!(["set_property", "pitch", ratio]))
        .context("mpv 無法設定音高，請確認 tools/mpv.exe 支援 pitch 屬性")?;
    // 只相信 mpv 確認後的實際值；不把送出指令當成操作成功。
    let actual = property(ipc, "pitch")?.as_f64()
        .ok_or_else(|| anyhow!("mpv 未回傳有效的 pitch 數值"))?;
    if (actual - ratio).abs() > 0.001 {
        bail!("KEY 設定未生效（預期倍率 {ratio:.5}，目前 {actual:.5}）");
    }
    Ok(())
}

fn report_key(ipc: &str, active: &Active, state: &mut PlaybackInfo, events: &Sender<PlayerEvent>) {
    match apply_key(ipc, active.requested_key) {
        Ok(()) => {
            state.key_semitones = active.requested_key;
            let _ = events.send(PlayerEvent::Notice(format!(
                "KEY {:+} 半音 · 已生效（播放速度不變）", active.requested_key)));
        }
        Err(e) => {
            let _ = events.send(PlayerEvent::KeyRejected {
                requested: active.requested_key,
                message: format!("KEY 調整失敗：{e:#}"),
            });
        }
    }
    let _ = events.send(PlayerEvent::State(state.clone()));
}

fn report_mode(ipc: &str, active: &Active, state: &mut PlaybackInfo, events: &Sender<PlayerEvent>) {
    match apply_mode(ipc, &active.format, active.requested_original) {
        Ok(report) => {
            state.original = Some(report.original);
            state.audio_tracks = report.count;
            state.mode_message = report.description.clone();
            let _ = events.send(PlayerEvent::Notice(report.description));
        }
        Err(err) => {
            // 切換失敗時保留上一個經過確認的模式，避免 UI 假顯示已切換。
            state.mode_message = format!("切換失敗：{err}");
            let _ = events.send(PlayerEvent::Error(state.mode_message.clone()));
        }
    }
    let _ = events.send(PlayerEvent::State(state.clone()));
}

fn refresh_snapshot(ipc: &str, state: &mut PlaybackInfo, events: &Sender<PlayerEvent>) {
    if let Ok(v) = property(ipc, "time-pos") { state.position = v.as_f64().unwrap_or(0.0).max(0.0); }
    if let Ok(v) = property(ipc, "duration") { state.duration = v.as_f64().unwrap_or(0.0).max(0.0); }
    if let Ok(v) = property(ipc, "pause") { state.paused = v.as_bool().unwrap_or(false); }
    if let Ok(v) = property(ipc, "volume") { state.volume = v.as_f64().unwrap_or(75.0).round().clamp(0.0, 100.0) as u32; }
    let _ = events.send(PlayerEvent::State(state.clone()));
}

fn controller(commands: Receiver<Action>, events: Sender<PlayerEvent>, binary: PathBuf, ipc: String) {
    let (signals_tx, signals_rx) = mpsc::channel();
    let mut child: Option<Child> = None;
    let mut current: Option<Active> = None;
    let mut state = PlaybackInfo::default();
    let mut last_snapshot = Instant::now();
    loop {
        while let Ok(signal) = signals_rx.try_recv() {
            match signal {
                MpvSignal::Loaded => {
                    if let Some(active) = current.as_mut() {
                        let path_is_current = property(&ipc, "path").ok()
                            .and_then(|p| p.as_str().map(PathBuf::from))
                            .map(|p| p == active.path || (p.canonicalize().ok().is_some() && p.canonicalize().ok() == active.path.canonicalize().ok()))
                            .unwrap_or(false);
                        if path_is_current {
                            active.loaded = true;
                            state.loaded = true;
                            state.position = 0.0;
                            state.duration = 0.0;
                            report_mode(&ipc, active, &mut state, &events);
                            report_key(&ipc, active, &mut state, &events);
                            last_snapshot = Instant::now() - Duration::from_secs(1);
                        }
                    }
                }
                MpvSignal::EndOfFile => {
                    if current.as_ref().map(|s| s.loaded).unwrap_or(false) {
                        current = None;
                        state = PlaybackInfo::default();
                        let _ = events.send(PlayerEvent::Finished);
                    }
                }
            }
        }
        match commands.recv_timeout(Duration::from_millis(25)) {
            Ok(Action::Play { path, format, original, video_window }) => {
                let result = ensure_running(&mut child, &binary, &ipc, video_window, &signals_tx)
                    .and_then(|_| {
                        // mpv 程序會沿用上一首的 pitch；先歸零再載入，避免前奏被意外移調。
                        // 若 mpv 版本不支援 pitch，不阻止一般播放；載入後會回報具體錯誤。
                        let _ = request(&ipc, json!(["set_property", "pitch", 1.0]));
                        request(&ipc, json!(["loadfile", path.to_string_lossy(), "replace"])).map(|_| ())
                    });
                match result {
                    Ok(()) => {
                        current = Some(Active { path, format, requested_original: original, requested_key: 0, loaded: false });
                        state = PlaybackInfo { mode_message: "音訊載入中，完成後自動設定原唱／伴奏".into(), ..PlaybackInfo::default() };
                        let _ = events.send(PlayerEvent::State(state.clone()));
                    }
                    Err(e) => {
                        current = None;
                        let _ = events.send(PlayerEvent::PlaybackFailed(format!("無法開始播放：{e:#}")));
                    }
                }
            }
            Ok(Action::Mode(original)) => {
                if let Some(active) = current.as_mut() {
                    if active.format == "single" {
                        let _ = events.send(PlayerEvent::Error("這首是單音軌，沒有可切換的伴奏；請使用 AI 製作雙音軌".into()));
                    } else {
                        active.requested_original = original;
                        if active.loaded {
                            report_mode(&ipc, active, &mut state, &events);
                            // 部分音軌類型切換會重新建立濾鏡鏈，再次確認 KEY 不會遺失。
                            if active.requested_key != 0 { report_key(&ipc, active, &mut state, &events); }
                        }
                        else { let _ = events.send(PlayerEvent::Notice("歌曲載入中，將於載入完成後切換".into())); }
                    }
                }
            }
            Ok(Action::Key(semitones)) => {
                if let Some(active) = current.as_mut() {
                    active.requested_key = semitones.clamp(-6, 6);
                    if active.loaded { report_key(&ipc, active, &mut state, &events); }
                    else { let _ = events.send(PlayerEvent::Notice(format!(
                        "KEY {:+} 半音：歌曲載入完成後套用", active.requested_key))); }
                }
            }
            Ok(Action::TogglePause) => {
                if current.is_some() {
                    match request(&ipc, json!(["cycle", "pause"])) {
                        Ok(_) => { state.paused = !state.paused; let _ = events.send(PlayerEvent::State(state.clone())); }
                        Err(e) => { let _ = events.send(PlayerEvent::Error(format!("暫停操作失敗：{e}"))); }
                    }
                }
            }
            Ok(Action::Stop) => {
                current = None;
                state = PlaybackInfo::default();
                if child.is_some() { let _ = request(&ipc, json!(["stop"])); }
                let _ = events.send(PlayerEvent::State(state.clone()));
            }
            Ok(Action::Seek(pos)) => {
                if state.loaded && state.duration > 0.0 {
                    let pos = pos.clamp(0.0, state.duration);
                    match request(&ipc, json!(["seek", pos, "absolute", "exact"])) {
                        Ok(_) => { state.position = pos; let _ = events.send(PlayerEvent::State(state.clone())); }
                        Err(e) => { let _ = events.send(PlayerEvent::Error(format!("快轉失敗：{e}"))); }
                    }
                }
            }
            Ok(Action::Volume(volume)) => {
                state.volume = volume;
                if child.is_some() {
                    if let Err(e) = request(&ipc, json!(["set_property", "volume", volume])) {
                        let _ = events.send(PlayerEvent::Error(format!("音量設定失敗：{e}")));
                    }
                }
                let _ = events.send(PlayerEvent::State(state.clone()));
            }
            Ok(Action::Shutdown) | Err(RecvTimeoutError::Disconnected) => break,
            Err(RecvTimeoutError::Timeout) => {}
        }
        if state.loaded && last_snapshot.elapsed() >= Duration::from_millis(350) {
            refresh_snapshot(&ipc, &mut state, &events);
            last_snapshot = Instant::now();
        }
        if let Some(p) = child.as_mut() {
            if let Ok(Some(exit)) = p.try_wait() {
                if current.take().is_some() {
                    state = PlaybackInfo::default();
                    let _ = events.send(PlayerEvent::PlaybackFailed(format!("mpv 播放視窗已結束（{exit}）")));
                }
                child = None;
            }
        }
    }
    if let Some(mut p) = child.take() { let _ = p.kill(); let _ = p.wait(); }
    #[cfg(unix)] let _ = std::fs::remove_file(&ipc);
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn supports_change_list_filter_clear() {
        let cmd = json!(["change-list", "af", "clr", ""]);
        assert_eq!(cmd.as_array().unwrap().len(), 4);
    }
    #[test]
    fn twelve_tone_equal_temperament_pitch_factors() {
        assert!((pitch_factor(0) - 1.0).abs() < 1e-12);
        assert!((pitch_factor(6) - 2.0_f64.sqrt()).abs() < 1e-12);
        assert!((pitch_factor(-6) - 1.0 / 2.0_f64.sqrt()).abs() < 1e-12);
        assert!((pitch_factor(1) - 1.059463094359).abs() < 1e-9);
    }
    #[test]
    fn mpv_pitch_ipc_command_has_correct_shape() {
        let command = json!(["set_property", "pitch", pitch_factor(3)]);
        assert_eq!(command[0], "set_property");
        assert_eq!(command[1], "pitch");
        assert!((command[2].as_f64().unwrap() - 2f64.powf(0.25)).abs() < 1e-12);
    }
    #[test]
    fn detects_real_mpv_track_ids() {
        let list = json!([
            {"type":"video","id":1},
            {"type":"audio","id":3,"title":"Original"},
            {"type":"audio","id":5,"title":"Instrumental"}
        ]);
        let ids = audio_ids(&list);
        assert_eq!(choose_dual_tracks(&ids).unwrap(), (3, 5));
        assert!(choose_dual_tracks(&ids[..1]).is_err());
    }
}
