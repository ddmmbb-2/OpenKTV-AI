//! Windows 原生影片顯示區。mpv 用 --wid 綁定此子視窗，影片與 egui 控制台在同一視窗。
//! mpv 自己維護子子視窗；本模組只建立、顯示、隱藏與調整 Win32 host。
use eframe::egui;

#[cfg(windows)]
mod platform {
    use super::*;
    use anyhow::{bail, Result};
    use std::{ffi::c_void, ptr};

    type Hwnd = *mut c_void;
    const WS_CHILD: u32 = 0x4000_0000;
    const WS_CLIPSIBLINGS: u32 = 0x0400_0000;
    const SWP_NOZORDER: u32 = 0x0004;
    const SWP_NOACTIVATE: u32 = 0x0010;
    const SW_HIDE: i32 = 0;
    const SW_SHOW: i32 = 5;
    const WINDOW_TITLE: &str = "OpenKTV · 點歌台";

    #[link(name = "user32")]
    extern "system" {
        fn FindWindowW(class_name: *const u16, window_name: *const u16) -> Hwnd;
        fn GetWindowThreadProcessId(window: Hwnd, process_id: *mut u32) -> u32;
        fn CreateWindowExW(
            ex_style: u32, class_name: *const u16, window_name: *const u16,
            style: u32, x: i32, y: i32, width: i32, height: i32,
            parent: Hwnd, menu: Hwnd, instance: Hwnd, param: *const c_void,
        ) -> Hwnd;
        fn SetWindowPos(window: Hwnd, insert_after: Hwnd, x: i32, y: i32,
                        width: i32, height: i32, flags: u32) -> i32;
        fn ShowWindow(window: Hwnd, command: i32) -> i32;
        fn DestroyWindow(window: Hwnd) -> i32;
        fn IsWindow(window: Hwnd) -> i32;
    }

    #[derive(Default)]
    pub struct VideoHost {
            hwnd: usize,
        last: Option<(i32, i32, i32, i32)>,
        visible: bool,
    }

    impl VideoHost {
        fn ensure(&mut self) -> Result<()> {
            if self.hwnd != 0 && unsafe { IsWindow(self.hwnd as Hwnd) } != 0 { return Ok(()); }
            self.hwnd = 0;
            self.visible = false;
            self.last = None;
            let title: Vec<u16> = WINDOW_TITLE.encode_utf16().chain(std::iter::once(0)).collect();
            let owner = unsafe { FindWindowW(ptr::null(), title.as_ptr()) };
            if owner.is_null() { bail!("尚未取得 OpenKTV 主視窗，請稍後重試"); }
            let mut pid = 0_u32;
            unsafe { GetWindowThreadProcessId(owner, &mut pid) };
            if pid != std::process::id() { bail!("找到的主視窗不屬於目前 OpenKTV 程序"); }
            let class_name: Vec<u16> = "STATIC".encode_utf16().chain(std::iter::once(0)).collect();
            let hwnd = unsafe {
                CreateWindowExW(0, class_name.as_ptr(), ptr::null(), WS_CHILD | WS_CLIPSIBLINGS,
                    0, 0, 1, 1, owner, ptr::null_mut(), ptr::null_mut(), ptr::null())
            };
            if hwnd.is_null() { bail!("Win32 影片容器建立失敗"); }
            self.hwnd = hwnd as usize;
            Ok(())
        }
        pub fn window_id(&self) -> Option<u64> {
            // mpv Win32 --wid 需要非負 HWND 整數值。
            (self.hwnd != 0).then_some((self.hwnd as u32) as u64)
        }
        pub fn present(&mut self, ctx: &egui::Context, rect: egui::Rect, show: bool) -> Result<()> {
            self.ensure()?;
            if !show { self.hide(); return Ok(()); }
            let scale = ctx.pixels_per_point();
            let x = (rect.min.x * scale).round() as i32;
            let y = (rect.min.y * scale).round() as i32;
            let w = (rect.width() * scale).round().max(1.0) as i32;
            let h = (rect.height() * scale).round().max(1.0) as i32;
            let coords = (x, y, w, h);
            if self.last != Some(coords) {
                let ok = unsafe { SetWindowPos(self.hwnd as Hwnd, ptr::null_mut(), x, y, w, h,
                    SWP_NOZORDER | SWP_NOACTIVATE) };
                if ok == 0 { bail!("無法調整內嵌影片尺寸"); }
                self.last = Some(coords);
            }
            if !self.visible {
                unsafe { ShowWindow(self.hwnd as Hwnd, SW_SHOW) };
                self.visible = true;
            }
            Ok(())
        }
        pub fn hide(&mut self) {
            if self.hwnd != 0 && self.visible {
                unsafe { ShowWindow(self.hwnd as Hwnd, SW_HIDE) };
                self.visible = false;
            }
        }
    }
    impl Drop for VideoHost {
        fn drop(&mut self) {
            if self.hwnd != 0 && unsafe { IsWindow(self.hwnd as Hwnd) } != 0 {
                unsafe { DestroyWindow(self.hwnd as Hwnd) };
            }
        }
    }
}

#[cfg(not(windows))]
mod platform {
    use super::*;
    #[derive(Default)]
    pub struct VideoHost;
    impl VideoHost {
        pub fn window_id(&self) -> Option<u64> { None }
        pub fn present(&mut self, _ctx: &egui::Context, _rect: egui::Rect, _show: bool) -> anyhow::Result<()> { Ok(()) }
        pub fn hide(&mut self) {}
    }
}

pub use platform::VideoHost;
