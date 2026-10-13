mod audio;
mod fonts;
mod download;
mod library;
mod phone;
mod player;
mod video_host;

use crate::{
    audio::{AudioEngine, MicState},
    download::{spawn_download, JobMessage},
    library::{Library, Song},
    phone::{PhoneService, RemoteAction, RemoteSnapshot, SongBrief},
    player::{PlaybackInfo, Player, PlayerEvent},
    video_host::VideoHost,
};
use anyhow::{Context, Result};
use eframe::egui::{self, Color32, RichText};
use std::{
    collections::VecDeque,
    path::PathBuf,
    sync::{atomic::Ordering, mpsc::{channel, Receiver, Sender}},
    time::Duration,
};

const BG: Color32 = Color32::from_rgb(16, 21, 31);
const PANEL: Color32 = Color32::from_rgb(28, 36, 52);
const CYAN: Color32 = Color32::from_rgb(100, 216, 224);
const LIGHT: Color32 = Color32::from_rgb(230, 238, 247);
const MUTED: Color32 = Color32::from_rgb(147, 161, 182);
const GOLD: Color32 = Color32::from_rgb(243, 185, 93);

#[derive(PartialEq, Clone, Copy)]
enum Tab { Songs, Downloads, Phone, Settings }
#[derive(PartialEq, Clone, Copy)]
enum Filter { All, Dual, Legacy, Single }
#[derive(PartialEq, Clone, Copy)]
enum ImportFormat { Legacy, Dual, Single }
impl ImportFormat {
    fn db_value(self) -> &'static str {
        match self { Self::Legacy => "legacy", Self::Dual => "dual", Self::Single => "single" }
    }
}

enum QueueAction { Up(usize), Down(usize), Delete(usize), Play(usize) }

struct OpenKtv {
    root: PathBuf,
    library: Library,
    songs: Vec<Song>,
    queue: VecDeque<Song>,
    current: Option<Song>,
    tab: Tab,
    filter: Filter,
    search: String,
    download_url: String,
    download_title: String,
    download_ai: bool,
    import_format: ImportFormat,
    original: bool,
    key_requested: i8,
    info: PlaybackInfo,
    scrub_position: Option<f64>,
    job_in_progress: bool,
    player: Player,
    video_host: VideoHost,
    theater: bool,
    video_error: Option<String>,
    phone: Option<PhoneService>,
    phone_qr: Option<egui::TextureHandle>,
    setup_qr: Option<egui::TextureHandle>,
    mic: MicState,
    _audio: Option<AudioEngine>,
    mic_error: Option<String>,
    status: String,
    font_status: String,
    tx: Sender<JobMessage>,
    rx: Receiver<JobMessage>,
}

fn base_dir() -> Result<PathBuf> {
    let exe = std::env::current_exe().context("找不到執行檔位置")?;
    let exe_dir = exe.parent().context("找不到程式目錄")?.to_owned();
    if cfg!(debug_assertions) {
        if let Ok(cwd) = std::env::current_dir() {
            if cwd.join("Cargo.toml").exists() { return Ok(cwd); }
        }
    }
    Ok(exe_dir)
}

fn make_qr(ctx: &egui::Context, url: &str, name: &str) -> Option<egui::TextureHandle> {
    let qr = qrcode::QrCode::new(url.as_bytes()).ok()?;
    let cells = qr.width();
    let border = 4;
    let scale = 5;
    let side = (cells + border * 2) * scale;
    let mut pixels = vec![255u8; side * side * 4];
    for y in 0..cells {
        for x in 0..cells {
            if qr[(x, y)] == qrcode::Color::Dark {
                for dy in 0..scale { for dx in 0..scale {
                    let p = ((y + border) * scale + dy) * side * 4 + ((x + border) * scale + dx) * 4;
                    pixels[p] = 12; pixels[p+1] = 20; pixels[p+2] = 30;
                }}
            }
        }
    }
    let color = egui::ColorImage::from_rgba_unmultiplied([side, side], &pixels);
    Some(ctx.load_texture(name, color, egui::TextureOptions::NEAREST))
}

fn format_name(s: &str) -> &'static str {
    match s {
        "dual" => "雙音軌 · 可切原唱/伴奏",
        "legacy" => "左右聲道 · 可切換",
        _ => "單音軌 · 不可切伴奏",
    }
}
fn fmt_color(s: &str) -> Color32 {
    match s { "dual" => CYAN, "legacy" => GOLD, _ => MUTED }
}
fn clock(s: f64) -> String {
    if !s.is_finite() || s <= 0.0 { return "00:00".into(); }
    let n = s as u64;
    if n >= 3600 { format!("{:02}:{:02}:{:02}", n/3600, (n/60)%60, n%60) }
    else { format!("{:02}:{:02}", n/60, n%60) }
}

