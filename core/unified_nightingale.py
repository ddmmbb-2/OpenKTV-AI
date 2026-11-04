from __future__ import annotations

import os
import shutil
import subprocess
import uuid
import urllib.request
import urllib.parse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import AppSettings
from .demucs_separator import ensure_demucs_weights
from .downloader import download_youtube_video
from .lyrics_alignment import LyricLine, write_ktv_lrc, parse_lyrics_text, build_text_transform
from .runtime import compute_type_for, resolve_device, align_device_for
from .separators import separate_audio
from .transcription import transcribe_segments
from .audio import detect_vocal_region, highpass_filter, normalize_rms, suppress_reverb
from .language_detection import detect_language_multiwindow
from .whisper_alignment import align_with_backend
from .cjk import is_cjk, tokenize_for_alignment, align_lang_code, attribute_chars_to_tokens, merge_punct, attach_reading, qwen_kept_len, is_supported_lang, align_model_for, clean_for_alignment
from .library import upsert_catalog_entry, fetch_lrclib_lyrics, fetch_lrclib_lyrics_by_url
from .metadata import ai_parse_youtube_title, extract_youtube_metadata, parse_youtube_title, search_metadata, spotify_track_metadata

@dataclass(frozen=True)
class PipelineArtifacts:
    original_instrumental: Path
    dialogue_instrumental: Path
    vocals: Path
    original_vocal_mp4: Path
    dialogue_vocals: Path
    dereverbed_vocals: Path
    lrc: Path
    detected_language: str

def get_ffmpeg_bin(settings: AppSettings) -> str:
    if settings.ffmpeg_dir:
        p = Path(settings.ffmpeg_dir)
        candidate = p / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        if candidate.is_file(): return str(candidate)
        if p.is_file(): return str(p)
    return os.environ.get("FFMPEG_PATH", "ffmpeg")

