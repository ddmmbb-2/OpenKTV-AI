//! First prototype: mono 16 kHz PCM packets from HTTPS phone microphone,
//! mixed by Windows with mpv's normal playback via the default audio device.
//! No AEC, feedback suppression or automatic gain control in this version.
use anyhow::{bail, Context, Result};
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use std::collections::VecDeque;
use std::sync::{Arc, Mutex};
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicUsize, Ordering};

pub const MIC_RATE: usize = 16_000;
pub const MAX_BUFFER_SAMPLES: usize = MIC_RATE; // at most one second

#[derive(Clone)]
pub struct MicState {
    pub queue: Arc<Mutex<VecDeque<f32>>>,
    pub muted: Arc<AtomicBool>,
    pub level: Arc<AtomicU32>,
    pub light: Arc<AtomicBool>,
    pub connected: Arc<AtomicUsize>,
}
impl Default for MicState {
    fn default() -> Self {
        Self {
            queue:Arc::new(Mutex::new(VecDeque::with_capacity(MAX_BUFFER_SAMPLES))),
            muted:Arc::new(AtomicBool::new(true)), // hard safety default
            level:Arc::new(AtomicU32::new(45)),
            light:Arc::new(AtomicBool::new(true)),
            connected:Arc::new(AtomicUsize::new(0)),
        }
    }
}
impl MicState {
    pub fn enqueue_pcm16(&self, payload: &[u8]) {
        if payload.len() % 2 != 0 || payload.len() > 8192 { return; }
        let mut q = match self.queue.lock() { Ok(q)=>q, Err(_)=>return };
        // Drop oldest input on congestion to minimize audio latency.
        for b in payload.chunks_exact(2) {
            if q.len()>=MAX_BUFFER_SAMPLES { q.pop_front(); }
            q.push_back(i16::from_le_bytes([b[0],b[1]]) as f32 / 32768.0);
        }
    }
    pub fn clear(&self) {
        if let Ok(mut q)=self.queue.lock() { q.clear(); }
    }
}

/// The stream must be kept alive for the application's entire lifetime.
pub struct AudioEngine { _stream: cpal::Stream }
impl AudioEngine {
    pub fn start(mic: MicState) -> Result<Self> {
        let host=cpal::default_host();
        let device=host.default_output_device().context("找不到系統預設音訊輸出裝置")?;
        let supported=device.default_output_config().context("無法讀取音效卡設定")?;
        if supported.sample_format()!=cpal::SampleFormat::F32 {
            bail!("此測試版 MIC 混音器需要 f32 音訊輸出裝置，現在是 {:?}",supported.sample_format());
        }
        let config: cpal::StreamConfig=supported.config();
        let sample_rate=config.sample_rate.0 as f32;
        let channels=config.channels as usize;
        let step=MIC_RATE as f32/sample_rate;
        let mut phase=0.0_f32;
        let mut a=0.0_f32;
        let mut b=0.0_f32;
        let mut delay=vec![0.0_f32;(sample_rate*0.16) as usize+1];
        let mut delay_cursor=0usize;
        let stream=device.build_output_stream(&config, move |buf: &mut [f32], _| {
            let mut q=match mic.queue.try_lock() {
                Ok(queue)=>queue,
                Err(_)=>{buf.fill(0.0);return;},
            };
            let muted=mic.muted.load(Ordering::Relaxed);
            let gain=mic.level.load(Ordering::Relaxed).min(100) as f32 / 100.0;
            let light=mic.light.load(Ordering::Relaxed);
            for frame in buf.chunks_mut(channels) {
                phase+=step;
                while phase>=1.0 {
                    phase-=1.0;
                    a=b;
                    b=q.pop_front().unwrap_or(0.0);
                }
                let input=a+(b-a)*phase;
                let delayed=delay[delay_cursor];
                delay[delay_cursor]=(input+delayed*0.16).clamp(-1.0,1.0);
                delay_cursor=(delay_cursor+1)%delay.len();
                let wet=if light { input+delayed*0.10 } else { input };
                // Keep >0 headroom; this is not an anti-feedback processor.
                let out=if muted {0.0} else {(wet*gain*0.80).clamp(-0.85,0.85)};
                frame.fill(out);
            }
        }, move |err| eprintln!("Audio output error: {err}"), None)?;
        stream.play()?;
        Ok(Self{_stream:stream})
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn pcm_input_guard() {
        let m=MicState::default();
        m.enqueue_pcm16(&[0,0,255,127]);
        assert_eq!(m.queue.lock().unwrap().len(),2);
        m.enqueue_pcm16(&[1]);
        assert_eq!(m.queue.lock().unwrap().len(),2);
        assert!(m.muted.load(Ordering::Relaxed));
    }
}