impl OpenKtv {
    fn new(ctx: &egui::Context) -> Result<Self> {
        ctx.set_visuals(egui::Visuals::dark());
        ctx.style_mut(|s| {
            s.spacing.item_spacing = egui::vec2(10.0, 9.0);
            s.spacing.button_padding = egui::vec2(13.0, 8.0);
            s.visuals.panel_fill = BG;
        });
        let root = base_dir()?;
        let font_status = match fonts::install_chinese_fonts(ctx, &root) {
            Ok(path) => format!("已載入中文字型：{}", path.display()),
            Err(e) => format!("中文字型載入失敗：{e}；請檢查 Windows 字型或 data/fonts/cjk.ttf"),
        };
        std::fs::create_dir_all(root.join("data/songs"))?;
        std::fs::create_dir_all(root.join("data/certs"))?;
        let library = Library::open(root.join("data/ktv.db"))?;
        let songs = library.all()?;
        let mic = MicState::default();
        let (audio, mic_error) = match AudioEngine::start(mic.clone()) {
            Ok(a) => (Some(a), None), Err(e) => (None, Some(e.to_string())),
        };
        let phone = phone::launch(mic.clone(), &root.join("data/certs"), library.clone());
        let (phone, phone_error) = match phone { Ok(p) => (Some(p), None), Err(e) => (None, Some(e.to_string())) };
        let phone_qr = phone.as_ref().and_then(|p| make_qr(ctx, &p.mic_url, "phone-qr"));
        let setup_qr = phone.as_ref().and_then(|p| make_qr(ctx, &p.setup_url, "setup-qr"));
        let (tx, rx) = channel();
        let player = Player::new(&root.join("tools"));
        let status = phone_error.map(|e| format!("HTTPS 未啟動：{e}"))
            .unwrap_or_else(|| "點選歌曲即可加入待播；沒有正在播放的歌曲時會直接開唱。".into());
        Ok(Self {
            root, library, songs, queue: VecDeque::new(), current: None,
            tab: Tab::Songs, filter: Filter::All, search: String::new(),
            download_url: String::new(), download_title: String::new(),
            download_ai: false, import_format: ImportFormat::Legacy,
            original: false, key_requested: 0, info: PlaybackInfo::default(), scrub_position: None,
            job_in_progress: false, player, video_host: VideoHost::default(), theater: false,
            video_error: None, phone, phone_qr, setup_qr, mic,
            _audio: audio, mic_error, status, font_status, tx, rx,
        })
    }

