#!/usr/bin/env python3
"""
generate_solfege_video.py - 简谱/节奏练习视频生成器

两种渲染引擎：
  --engine pillow    用 Pillow 手绘数字简谱，快速
  --engine lilypond  用 jianpu-ly + LilyPond 渲染专业排版简谱，美观（默认）

音频模式：
  默认：FluidSynth + FluidR3 GM soundfont（Yamaha Grand Piano，专业 MIDI 音色）
  --voice：AI 人声唱名（哆来咪发嗦啦西），需要 edge-tts + librosa
  --sine：简单正弦波（不依赖 FluidSynth）

Soundfont 自动查找路径（按优先级）：
  ~/.cache/solfege_soundfonts/FluidR3_GM.sf2
  /opt/homebrew/share/fluid-synth/sf2/*.sf2

输入: 简谱文本 或 歌曲名
输出: MP4 视频

依赖:
  pip install numpy scipy Pillow mido
  pip install edge-tts librosa soundfile  # --voice 模式必须
  brew install lilypond                   # LilyPond 引擎必须
  brew install fluidsynth                 # FluidSynth 钢琴音色必须
  pip install jianpu-ly                   # LilyPond 引擎必须
"""

import argparse
import asyncio
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np
import scipy.io.wavfile as wavfile
from PIL import Image, ImageDraw, ImageFont, ImageChops

# ════════════════════════════════════════════════
# 常量
# ════════════════════════════════════════════════

_BASE_FREQS = {
    'C': 261.63, 'D': 293.66, 'E': 329.63, 'F': 349.23,
    'G': 392.00, 'A': 440.00, 'B': 493.88
}
_MAJOR_SCALE = ['C', 'D', 'E', 'F', 'G', 'A', 'B']
_KEY_SEMITONES = {
    'C': 0, 'D': 2, 'E': 4, 'F': 5, 'G': 7, 'A': 9, 'B': 11,
    'bB': 10, 'bE': 3, 'bA': 8,
}
_SOLFEGE_NAMES = {1: 'do', 2: 're', 3: 'mi', 4: 'fa', 5: 'sol', 6: 'la', 7: 'si'}
# 用于 AI 人声唱名的中文谐音字（发音接近 do re mi fa sol la si）
_VOICE_SYLLABLES = {1: '哆', 2: '来', 3: '咪', 4: '发', 5: '嗦', 6: '啦', 7: '西'}
_VOICE_TTS_NAME  = 'zh-CN-XiaoxiaoNeural'   # 晓晓，自然女声
_VOICE_CACHE_DIR = os.path.expanduser('~/.cache/solfege_voice')
# XiaoxiaoNeural 说短单字时基频约在 E4 附近（实测约 330 Hz）
_VOICE_BASE_FREQ = 329.63   # E4

# 品牌配色
C_BG     = (255, 248, 238)   # 米黄底色
C_TEXT   = (92,  58,  30)    # 深棕
C_HL     = (232, 93,  38)    # 红松橙（高亮）
C_DIM    = (180, 150, 120)   # 灰棕次要
C_BAR    = (220, 200, 180)   # 进度条底色
C_WHITE  = (255, 255, 255)
C_BEAT   = (140, 100, 60)

# ════════════════════════════════════════════════
# 歌曲库
# ════════════════════════════════════════════════

def _lib_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'song_library.json')

def load_song_library():
    p = _lib_path()
    if not os.path.exists(p):
        return []
    with open(p, 'r', encoding='utf-8') as f:
        return json.load(f).get('songs', [])

def search_song(query: str):
    songs = load_song_library()
    q = query.strip().lower()
    for s in songs:
        if s['name'].lower() == q or any(a.lower() == q for a in s.get('aliases', [])):
            return s
    for s in songs:
        if q in s['name'].lower() or any(q in a.lower() for a in s.get('aliases', [])):
            return s
    return None

def list_songs():
    songs = load_song_library()
    lines = [f"  • {s['name']}（{'、'.join(s.get('aliases', [])[:3])}）— {s['bpm']}BPM {s['key']}调"
             for s in songs]
    return '\n'.join(lines)

# ════════════════════════════════════════════════
# 简谱解析
# ════════════════════════════════════════════════

def parse_notation(text: str) -> list:
    notes = []
    for raw in text.replace('|', ' | ').split():
        if raw == '|':
            continue
        tok = raw
        note = {'pitch': None, 'octave': 0, 'beats': 1.0, 'sharp': False}

        # 时值前缀必须先处理（/^4 要先剥离 / 才能识别 ^ 八度）
        if tok.startswith('//'): note['beats'] = 0.25; tok = tok[2:]
        elif tok.startswith('/'): note['beats'] = 0.5; tok = tok[1:]

        # 再处理八度前缀
        if tok.startswith('^^'):   note['octave'] = 2;  tok = tok[2:]
        elif tok.startswith('^'):  note['octave'] = 1;  tok = tok[1:]
        elif tok.startswith(',,'): note['octave'] = -2; tok = tok[2:]
        elif tok.startswith(','):  note['octave'] = -1; tok = tok[1:]

        if tok.startswith('#'):
            note['sharp'] = True; tok = tok[1:]

        ext = 0.0
        while tok.endswith('='): ext += 1.0; tok = tok[:-1]
        note['beats'] += ext

        if tok.endswith('.'):
            note['beats'] *= 1.5; tok = tok[:-1]

        if not tok: continue
        if tok == '0':         note['pitch'] = None
        elif tok.isdigit() and 1 <= int(tok) <= 7: note['pitch'] = int(tok)
        else: continue
        notes.append(note)
    return notes

# ════════════════════════════════════════════════
# 音高 → 频率
# ════════════════════════════════════════════════

def note_to_freq(pitch, octave, key='C', sharp=False):
    if pitch is None: return None
    f = _BASE_FREQS[_MAJOR_SCALE[pitch - 1]]
    ks = _KEY_SEMITONES.get(key, 0)
    if ks: f *= 2 ** (ks / 12)
    if sharp: f *= 2 ** (1 / 12)
    return f * (2 ** octave)

# ════════════════════════════════════════════════
# 音频合成
# ════════════════════════════════════════════════

def generate_tone(freq, duration, sr=44100, vol=0.65):
    n = max(1, int(sr * duration))
    if freq is None: return np.zeros(n)
    t = np.linspace(0, duration, n, endpoint=False)
    w = np.sin(2*np.pi*freq*t) + 0.15*np.sin(4*np.pi*freq*t) + 0.06*np.sin(6*np.pi*freq*t)
    w /= 1.21
    a = min(int(0.02*sr), n); d = min(int(0.04*sr), n-a); r = min(int(0.06*sr), n)
    env = np.ones(n)
    if a: env[:a] = np.linspace(0, 1, a)
    if d: env[a:a+d] = np.linspace(1, 0.85, d)
    env[a+d:] = 0.85
    if r and r < n: env[-r:] = np.linspace(0.85, 0, r)
    return w * env * vol

def generate_audio(notes, bpm, key='C', sr=44100):
    bd = 60.0 / bpm
    chunks = [generate_tone(note_to_freq(n['pitch'], n['octave'], key, n['sharp']),
                             n['beats']*bd, sr) for n in notes]
    return np.concatenate(chunks) if chunks else np.zeros(sr)


# ════════════════════════════════════════════════
# FluidSynth 钢琴音色（FluidR3 GM Soundfont）
# ════════════════════════════════════════════════

_SF2_SEARCH_PATHS = [
    os.path.expanduser('~/.cache/solfege_soundfonts/FluidR3_GM.sf2'),
    '/opt/homebrew/share/fluid-synth/sf2/FluidR3_GM.sf2',
    '/usr/share/sounds/sf2/FluidR3_GM.sf2',
    '/usr/share/sounds/sf2/FluidR3_GS.sf2',
    os.path.expanduser('~/Library/Audio/Sounds/Banks/FluidR3_GM.sf2'),
]

