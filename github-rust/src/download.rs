//! Portable OpenKTV tools: no reliance on system Python at runtime.
//! Demucs is a standalone executable bundled under tools/demucs/demucs.exe
//! (PyInstaller onedir) or tools/demucs.exe (custom standalone build).
//! Model cache is always rooted at tools/demucs-models via TORCH_HOME.
use crate::library::{Library, Song};
use anyhow::{Context, Result};
use std::{
    env, fs,
    path::{Path, PathBuf},
    process::{Command, Output},
    sync::mpsc::Sender,
};

pub enum JobMessage { Progress(String), Finished(Song), Error(String) }

// Official facebookresearch/demucs `htdemucs` bag contains this single model.
// Path: $TORCH_HOME/hub/checkpoints/955717e8-8726e21a.th.
const HTDEMUCS_WEIGHTS: &str = "955717e8-8726e21a.th";

pub fn model_home(tools: &Path) -> PathBuf { tools.join("demucs-models") }
pub fn cached_htdemucs(tools: &Path) -> bool {
    model_home(tools).join("hub/checkpoints").join(HTDEMUCS_WEIGHTS).is_file()
}

fn standalone_demucs(tools: &Path) -> Option<PathBuf> {
    let filename = if cfg!(windows) { "demucs.exe" } else { "demucs" };
    // Prefer onedir packaging so PyTorch DLLs stay alongside the EXE.
    let paths = [tools.join("demucs").join(filename), tools.join(filename)];
    paths.into_iter().find(|p| p.is_file())
}

pub fn standalone_available(tools: &Path) -> bool { standalone_demucs(tools).is_some() }

fn resolve_tool(tools: &Path, base: &str) -> PathBuf {
    let name = if cfg!(windows) { format!("{base}.exe") } else { base.into() };
    let path = tools.join(&name);
    if path.exists() { path } else { PathBuf::from(name) }
}

fn run(mut cmd: Command, what: &str) -> Result<Output> {
    let result = cmd.output().with_context(|| format!("無法執行 {what}，請檢查 tools 資料夾或 PATH"))?;
    if !result.status.success() {
        let err = String::from_utf8_lossy(&result.stderr);
        let out = String::from_utf8_lossy(&result.stdout);
        anyhow::bail!("{what} 失敗 ({}): stderr={} stdout={}", result.status,
            err.chars().take(2500).collect::<String>(),
            out.chars().take(800).collect::<String>());
    }
    Ok(result)
}

fn ffmpeg(tools: &Path, args: &[&str]) -> Result<()> {
    let mut cmd = Command::new(resolve_tool(tools, "ffmpeg"));
    cmd.arg("-y").args(args);
    run(cmd, "FFmpeg")?;
    Ok(())
}

fn demucs_command(tools: &Path) -> Result<Command> {
    let executable = standalone_demucs(tools).context(
        "找不到可攜式 Demucs 執行器。請先執行 scripts/build_portable_demucs.ps1，\
         將產生的 tools/demucs/demucs.exe 和旁邊的程式庫一起保留；\
         tools/demucs-env 不會被當成可攜式執行器。"
    )?;
    let home = model_home(tools);
    fs::create_dir_all(home.join("hub/checkpoints"))
        .with_context(|| format!("無法建立 AI 模型目錄：{}", home.display()))?;
    let mut cmd = Command::new(&executable);
    // Demucs internally calls torch.hub.load_state_dict_from_url, which uses
    // $TORCH_HOME/hub/checkpoints. Avoid any dependency on the Windows user profile.
    cmd.env("TORCH_HOME", &home);
    // Bundle ffmpeg.exe in tools; packaged binaries and torch DLLs may also need PATH.
    let mut paths = vec![tools.to_path_buf()];
    if let Some(parent) = executable.parent() { paths.push(parent.to_path_buf()); }
    if let Some(system) = env::var_os("PATH") { paths.extend(env::split_paths(&system)); }
    if let Ok(joined) = env::join_paths(paths) { cmd.env("PATH", joined); }
    Ok(cmd)
}