    fn refresh(&mut self) {
        match self.library.all() {
            Ok(v) => self.songs = v,
            Err(e) => self.status = format!("曲庫讀取失敗：{e}"),
        }
    }
    fn play(&mut self, song: Song) -> bool {
        // 切歌時沿用目前選擇的模式，但單音軌直接播放原始聲音。
        self.scrub_position = None;
        match self.player.play(&song, &self.root.join("data"), self.original) {
            Ok(()) => {
                self.key_requested = 0; // 每首歌從原 KEY 開始，不繼承上一首。
                self.info = PlaybackInfo { mode_message: "載入中…".into(), ..PlaybackInfo::default() };
                self.status = format!("正在開啟：{}", song.title);
                self.current = Some(song);
                true
            }
            Err(e) => { self.status = format!("播放失敗：{e:#}"); false }
        }
    }
    fn add_song(&mut self, song: Song) {
        if self.current.is_none() { self.play(song); }
        else { self.status = format!("已加入待播：{}", song.title); self.queue.push_back(song); }
    }
    fn next_song(&mut self) {
        if let Some(song) = self.queue.pop_front() { self.play(song); }
        else {
            let _ = self.player.stop();
            self.current = None;
            self.key_requested = 0;
            self.info = PlaybackInfo::default();
            self.status = "待播清單已唱完".into();
        }
    }
    fn stop(&mut self) {
        if let Err(e) = self.player.stop() { self.status = format!("停止失敗：{e}"); }
        else {
            self.current = None;
            self.key_requested = 0;
            self.info = PlaybackInfo::default();
            self.status = "播放已停止，待播清單仍保留".into();
        }
    }
    fn change_vocal(&mut self, original: bool) {
        let Some(song) = self.current.as_ref() else { return };
        if song.format == "single" {
            self.status = "此歌曲只有單音軌：請先使用 AI 去人聲製作伴奏。".into();
            return;
        }
        match self.player.set_vocal(original) {
            Ok(()) => { self.original = original; self.status = "正在切換音訊，等待 mpv 確認…".into(); }
            Err(e) => self.status = format!("切換失敗：{e}"),
        }
    }
    fn change_key(&mut self, target: i8) {
        if self.current.is_none() { return; }
        let target = target.clamp(-6, 6);
        if target == self.key_requested { return; }
        match self.player.set_key(target) {
            Ok(()) => {
                // 使用者快速連點時，以下次「要求的 KEY」為基準，不會掉操作。
                self.key_requested = target;
                self.status = format!("KEY {target:+} 半音，等待 mpv 確認…");
            }
            Err(e) => self.status = format!("KEY 操作失敗：{e}"),
        }
    }
    /// Commands from authenticated phones are executed on egui's GUI thread.
    /// This is the same playback/queue state used by the Windows buttons.
    fn poll_remote_commands(&mut self) {
        let mut pending = Vec::new();
        if let Some(phone) = self.phone.as_mut() {
            while let Ok(request) = phone.remote_rx.try_recv() { pending.push(request); }
        }
        for request in pending {
            let outcome: std::result::Result<(), String> = match request.action {
                RemoteAction::AddSong { id } => {
                    match self.songs.iter().find(|s| s.id == id).cloned() {
                        Some(song) => {
                            if self.current.is_none() {
                                if self.play(song) { self.tab = Tab::Songs; Ok(()) } else { Err(self.status.clone()) }
                            } else { self.add_song(song); Ok(()) }
                        }
                        None => Err("曲庫已找不到這首歌，請重新整理手機曲庫".into()),
                    }
                }
                RemoteAction::PlayNow { id } => {
                    match self.songs.iter().find(|s| s.id == id).cloned() {
                        Some(song) => if self.play(song) { self.tab = Tab::Songs; Ok(()) } else { Err(self.status.clone()) },
                        None => Err("曲庫已找不到這首歌，請重新整理手機曲庫".into()),
                    }
                }
                RemoteAction::Next => {
                    if self.current.is_none() && self.queue.is_empty() {
                        Err("目前沒有歌曲可以切換".into())
                    } else { self.next_song(); self.tab = Tab::Songs; Ok(()) }
                }
                RemoteAction::SetKey { value } => {
                    if self.current.is_none() { Err("請先點歌，才能升降 KEY".into()) }
                    else if !(-6..=6).contains(&value) { Err("KEY 僅支援 -6 到 +6 半音".into()) }
                    else {
                        self.change_key(value);
                        if self.key_requested == value { Ok(()) } else { Err(self.status.clone()) }
                    }
                }
            };
            self.publish_remote_state();
            let _ = request.reply.send(outcome);
        }
    }

    fn publish_remote_state(&self) {
        if let Some(phone) = self.phone.as_ref() {
            phone.publish(RemoteSnapshot {
                current: self.current.as_ref().map(SongBrief::from),
                queue: self.queue.iter().map(SongBrief::from).collect(),
                key_requested: self.key_requested,
                key_confirmed: self.info.key_semitones,
                key_pending: self.key_requested != self.info.key_semitones,
                loaded: self.info.loaded,
                paused: self.info.paused,
                status: self.status.clone(),
            });
        }
    }

    fn import_file(&mut self) {
        let path = rfd::FileDialog::new().add_filter("影音檔案", &["mp4", "mkv", "webm"]).pick_file();
        let Some(path) = path else { return };
        let Some(name) = path.file_stem().map(|s| s.to_string_lossy().to_string()) else { return };
        let Some(ext) = path.extension().map(|s| s.to_string_lossy().to_ascii_lowercase()) else { return };
        let rel = format!("songs/{}.{}", uuid::Uuid::new_v4().simple(), ext);
        let target = self.root.join("data").join(&rel);
        match std::fs::copy(&path, &target) {
            Ok(_) => match self.library.insert(&name, &rel, self.import_format.db_value()) {
                Ok(_) => { self.refresh(); self.status = format!("已匯入：{name}"); }
                Err(e) => { let _ = std::fs::remove_file(&target); self.status = format!("寫入曲庫失敗：{e}"); }
            },
            Err(e) => self.status = format!("匯入失敗：{e}"),
        }
    }
    fn poll_jobs(&mut self) {
        for event in self.player.events() {
            match event {
                PlayerEvent::State(s) => self.info = s,
                PlayerEvent::KeyRejected { requested, message } => {
                    // 舊請求即使失敗，也不能覆寫使用者稍後已送出的新 KEY。
                    if self.key_requested == requested { self.key_requested = self.info.key_semitones; }
                    self.status = message;
                }
                PlayerEvent::Notice(msg) => self.status = msg,
                PlayerEvent::Error(msg) => self.status = msg,
                PlayerEvent::Finished => self.next_song(),
                PlayerEvent::PlaybackFailed(msg) => {
                    self.status = msg; self.current = None; self.key_requested = 0; self.info = PlaybackInfo::default();
                }
            }
        }
        while let Ok(ev) = self.rx.try_recv() {
            match ev {
                JobMessage::Progress(s) => self.status = s,
                JobMessage::Finished(s) => {
                    self.job_in_progress = false;
                    self.status = format!("製作完成並加入曲庫：{}", s.title);
                    self.refresh();
                }
                JobMessage::Error(e) => { self.job_in_progress = false; self.status = format!("下載或 AI 處理失敗：{e}"); }
            }
        }
    }

