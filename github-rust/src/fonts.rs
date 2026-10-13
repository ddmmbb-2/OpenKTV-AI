//! OpenKTV-Rust CJK font loader (v0.1.3)
//!
//! Only selecting a readable .ttc file is not enough: a font collection has
//! multiple faces, and not every face necessarily contains CJK glyphs.
//! This module validates that egui's actual font backend (ab_glyph) can parse
//! the selected face AND render the sample Traditional Chinese glyphs.
//!
//! Font files are loaded from the Windows installation, not redistributed.
use ab_glyph::{Font as _, FontVec};
use eframe::egui::{self, FontData, FontDefinitions, FontFamily};
use std::{path::{Path, PathBuf}, sync::Arc};

const REQUIRED: &str = "中文點歌播放設定手機麥克風";

pub fn install_chinese_fonts(ctx: &egui::Context, app_root: &Path) -> anyhow::Result<PathBuf> {
    let mut candidates: Vec<PathBuf> = Vec::new();
    
    // 1. 環境變數最高優先，保留給除錯用
    if let Some(custom) = std::env::var_os("OPENKTV_CJK_FONT") {
        candidates.push(PathBuf::from(custom));
    }

    let windows_dir = std::env::var_os("WINDIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(r"C:\Windows"));
    let win_fonts = windows_dir.join("Fonts");

    // 2. 預設：微軟正黑體系列
    for filename in [
        "msjh.ttc",      // 正黑體 Regular (face 0) + Bold (face 1) -> 你要的預設
        "msjhl.ttc",     // 正黑體 Light
        "msjhbd.ttc",    // 正黑體 Bold 獨立檔
        "msyh.ttc",      // 微軟雅黑 (簡中備援)
        "msyhbd.ttc",
        "kaiu.ttf",      // 標楷體，放最後當備援
        "mingliu.ttc",
        "simsun.ttc",
    ] {
        candidates.push(win_fonts.join(filename));
    }

    // 3. 你自己打包的 cjk.* 放最後，當 Windows 上完全沒字型才用
    for filename in ["cjk.ttf", "cjk.otf", "cjk.ttc"] {
        candidates.push(app_root.join("data/fonts").join(filename));
    }

    eprintln!("[OpenKTV FONT] Checking fonts; sample: {REQUIRED}");
    for path in candidates {
        let bytes = match std::fs::read(&path) {
            Ok(bytes) => bytes,
            Err(_) => continue,
        };
        let faces = face_count(&bytes);
        if faces == 0 {
            eprintln!("[OpenKTV FONT] Invalid font header: {}", path.display());
            continue;
        }
        for index in 0..faces {
            let parsed = match FontVec::try_from_vec_and_index(bytes.clone(), index) {
                Ok(font) => font,
                Err(_) => {
                    eprintln!("[OpenKTV FONT] Face {index} cannot be parsed: {}", path.display());
                    continue;
                }
            };
            let supported = REQUIRED.chars().filter(|ch| parsed.glyph_id(*ch).0 != 0).count();
            let expected = REQUIRED.chars().count();
            if supported != expected {
                eprintln!("[OpenKTV FONT] Rejected {} face {}: CJK glyphs {}/{}",
                    path.display(), index, supported, expected);
                continue;
            }

            let mut definitions = FontDefinitions::default();
            let name = "openktv-cjk".to_string();
            let mut data = FontData::from_owned(bytes.clone());
            data.index = index;
            definitions.font_data.insert(name.clone(), Arc::new(data));
            for family in [FontFamily::Proportional, FontFamily::Monospace] {
                definitions.families.entry(family).or_default().insert(0, name.clone());
            }
            ctx.set_fonts(definitions);
            eprintln!("[OpenKTV FONT] OK: {} (face {}, {}/{} CJK glyphs)",
                path.display(), index, supported, expected);
            return Ok(path);
        }
    }
    eprintln!("[OpenKTV FONT] FAILED. Check that C:\\Windows\\Fonts\\msjh.ttc exists, or set OPENKTV_CJK_FONT to a CJK TTF.");
    anyhow::bail!("沒有找到可以顯示繁體中文的可用字型。請在 PowerShell 設定 OPENKTV_CJK_FONT 或安裝繁體中文字型。")
}

fn face_count(bytes: &[u8]) -> u32 {
    if bytes.starts_with(b"ttcf") && bytes.len() >= 12 {
        // TrueType Collection header: tag[4], version[4], numFonts[4], BE.
        u32::from_be_bytes([bytes[8], bytes[9], bytes[10], bytes[11]]).min(24)
    } else if bytes.starts_with(b"\x00\x01\x00\x00")
        || bytes.starts_with(b"OTTO")
        || bytes.starts_with(b"true") {
        1
    } else {
        0
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn recognizes_ttf_and_ttc_headers() {
        assert_eq!(face_count(b"\x00\x01\x00\x00"), 1);
        assert_eq!(face_count(b"OTTO"), 1);
        assert_eq!(face_count(b"ttcf\x00\x01\x00\x00\x00\x00\x00\x02"), 2);
        assert_eq!(face_count(b"invalid"), 0);
    }
}