fn candidate_legacy_homes() -> Vec<PathBuf> {
    let mut homes = Vec::new();
    if let Some(home) = env::var_os("TORCH_HOME") { homes.push(PathBuf::from(home)); }
    if let Some(xdg) = env::var_os("XDG_CACHE_HOME") {
        homes.push(PathBuf::from(xdg).join("torch"));
    }
    for var in ["USERPROFILE", "HOME"] {
        if let Some(dir) = env::var_os(var) {
            homes.push(PathBuf::from(dir).join(".cache/torch"));
        }
    }
    homes
}

/// On first AI job, copy an existing *specific* htdemucs weight, if found.
/// Never delete the original cache, and never import other PyTorch weights.
/// If absent, Demucs will download automatically into the portable cache.
fn migrate_htdemucs_cache(tools: &Path) -> Result<bool> {
    let target_dir = model_home(tools).join("hub/checkpoints");
    fs::create_dir_all(&target_dir)?;
    let target = target_dir.join(HTDEMUCS_WEIGHTS);
    if target.exists() { return Ok(false); }
    for home in candidate_legacy_homes() {
        let source = home.join("hub/checkpoints").join(HTDEMUCS_WEIGHTS);
        if source == target { continue; }
        let Ok(metadata) = fs::metadata(&source) else { continue; };
        if !metadata.is_file() || metadata.len() < 1_000_000 { continue; }
        let temporary = target_dir.join(format!("{}.{}.partial", HTDEMUCS_WEIGHTS, uuid::Uuid::new_v4()));
        fs::copy(&source, &temporary).with_context(|| format!("複製既有 Demucs 權重失敗：{}", source.display()))?;
        if target.exists() { let _ = fs::remove_file(&temporary); return Ok(false); }
        if let Err(e) = fs::rename(&temporary, &target) {
            let _ = fs::remove_file(&temporary);
            return Err(e).context("搬移 Demucs 權重到 tools 失敗");
        }
        return Ok(true);
    }
    Ok(false)
}