def find_sf2() -> str | None:
    """
    查找可用的 SF2/SF3 soundfont 文件。
    优先 FluidR3_GM（真实钢琴音色），跳过软链接（Homebrew 可能创建循环软链接）。
    """
    import glob as _glob

    # ── 已知 FluidR3_GM 路径（优先）────────────────────────────────────────────
    for p in _SF2_SEARCH_PATHS:
        if os.path.exists(p) and not os.path.islink(p) and os.path.getsize(p) > 10_000_000:
            return p

    # ── Homebrew Cellar 扫描（跳过软链接）──────────────────────────────────────
    for cellar_base in ['/opt/homebrew/Cellar/fluid-synth', '/usr/local/Cellar/fluid-synth']:
        if not os.path.isdir(cellar_base):
            continue
        sf2_files = _glob.glob(os.path.join(cellar_base, '*', 'share', '**', '*.sf2'),
                               recursive=True)
        # FluidR3 优先
        for p in sorted(sf2_files):
            if 'FluidR3' in os.path.basename(p) and not os.path.islink(p):
                try:
                    if os.path.getsize(p) > 10_000_000:
                        return p
                except OSError:
                    pass
        # 任意非链接 sf2
        for p in sorted(sf2_files):
            if not os.path.islink(p):
                try:
                    if os.path.getsize(p) > 10_000_000:
                        return p
                except OSError:
                    pass
        # sf3 fallback
        for p in sorted(_glob.glob(os.path.join(cellar_base, '*', 'share', '**', '*.sf3'),
                                   recursive=True)):
            if not os.path.islink(p):
                try:
                    if os.path.getsize(p) > 1_000_000:
                        print(f'  ⚠️  使用 SF3 音色库：{os.path.basename(p)}（建议改用 FluidR3_GM.sf2）',
                              file=sys.stderr)
                        return p
                except OSError:
                    pass

    # ── /opt/homebrew/share 目录扫描（跳过软链接）──────────────────────────────
    for brew_dir in ['/opt/homebrew/share/fluid-synth/sf2', '/usr/local/share/fluid-synth/sf2']:
        if os.path.isdir(brew_dir):
            for f in os.listdir(brew_dir):
                p = os.path.join(brew_dir, f)
                if f.endswith('.sf2') and not os.path.islink(p):
                    try:
                        if os.path.getsize(p) > 10_000_000:
                            return p
                    except OSError:
                        pass
    return None


def generate_fluidsynth_audio(notes, bpm, key='C', sr=44100,
                               sf2_path: str = None,
                               program: int = 0) -> np.ndarray:
    """
    用 FluidSynth + GM soundfont 生成钢琴音频。
    program=0: Acoustic Grand Piano（Yamaha Grand）
    返回 float32 numpy 数组，失败则退回正弦波。
    """
    if sf2_path is None:
        sf2_path = find_sf2()
    if sf2_path is None:
        print('  ⚠️  找不到 SF2 soundfont，退回正弦波', file=sys.stderr)
        return generate_audio(notes, bpm, key, sr)

    fluidsynth_bin = shutil.which('fluidsynth')
    if fluidsynth_bin is None:
        print('  ⚠️  未找到 fluidsynth 命令，退回正弦波', file=sys.stderr)
        return generate_audio(notes, bpm, key, sr)

    try:
        import mido
    except ImportError:
        print('  ⚠️  缺少 mido 包（pip install mido），退回正弦波', file=sys.stderr)
        return generate_audio(notes, bpm, key, sr)

    # 1. 生成 MIDI 文件
    with tempfile.NamedTemporaryFile(suffix='.mid', delete=False) as mf:
        mid_path = mf.name
    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as wf:
        wav_path = wf.name

    try:
        ticks_per_beat = 480
        tempo = int(60_000_000 / bpm)   # microseconds per beat

        mid = mido.MidiFile(type=0, ticks_per_beat=ticks_per_beat)
        track = mido.MidiTrack()
        mid.tracks.append(track)

        track.append(mido.MetaMessage('set_tempo', tempo=tempo, time=0))
        track.append(mido.Message('program_change', channel=0, program=program, time=0))

        # 音调偏移（调性移调）
        semitone_shift = _KEY_SEMITONES.get(key, 0)

        for n in notes:
            dur_ticks = int(n['beats'] * ticks_per_beat)
            if dur_ticks < 1:
                dur_ticks = 1

            pitch = n.get('pitch')
            if pitch is None:
                # 休止符：不发音，但保留时长
                track.append(mido.Message('note_on',  channel=0, note=60, velocity=0, time=0))
                track.append(mido.Message('note_off', channel=0, note=60, velocity=0, time=dur_ticks))
                continue

            # 简谱音级 → MIDI 音高
            # 简谱 1=do，对应 C 大调：C4=60
            # 音级到半音偏移（C大调下）
            scale_semitones = [0, 2, 4, 5, 7, 9, 11]  # do re mi fa sol la si
            pitch_idx = int(pitch) - 1  # 1-7 → 0-6
            if pitch_idx < 0 or pitch_idx > 6:
                pitch_idx = 0

            # 基础 MIDI 音高：C4 = 60，中央 C
            octave_base = 60 + scale_semitones[pitch_idx] + semitone_shift
            # 八度调整：n['octave'] = 0 是中央八度
            octave_offset = n.get('octave', 0) * 12
            # sharp 升半音
            sharp_offset = 1 if n.get('sharp', False) else 0

            midi_note = octave_base + octave_offset + sharp_offset
            # 钳位到合理范围
            midi_note = max(21, min(108, midi_note))

            velocity = 85
            track.append(mido.Message('note_on',  channel=0, note=midi_note, velocity=velocity, time=0))
            track.append(mido.Message('note_off', channel=0, note=midi_note, velocity=0,        time=dur_ticks))

        track.append(mido.MetaMessage('end_of_track', time=0))
        mid.save(mid_path)

        # 2. FluidSynth 渲染 MIDI → WAV
        cmd = [fluidsynth_bin, '-F', wav_path, '-r', str(sr), sf2_path, mid_path]
        result = subprocess.run(cmd, capture_output=True, timeout=60)
        if result.returncode != 0 or not os.path.exists(wav_path):
            print(f'  ⚠️  fluidsynth 渲染失败，退回正弦波', file=sys.stderr)
            return generate_audio(notes, bpm, key, sr)

        # 3. 读取 WAV
        rate, data = wavfile.read(wav_path)
        if data.ndim > 1:
            data = data.mean(axis=1)  # stereo → mono
        audio = data.astype(np.float32) / 32768.0

        # 补零至预期长度（fluidsynth 可能多渲染一点 reverb 尾音）
        bd = 60.0 / bpm
        expected_samples = int(sum(n['beats'] for n in notes) * bd * sr) + int(0.5 * sr)
        if len(audio) < expected_samples:
            audio = np.concatenate([audio, np.zeros(expected_samples - len(audio), dtype=np.float32)])

        print(f'  ✅ FluidSynth 渲染完成 ({len(audio)/sr:.1f}s, prog={program})')
        return audio

    except Exception as e:
        print(f'  ⚠️  FluidSynth 异常 ({e})，退回正弦波', file=sys.stderr)
        return generate_audio(notes, bpm, key, sr)
    finally:
        for p in [mid_path, wav_path]:
            try: os.unlink(p)
            except: pass


# ════════════════════════════════════════════════
# AI 人声唱名：WORLD 声码器 + edge-tts（speech-to-singing）
#
# 原理：
#   1. edge-tts 生成晓晓读"哆来咪发嗦啦西"的真实人声（保留音色、气息、辅音）
#   2. PyWORLD 分析：把音频拆成 F0（音高曲线）+ 声道包络 + 气声
#   3. 把 F0 整条曲线替换成目标音乐音高（精确平直线）
#   4. WORLD 重新合成：保留晓晓声线，但唱在正确音高上
#
# 这是学术论文里 speech-to-singing 的标准做法。
# ════════════════════════════════════════════════

