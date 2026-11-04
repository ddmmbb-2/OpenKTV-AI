"""
Pitch extraction module for KTV vocal tracks.
Extracts fundamental frequency (F0) contour and compiles it into discrete pitch blocks (.pitch.json)
for the real-time scoring engine using the YIN pitch tracking algorithm.
"""
from __future__ import annotations

import json
import math
import struct
import wave
from pathlib import Path


def _read_wav_samples(wav_path: Path) -> tuple[list[float], int]:
    """Reads a mono or stereo WAV file into normalized float samples [-1.0, 1.0] and sample rate."""
    try:
        from scipy.io import wavfile
        import numpy as np
        sr, data = wavfile.read(str(wav_path))
        if data.ndim > 1:
            data = data.mean(axis=1)
        if data.dtype == np.int16:
            samples = (data / 32768.0).tolist()
        elif data.dtype == np.float32 or data.dtype == np.float64:
            samples = data.tolist()
        else:
            samples = (data / (2 ** (8 * data.itemsize - 1))).tolist()
        return samples, int(sr)
    except Exception:
        pass

    with wave.open(str(wav_path), "rb") as wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        sr = wf.getframerate()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)

    samples = []
    if sampwidth == 2:
        total_samples = n_frames * n_channels
        fmt = f"<{total_samples}h"
        unpacked = struct.unpack(fmt, raw)
        if n_channels == 1:
            samples = [s / 32768.0 for s in unpacked]
        else:
            samples = [(unpacked[i] + unpacked[i + 1]) / (2 * 32768.0) for i in range(0, len(unpacked), n_channels)]
    return samples, int(sr)


def _yin_frame(frame: list[float], sr: int, min_freq: float = 75.0, max_freq: float = 900.0, threshold: float = 0.15) -> float | None:
    """Estimates MIDI note for a single frame using the YIN algorithm."""
    frame_len = len(frame)
    min_period = int(sr / max_freq)
    max_period = min(int(sr / min_freq), frame_len // 2)
    if min_period >= max_period or max_period <= 2:
        return None

    # 1. Difference function
    diff = [0.0] * max_period
    for tau in range(1, max_period):
        s = 0.0
        for j in range(frame_len - tau):
            delta = frame[j] - frame[j + tau]
            s += delta * delta
        diff[tau] = s

    # 2. Cumulative mean normalized difference
    cmnd = [0.0] * max_period
    cmnd[0] = 1.0
    running_sum = 0.0
    for tau in range(1, max_period):
        running_sum += diff[tau]
        if running_sum > 0:
            cmnd[tau] = diff[tau] / (running_sum / tau)
        else:
            cmnd[tau] = 1.0

    # 3. Absolute threshold (find first dip below threshold)
    tau_chosen = 0
    for tau in range(min_period, max_period):
        if cmnd[tau] < threshold:
            while tau + 1 < max_period and cmnd[tau + 1] < cmnd[tau]:
                tau += 1
            tau_chosen = tau
            break

    if tau_chosen == 0:
        min_val = 0.8
        for tau in range(min_period, max_period):
            if cmnd[tau] < min_val:
                min_val = cmnd[tau]
                tau_chosen = tau

    if tau_chosen > 0:
        # Parabolic interpolation for sub-sample precision
        tau_ref = float(tau_chosen)
        if 0 < tau_chosen < max_period - 1:
            alpha = cmnd[tau_chosen - 1]
            beta = cmnd[tau_chosen]
            gamma = cmnd[tau_chosen + 1]
            denom = 2.0 * (alpha - 2.0 * beta + gamma)
            if abs(denom) > 1e-9:
                delta = (alpha - gamma) / denom
                tau_ref += delta

        if tau_ref > 0:
            freq = sr / tau_ref
            if min_freq <= freq <= max_freq:
                midi = 69.0 + 12.0 * math.log2(freq / 440.0)
                return midi
    return None


def extract_pitch_blocks(
    wav_path: Path,
    frame_duration_ms: float = 50.0,
    min_freq: float = 75.0,
    max_freq: float = 900.0,
    rms_threshold: float = 0.02,
) -> dict:
    """
    Extracts pitch blocks from a vocal WAV file.
    Returns:
        dict with {"duration": float, "notes": [{"start": float, "duration": float, "midi": float}]}
    """
    samples, sr = _read_wav_samples(wav_path)
    total_duration = len(samples) / sr if sr > 0 else 0
    if not samples or sr <= 0:
        return {"duration": 0, "notes": []}

    frame_size = int(sr * (frame_duration_ms / 1000.0))
    hop_size = frame_size

    frames_notes = []
    num_frames = len(samples) // hop_size

    for i in range(num_frames):
        start_idx = i * hop_size
        frame = samples[start_idx : start_idx + frame_size]
        if len(frame) < frame_size:
            break

        # Compute RMS
        sum_sq = sum(s * s for s in frame)
        rms = math.sqrt(sum_sq / len(frame))
        if rms < rms_threshold:
            frames_notes.append(None)
            continue

        midi = _yin_frame(frame, sr, min_freq, max_freq)
        frames_notes.append(round(midi, 1) if midi is not None else None)

    # Group consecutive frames into continuous note blocks
    notes = []
    current_start = None
    current_midi_sum = 0.0
    current_count = 0

    step_sec = frame_duration_ms / 1000.0
    for i, midi in enumerate(frames_notes):
        t = i * step_sec
        if midi is not None:
            if current_start is None:
                current_start = t
                current_midi_sum = midi
                current_count = 1
            else:
                avg_midi = current_midi_sum / current_count
                if abs(midi - avg_midi) <= 1.5:
                    current_midi_sum += midi
                    current_count += 1
                else:
                    dur = current_count * step_sec
                    if dur >= 0.12:
                        notes.append({
                            "start": round(current_start, 2),
                            "duration": round(dur, 2),
                            "midi": round(avg_midi, 1),
                        })
                    current_start = t
                    current_midi_sum = midi
                    current_count = 1
        else:
            if current_start is not None:
                dur = current_count * step_sec
                if dur >= 0.12:
                    notes.append({
                        "start": round(current_start, 2),
                        "duration": round(dur, 2),
                        "midi": round(current_midi_sum / current_count, 1),
                    })
                current_start = None
                current_midi_sum = 0.0
                current_count = 0

    if current_start is not None:
        dur = current_count * step_sec
        if dur >= 0.12:
            notes.append({
                "start": round(current_start, 2),
                "duration": round(dur, 2),
                "midi": round(current_midi_sum / current_count, 1),
            })

    return {
        "duration": round(total_duration, 2),
        "notes": notes,
    }


def generate_pitch_json_for_song(vocal_or_media_path: Path, output_json_path: Path) -> Path | None:
    """Extracts and writes .pitch.json for a song given its vocal track."""
    if not vocal_or_media_path.exists():
        return None
    data = extract_pitch_blocks(vocal_or_media_path)
    output_json_path.parent.mkdir(parents=True, exist_ok=True)
    with output_json_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return output_json_path