fn download_and_prepare(url: String, title: String, ai: bool, root: PathBuf,
    db: Library, tx: &Sender<JobMessage>) -> Result<Song> {
    let trimmed = url.trim();
    if !(trimmed.starts_with("https://www.youtube.com/") || trimmed.starts_with("https://youtube.com/") ||
         trimmed.starts_with("https://youtu.be/") || trimmed.starts_with("https://m.youtube.com/")) {
        anyhow::bail!("目前只接受 HTTPS YouTube 網址");
    }
    if title.trim().is_empty() { anyhow::bail!("請輸入歌曲名稱"); }
    let id = uuid::Uuid::new_v4().simple().to_string();
    let tools = root.join("tools");
    if ai {
        // Fail early if the independent Demucs runtime is not installed.
        let _ = demucs_command(&tools)?;
        if migrate_htdemucs_cache(&tools)? {
            tx.send(JobMessage::Progress("已從舊快取複製 htdemucs 模型至 tools/demucs-models".into())).ok();
        } else if !cached_htdemucs(&tools) {
            tx.send(JobMessage::Progress("首次 AI 製作會自動下載 htdemucs 模型到 tools/demucs-models（須連網）".into())).ok();
        }
    }
    let data = root.join("data");
    let work = data.join("work").join(&id);
    let target_rel = format!("songs/{id}.mp4");
    let target = data.join(&target_rel);
    fs::create_dir_all(&work)?;
    fs::create_dir_all(data.join("songs"))?;
    let pattern = work.join("source.%(ext)s");
    tx.send(JobMessage::Progress("開始下載 YouTube 影音…".into())).ok();
    let mut cmd = Command::new(resolve_tool(&tools, "yt-dlp"));
    if let Some(current) = env::var_os("PATH") {
        let mut paths = vec![tools.clone()];
        paths.extend(env::split_paths(&current));
        if let Ok(joined) = env::join_paths(paths) { cmd.env("PATH", joined); }
    }
    cmd.arg("--no-playlist").arg("--no-progress")
        .arg("-f").arg("bv*+ba/b")
        .arg("--merge-output-format").arg("mp4")
        .arg("--remux-video").arg("mp4")
        .arg("--ffmpeg-location").arg(&tools)
        .arg("--print").arg("after_move:filepath")
        .arg("-o").arg(&pattern)
        .arg(trimmed);
    let output = run(cmd, "yt-dlp")?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    let source = stdout.lines().rev().map(str::trim).map(PathBuf::from).find(|p| p.is_file())
        .or_else(|| { let p = work.join("source.mp4"); p.exists().then_some(p) })
        .context("下載成功但找不到輸出影片，請檢查 yt-dlp stdout")?;
    if ai {
        let mut separator = demucs_command(&tools)?;
        tx.send(JobMessage::Progress("正在提取音訊…".into())).ok();
        let wav = work.join("input.wav");
        let source_s = source.to_string_lossy().into_owned();
        let wav_s = wav.to_string_lossy().into_owned();
        ffmpeg(&tools, &["-i", &source_s, "-vn", "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", &wav_s])?;
        tx.send(JobMessage::Progress("正在執行 AI 去人聲（Demucs；首次可能下載模型）…".into())).ok();
        separator.arg("--two-stems").arg("vocals").arg("-n").arg("htdemucs")
            .arg("-o").arg(&work).arg(&wav);
        run(separator, "可攜式 Demucs AI")?;
        let accompaniment = work.join("htdemucs").join("input").join("no_vocals.wav");
        if !accompaniment.exists() { anyhow::bail!("AI 處理後找不到 no_vocals.wav"); }
        tx.send(JobMessage::Progress("正在合成雙立體聲音軌影片…".into())).ok();
        let acc_s = accompaniment.to_string_lossy().into_owned();
        let out_s = target.to_string_lossy().into_owned();
        ffmpeg(&tools, &[
            "-i", &source_s, "-i", &acc_s,
            "-map", "0:v:0", "-map", "0:a:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-metadata:s:a:0", "title=Original",
            "-metadata:s:a:1", "title=Instrumental",
            "-disposition:a:0", "default", "-disposition:a:1", "0",
            "-shortest", &out_s,
        ])?;
        ffmpeg(&tools, &["-v", "error", "-i", &out_s, "-map", "0:a:1", "-t", "0.1", "-f", "null", "-"])
            .context("成品驗證失敗：未偵測到第二條可解碼的伴奏音軌")?;
    } else {
        fs::copy(&source, &target)?;
    }
    let result = db.insert(&title, &target_rel, if ai { "dual" } else { "single" })?;
    let _ = fs::remove_dir_all(work);
    Ok(result)
}

pub fn spawn_download(url: String, title: String, ai: bool, root: PathBuf,
    db: Library, tx: Sender<JobMessage>) {
    std::thread::spawn(move || {
        match download_and_prepare(url, title, ai, root, db, &tx) {
            Ok(song) => { let _ = tx.send(JobMessage::Finished(song)); }
            Err(e) => { let _ = tx.send(JobMessage::Error(format!("{e:#}"))); }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn portable_cache_path() {
        let tools = PathBuf::from("test-tools");
        assert_eq!(model_home(&tools), tools.join("demucs-models"));
    }
    #[test]
    fn finds_standalone_exe_only() {
        let temp = env::temp_dir().join(format!("openktv-demucs-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(temp.join("demucs")).unwrap();
        let exe = temp.join("demucs").join(if cfg!(windows) { "demucs.exe" } else { "demucs" });
        fs::write(&exe, b"stub").unwrap();
        assert!(standalone_available(&temp));
        let _ = fs::remove_dir_all(&temp);
    }
}