def _fix_pkg_resources():
    """Python 3.13 中 pyworld 依赖 pkg_resources，需要 shim 修复。"""
    import sys, importlib.metadata
    if 'pkg_resources' in sys.modules:
        return
    class _Dist:
        def __init__(self, v): self.version = v
    class _PR:
        @staticmethod
        def get_distribution(name):
            try: v = importlib.metadata.version(name)
            except Exception: v = '0.0.0'
            return _Dist(v)
        @staticmethod
        def require(r): pass
    sys.modules['pkg_resources'] = _PR()


def _world_pitch_replace(speech: np.ndarray, sr: int,
                          target_hz: float, out_samples: int) -> np.ndarray:
    """
    WORLD 声码器：将 speech 的 F0 替换为 target_hz，重新合成。
    返回长度为 out_samples 的 float32 数组。
    """
    _fix_pkg_resources()
    import pyworld as pw
    from scipy import signal as sp_sig

    WORLD_SR = 16000   # WORLD 分析最佳采样率
    # 降采样至 16kHz
    n16 = int(len(speech) * WORLD_SR / sr)
    x16 = sp_sig.resample(speech.astype(np.float64), n16)
    pk = np.max(np.abs(x16))
    if pk > 0: x16 /= pk

    # 分析
    f0, t = pw.dio(x16, WORLD_SR, f0_floor=60, f0_ceil=800)
    f0 = pw.stonemask(x16, f0, t, WORLD_SR)
    sp = pw.cheaptrick(x16, f0, t, WORLD_SR)
    ap = pw.d4c(x16, f0, t, WORLD_SR)

    # 替换音高：有声帧→目标音高，无声帧保持为 0
    # 至少确保整段都是有声（让全部时间都在唱目标音高）
    voiced_ratio = np.mean(f0 > 0)
    if voiced_ratio < 0.4:
        # TTS 辅音段较多，强制整体有声
        f0_new = np.full_like(f0, target_hz)
    else:
        f0_new = np.where(f0 > 0, target_hz, 0.0)

    # 重新合成
    y16 = pw.synthesize(f0_new, sp, ap, WORLD_SR)

    # 恢复到原采样率
    y = sp_sig.resample(y16, int(len(y16) * sr / WORLD_SR))

    # 截取 / 补零至目标长度
    if len(y) >= out_samples:
        # 加淡出避免截断噪音
        fade = min(int(0.03 * sr), out_samples // 4)
        out = y[:out_samples].copy()
        out[-fade:] *= np.linspace(1, 0, fade)
        return out.astype(np.float32) * 0.82
    pad = np.zeros(out_samples - len(y))
    return np.concatenate([y, pad]).astype(np.float32) * 0.82


def build_voice_bank(sr: int = 44100) -> dict | None:
    """
    用 edge-tts 预生成7个唱名的语音样本，缓存到磁盘。
    返回 {pitch_num: np.ndarray}，失败返回 None。
    """
    try:
        import edge_tts
        import librosa
        import soundfile as sf
        _fix_pkg_resources()
        import pyworld  # noqa: 验证可用
    except ImportError as e:
        print(f'  ⚠️  缺少依赖（{e}），voice 模式不可用', file=sys.stderr)
        return None

    os.makedirs(_VOICE_CACHE_DIR, exist_ok=True)
    bank = {}

    async def _gen(pitch_num: int, syllable: str):
        wav_path = os.path.join(_VOICE_CACHE_DIR, f'raw_{pitch_num}.wav')
        if not os.path.exists(wav_path):
            mp3_path = wav_path.replace('.wav', '.mp3')
            # 语速慢一点，让元音更饱满
            comm = edge_tts.Communicate(syllable, _VOICE_TTS_NAME, rate='-40%')
            await comm.save(mp3_path)
            y, orig_sr = librosa.load(mp3_path, sr=None, mono=True)
            y = librosa.resample(y, orig_sr=orig_sr, target_sr=sr)
            # 去头尾静音
            y, _ = librosa.effects.trim(y, top_db=20)
            sf.write(wav_path, y, sr)
            print(f'    生成 {syllable} ({len(y)/sr:.2f}s)')
        else:
            y, _ = librosa.load(wav_path, sr=sr, mono=True)
        bank[pitch_num] = y.astype(np.float32)

    async def _all():
        await asyncio.gather(*[_gen(p, s) for p, s in _VOICE_SYLLABLES.items()])

    try:
        asyncio.run(_all())
        print(f'  🎤 人声库就绪（WORLD 声码器模式，缓存于 {_VOICE_CACHE_DIR}）')
        return bank
    except Exception as e:
        print(f'  ⚠️  人声库生成失败（{e}）', file=sys.stderr)
        return None


def _make_voice_chunk(pitch_num: int, octave: int, key: str, sharp: bool,
                       beats: float, bpm: float, sr: int,
                       voice_bank: dict) -> np.ndarray:
    """
    单个音符的人声合成：
    WORLD 把 edge-tts 音节的 F0 替换成目标音高 → 保留晓晓音色但唱准音高。
    若时长比语音样本长，后半段用正弦波延音衔接。
    """
    target_freq = note_to_freq(pitch_num, octave, key, sharp)
    dur_s = beats * 60.0 / bpm
    dur_samples = max(1, int(dur_s * sr))

    if target_freq is None:
        return np.zeros(dur_samples, dtype=np.float32)

    raw = voice_bank.get(pitch_num, voice_bank.get(1))
    if raw is None or len(raw) == 0:
        return generate_tone(target_freq, dur_s, sr).astype(np.float32)

    try:
        singing = _world_pitch_replace(raw, sr, target_freq, min(dur_samples, len(raw) * 2))
    except Exception as e:
        print(f'  WORLD 合成失败 ({e})，用正弦波代替', file=sys.stderr)
        return generate_tone(target_freq, dur_s, sr).astype(np.float32)

    voice_len = len(singing)
    if voice_len >= dur_samples:
        return singing[:dur_samples]

    # 语音部分结束后接正弦波延音
    sustain_s = (dur_samples - voice_len) / sr
    sustain = generate_tone(target_freq, sustain_s, sr, vol=0.55).astype(np.float32)
    xf = min(int(0.015 * sr), voice_len, len(sustain))
    if xf > 0:
        singing[-xf:] *= np.linspace(1, 0, xf)
        sustain[:xf] *= np.linspace(0, 1, xf)

    return np.concatenate([singing, sustain])[:dur_samples]


def generate_voice_audio(notes, bpm, key='C', sr=44100, voice_bank=None):
    """用 WORLD speech-to-singing 合成完整音频。"""
    bd = 60.0 / bpm
    chunks = []
    for n in notes:
        if n['pitch'] is None:
            chunks.append(np.zeros(max(1, int(n['beats'] * bd * sr)), dtype=np.float32))
        else:
            chunks.append(_make_voice_chunk(n['pitch'], n['octave'], key, n['sharp'],
                                             n['beats'], bpm, sr, voice_bank))
    return np.concatenate(chunks) if chunks else np.zeros(sr, dtype=np.float32)

# ════════════════════════════════════════════════
# 时间轴
# ════════════════════════════════════════════════

def build_timings(notes, bpm):
    bd = 60.0 / bpm
    timings, t = [], 0.0
    for n in notes:
        dur = n['beats'] * bd
        timings.append({'start': t, 'end': t+dur, 'note': n})
        t += dur
    return timings, t

# ════════════════════════════════════════════════
# V 形节拍符（Pillow 引擎用）
# ════════════════════════════════════════════════

def build_beat_markers(notes, time_sig='4/4'):
    markers, eq = [], []
    for n in notes:
        b = n['beats']
        is_rest = (n['pitch'] is None)
        if is_rest:               markers.append({'symbols': ['○']}); eq=[]
        elif abs(b-0.25)<0.01:    markers.append({'symbols': ['⌃']})
        elif abs(b-0.5)<0.01:
            eq.append(len(markers)); markers.append({'symbols': ['⌣']})
            if len(eq)==2: eq=[]
        elif abs(b-1.0)<0.01:     markers.append({'symbols': ['v']}); eq=[]
        elif abs(b-1.5)<0.01:     markers.append({'symbols': ['v','·']}); eq=[]
        elif abs(b-2.0)<0.01:     markers.append({'symbols': ['v','—']}); eq=[]
        elif abs(b-3.0)<0.01:     markers.append({'symbols': ['v','—','—']}); eq=[]
        elif abs(b-4.0)<0.01:     markers.append({'symbols': ['v','—','—','—']}); eq=[]
        else:                     markers.append({'symbols': ['v']}); eq=[]
    return markers

def _draw_v(draw, cx, cy, sym, color, size=16):
    if sym == 'v':
        pts = [(cx-size, cy), (cx, cy+size*1.2), (cx+size, cy)]
        draw.line([pts[0], pts[1], pts[2]], fill=color, width=3)
    elif sym == '⌣':
        draw.arc([cx-size, cy-size//2, cx+size, cy+size], 0, 180, fill=color, width=3)
    elif sym == '⌃':
        pts = [(cx-size//2, cy+size), (cx, cy), (cx+size//2, cy+size)]
        draw.line([pts[0], pts[1], pts[2]], fill=color, width=2)
    elif sym == '—':
        draw.line([(cx-size, cy+size//2), (cx+size, cy+size//2)], fill=color, width=3)
    elif sym == '·':
        draw.ellipse([cx-3, cy+size//2-3, cx+3, cy+size//2+3], fill=color)
    elif sym == '○':
        r = size//2
        draw.ellipse([cx-r, cy, cx+r, cy+size], outline=color, width=2)

# ════════════════════════════════════════════════
# 字体加载
# ════════════════════════════════════════════════

def _font(size):
    for p in ['/System/Library/Fonts/PingFang.ttc',
              '/System/Library/Fonts/STHeiti Light.ttc',
              '/System/Library/Fonts/Helvetica.ttc',
              '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf']:
        if os.path.exists(p):
            try: return ImageFont.truetype(p, size)
            except: pass
    return ImageFont.load_default()

# ════════════════════════════════════════════════
# Pillow 引擎：绘制单帧
# ════════════════════════════════════════════════

def create_frame_pillow(notes, current_idx, meta, beat_markers=None,
                        width=1080, height=1920):
    img = Image.new('RGB', (width, height), C_BG)
    draw = ImageDraw.Draw(img)

    f64 = _font(64); f40 = _font(40); f130 = _font(130)
    f32 = _font(32); f52 = _font(52)

    draw.text((width//2, 110), meta.get('title','视唱练习'), font=f64, fill=C_TEXT, anchor='mm')

    ts = meta.get('time_sig','4/4').split('/')
    if len(ts)==2:
        draw.text((60, 60), ts[0], font=f52, fill=C_DIM, anchor='mm')
        draw.line([(38, 72),(82, 72)], fill=C_DIM, width=3)
        draw.text((60, 98), ts[1], font=f52, fill=C_DIM, anchor='mm')
    draw.text((width-60, 60), f"1={meta.get('key','C')}", font=f52, fill=C_DIM, anchor='rm')

    sub = f"♩= {meta['bpm']}  ·  第 {meta['rep_cur']} 遍 / 共 {meta['rep_total']} 遍"
    draw.text((width//2, 200), sub, font=f40, fill=C_DIM, anchor='mm')

    n = len(notes); cols = min(n, 8)
    cell_w = (width-80) // cols; cell_h = 260; area_y = height//3 - 40

    for i, note in enumerate(notes):
        row, col = i//cols, i%cols
        cx = 40 + col*cell_w + cell_w//2
        cy = area_y + row*cell_h + 100

        is_cur = (i == current_idx); is_rest = (note['pitch'] is None)
        r = 70
        if is_cur:
            draw.ellipse([cx-r, cy-r, cx+r, cy+r], fill=C_HL)
            tc, bc = C_WHITE, C_HL
        else:
            tc, bc = C_TEXT, C_BEAT

        draw.text((cx, cy), '0' if is_rest else str(note['pitch']),
                  font=f130, fill=tc, anchor='mm')

        if note['octave'] == 1:
            draw.ellipse([cx-6, cy-r-20, cx+6, cy-r-8], fill=tc)
        elif note['octave'] == -1:
            draw.ellipse([cx-6, cy+r+8, cx+6, cy+r+20], fill=tc)

        if note['beats'] <= 0.5:
            for li in range(1 if note['beats']==0.5 else 2):
                draw.line([cx-25, cy+r+28+li*10, cx+25, cy+r+28+li*10], fill=tc, width=4)
        if note['beats'] >= 2.0 and col < cols-1:
            draw.line([cx+r+8, cy, cx+cell_w-20, cy], fill=C_DIM, width=3)
        if note['sharp']:
            draw.text((cx+r+5, cy-20), '#', font=f32, fill=C_HL)

        if beat_markers and i < len(beat_markers):
            syms = beat_markers[i]['symbols']
            by = cy + r + 45; sz = 16
            tw = len(syms)*(sz*3)
            for j, sym in enumerate(syms):
                sx = cx - tw//2 + j*(sz*3) + sz
                _draw_v(draw, sx, by, sym, bc, sz)

        if is_cur and not is_rest:
            draw.text((cx, cy+r+95), _SOLFEGE_NAMES.get(note['pitch'],''),
                      font=f32, fill=C_HL, anchor='mm')

    if current_idx >= 0:
        n_now = notes[current_idx]
        if n_now['pitch'] is not None:
            draw.text((width//2, height-280), _SOLFEGE_NAMES.get(n_now['pitch'],''),
                      font=_font(90), fill=C_HL, anchor='mm')

    prog = max(0.0, min(1.0, meta.get('progress', 0.0)))
    by = height-150; bx0, bx1 = 60, width-60
    draw.rectangle([bx0, by, bx1, by+18], fill=C_BAR)
    if prog > 0:
        draw.rectangle([bx0, by, bx0+int((bx1-bx0)*prog), by+18], fill=C_HL)
    draw.text((width//2, height-100),
              f"{meta.get('elapsed',0):.0f}s / {meta.get('total_dur',1):.0f}s",
              font=f32, fill=C_DIM, anchor='mm')
    return img

# ════════════════════════════════════════════════
# LilyPond 引擎：简谱 → jianpu-ly 格式
# ════════════════════════════════════════════════

def _note_to_jianpu_tokens(note: dict) -> list:
    """将单个音符转换为 jianpu-ly token 列表。
    半音符=[ '1', '-' ]，全音符=[ '1', '-', '-', '-' ]。
    """
    pitch = note['pitch']
    octave = note['octave']
    beats = note['beats']
    sharp = note['sharp']

    # 音符基础
    if pitch is None:
        base = '0'
    else:
        base = str(pitch)
        if sharp:
            base = '#' + base
        # 八度（jianpu-ly 用 ' 上八度，, 下八度，接在音符后面）
        if octave == 1:    base += "'"
        elif octave == 2:  base += "''"
        elif octave == -1: base += ','
        elif octave == -2: base += ',,'

    # 时值前缀
    if abs(beats - 0.25) < 0.01:
        tokens = ['s' + base]
    elif abs(beats - 0.5) < 0.01:
        tokens = ['q' + base]
    elif abs(beats - 1.0) < 0.01:
        tokens = [base]
    elif abs(beats - 1.5) < 0.01:
        tokens = [base + '.']
    elif abs(beats - 2.0) < 0.01:
        tokens = [base, '-']
    elif abs(beats - 3.0) < 0.01:
        tokens = [base, '-', '-']
    elif abs(beats - 4.0) < 0.01:
        tokens = [base, '-', '-', '-']
    else:
        # 其他时值：近似为四分音符
        tokens = [base]

    return tokens


def notes_to_jianpu_ly(notes: list, key='C', time_sig='4/4',
                        title='', bpm=60, bars_per_line=4) -> str:
    """将解析后的音符列表转换为 jianpu-ly 输入文本。"""
    lines = []
    if title:
        lines.append(f'title={title}')
    lines.append(f'SeparateTimesig 1={key} {time_sig}')
    lines.append('NoBarNums')
    lines.append('')

    try:
        beats_per_bar = int(time_sig.split('/')[0])
    except:
        beats_per_bar = 4

    # 按小节分组生成 token
    current_bar_beats = 0.0
    bar_tokens = []
    bar_count = 0
    all_bar_lines = []

    for note in notes:
        toks = _note_to_jianpu_tokens(note)
        bar_tokens.extend(toks)
        current_bar_beats += note['beats']

        if current_bar_beats >= beats_per_bar - 0.01:
            all_bar_lines.append(bar_tokens[:])
            bar_tokens = []
            current_bar_beats = 0.0
            bar_count += 1

    if bar_tokens:
        all_bar_lines.append(bar_tokens)

    # 按 bars_per_line 分行，加换行
    for i, bar in enumerate(all_bar_lines):
        lines.append(' '.join(bar) + ' |')
        if (i+1) % bars_per_line == 0 and i < len(all_bar_lines)-1:
            lines.append('LP:')
            lines.append('\\break')
            lines.append(':LP')
            lines.append('')

    return '\n'.join(lines) + '\n'


def _build_ly_content(jianpu_text: str, work_dir: str) -> str:
    """运行 jianpu-ly，返回修改后的 .ly 文件路径。"""
    input_path = os.path.join(work_dir, 'score.jianpu')
    ly_path    = os.path.join(work_dir, 'score.ly')

    with open(input_path, 'w', encoding='utf-8') as f:
        f.write(jianpu_text)

    with open(ly_path, 'w', encoding='utf-8') as out_f:
        with open(input_path, 'r', encoding='utf-8') as in_f:
            r = subprocess.run(
                [sys.executable, '-m', 'jianpu_ly'],
                stdin=in_f, stdout=out_f, stderr=subprocess.PIPE,
                cwd=work_dir, timeout=30
            )
    if r.returncode != 0:
        raise RuntimeError(f'jianpu-ly failed: {r.stderr.decode()}')

    with open(ly_path, 'r', encoding='utf-8') as f:
        ly = f.read()

    # 去水印
    ly = ly.replace(
        '% un-comment the next line to remove Lilypond tagline:\n% \\header { tagline="" }',
        '\\header { tagline="" }'
    )
    if 'tagline=""' not in ly:
        ly = ly.replace('\\pointAndClickOff',
                        '\\header { tagline="" }\n\\pointAndClickOff')

    # 字号
    ly = ly.replace('#(set-global-staff-size 20)', '#(set-global-staff-size 28)')
    ly = ly.replace('#(set-global-staff-size 26)', '#(set-global-staff-size 28)')

    # 纸张（A4 横向）
    paper_block = (
        '  #(set-default-paper-size "a4" \'landscape)\n'
        '  ragged-last-bottom = ##t\n'
        '  ragged-bottom = ##t\n'
        '  top-margin = 5\\mm\n'
        '  bottom-margin = 5\\mm\n'
        '  left-margin = 8\\mm\n'
        '  right-margin = 8\\mm\n'
    )
    ly = ly.replace(
        'print-all-headers = ##t %% allow per-score headers',
        'print-all-headers = ##t\n' + paper_block
    )

    # 修复降号/升号在 \markup{} 内的 LilyPond 兼容性问题
    # jianpu-ly 生成 \mark \markup{1=E\flat 4/4} 但部分 LilyPond 版本不接受
    # 改为 Unicode 符号 ♭ ♯，更简单且兼容所有版本
    import re as _re
    def _fix_key_markup(m):
        content = m.group(1)
        content = content.replace(r'\flat', '♭').replace(r'\sharp', '♯')
        return r'\mark \markup{"' + content + '"}'
    ly = _re.sub(
        r'\\mark \\markup\{([^}]+(?:\\flat|\\sharp)[^}]*)\}',
        _fix_key_markup, ly
    )

    with open(ly_path, 'w', encoding='utf-8') as f:
        f.write(ly)
    return ly_path


def render_score_lilypond(jianpu_text: str, work_dir: str, resolution: int = 250) -> tuple:
    """
    运行 jianpu-ly + LilyPond，同时生成 PNG 和 SVG。
    返回 (png_path, svg_path)。
    """
    ly_path = _build_ly_content(jianpu_text, work_dir)
    lilypond = shutil.which('lilypond') or '/opt/homebrew/bin/lilypond'

    # 生成 PNG
    r_png = subprocess.run(
        [lilypond, '--png', f'-dresolution={resolution}', ly_path],
        capture_output=True, cwd=work_dir, timeout=90
    )
    if r_png.returncode != 0:
        raise RuntimeError(f'lilypond PNG failed:\n{r_png.stderr.decode()[-800:]}')

    # 生成 SVG（同一 .ly，坐标体系一致）
    r_svg = subprocess.run(
        [lilypond, '--svg', ly_path],
        capture_output=True, cwd=work_dir, timeout=90
    )
    # SVG 失败不阻塞，降级为估算法

    # 找 PNG
    png_path = None
    for fname in ['score.png', 'score-1.png']:
        p = os.path.join(work_dir, fname)
        if os.path.exists(p):
            png_path = p; break
    if not png_path:
        pngs = sorted(f for f in os.listdir(work_dir) if f.endswith('.png'))
        if pngs: png_path = os.path.join(work_dir, pngs[0])
    if not png_path:
        raise FileNotFoundError('LilyPond 未生成 PNG')

    # 找 SVG
    svg_path = None
    for fname in ['score.svg', 'score-1.svg']:
        p = os.path.join(work_dir, fname)
        if os.path.exists(p):
            svg_path = p; break
    if not svg_path:
        svgs = sorted(f for f in os.listdir(work_dir) if f.endswith('.svg'))
        if svgs: svg_path = os.path.join(work_dir, svgs[0])

    return png_path, svg_path


def crop_whitespace(img: Image.Image, padding=15):
    """裁剪图片四周白色空白。返回 (cropped_img, (x0, y0)) crop 偏移。"""
    bg = Image.new(img.mode, img.size, (255, 255, 255))
    diff = ImageChops.difference(img, bg)
    bbox = diff.convert('L').getbbox()
    if bbox:
        x0 = max(0, bbox[0] - padding)
        y0 = max(0, bbox[1] - padding)
        x1 = min(img.width,  bbox[2] + padding)
        y1 = min(img.height, bbox[3] + padding)
        return img.crop((x0, y0, x1, y1)), (x0, y0)
    return img, (0, 0)


# ════════════════════════════════════════════════
# SVG 精确坐标解析
# ════════════════════════════════════════════════

def parse_note_positions_from_svg(svg_path: str, notes: list,
                                   resolution: int, crop_offset: tuple,
                                   scale: float) -> list | None:
    """
    从 LilyPond SVG 中提取每个音符数字的精确像素坐标。

    原理：jianpu-ly 将音符渲染为 sans-serif bold 的 <text> 元素（数字 0-7）。
    SVG 坐标体系与 PNG 像素坐标可通过 viewBox + 分辨率换算。
    最终坐标 = (SVG坐标 → 全幅PNG像素) - crop偏移，再乘缩放。

    返回 [{'x': float, 'y': float}, ...] 与 notes 等长；失败返回 None。
    """
    import xml.etree.ElementTree as ET
    if not svg_path or not os.path.exists(svg_path):
        return None

    try:
        tree = ET.parse(svg_path)
        root = tree.getroot()

        # SVG 可能有命名空间前缀
        ns_pre = ''
        tag = root.tag
        if tag.startswith('{'):
            ns_pre = tag[:tag.index('}') + 1]

        # 读 viewBox 和物理尺寸
        vb = root.get('viewBox', '').split()
        if len(vb) < 4:
            return None
        vb_w, vb_h = float(vb[2]), float(vb[3])

        import re
        w_mm = float(re.search(r'[\d.]+', root.get('width',  '297mm')).group())
        h_mm = float(re.search(r'[\d.]+', root.get('height', '210mm')).group())

        # 全幅 PNG 尺寸（mm → px at resolution DPI）
        px_per_mm = resolution / 25.4
        full_w_px = w_mm * px_per_mm
        full_h_px = h_mm * px_per_mm
        sx = full_w_px / vb_w
        sy = full_h_px / vb_h

        # 遍历所有 <g transform="translate(x,y)"><text> 找音符数字
        raw_positions = []
        for g in root.iter(f'{ns_pre}g'):
            m = re.match(r'translate\(([\d.eE+-]+),\s*([\d.eE+-]+)\)',
                          g.get('transform', ''))
            if not m:
                continue
            tx, ty = float(m.group(1)), float(m.group(2))

            for text_el in g.findall(f'{ns_pre}text'):
                # 仅 sans-serif bold = 音符数字（区分于标题/调号的 serif 字体）
                font = text_el.get('font-family', '')
                weight = text_el.get('font-weight', '')
                if 'sans-serif' not in font or weight != 'bold':
                    continue

                for tspan in text_el.findall(f'{ns_pre}tspan'):
                    content = (tspan.text or '').strip()
                    if content in {'0', '1', '2', '3', '4', '5', '6', '7'}:
                        # 转换为全幅 PNG 像素坐标
                        x_full = tx * sx
                        y_full = ty * sy
                        raw_positions.append({'x_full': x_full, 'y_full': y_full,
                                              'digit': content})

        if not raw_positions:
            return None

        # 按阅读顺序排列：先按行（y 相近的归同一行），行内按 x 排
        # 用聚类：y 相差 < line_gap 视为同一行
        raw_positions.sort(key=lambda p: p['y_full'])
        line_gap = full_h_px * 0.04  # 行间距阈值 = 纸高的 4%

        rows, cur_row = [], []
        prev_y = None
        for p in raw_positions:
            if prev_y is None or abs(p['y_full'] - prev_y) < line_gap:
                cur_row.append(p)
            else:
                rows.append(sorted(cur_row, key=lambda q: q['x_full']))
                cur_row = [p]
            prev_y = p['y_full']
        if cur_row:
            rows.append(sorted(cur_row, key=lambda q: q['x_full']))

        # 展平为阅读顺序
        ordered = [p for row in rows for p in row]

        # 过滤掉与音符数不符的情况（可能有其他数字，如章节编号等）
        # 策略：若数量不等，尝试只保留 note 数量的前 N 个
        n_notes = len(notes)
        if len(ordered) < n_notes:
            # SVG 坐标不够，降级
            return None
        if len(ordered) > n_notes:
            # 裁剪多余的（通常末尾是伪元素）
            ordered = ordered[:n_notes]

        # 应用 crop 偏移 + 缩放
        cx0, cy0 = crop_offset
        result = []
        for p in ordered:
            x = (p['x_full'] - cx0) * scale
            y = (p['y_full'] - cy0) * scale
            result.append({'x': x, 'y': y})

        return result

    except Exception as e:
        print(f'  ⚠️  SVG 坐标解析失败（{e}），降级为估算', file=sys.stderr)
        return None


def detect_score_rows(score_img: Image.Image) -> list:
    """扫描简谱 PNG，返回每行内容的 y 区间列表 [{'y0':..,'y1':..,'y_mid':..}]。"""
    arr = np.array(score_img.convert('L'))
    # 每行像素中暗像素数量
    dark_per_row = np.sum(arr < 180, axis=1)
    has_content = dark_per_row > max(6, score_img.width * 0.005)

    bands, in_band, start = [], False, 0
    for y, c in enumerate(has_content):
        if c and not in_band:
            in_band = True; start = y
        elif not c and in_band:
            if y - start > 12:
                bands.append({'y0': start, 'y1': y, 'y_mid': (start + y) // 2})
            in_band = False
    if in_band and len(has_content) - start > 12:
        bands.append({'y0': start, 'y1': len(has_content),
                      'y_mid': (start + len(has_content)) // 2})

    # 合并间距小于 20px 的相邻条带（同一行的多个子元素）
    merged = []
    for b in bands:
        if merged and b['y0'] - merged[-1]['y1'] < 25:
            merged[-1]['y1'] = b['y1']
            merged[-1]['y_mid'] = (merged[-1]['y0'] + merged[-1]['y1']) // 2
        else:
            merged.append(dict(b))
    return merged


def estimate_note_positions_v2(notes: list, bars_per_line: int, beats_per_bar: int,
                                score_rows: list, score_w: int) -> list:
    """
    计算每个音符在简谱图中的 (x, y) 位置，考虑换行。
    返回 [{'x': float, 'y': int, 'row': int}, ...]
    """
    total_bars = math.ceil(sum(n['beats'] for n in notes) / beats_per_bar)
    num_rows = math.ceil(total_bars / bars_per_line)

    positions = []
    beat_acc = 0.0
    bar_idx = 0

    for note in notes:
        row_idx = bar_idx // bars_per_line

        # Y 坐标：来自行检测，若行数不够则用最后一行
        if score_rows and row_idx < len(score_rows):
            y = score_rows[row_idx]['y_mid']
        elif score_rows:
            y = score_rows[-1]['y_mid']
        else:
            row_h = score_w * 0.15  # fallback
            y = int(row_idx * row_h + row_h / 2)

        # X 坐标：在本行内按拍值线性分布
        bars_in_row = min(bars_per_line, total_bars - row_idx * bars_per_line)
        beats_in_row = bars_in_row * beats_per_bar

        # 左边距：第一行有调号/拍号，占更多空间
        left_ratio  = 0.24 if row_idx == 0 else 0.10
        right_ratio = 0.04

        # 在本行内的拍值累计
        row_start_beat = row_idx * bars_per_line * beats_per_bar
        beat_in_row = beat_acc - row_start_beat

        usable_w = score_w * (1 - left_ratio - right_ratio)
        cx = score_w * left_ratio + (beat_in_row + note['beats'] / 2) / beats_in_row * usable_w

        positions.append({'x': cx, 'y': y, 'row': row_idx})
        beat_acc += note['beats']

        # 更新 bar_idx（允许浮点误差）
        if beat_acc >= (bar_idx + 1) * beats_per_bar - 0.01:
            bar_idx += 1

    return positions

# ════════════════════════════════════════════════
# LilyPond 引擎：合成帧
# ════════════════════════════════════════════════

def create_frame_lilypond(score_img: Image.Image,
                           note_positions: list,   # [{'x', 'y', 'row'}, ...]
                           score_rows: list,        # [{'y0','y1','y_mid'}, ...]
                           current_idx: int,
                           notes: list, meta: dict,
                           score_paste_y: int, score_paste_x: int,
                           width=1080, height=1920) -> Image.Image:
    """在专业简谱图上叠加行高亮 + 精确光标，合成单帧。"""
    img = Image.new('RGB', (width, height), C_BG)
    draw = ImageDraw.Draw(img)

    f64 = _font(64); f44 = _font(44); f36 = _font(36); f52 = _font(52)

    # ── 标题 ──
    draw.text((width//2, 90), meta.get('title', '视唱练习'),
              font=f64, fill=C_TEXT, anchor='mm')

    # ── 拍号 + 调号 ──
    ts = meta.get('time_sig', '4/4').split('/')
    if len(ts) == 2:
        draw.text((60, 60), ts[0], font=f52, fill=C_DIM, anchor='mm')
        draw.line([(38, 72), (82, 72)], fill=C_DIM, width=3)
        draw.text((60, 98), ts[1], font=f52, fill=C_DIM, anchor='mm')
    draw.text((width - 60, 60), f"1={meta.get('key', 'C')}",
              font=f52, fill=C_DIM, anchor='rm')

    # ── 副标题 ──
    draw.text((width // 2, 170),
              f"♩= {meta['bpm']}  ·  第 {meta['rep_cur']} 遍 / 共 {meta['rep_total']} 遍",
              font=f44, fill=C_DIM, anchor='mm')

    # ── 贴入简谱图 ──
    img.paste(score_img, (score_paste_x, score_paste_y))

    # ── 当前位置信息 ──
    cur_pos = note_positions[current_idx] if (0 <= current_idx < len(note_positions)) else None

    overlay = Image.new('RGBA', (width, height), (0, 0, 0, 0))
    ov = ImageDraw.Draw(overlay)

    if cur_pos is not None:
        row_idx = cur_pos['row']
        note_cx = score_paste_x + int(cur_pos['x'])
        note_cy = score_paste_y + cur_pos['y']

        # ── 当前行高亮（半透明橙色背景）──
        if score_rows and row_idx < len(score_rows):
            row = score_rows[row_idx]
            ry0 = score_paste_y + row['y0'] - 8
            ry1 = score_paste_y + row['y1'] + 8
        else:
            ry0 = note_cy - 45
            ry1 = note_cy + 45
        ov.rectangle([score_paste_x, ry0, score_paste_x + score_img.width, ry1],
                     fill=(255, 220, 160, 55))  # 淡橙行背景

        # ── 精确光标：粗竖条，半透明橙 ──────────────
        cw = 28   # 加宽，覆盖整个数字
        # 外层：宽扩散光晕（更半透明）
        ov.rectangle([note_cx - cw, ry0, note_cx + cw, ry1],
                     fill=(232, 93, 38, 40))
        # 中间核心竖条（半透明）
        ov.rectangle([note_cx - cw // 2, ry0, note_cx + cw // 2, ry1],
                     fill=(232, 93, 38, 130))
        # 中心亮线（实线，给出精确对齐感）
        ov.rectangle([note_cx - 3, ry0, note_cx + 3, ry1],
                     fill=(255, 140, 60, 220))

        # ── 光标顶部三角箭头 ──
        ov.polygon([(note_cx - 18, ry0 - 2),
                    (note_cx + 18, ry0 - 2),
                    (note_cx, ry0 + 20)],
                   fill=(232, 93, 38, 240))
        # ── 光标底部三角箭头（上下夹住当前音符）──
        ov.polygon([(note_cx - 18, ry1 + 2),
                    (note_cx + 18, ry1 + 2),
                    (note_cx, ry1 - 20)],
                   fill=(232, 93, 38, 240))

    img_rgba = img.convert('RGBA')
    img = Image.alpha_composite(img_rgba, overlay).convert('RGB')
    draw = ImageDraw.Draw(img)

    # ── 当前音符名（底部大字）──
    if current_idx >= 0 and current_idx < len(notes):
        n_now = notes[current_idx % len(notes)]
        if n_now['pitch'] is not None:
            draw.text((width // 2, height - 260),
                      _SOLFEGE_NAMES.get(n_now['pitch'], ''),
                      font=_font(100), fill=C_HL, anchor='mm')

    # ── 进度条 ──
    prog = max(0.0, min(1.0, meta.get('progress', 0.0)))
    by = height - 140; bx0, bx1 = 60, width - 60
    draw.rectangle([bx0, by, bx1, by + 18], fill=C_BAR)
    if prog > 0:
        draw.rectangle([bx0, by, bx0 + int((bx1 - bx0) * prog), by + 18], fill=C_HL)
    draw.text((width // 2, height - 90),
              f"{meta.get('elapsed', 0):.0f}s / {meta.get('total_dur', 1):.0f}s",
              font=f36, fill=C_DIM, anchor='mm')

    return img

# ════════════════════════════════════════════════
# 主流程
# ════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description='简谱练习视频生成器 —— 正弦波音频 + 动态高亮简谱 → MP4'
    )
    parser.add_argument('--song', default='', help='歌曲名（从内置库查找）')
    parser.add_argument('--list-songs', action='store_true', help='列出内置歌曲库')
    parser.add_argument('--notation', default='', help='手动输入简谱文本')
    parser.add_argument('--musicxml', default='', help='MusicXML 文件路径（自动提取主旋律）')
    parser.add_argument('--musicxml-part', default=None, help='MusicXML 声部 ID（如 P1），默认自动选主旋律')
    parser.add_argument('--musicxml-ref-octave', type=int, default=4, help='MusicXML 参考八度（默认4）')
    parser.add_argument('--bpm', type=float, default=0)
    parser.add_argument('--key', default='')
    parser.add_argument('--time-sig', default='', help='拍号，如 4/4 3/4')
    parser.add_argument('--repeat', type=int, default=1,
                        help='重复播放遍数（默认1遍）')
    parser.add_argument('--title', default='')
    parser.add_argument('--output', default='output.mp4')
    parser.add_argument('--width',  type=int, default=1080)
    parser.add_argument('--height', type=int, default=1920)
    parser.add_argument('--fps',    type=int, default=25)
    parser.add_argument('--sample-rate', type=int, default=44100)
    parser.add_argument('--mode', choices=['pitch','rhythm'], default='pitch')
    parser.add_argument('--engine', choices=['pillow','lilypond'], default='lilypond',
                        help='渲染引擎：lilypond（专业排版，默认）或 pillow（快速数字）')
    parser.add_argument('--voice', action='store_true',
                        help='用 AI 人声唱名（哆来咪发嗦啦西）代替正弦波，需要 edge-tts + librosa')
    parser.add_argument('--sine', action='store_true',
                        help='用简单正弦波（不依赖 FluidSynth/soundfont）')
    parser.add_argument('--soundfont', type=str, default=None,
                        help='自定义 SF2 soundfont 路径（默认自动查找 FluidR3_GM.sf2）')
    parser.add_argument('--program', type=int, default=0,
                        help='GM 程序号：0=Acoustic Grand Piano, 4=Rhodes EP（默认0）')
    parser.add_argument('--no-beat-markers', action='store_true',
                        help='不显示 V 形节拍符（仅 pillow 引擎）')
    parser.add_argument('--bars-per-line', type=int, default=4,
                        help='LilyPond 引擎：每行显示几小节（默认4）')
    args = parser.parse_args()

    if args.list_songs:
        print('📚 内置歌曲库：\n'); print(list_songs()); return

    # 解析歌曲源
    song_info = None
    if args.musicxml:
        # 从 MusicXML 文件提取主旋律
        try:
            _script_dir = os.path.dirname(os.path.abspath(__file__))
            sys.path.insert(0, _script_dir)
            from musicxml_to_notation import parse_musicxml
            mx_result = parse_musicxml(
                args.musicxml,
                part_id=args.musicxml_part,
                ref_octave=args.musicxml_ref_octave
            )
            print(f'🎵 MusicXML 解析完成：{mx_result["tokens"]} 个音符，调号 {mx_result["key"]}，{mx_result["bpm"]}BPM，{mx_result["time_sig"]}')
            song_info = {
                'name': mx_result.get('title') or os.path.splitext(os.path.basename(args.musicxml))[0],
                'notation': mx_result['notation'],
                'key': mx_result['key'],
                'bpm': mx_result['bpm'],
                'time_sig': mx_result['time_sig'],
            }
        except Exception as e:
            print(f'❌ MusicXML 解析失败：{e}', file=sys.stderr)
            sys.exit(1)
    elif args.song:
        song_info = search_song(args.song)
        if not song_info:
            print(f'❌ 找不到「{args.song}」\n可用：\n{list_songs()}', file=sys.stderr)
            sys.exit(1)
        print(f'🎵 找到：{song_info["name"]}')
    elif not args.notation:
        print('❌ 请提供 --song、--notation 或 --musicxml', file=sys.stderr); sys.exit(1)

    notation = args.notation or song_info['notation']
    bpm      = args.bpm if args.bpm > 0 else (song_info['bpm'] if song_info else 60.0)
    key      = args.key or (song_info['key'] if song_info else 'C')
    time_sig = args.time_sig or (song_info.get('time_sig','4/4') if song_info else '4/4')
    title    = args.title or (song_info['name'] if song_info else '视唱练习')

    print(f'🎵 解析简谱: {notation[:60]}{"..." if len(notation)>60 else ""}')
    notes = parse_notation(notation)
    if not notes:
        print('❌ 简谱解析失败', file=sys.stderr); sys.exit(1)
    print(f'  → {len(notes)} 个音符  |  {bpm}BPM  |  {key}大调  |  {time_sig}')

    if args.mode == 'rhythm':
        for n in notes: n['pitch'] = 6; n['octave'] = 0

    timings_single, single_dur = build_timings(notes, bpm)
    total_dur = single_dur * args.repeat
    print(f'  → 单遍 {single_dur:.1f}s，{args.repeat}遍共 {total_dur:.1f}s')

    # 生成音频
    voice_bank = None
    if args.voice:
        print('🎤 AI 人声唱名（WORLD 声码器）...')
        voice_bank = build_voice_bank(sr=args.sample_rate)
        single_audio = generate_voice_audio(notes, bpm, key, args.sample_rate, voice_bank)
    elif args.sine:
        print('🔊 正弦波音频...')
        single_audio = generate_audio(notes, bpm, key, args.sample_rate)
    else:
        sf2 = args.soundfont or find_sf2()
        if sf2:
            print(f'🎹 FluidSynth 钢琴音色（{os.path.basename(sf2)}, prog={args.program}）...')
        else:
            print('🔊 未找到 soundfont，使用正弦波...')
        single_audio = generate_fluidsynth_audio(
            notes, bpm, key, args.sample_rate,
            sf2_path=sf2, program=args.program
        )

    audio = np.tile(single_audio, args.repeat)
    pk = np.max(np.abs(audio))
    if pk > 0: audio = audio / pk * 0.95

    # 完整时间轴
    all_timings = []
    for rep in range(args.repeat):
        off = single_dur * rep
        for idx, t in enumerate(timings_single):
            all_timings.append({'start': t['start']+off, 'end': t['end']+off,
                                 'note_idx': idx, 'rep': rep+1})

    with tempfile.TemporaryDirectory(prefix='solfege_') as tmpdir:
        wav_path = os.path.join(tmpdir, 'audio.wav')
        wavfile.write(wav_path, args.sample_rate, (audio*32767).astype(np.int16))

        # ── LilyPond 引擎 ──────────────────────────────
        if args.engine == 'lilypond':
            print('🎼 渲染专业简谱（LilyPond + SVG 精确坐标）...')
            jianpu_text = notes_to_jianpu_ly(notes, key, time_sig, title, int(bpm),
                                              args.bars_per_line)

            RENDER_RES = 250   # DPI，PNG 和 SVG 必须用同一个值
            score_raw_png, score_svg = render_score_lilypond(jianpu_text, tmpdir,
                                                              resolution=RENDER_RES)
            score_img_full = Image.open(score_raw_png).convert('RGB')
            # crop_whitespace 现在返回 (img, (x0, y0))
            score_img_cropped, crop_xy = crop_whitespace(score_img_full, padding=20)

            # 缩放：宽度适配视频，留出上下空间
            top_margin    = 210
            bottom_margin = 320
            max_score_h   = args.height - top_margin - bottom_margin
            max_score_w   = args.width - 40

            sw, sh = score_img_cropped.size
            scale = min(max_score_w / sw, max_score_h / sh)
            new_w, new_h = int(sw * scale), int(sh * scale)
            score_img = score_img_cropped.resize((new_w, new_h), Image.LANCZOS)
            score_paste_x = (args.width - new_w) // 2
            score_paste_y = top_margin + (max_score_h - new_h) // 2

            # ── 优先：从 SVG 解析精确音符坐标 ──────────
            note_positions = parse_note_positions_from_svg(
                score_svg, notes, RENDER_RES, crop_xy, scale)

            if note_positions:
                print(f'  ✅ SVG 精确坐标：{len(note_positions)} 个音符位置已解析')
                # 补充 row 信息（用于行高亮背景）：从 y 坐标反推
                score_rows_raw = detect_score_rows(score_img_cropped)
                score_rows = [{'y0': int(r['y0']*scale), 'y1': int(r['y1']*scale),
                               'y_mid': int(r['y_mid']*scale)}
                              for r in score_rows_raw]
                # 给每个 position 加 row 字段
                for pos in note_positions:
                    pos['row'] = 0
                    for ri, row in enumerate(score_rows):
                        if row['y0'] <= pos['y'] <= row['y1']:
                            pos['row'] = ri; break
                        if pos['y'] < row['y0']:
                            pos['row'] = max(0, ri - 1); break
            else:
                # ── 降级：行检测估算 ──────────────────────
                print('  ⚠️  SVG 解析失败，降级为行检测估算法')
                score_rows_raw = detect_score_rows(score_img_cropped)
                print(f'  → 检测到 {len(score_rows_raw)} 行: {[(r["y0"],r["y1"]) for r in score_rows_raw]}')
                score_rows = [{'y0': int(r['y0']*scale), 'y1': int(r['y1']*scale),
                               'y_mid': int(r['y_mid']*scale)}
                              for r in score_rows_raw]
                try:
                    bpb = int(time_sig.split('/')[0])
                except Exception:
                    bpb = 4
                note_positions = estimate_note_positions_v2(
                    notes, args.bars_per_line, bpb, score_rows, new_w)

            print(f'  → 简谱图: {new_w}×{new_h}px，放置于 y={score_paste_y}')

        # ── 生成帧 ──────────────────────────────────────
        total_frames = int(total_dur * args.fps) + 1
        print(f'🎬 生成视频帧（共 {total_frames} 帧）...')
        frames_dir = os.path.join(tmpdir, 'frames'); os.makedirs(frames_dir)

        beat_markers = (None if (args.engine=='lilypond' or args.no_beat_markers)
                        else build_beat_markers(notes, time_sig))

        for fi in range(total_frames):
            t_now = fi / args.fps
            cur_idx, cur_rep = -1, 1
            for entry in all_timings:
                if entry['start'] <= t_now < entry['end']:
                    cur_idx = entry['note_idx']; cur_rep = entry['rep']; break

            meta = {'title': title, 'bpm': int(bpm), 'key': key,
                    'time_sig': time_sig, 'rep_cur': cur_rep,
                    'rep_total': args.repeat,
                    'progress': t_now/total_dur,
                    'elapsed': t_now, 'total_dur': total_dur}

            if args.engine == 'lilypond':
                frame = create_frame_lilypond(
                    score_img, note_positions, score_rows, cur_idx,
                    notes, meta, score_paste_y, score_paste_x,
                    args.width, args.height)
            else:
                frame = create_frame_pillow(notes, cur_idx, meta, beat_markers,
                                            args.width, args.height)

            frame.save(os.path.join(frames_dir, f'f{fi:06d}.png'))
            if fi % 50 == 0:
                print(f'  帧 {fi}/{total_frames}  ({fi/total_frames*100:.0f}%)')

        # ── ffmpeg 合成 ──────────────────────────────────
        print('🎞️  合成 MP4...')
        r = subprocess.run([
            'ffmpeg', '-y',
            '-framerate', str(args.fps),
            '-i', os.path.join(frames_dir, 'f%06d.png'),
            '-i', wav_path,
            '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '192k',
            '-crf', '22', '-preset', 'fast', '-shortest',
            args.output,
        ], capture_output=True, text=True)
        if r.returncode != 0:
            print('❌ ffmpeg 失败:', r.stderr[-1500:], file=sys.stderr); sys.exit(1)

    print(f'✅ 视频已生成: {args.output}')
    print(json.dumps({'success': True, 'output': os.path.abspath(args.output),
                      'song': title, 'duration_seconds': round(total_dur,1),
                      'notes_count': len(notes), 'engine': args.engine},
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