def enhanced_fetch_lrclib(song_name: str, singer: str) -> str:
    queries = []
    if singer:
        queries.append(urllib.parse.urlencode({"track_name": song_name, "artist_name": singer}))
        queries.append(urllib.parse.urlencode({"q": f"{singer} {song_name}"}))
    else:
        queries.append(urllib.parse.urlencode({"q": song_name}))

    for query in queries:
        url = f"https://lrclib.net/api/search?{query}"
        print("\t"+url)
        req = urllib.request.Request(url, headers={"User-Agent": "OpenKTV-AI/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                print("\t"+data)
                for item in data:
                    print("\t"+item)
                    if item.get("plainLyrics"):
                        return item.get("plainLyrics")
        except Exception:
            continue
    return ""

def smart_join(words: list[str]) -> str:
    if not words: return ""
    res = [words[0]]
    for w in words[1:]:
        prev = res[-1]
        prev_is_en = bool(re.search(r'[A-Za-z0-9]', prev[-1:]))
        curr_is_en = bool(re.search(r'[A-Za-z0-9]', w[:1]))
        res.append(" " + w if (prev_is_en or curr_is_en) else w)
    return "".join(res)

def _normalize(word: str) -> str:
    return re.sub(r"[^\w]", "", word).lower()

def _collect_aligned(align_result: dict) -> list[dict]:
    out = []
    for seg in align_result.get("segments", []):
        items = seg.get("words") or seg.get("chars") or []
        for w in items:
            text = w.get("word", w.get("char", ""))
            if text is None or not str(text).strip(): continue
            out.append({
                "word": str(text), "norm": _normalize(str(text)),
                "start": w.get("start"), "end": w.get("end"), "score": w.get("score"),
            })
    return out

def _interpolate_missing(word_entries: list[dict]):
    unset = [i for i, e in enumerate(word_entries) if e.get("start") is None]
    set_entries = [e for e in word_entries if e.get("start") is not None]
    if not unset or not set_entries: return
    for ui in unset:
        prev_end = set_entries[0]["start"]
        next_start = set_entries[-1]["end"]
        for j in range(ui - 1, -1, -1):
            if word_entries[j].get("start") is not None:
                prev_end = word_entries[j]["end"]
                break
        for j in range(ui + 1, len(word_entries)):
            if word_entries[j].get("start") is not None:
                next_start = word_entries[j]["start"]
                break
        mid = (prev_end + next_start) / 2
        word_entries[ui]["start"] = round(prev_end, 3)
        word_entries[ui]["end"] = round(mid, 3)

# ==========================================
# 核心防漂移收束邏輯 (Anti-Drift Clamp)
# ==========================================
MAX_WORD_DURATION = 2.0

def _map_words_to_lines(align_result: dict, clean_lines: list[str]) -> list[LyricLine]:
    aligned = _collect_aligned(align_result)
    LOOKAHEAD = 6
    ai = 0
    segments = []
    for line_text in clean_lines:
        word_entries = []
        for word_text in line_text.split():
            target = _normalize(word_text)
            matched = -1
            if target:
                limit = min(ai + LOOKAHEAD, len(aligned))
                for k in range(ai, limit):
                    if aligned[k]["norm"] == target:
                        matched = k
                        break
            if matched >= 0:
                a = aligned[matched]
                ai = matched + 1
                entry = {"word": word_text, "start": a.get("start"), "end": a.get("end")}
            else:
                entry = {"word": word_text, "start": None, "end": None, "estimated": True}
            word_entries.append(entry)

        _interpolate_missing(word_entries)
        valid_words = [e for e in word_entries if e.get("start") is not None]
        if not valid_words: continue
        for w in valid_words:
            w["start"] = round(w["start"], 3)
            w["end"] = round(w["end"], 3)
            w["text"] = w["word"]
            if w["end"] - w["start"] > MAX_WORD_DURATION:
                w["end"] = round(w["start"] + MAX_WORD_DURATION, 3)
        segments.append(LyricLine(start=valid_words[0]["start"], end=valid_words[-1]["end"], text=line_text, words=valid_words))
    return segments

def _map_chars_to_lines_cjk(align_result: dict, clean_lines: list[str], line_token_pairs: list, language: str) -> list[LyricLine]:
    aligned = _collect_aligned(align_result)
    segments = []
    cursor = 0
    for original, token_pairs in zip(clean_lines, line_token_pairs):
        n = sum(len(r) for _, r in token_pairs)
        if n == 0: continue
        slice_chars = aligned[cursor:cursor + n]
        cursor += len(slice_chars)

        if len(slice_chars) < n:
            slice_chars += [{"word": "", "start": None, "end": None}] * (n - len(slice_chars))

        surfaces = [s for s, _ in token_pairs]
        lengths = [len(r) for _, r in token_pairs]
        fb_start = next((c["start"] for c in slice_chars if c.get("start") is not None), None)
        fb_end = next((c["end"] for c in reversed(slice_chars) if c.get("end") is not None), None)

        entries = attribute_chars_to_tokens(surfaces, slice_chars, fallback_start=fb_start, fallback_end=fb_end, cleaned_lengths=lengths)
        entries = merge_punct(entries)
        valid = [e for e in entries if e.get("start") is not None and e.get("end") is not None]
        if not valid: continue

        for e in valid:
            e["start"] = round(e["start"], 3)
            e["end"] = round(e["end"], 3)
            if e["end"] < e["start"]: e["end"] = e["start"]
            if e["end"] - e["start"] > MAX_WORD_DURATION:
                e["end"] = round(e["start"] + MAX_WORD_DURATION, 3)
            e["text"] = e["word"]

        attach_reading(valid, language)
        segments.append(LyricLine(start=valid[0]["start"], end=valid[-1]["end"], text=original, words=valid))
    return segments

def _map_qwen_units_to_lines(align_result: dict, clean_lines: list[str], language: str) -> list[LyricLine]:
    units = _collect_aligned(align_result)
    segments = []
    cursor = 0
    for line_text in clean_lines:
        need = qwen_kept_len(line_text)
        if need == 0: continue
        taken = []
        acc = 0
        while cursor < len(units) and acc < need:
            u = units[cursor]
            cursor += 1
            taken.append(u)
            acc += qwen_kept_len(u["word"])
        words = []
        for u in taken:
            if u.get("start") is None or u.get("end") is None: continue
            s = round(u["start"], 3)
            e = round(u["end"], 3)
            if e - s > MAX_WORD_DURATION:
                e = round(s + MAX_WORD_DURATION, 3)
            words.append({"word": u["word"], "text": u["word"], "start": s, "end": e})
        if not words: continue
        if is_supported_lang(language): attach_reading(words, language)
        segments.append(LyricLine(start=words[0]["start"], end=words[-1]["end"], text=line_text, words=words))
    return segments

def _split_long_segments(segments: list[LyricLine]) -> list[LyricLine]:
    MAX_WORDS_PER_LINE = 10
    out = []
    for seg in segments:
        words = seg.words
        if len(words) <= MAX_WORDS_PER_LINE:
            out.append(seg)
            continue
        for i in range(0, len(words), MAX_WORDS_PER_LINE):
            chunk = words[i:i + MAX_WORDS_PER_LINE]
            text = smart_join([w.get("text", w.get("word", "")) for w in chunk])
            out.append(LyricLine(start=chunk[0]["start"], end=chunk[-1]["end"], text=text, words=chunk))
    return out

def _merge_short_segments(segments: list[LyricLine]) -> list[LyricLine]:
    MIN_WORDS = 3
    merged = True
    while merged:
        merged = False
        i = 0
        while i < len(segments):
            if len(segments[i].words) < MIN_WORDS and len(segments) > 1:
                neighbor = i + 1 if i == 0 else (i - 1 if i == len(segments) - 1 else (i - 1 if segments[i].start - segments[i - 1].end <= segments[i + 1].start - segments[i].end else i + 1))
                if neighbor < i:
                    new_words = segments[neighbor].words + segments[i].words
                    new_text = smart_join([w.get("text", w.get("word", "")) for w in new_words])
                    segments[neighbor] = LyricLine(start=new_words[0]["start"], end=new_words[-1]["end"], text=new_text, words=new_words)
                    segments.pop(i)
                else:
                    new_words = segments[i].words + segments[neighbor].words
                    new_text = smart_join([w.get("text", w.get("word", "")) for w in new_words])
                    segments[neighbor] = LyricLine(start=new_words[0]["start"], end=new_words[-1]["end"], text=new_text, words=new_words)
                    segments.pop(i)
                merged = True
            else:
                i += 1
    return segments

# ==========================================
# 音訊處理與防漏音對白提取
# ==========================================

def make_pseudo_spatial_m4a(input_wav: Path, output_m4a: Path, ffmpeg_bin: str) -> None:
    cmd = [ffmpeg_bin, "-y", "-i", str(input_wav), "-af", "extrastereo=m=1.35:c=0", "-c:a", "aac", "-b:a", "320k", str(output_m4a)]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def make_original_vocal_mp4(input_mp4: Path, accompaniment_wav: Path, vocals_wav: Path, output_mp4: Path, ffmpeg_bin: str) -> None:
    cmd = [
        ffmpeg_bin, "-y", "-i", str(input_mp4), "-i", str(accompaniment_wav), "-i", str(vocals_wav),
        "-filter_complex", "[1:a]extrastereo=m=1.35:c=0[acc];[acc][2:a]amix=inputs=2:normalize=0:duration=first[a]",
        "-map", "0:v:0", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "320k",
        "-movflags", "+faststart", str(output_mp4)
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def make_dialogue_instrumental_m4a(accompaniment_wav: Path, dialogue_wav: Path, output_m4a: Path, ffmpeg_bin: str, has_dialogue: bool) -> None:
    if not has_dialogue or not dialogue_wav.is_file():
        make_pseudo_spatial_m4a(accompaniment_wav, output_m4a, ffmpeg_bin)
        return
    cmd = [
        ffmpeg_bin, "-y", "-i", str(accompaniment_wav), "-i", str(dialogue_wav),
        "-filter_complex", "[0:a]extrastereo=m=1.35:c=0[acc];[acc][1:a]amix=inputs=2:normalize=0:duration=first[a]",
        "-map", "[a]", "-c:a", "aac", "-b:a", "320k", str(output_m4a)
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def get_safe_vocal_regions(lines: list[LyricLine], pad_start=0.4, pad_end=0.8, merge_gap=2.5) -> list[dict]:
    if not lines: return []
    regions = [{"start": max(0.0, line.start - pad_start), "end": line.end + pad_end} for line in lines]
    merged = [regions[0]]
    for curr in regions[1:]:
        prev = merged[-1]
        if curr["start"] - prev["end"] <= merge_gap:
            prev["end"] = max(prev["end"], curr["end"])
        else:
            merged.append(curr)
    return merged

def erase_lyrics_from_vocals(vocals_wav: Path, safe_regions: list[dict], output_dialogue_wav: Path) -> None:
    import soundfile as sf
    data, sr = sf.read(str(vocals_wav))
    for reg in safe_regions:
        s = max(0, int(reg["start"] * sr))
        e = min(len(data), int(reg["end"] * sr))
        if e > s: data[s:e] = 0
    sf.write(str(output_dialogue_wav), data, sr)

# ==========================================
# 主流程：核心對齊與對白辨識
# ==========================================

def run_unified_nightingale(
    *, settings: AppSettings, vocals_wav: Path, accompaniment_wav: Path, temp_input_mp4: Path,
    output_orig_instrumental: Path, output_dialogue_instrumental: Path, output_vocals: Path, output_mp4: Path,
    output_dialogue_vocals: Path, output_dereverbed_vocals: Path, output_lrc: Path, song_name: str, singer: str, lyrics_text: str,
    device_preference: str, alignment_backend: str,
) -> PipelineArtifacts:
    device = resolve_device(device_preference)
    compute_type = settings.whisper_compute_type if settings.whisper_compute_type != "auto" else compute_type_for(device)
    ffmpeg_bin = get_ffmpeg_bin(settings)

    print("[core:pipeline] 製作原唱音軌 MP4 (空間化伴奏 + 完整無損人聲)...", flush=True)
    make_original_vocal_mp4(temp_input_mp4, accompaniment_wav, vocals_wav, output_mp4, ffmpeg_bin)
    print("[core:pipeline] 產生純伴奏 A' (.original.instrumental.m4a)...", flush=True)
    make_pseudo_spatial_m4a(accompaniment_wav, output_orig_instrumental, ffmpeg_bin)
    shutil.copyfile(str(vocals_wav), str(output_vocals))

    import whisperx
    transcription_device = align_device_for(device)
    full_audio = whisperx.load_audio(str(vocals_wav))
    full_audio = highpass_filter(full_audio)
    full_audio = suppress_reverb(full_audio)
    full_audio = normalize_rms(full_audio)
    import numpy as np
    full_audio = np.nan_to_num(full_audio, nan=0.0, posinf=1.0, neginf=-1.0)
    peak = float(np.max(np.abs(full_audio), initial=0.0))
    if peak > 0.98:
        full_audio *= 0.98 / peak
    import soundfile as sf
    sf.write(str(output_dereverbed_vocals), full_audio, 16000)
    vocal_start, vocal_end = detect_vocal_region(full_audio)

    if not (lyrics_text and lyrics_text.strip()):
        print("[core:pipeline] 使用去混響人聲執行 Whisper 歌詞辨識...", flush=True)
        t_segs, det_lang = transcribe_segments(
            vocals_wav=output_dereverbed_vocals, model_name=settings.whisper_model, device=device, compute_type=compute_type,
            language=(settings.whisper_language or "").strip() or None, alignment_backend=alignment_backend,
        )
        lyrics_text = "\n".join(s.get("text", "") for s in t_segs if s.get("text"))

    detected_language = (settings.whisper_language or "").strip()
    if not detected_language:
        from .gpu import gpu_model
        with gpu_model(f"whisperx:tiny:{transcription_device}") as held:
            tiny_model = whisperx.load_model("tiny", device=transcription_device, compute_type=compute_type)
            held.append(tiny_model)
            detected_language = detect_language_multiwindow(tiny_model, full_audio)

    clean_lines = parse_lyrics_text(lyrics_text)
    text_transform, _ = build_text_transform(detected_language)
    is_cjk_lang = is_cjk(detected_language)
    aligned_lines = None

    if alignment_backend == "qwen":
        from .qwen_alignment import qwen_align_with_cpu_fallback, is_supported as qwen_supported
        if qwen_supported(detected_language):
            qwen_full_text = "\n".join(clean_lines)
            qwen_segments = [{"text": qwen_full_text, "start": vocal_start, "end": vocal_end}]
            try:
                aligned_payload = qwen_align_with_cpu_fallback(qwen_segments, full_audio, detected_language)
                aligned_lines = _map_qwen_units_to_lines(aligned_payload, clean_lines, detected_language)
            except Exception as e:
                print(f"[core:pipeline] Qwen 對齊失敗: {e}，改用 wav2vec2")

    if aligned_lines is None:
        # =========================================================================
        # 【致勝核心】：針對中文，徹底拋棄 Jieba，強制將每句歌詞拆解為 1:1 單一字元！
        # 讓 wav2vec2 直接為每個「字」提供真實的聲學時間點，絕不使用數學平均。
        # =========================================================================
        line_token_pairs = []
        for line in clean_lines:
            if detected_language in ["zh", "yue"]:
                # 遇到中文/粵語，逐字拆解並清理標點
                pairs = [(ch, clean_for_alignment(ch)) for ch in line]
                line_token_pairs.append(pairs)
            elif is_cjk_lang:
                # 日文維持原樣 (因為需要 Fugashi 轉換假名)
                line_token_pairs.append(tokenize_for_alignment(line, detected_language))

        if is_cjk_lang:
            full_alignment_text = "".join("".join(r for _, r in pairs) for pairs in line_token_pairs)
        else:
            full_alignment_text = " ".join(clean_lines)

        raw_segments = [{"text": full_alignment_text, "start": vocal_start, "end": vocal_end}]
        aligned_payload = align_with_backend(
            raw_segments, full_audio, align_lang_code(detected_language) if is_cjk_lang else detected_language,
            transcription_device, backend=alignment_backend if alignment_backend != "qwen" else "whisperx",
            model_name=align_model_for(detected_language) if is_cjk_lang else None
        )
        if is_cjk_lang:
            aligned_lines = _map_chars_to_lines_cjk(aligned_payload, clean_lines, line_token_pairs, detected_language)
        else:
            aligned_lines = _map_words_to_lines(aligned_payload, clean_lines)

    for line in aligned_lines:
        line.text = text_transform(line.text)
        for w in line.words:
            w["text"] = text_transform(w.get("text", w.get("word", "")))

    # 防重疊收束：確保不發生「偷跑」現象
    for i in range(1, len(aligned_lines)):
        prev = aligned_lines[i - 1]
        cur = aligned_lines[i]
        if cur.start < prev.end:
            overlap_pt = max(cur.start, prev.start + 0.1)
            prev.end = round(overlap_pt, 3)
            if prev.words and prev.words[-1]["end"] > prev.end:
                prev.words[-1]["end"] = prev.end
            if cur.start < prev.end:
                cur.start = prev.end
                if cur.words and cur.words[0]["start"] < cur.start:
                    cur.words[0]["start"] = cur.start

    aligned_lines = _split_long_segments(aligned_lines)
    aligned_lines = _merge_short_segments(aligned_lines)

    print("[core:pipeline] 產生安全發聲區間，提取純對白人聲 D (.dialogue.vocals.wav)...", flush=True)
    safe_vocal_regions = get_safe_vocal_regions(aligned_lines, pad_start=0.4, pad_end=0.8, merge_gap=2.5)
    erase_lyrics_from_vocals(vocals_wav, safe_vocal_regions, output_dialogue_vocals)
    dialogue_alignment_input = output_dereverbed_vocals.with_name("dialogue.vocals.dereverbed.wav")
    erase_lyrics_from_vocals(output_dereverbed_vocals, safe_vocal_regions, dialogue_alignment_input)

    print("[core:pipeline] 對純對白軌執行 ASR 語音辨識...", flush=True)
    dialogue_items = []
    try:
        diag_segs, _ = transcribe_segments(
            vocals_wav=dialogue_alignment_input, model_name="tiny", device=device, compute_type=compute_type,
            language=(settings.whisper_language or "").strip() or None, alignment_backend="whisperx",
        )
        for ds in diag_segs:
            txt = ds.get("text", "").strip()
            if txt: dialogue_items.append({"start": ds["start"], "end": ds["end"], "text": txt})
    except Exception as e:
        print(f"[core:pipeline] 對白辨識略過: {e}")

    has_dialogue = len(dialogue_items) > 0
    print(f"[core:pipeline] 產生含對白伴奏 A'' (.instrumental.m4a) [偵測到 {len(dialogue_items)} 段對白]...", flush=True)
    make_dialogue_instrumental_m4a(accompaniment_wav, output_dialogue_vocals, output_dialogue_instrumental, ffmpeg_bin, has_dialogue)
    dialogue_alignment_input.unlink(missing_ok=True)

    write_ktv_lrc(output_lrc, song_name, singer, vocal_end, aligned_lines, dialogue_items)

    return PipelineArtifacts(
        original_instrumental=output_orig_instrumental, dialogue_instrumental=output_dialogue_instrumental,
        vocals=output_vocals, original_vocal_mp4=output_mp4, dialogue_vocals=output_dialogue_vocals,
        dereverbed_vocals=output_dereverbed_vocals, lrc=output_lrc, detected_language=detected_language,
    )

class KTVProcessor:
    def __init__(self, settings: AppSettings, log_cb):
        self.settings = settings
        self.log = log_cb
        self.last_error = ""

    def sanitize_filename(self, name: str) -> str:
        return "".join([char for char in name if char not in r'\\/:*?"<>|'])

    def _extract_song_artist(self, title: str) -> tuple[str, str]:
        if " - " in title:
            artist, song = title.split(" - ", 1)
            return song.strip(), artist.strip()
        return title.strip(), ""

    def _probe_duration(self, media_path: Path) -> str:
        ffmpeg_bin = get_ffmpeg_bin(self.settings)
        ffmpeg_path = Path(ffmpeg_bin)
        ffprobe = ffmpeg_path.with_name("ffprobe.exe" if os.name == "nt" else "ffprobe") if ffmpeg_path.is_file() else "ffprobe"
        try:
            result = subprocess.run(
                [
                    str(ffprobe), "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1", str(media_path),
                ],
                check=True, capture_output=True, text=True, timeout=20,
            )
            return str(round(float(result.stdout.strip())))
        except (OSError, subprocess.SubprocessError, ValueError):
            return ""

    def process_song(self, url: str, manual_title: str = "", options: dict | None = None) -> bool:
        self.last_error = ""
        options = options or {}
        stems_val = options.get("stems")
        stems = 4 if str(stems_val if stems_val is not None else self.settings.separator_stems) == "4" else 2

        dev_val = options.get("device")
        device_pref = str(dev_val if dev_val and str(dev_val).lower() != "none" else self.settings.device_preference).lower()

        sep_val = options.get("separator_backend")
        separator_backend = str(sep_val if sep_val and str(sep_val).lower() != "none" else self.settings.separator_backend).lower()
        if separator_backend not in {"demucs", "uvr", "hybrid"}:
            separator_backend = str(self.settings.separator_backend or "demucs").lower()
            if separator_backend not in {"demucs", "uvr", "hybrid"}:
                separator_backend = "demucs"

        ali_val = options.get("alignment_backend")
        alignment_backend = str(ali_val if ali_val and str(ali_val).lower() != "none" else self.settings.alignment_backend).lower()
        lyrics_text = (options.get("lyrics_text") or "").strip()
        lyrics_url = (options.get("lrclib_url") or "").strip()
        singer = (options.get("singer") or "").strip()
        source_url = url.strip()
        youtube_title = str(options.get("youtube_title") or "")
        metadata: dict[str, str] = {}
        moved_files: list[Path] = []

        job_temp_dir: Path | None = None
        try:
            try:
                if options.get("youtube_title"):
                    youtube_title = str(options["youtube_title"])
                    if options.get("title_is_auto"):
                        youtube_metadata = ai_parse_youtube_title(youtube_title, singer) or parse_youtube_title(youtube_title, singer)
                    else:
                        youtube_metadata = parse_youtube_title(youtube_title, singer)
                else:
                    youtube_metadata = extract_youtube_metadata(source_url)
                    youtube_title = youtube_metadata.get("youtube_title", "")
                if options.get("title_is_auto") and youtube_metadata.get("title"):
                    artist = youtube_metadata.get("artist", "")
                    manual_title = f"{artist} - {youtube_metadata['title']}" if artist else youtube_metadata["title"]
                    if not singer:
                        singer = artist
                elif not manual_title.strip():
                    manual_title = youtube_metadata.get("title", "")
                    if not singer:
                        singer = youtube_metadata.get("artist", "")
            except Exception as error:
                self.log(f"⚠️ YouTube 標題解析失敗：{error}")
                youtube_metadata = {}
                if not manual_title.strip():
                    manual_title = source_url
            metadata = youtube_metadata
            safe_title = self.sanitize_filename(manual_title)
            self.log(f"目標歌曲：{safe_title}")
            song_name, inferred_singer = self._extract_song_artist(safe_title)
            if not singer: singer = inferred_singer
            if " - " not in safe_title and metadata.get("title"):
                song_name = metadata["title"]
            try:
                spotify_metadata = spotify_track_metadata(song_name, singer)
            except Exception as error:
                self.log(f"⚠️ Spotify metadata 查詢失敗：{error}")
                spotify_metadata = {}
            print(metadata)
            if spotify_metadata.get("title"):
                song_name = spotify_metadata["title"]
                print(spotify_metadata["title"])
            if spotify_metadata.get("artist") and not options.get("singer_is_manual"):
                singer = spotify_metadata["artist"]
            metadata = {**metadata, **spotify_metadata}
            if not youtube_title:
                youtube_title = str(options.get("youtube_title") or manual_title)
            if not lyrics_text and lyrics_url:
                try:
                    lyrics_text = fetch_lrclib_lyrics_by_url(lyrics_url) or ""
                except Exception as error:
                    self.log(f"⚠️ LRCLIB 指定網址讀取失敗：{error}")

            job_id = uuid.uuid4().hex
            job_temp_dir = self.settings.temp_base_dir / job_id
            job_temp_dir.mkdir(parents=True, exist_ok=True)

            temp_input = job_temp_dir / "input.mp4"
            temp_output_mp4 = job_temp_dir / "output.mp4"
            temp_orig_instrumental = job_temp_dir / "original.instrumental.m4a"
            temp_dialogue_instrumental = job_temp_dir / "instrumental.m4a"
            temp_vocals = job_temp_dir / "vocals.wav"
            temp_dialogue_vocals = job_temp_dir / "dialogue.vocals.wav"
            temp_dereverbed_vocals = job_temp_dir / "vocals.dereverbed.wav"
            temp_lrc = job_temp_dir / "lyrics.lrc"

            if options.get("local_file") and Path(options["local_file"]).is_file():
                self.log("步驟 1/8: 載入本機影片/音訊...")
                shutil.copy2(options["local_file"], temp_input)
            else:
                self.log("步驟 1/8: 下載影片...")
                download_youtube_video(url, temp_input, self.settings.ffmpeg_dir, self.settings.yt_dlp_path)

            if separator_backend in {"demucs", "hybrid"}:
                ensure_demucs_weights(self.settings.demucs_model, cache_dir=self.settings.demucs_cache_dir, log_cb=self.log)

            self.log(f"步驟 2/8: 分離人聲/伴奏 ({separator_backend})...")
            separated = separate_audio(
                input_path=temp_input, work_dir=job_temp_dir, settings=self.settings, stems=stems,
                device_preference=device_pref, backend=separator_backend, log_cb=self.log,
            )

            if not lyrics_text:
                self.log("步驟 3/8: 強化抓取正確歌詞...")
                lyrics_text = fetch_lrclib_lyrics(song_name, singer or "") or ""
                if lyrics_text:
                    self.log(f"✅ 成功獲取歌詞！(字數: {len(lyrics_text)})")
                else:
                    self.log("⚠️ 無法獲取歌詞！將啟用 Whisper 盲聽辨識。")
            else:
                self.log("步驟 3/8: 使用手動提供歌詞...")

            self.log(f"步驟 4-7/8: 去除人聲混響後辨識／對齊歌詞、音訊空間化與對白分離...")
            artifacts = run_unified_nightingale(
                settings=self.settings, vocals_wav=separated.vocals_path, accompaniment_wav=separated.accompaniment_path,
                temp_input_mp4=temp_input, output_orig_instrumental=temp_orig_instrumental,
                output_dialogue_instrumental=temp_dialogue_instrumental, output_vocals=temp_vocals,
                output_mp4=temp_output_mp4, output_dialogue_vocals=temp_dialogue_vocals, output_lrc=temp_lrc,
                output_dereverbed_vocals=temp_dereverbed_vocals,
                song_name=song_name or safe_title, singer=singer or "", lyrics_text=lyrics_text,
                device_preference=device_pref, alignment_backend=alignment_backend,
            )

            self.log(f"步驟 8/8: 歸檔 6 大最終成品檔案至歌曲庫...")
            final = self.settings.songs_dir / f"{safe_title}.mp4"
            if final.exists(): final = self.settings.songs_dir / f"{safe_title}_{job_id}.mp4"

            final_orig_instrumental = final.with_name(f"{final.stem}.original.instrumental.m4a")
            final_dialogue_instrumental = final.with_name(f"{final.stem}.instrumental.m4a")
            final_vocals = final.with_name(f"{final.stem}.vocals.wav")
            final_dialogue_vocals = final.with_name(f"{final.stem}.dialogue.vocals.wav")
            final_dereverbed_vocals = final.with_name(f"{final.stem}.vocals.dereverbed.wav")
            final_lrc = final.with_name(f"{final.stem}.lrc")

            self.settings.songs_dir.mkdir(parents=True, exist_ok=True)
            artifact_pairs = [
                (temp_orig_instrumental, final_orig_instrumental),
                (temp_dialogue_instrumental, final_dialogue_instrumental),
                (temp_vocals, final_vocals),
                (temp_dialogue_vocals, final_dialogue_vocals),
                (temp_dereverbed_vocals, final_dereverbed_vocals),
                (temp_lrc, final_lrc),
                (temp_output_mp4, final),
            ]
            for source, destination in artifact_pairs:
                if destination.exists():
                    raise FileExistsError(f"歌曲檔名衝突：{destination.name}")
                shutil.move(str(source), str(destination))
                moved_files.append(destination)

            from .library import plain_lyrics_from_lrc
            search_fields = search_metadata(song_name, artifacts.detected_language)
            separator_model = {
                "demucs": self.settings.demucs_model,
                "uvr": self.settings.uvr_model,
                "hybrid": f"{self.settings.demucs_model}+{self.settings.uvr_model}",
            }.get(separator_backend, separator_backend)
            upsert_catalog_entry(
                self.settings.library_index_path,
                {
                    "artist": singer,
                    "title": song_name,
                    "album": metadata.get("album", ""),
                    "release_year": metadata.get("release_year", ""),
                    "duration_seconds": self._probe_duration(final) or metadata.get("duration_seconds", ""),
                    "genre": metadata.get("genre", ""),
                    "lyrics": plain_lyrics_from_lrc(final_lrc),
                    "separator_model": separator_model,
                    "separator_mode": f"{separator_backend}:{stems}-stem",
                    "alignment_model": alignment_backend,
                    "video_filename": final.name,
                    "instrumental_filename": final_dialogue_instrumental.name,
                    "lyrics_filename": final_lrc.name,
                    "source_url": source_url,
                    "youtube_title": youtube_title,
                    "vocals_filename": final_vocals.name,
                    "dereverbed_vocals_filename": final_dereverbed_vocals.name,
                    "alignment_results_filename": final_lrc.name,
                    "processing_status": "complete",
                    **search_fields,
                },
            )

            self.log("✅ 製作完成！歌曲檔案與正式曲庫 CSV 已更新。")
            return True
        except subprocess.CalledProcessError as error:
            self.last_error = f"外部指令執行失敗 (Code {error.returncode}): {error}"
            self.log(f"❌ 執行失敗 (Code {error})")
            return False
        except Exception as error:
            self.last_error = str(error)
            self.log(f"❌ 錯誤: {error}")
            for path in moved_files:
                path.unlink(missing_ok=True)
            import traceback
            traceback.print_exc()
            return False
        finally:
            if job_temp_dir and job_temp_dir.exists():
                shutil.rmtree(job_temp_dir, ignore_errors=True)