    fn nav(&mut self, ui: &mut egui::Ui) {
        ui.horizontal(|ui| {
            ui.label(RichText::new("◉ OpenKTV").size(24.0).color(CYAN).strong());
            ui.weak("RUST  ·  本機 KTV 控制台");
            ui.add_space(28.0);
            ui.selectable_value(&mut self.tab, Tab::Songs, "♫  點歌台");
            ui.selectable_value(&mut self.tab, Tab::Downloads, "↓  下載 / AI 製作");
            ui.selectable_value(&mut self.tab, Tab::Phone, "♬  手機 MIC");
            ui.selectable_value(&mut self.tab, Tab::Settings, "⚙  設定");
        });
    }

    fn song_tab(&mut self, ui: &mut egui::Ui) {
        ui.horizontal(|ui| {
            ui.heading(RichText::new("我的歌庫").color(LIGHT));
            ui.label(RichText::new(format!("{} 首", self.songs.len())).color(MUTED));
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if ui.button("↻ 更新").clicked() { self.refresh(); }
                if ui.button("＋ 匯入影音").clicked() { self.import_file(); }
            });
        });
        ui.add(egui::TextEdit::singleline(&mut self.search)
            .hint_text("🔎  輸入歌名、歌手或關鍵字…")
            .desired_width(f32::INFINITY));
        ui.horizontal_wrapped(|ui| {
            ui.selectable_value(&mut self.filter, Filter::All, "全部");
            ui.selectable_value(&mut self.filter, Filter::Dual, "AI 雙音軌");
            ui.selectable_value(&mut self.filter, Filter::Legacy, "舊版左右聲道");
            ui.selectable_value(&mut self.filter, Filter::Single, "一般影音");
        });
        ui.collapsing("匯入檔案類型（依實際音軌選擇）", |ui| {
            ui.horizontal_wrapped(|ui| {
                ui.radio_value(&mut self.import_format, ImportFormat::Legacy, "舊版左右聲道");
                ui.radio_value(&mut self.import_format, ImportFormat::Dual, "原唱 + 伴奏雙音軌");
                ui.radio_value(&mut self.import_format, ImportFormat::Single, "一般影音");
            });
            ui.small("注意：選雙音軌不會替影片產生伴奏。影片需已包含兩條不同的音訊串流。");
        });
        ui.separator();
        let needle = self.search.trim().to_lowercase();
        let shown: Vec<Song> = self.songs.iter()
            .filter(|s| s.title.to_lowercase().contains(&needle))
            .filter(|s| match self.filter {
                Filter::All => true,
                Filter::Dual => s.format == "dual",
                Filter::Legacy => s.format == "legacy",
                Filter::Single => s.format == "single",
            }).cloned().collect();
        ui.label(RichText::new(format!("符合 {} 首  ·  點歌後加入待播，空閒時直接播放", shown.len())).color(MUTED));
        egui::ScrollArea::vertical().auto_shrink([false, false]).show_rows(ui, 67.0, shown.len(), |ui, row_range| {
            for i in row_range {
                let song = &shown[i];
                egui::Frame::group(ui.style()).fill(PANEL).show(ui, |ui| {
                    ui.set_min_width(ui.available_width());
                    ui.horizontal(|ui| {
                        ui.vertical(|ui| {
                            ui.label(RichText::new(&song.title).size(16.0).color(LIGHT).strong());
                            ui.label(RichText::new(format_name(&song.format)).size(12.0).color(fmt_color(&song.format)));
                        });
                        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                            if ui.button("▶ 立刻唱").clicked() { self.play(song.clone()); }
                            if ui.button("＋ 點歌").clicked() { self.add_song(song.clone()); }
                        });
                    });
                });
            }
            if shown.is_empty() { ui.label("找不到歌曲。可匯入本機影音或用 YouTube 製作。"); }
        });
    }

    fn set_theater(&mut self, ctx: &egui::Context, enabled: bool) {
        self.theater = enabled;
        ctx.send_viewport_cmd(egui::ViewportCommand::Fullscreen(enabled));
    }

    /// 中央原生影片區；與點歌視窗共用 HWND，絕不另開 mpv 獨立視窗。
    fn video_panel(&mut self, ui: &mut egui::Ui) {
        if !self.theater {
            ui.horizontal(|ui| {
                ui.heading(RichText::new("KTV 影片畫面").color(LIGHT));
                ui.weak("內嵌 mpv · 跟隨視窗縮放");
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    if ui.button("⛶ 全螢幕").clicked() { self.set_theater(ui.ctx(), true); }
                });
            });
            ui.separator();
        }
        let available = ui.available_size();
        // mpv --wid 會在此視窗內維持影片比例，左右或上下黑邊由 mpv 自行補足。
        let size = egui::vec2(available.x.max(1.0), available.y.max(1.0));
        let (rect, _) = ui.allocate_exact_size(size, egui::Sense::hover());
        ui.painter().rect_filled(rect, 4.0, egui::Color32::BLACK);
        if self.current.is_none() {
            ui.painter().text(rect.center(), egui::Align2::CENTER_CENTER,
                "♫\n\n從左側歌庫點選「立刻唱」\n影片就會顯示在這裡",
                egui::FontId::proportional(23.0), MUTED);
        }
        // 首次只建立隱藏容器，即使尚未播放也能取得 --wid 的 HWND。
        if let Err(error) = self.video_host.present(ui.ctx(), rect, self.current.is_some()) {
            self.video_error = Some(error.to_string());
        } else {
            self.video_error = None;
        }
        self.player.video_window = self.video_host.window_id();
        if let Some(err) = &self.video_error {
            ui.painter().text(rect.center_top() + egui::vec2(0.0, 45.0), egui::Align2::CENTER_CENTER,
                format!("影片容器異常：{err}"), egui::FontId::proportional(16.0), GOLD);
        }
    }

    fn queue_panel(&mut self, ui: &mut egui::Ui) {
        ui.horizontal(|ui| {
            ui.heading(RichText::new("已點歌單").color(LIGHT));
            ui.label(RichText::new(format!("{} 首待播", self.queue.len())).color(CYAN));
        });
        if let Some(song) = &self.current {
            egui::Frame::group(ui.style()).fill(PANEL).show(ui, |ui| {
                ui.label(RichText::new("●  NOW PLAYING").color(CYAN).size(12.0));
                ui.label(RichText::new(&song.title).size(17.0).color(LIGHT).strong());
                ui.small(format_name(&song.format));
            });
        } else {
            ui.label(RichText::new("目前沒有歌曲播放").color(MUTED));
        }
        ui.separator();
        if !self.queue.is_empty() {
            ui.horizontal(|ui| {
                if ui.button("▶ 從第一首開始").clicked() { self.next_song(); }
                if ui.button("清空待播").clicked() { self.queue.clear(); }
            });
        }
        let mut action: Option<QueueAction> = None;
        egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| {
            if self.queue.is_empty() { ui.weak("尚無待播歌曲。從左側歌庫按『＋ 點歌』加入。"); }
            for (i, song) in self.queue.iter().enumerate() {
                egui::Frame::group(ui.style()).fill(PANEL).show(ui, |ui| {
                    ui.set_min_width(ui.available_width());
                    ui.label(RichText::new(format!("{:02}   {}", i+1, song.title)).color(LIGHT));
                    ui.horizontal(|ui| {
                        if ui.small_button("▶ 插播").clicked() { action = Some(QueueAction::Play(i)); }
                        if ui.add_enabled(i > 0, egui::Button::new("↑")).clicked() { action = Some(QueueAction::Up(i)); }
                        if ui.add_enabled(i+1 < self.queue.len(), egui::Button::new("↓")).clicked() { action = Some(QueueAction::Down(i)); }
                        if ui.small_button("× 移除").clicked() { action = Some(QueueAction::Delete(i)); }
                    });
                });
            }
        });
        match action {
            Some(QueueAction::Up(i)) => self.queue.swap(i, i-1),
            Some(QueueAction::Down(i)) => self.queue.swap(i, i+1),
            Some(QueueAction::Delete(i)) => { self.queue.remove(i); },
            Some(QueueAction::Play(i)) => { if let Some(song) = self.queue.remove(i) { self.play(song); } },
            None => {}
        }
    }

    fn player_bar(&mut self, ui: &mut egui::Ui) {
        ui.horizontal(|ui| {
            ui.label(RichText::new("♫  播放控制").size(16.0).strong().color(CYAN));
            ui.label(RichText::new(self.current.as_ref().map(|s| s.title.as_str()).unwrap_or("沒有正在播放的歌曲"))
                .size(15.0).color(LIGHT));
            if let Some(song) = &self.current { ui.colored_label(fmt_color(&song.format), format_name(&song.format)); }
        });
        ui.horizontal_wrapped(|ui| {
            if ui.add_enabled(self.current.is_some(), egui::Button::new(if self.info.paused { "▶ 繼續" } else { "⏸ 暫停" })).clicked() {
                if let Err(e) = self.player.pause() { self.status = e.to_string(); }
            }
            if ui.add_enabled(self.current.is_some() || !self.queue.is_empty(), egui::Button::new("⏭ 下一首")).clicked() { self.next_song(); }
            if ui.add_enabled(self.current.is_some(), egui::Button::new("■ 停止")).clicked() { self.stop(); }
            ui.separator();
            let supports_switch = self.current.as_ref().map(|s| s.format != "single").unwrap_or(false);
            let selected_original = self.info.original.unwrap_or(self.original);
            if ui.add_enabled(supports_switch, egui::Button::new(if selected_original { "● 原唱" } else { "○ 原唱" })).clicked() {
                self.change_vocal(true);
            }
            if ui.add_enabled(supports_switch, egui::Button::new(if !selected_original { "● 伴奏" } else { "○ 伴奏" })).clicked() {
                self.change_vocal(false);
            }
            ui.label(RichText::new(&self.info.mode_message).color(if self.info.original.is_some() { CYAN } else { GOLD }).size(12.0));
        });
        ui.horizontal_wrapped(|ui| {
            ui.label(RichText::new("♯ 升降 KEY").color(CYAN).strong());
            let playable = self.current.is_some();
            if ui.add_enabled(playable && self.key_requested > -6, egui::Button::new("− 降 KEY")).clicked() {
                self.change_key(self.key_requested - 1);
            }
            let pending = self.key_requested != self.info.key_semitones;
            let label = format!("KEY {:+}", self.key_requested);
            ui.label(RichText::new(label).size(17.0).strong().monospace()
                .color(if pending { GOLD } else { LIGHT }));
            if ui.add_enabled(playable && self.key_requested != 0, egui::Button::new("原 KEY (0)")).clicked() {
                self.change_key(0);
            }
            if ui.add_enabled(playable && self.key_requested < 6, egui::Button::new("＋ 升 KEY")).clicked() {
                self.change_key(self.key_requested + 1);
            }
            ui.weak(if pending { "套用中…" } else { "每格 1 半音 · 範圍 −6～+6 · 速度不變" });
        });
        ui.horizontal(|ui| {
            let duration = self.info.duration.max(0.0);
            let mut position = self.scrub_position.unwrap_or(self.info.position).clamp(0.0, duration);
            ui.label(RichText::new(clock(position)).color(LIGHT).monospace());
            let slider = ui.add_enabled(self.info.loaded && duration > 0.0,
                egui::Slider::new(&mut position, 0.0..=duration.max(1.0)).show_value(false));
            if slider.dragged() { self.scrub_position = Some(position); }
            if slider.drag_stopped() || (slider.changed() && !slider.dragged()) {
                self.scrub_position = None;
                if let Err(e) = self.player.seek(position) { self.status = format!("跳轉失敗：{e}"); }
            }
            ui.label(RichText::new(clock(duration)).color(MUTED).monospace());
            ui.add_space(8.0);
            ui.label("♪");
            let mut volume = self.info.volume;
            if ui.add(egui::Slider::new(&mut volume, 0..=100).text("音量")).changed() {
                self.info.volume = volume;
                if let Err(e) = self.player.volume(volume) { self.status = format!("音量設定失敗：{e}"); }
            }
        });
    }

    fn download_tab(&mut self, ui: &mut egui::Ui) {
        ui.heading("YouTube 下載與 AI KTV 製作");
        ui.label("貼上 YouTube 網址，將影片加入歌庫；需要可切換的伴奏時必須勾選 AI 去人聲。");
        ui.label("YouTube 網址");
        ui.add(egui::TextEdit::singleline(&mut self.download_url).hint_text("https://www.youtube.com/watch?v=...").desired_width(f32::INFINITY));
        ui.label("歌曲名稱（建議：歌手 - 歌名）");
        ui.add(egui::TextEdit::singleline(&mut self.download_title).desired_width(f32::INFINITY));
        ui.checkbox(&mut self.download_ai, "使用 Demucs 分離原唱／伴奏，製作雙音軌 MP4");
        if self.download_ai {
            ui.colored_label(GOLD, "需要可用的 Demucs 與 htdemucs 模型；模型固定下載至 tools/demucs-models；第一次使用需連線下載。");
        } else {
            ui.colored_label(GOLD, "目前未勾選 AI：下載結果只有原始音軌，切換『伴奏』不會移除人聲。");
        }
        if self.download_ai {
            let tools = self.root.join("tools");
            ui.label(if download::standalone_available(&tools) {
                "● 已找到可攜式 Demucs 執行器"
            } else {
                "○ 尚未安裝可攜式 Demucs：請參閱 scripts/建立可攜式Demucs.ps1"
            });
            ui.label(if download::cached_htdemucs(&tools) {
                "● htdemucs 模型已在 tools/demucs-models"
            } else {
                "○ 未找到本機模型：第一次 AI 製作將自動下載（也會嘗試匯入舊模型）"
            });
        }
        ui.add_enabled_ui(!self.job_in_progress, |ui| {
            if ui.button("↓ 開始下載 / 製作").clicked() {
                self.job_in_progress = true;
                self.status = "下載工作已開始…".into();
                spawn_download(self.download_url.clone(), self.download_title.clone(),
                    self.download_ai, self.root.clone(), self.library.clone(), self.tx.clone());
            }
        });
        if self.job_in_progress { ui.spinner(); ui.label("工作於背景執行，可繼續點歌與唱歌。"); }
        ui.separator();
        ui.weak("工具：tools/yt-dlp.exe、tools/ffmpeg.exe；AI：tools/demucs/demucs.exe 與 tools/demucs-models。請遵守著作權規範。");
    }

    fn phone_tab(&mut self, ui: &mut egui::Ui) {
        ui.heading("手機無線麥克風 ＋ 手機點歌台");
        ui.label("手機和電腦連接同一 Wi-Fi。掃描同一個配對 QR Code，即可在手機切換『點歌／MIC』。首次使用請先安裝並信任本機 CA。");
        if let Some(phone) = self.phone.as_ref() {
            ui.columns(2, |cols| {
                cols[0].heading("① 安裝本機憑證");
                if let Some(t) = &self.setup_qr { cols[0].image((t.id(), egui::vec2(168.0, 168.0))); }
                cols[0].small(phone.setup_url.as_str());
                if cols[0].button("複製憑證設定網址").clicked() { cols[0].ctx().copy_text(phone.setup_url.clone()); }
                cols[1].heading("② 手機點歌／MIC");
                if let Some(t) = &self.phone_qr { cols[1].image((t.id(), egui::vec2(168.0, 168.0))); }
                cols[1].small("HTTPS 8443 · 手機點歌、切歌、升降 KEY、MIC 共用");
                if cols[1].button("複製配對網址").clicked() { cols[1].ctx().copy_text(phone.mic_url.clone()); }
            });
            ui.collapsing("憑證與安全資訊", |ui| {
                ui.label("請先在電腦核對 CA SHA256 指紋再安裝：");
                ui.monospace(&phone.ca_fingerprint);
                ui.label(format!("憑證：{}", phone.ca_path.display()));
                ui.weak("不可分享 data/certs/openktv-ca.key 私鑰。");
            });
        } else { ui.colored_label(GOLD, "HTTPS 未啟動，請檢查 LAN、8080/8443 與 Windows 防火牆。"); }
        ui.separator();
        let connected = self.mic.connected.load(Ordering::SeqCst) > 0;
        ui.colored_label(if connected { CYAN } else { MUTED }, if connected { "● 手機已連線" } else { "○ 等待手機連線" });
        let mut muted = self.mic.muted.load(Ordering::Relaxed);
        if ui.checkbox(&mut muted, "MIC 靜音（預設啟用）").changed() { self.mic.muted.store(muted, Ordering::SeqCst); }
        let mut level = self.mic.level.load(Ordering::Relaxed);
        if ui.add(egui::Slider::new(&mut level, 0..=100).text("MIC 音量 %")).changed() { self.mic.level.store(level, Ordering::SeqCst); }
        let mut light = self.mic.light.load(Ordering::Relaxed);
        if ui.checkbox(&mut light, "Light 混響").changed() { self.mic.light.store(light, Ordering::SeqCst); }
        if let Some(e) = &self.mic_error { ui.colored_label(GOLD, format!("音訊引擎未啟動：{e}")); }
        ui.weak("手機點歌不用開啟 MIC 權限，可多支手機同時操作；MIC 仍限一支手機連線。手機頁面不會播放伴奏。");
        ui.weak("本版手機 MIC 尚無完整 AEC／防嘯叫／自動增益保證。請先小音量試唱，嘯叫時立即靜音。" );
    }

    fn settings_tab(&mut self, ui: &mut egui::Ui) {
        ui.heading("桌面播放器設定");
        ui.label(&self.font_status);
        ui.label("影片已整合到點歌台中央，可在點歌台按『⛶ 全螢幕』切換整個視窗。按 F11 也可切換。");
        ui.label(format!("主程式：{}", self.root.display()));
        ui.label(format!("歌庫：{}", self.root.join("data/ktv.db").display()));
        ui.label(format!("工具：{}", self.root.join("tools").display()));
        ui.label(format!("AI 模型：{}", download::model_home(&self.root.join("tools")).display()));
        ui.label(if download::standalone_available(&self.root.join("tools")) {
            "AI 執行器：已找到可攜式 Demucs EXE（不需要在目標電腦安裝 Python）"
        } else { "AI 執行器：尚未安裝可攜式 Demucs EXE" });
        ui.separator();
        ui.label("點歌控制是 Rust 原生 egui；影片透過 mpv --wid 與 Win32 子視窗嵌入同一主視窗。");
        ui.label("音軌使用 mpv IPC 實際讀取與確認；歌曲結束後自動接續待播下一首。");
        ui.label("升降 KEY：使用 mpv 內建 pitch 屬性，範圍 −6～+6 半音，保持播放速度；每首新歌恢復 KEY 0。");
        ui.label("手機點歌：HTTPS 8443 配對連線，和 Windows 點歌台共用歌曲、待播與 KEY 狀態。手機控制指令在 Rust GUI 執行緒處理。");
    }
}

