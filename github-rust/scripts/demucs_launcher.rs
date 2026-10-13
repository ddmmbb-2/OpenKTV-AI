//! Minimal Windows launcher for OpenKTV-Rust.
//! The launcher is NOT the Demucs inference engine; it starts the adjacent Python venv.
use std::{env, ffi::OsString, fs, path::PathBuf, process::{self, Command}};

fn run() -> Result<i32, String> {
    let launcher = env::current_exe().map_err(|e| format!("Cannot locate demucs.exe: {e}"))?;
    let tools = launcher.parent().ok_or("Cannot locate the tools directory")?.to_path_buf();
    let python = tools.join("demucs-env").join("Scripts").join("python.exe");
    if !python.is_file() {
        return Err(format!(
            "Demucs Python environment not found: {}\nRun scripts\\setup_demucs.ps1 first.",
            python.display()
        ));
    }

    // Store the pre-trained model under the OpenKTV tools directory.
    let models = tools.join("demucs-models");
    fs::create_dir_all(&models).map_err(|e| format!("Cannot create model cache: {e}"))?;

    // Demucs uses FFmpeg on Windows. Give it access to tools/ffmpeg.exe.
    let mut search_paths: Vec<PathBuf> = vec![tools.clone()];
    if let Some(old_path) = env::var_os("PATH") {
        search_paths.extend(env::split_paths(&old_path));
    }
    let merged: Option<OsString> = env::join_paths(search_paths).ok();

    let mut command = Command::new(&python);
    command.arg("-m").arg("demucs").args(env::args_os().skip(1));
    command.env("TORCH_HOME", &models);
    if let Some(path) = merged { command.env("PATH", path); }
    // Inherit stdin/stdout/stderr by default. OpenKTV captures the output.
    let status = command.status().map_err(|e| format!("Failed to run Demucs Python: {e}"))?;
    Ok(status.code().unwrap_or(1))
}

fn main() {
    match run() {
        Ok(code) => process::exit(code),
        Err(message) => {
            eprintln!("[OpenKTV Demucs] {message}");
            process::exit(1);
        }
    }
}