impl eframe::App for OpenKtv {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        self.poll_jobs();
        self.poll_remote_commands();
        if (self.tab == Tab::Songs || self.theater) && ctx.input(|i| i.key_pressed(egui::Key::F11)) {
            self.set_theater(ctx, !self.theater);
        }
        // 非點歌頁先隱藏 Win32 子視窗，避免影片蓋到其他頁面。
        if self.tab != Tab::Songs { self.video_host.hide(); }
        egui::TopBottomPanel::top("header")
            .frame(egui::Frame::new().fill(PANEL).inner_margin(12.0))
            .show(ctx, |ui| {
                if self.theater {
                    ui.horizontal(|ui| {
                        ui.label(RichText::new("◉ OpenKTV · 全螢幕播放").color(CYAN).strong());
                        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                            if ui.button("⤢ 離開全螢幕 (F11)").clicked() { self.set_theater(ui.ctx(), false); }
                        });
                    });
                } else { self.nav(ui); }
            });
        if self.tab != Tab::Songs { self.video_host.hide(); }
        if !self.theater {
            egui::TopBottomPanel::bottom("status").show(ctx, |ui| {
                ui.horizontal(|ui| {
                    ui.label(RichText::new("●").color(CYAN));
                    ui.label(RichText::new(&self.status).color(MUTED).size(12.0));
                });
            });
        }
        egui::TopBottomPanel::bottom("playback")
            .frame(egui::Frame::new().fill(PANEL).inner_margin(12.0))
            .show(ctx, |ui| self.player_bar(ui));
        if self.tab == Tab::Songs && !self.theater {
            egui::SidePanel::left("library").resizable(true).default_width(320.0).min_width(245.0)
                .frame(egui::Frame::new().fill(BG).inner_margin(10.0))
                .show(ctx, |ui| self.song_tab(ui));
            egui::SidePanel::right("queue").resizable(true).default_width(305.0).min_width(245.0)
                .frame(egui::Frame::new().fill(BG).inner_margin(10.0))
                .show(ctx, |ui| self.queue_panel(ui));
        }
        egui::CentralPanel::default().frame(egui::Frame::new().fill(BG).inner_margin(8.0)).show(ctx, |ui| {
            if self.tab == Tab::Songs { self.video_panel(ui); }
            else {
                egui::ScrollArea::vertical().show(ui, |ui| match self.tab {
                    Tab::Downloads => self.download_tab(ui),
                    Tab::Phone => self.phone_tab(ui),
                    Tab::Settings => self.settings_tab(ui),
                    Tab::Songs => {},
                });
            }
        });
        self.publish_remote_state();
        ctx.request_repaint_after(Duration::from_millis(150));
    }
}

fn main() -> Result<()> {
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_inner_size([1380.0, 850.0])
            .with_min_inner_size([900.0, 600.0]),
        ..Default::default()
    };
    eframe::run_native("OpenKTV · 點歌台", options, Box::new(|cc| {
        OpenKtv::new(&cc.egui_ctx)
            .map(|app| Box::new(app) as Box<dyn eframe::App>)
            .map_err(|e| Box::new(std::io::Error::other(format!("OpenKTV 啟動失敗：{e:#}"))) as Box<dyn std::error::Error + Send + Sync>)
    })).map_err(|e| anyhow::anyhow!("GUI 啟動失敗：{e}"))?;
    Ok(())
}
