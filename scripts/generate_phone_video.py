#!/usr/bin/env python3
"""
generate_phone_video.py
把 HTML 简谱播放器"录制"成手机竖版视频 (1080×1920)。

原理：
  1. 用 generate_jianpu_html.py 生成 HTML 预览文件
  2. Playwright 无头浏览器打开 HTML，逐音符截图（每个音符一帧）
  3. FluidSynth 渲染整曲 WAV 音频
  4. ffmpeg concat 截图序列 + 音频 → MP4

用法：
  python3 generate_phone_video.py --musicxml 送别.musicxml --output 送别.mp4
  python3 generate_phone_video.py --html songbie_phone.html --output 送别.mp4   # 跳过 HTML 生成
"""

import os, sys, json, re, shutil, subprocess, tempfile, argparse, time, base64

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

# ─── 视频规格 ────────────────────────────────────────────────────────────────
VIDEO_W, VIDEO_H = 1080, 1920
VIEWPORT_W, VIEWPORT_H = 540, 960   # 2× retina → 1080×1920
DEVICE_SCALE = 2                     # deviceScaleFactor


# ────────────────────────────────────────────────────────────────────────────
# 步骤 1：生成 HTML（若未提供）
# ────────────────────────────────────────────────────────────────────────────

def build_html(musicxml, title, composer, html_path):
    """调用 generate_jianpu_html.py 生成 HTML。"""
    import subprocess
    cmd = [
        sys.executable,
        os.path.join(_HERE, 'generate_jianpu_html.py'),
        '--musicxml', musicxml,
        '--title', title,
        '--composer', composer,
        '--style', 'phone',
        '--output', html_path,
    ]
    r = subprocess.run(cmd, capture_output=False)
    if r.returncode != 0:
        raise RuntimeError('generate_jianpu_html.py 失败')
    return html_path


# ────────────────────────────────────────────────────────────────────────────
# 步骤 2：从 HTML 提取音符 JSON 和时序信息
# ────────────────────────────────────────────────────────────────────────────

def extract_notes_from_html(html_path):
    """从已生成的 HTML 里提取 NOTES_JSON 和 BPM。"""
    with open(html_path, 'r', encoding='utf-8') as f:
        html = f.read()
    m = re.search(r'const NOTES = (\[.*?\]);', html, re.DOTALL)
    if not m:
        raise ValueError('HTML 中找不到 NOTES 数组')
    # JS 对象字面量（key 不带引号） → 标准 JSON
    js_array = m.group(1)
    js_array = re.sub(r'([{,])\s*(\w+)\s*:', r'\1"\2":', js_array)
    notes = json.loads(js_array)
    # BPM：匹配 id="bpmSlider" ... value="80" 任意顺序
    m2 = re.search(r'id="bpmSlider"[^>]*value="(\d+)"', html)
    if not m2:
        m2 = re.search(r'value="(\d+)"[^>]*id="bpmSlider"', html)
    if not m2:
        # 匹配 bpmSlider" min="..." max="..." value="80"
        m2 = re.search(r'bpmSlider"[^>]*value="(\d+)"', html)
    if not m2:
        m2 = re.search(r'const BPM\s*=\s*(\d+)', html)
    bpm = int(m2.group(1)) if m2 else 80
    return notes, bpm


# ────────────────────────────────────────────────────────────────────────────
# 步骤 3：Playwright 截图
# ────────────────────────────────────────────────────────────────────────────

def screenshot_notes(html_path, notes, bpm, frames_dir, scroll_frames=3):
    """
    用 Playwright 无头浏览器截图每个音符状态。

    - 视频模式：隐藏播放控件（ctrl-bar），只保留标题和进度条
    - 时长精确：过渡帧时长从主帧扣除，保证 visual == audio 时序
    返回 list of (png_path, duration_sec)
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ImportError('请先安装 Playwright：pip install playwright && playwright install chromium')

    os.makedirs(frames_dir, exist_ok=True)
    frame_list = []
    beat_sec = 60.0 / bpm

    print(f'  📸 Playwright 截图（{len(notes)} 音符）...', flush=True)
    t0 = time.time()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={'width': VIEWPORT_W, 'height': VIEWPORT_H},
            device_scale_factor=DEVICE_SCALE,
        )
        page = ctx.new_page()
        page.goto('file://' + os.path.abspath(html_path))
        page.wait_for_load_state('domcontentloaded')
        page.wait_for_timeout(400)

        # ── 隐藏播放控件，视频不需要交互 UI ──
        page.evaluate("""
            () => {
                const ctrl = document.querySelector('.ctrl-bar');
                if (ctrl) ctrl.style.display = 'none';
                // 初始化：所有高亮隐藏
                document.querySelectorAll('[id^="hl-"]').forEach(e => {
                    e.style.visibility = 'hidden';
                    e.classList.remove('hl-on');
                });
                // 进度条清零
                const fill = document.getElementById('progFill');
                if (fill) fill.style.width = '0%';
                // 让 scoreWrap 占满隐藏 ctrl 后的空间
                const wrap = document.getElementById('scoreWrap');
                if (wrap) {
                    wrap.style.height = 'calc(100vh - 90px)';
                }
            }
        """)
        page.wait_for_timeout(100)

        prev_scroll = 0.0

        for i, note in enumerate(notes):
            dur = note['beats'] * beat_sec

            # ── 计算当前音符中心的 scroll 目标 ──
            js_scroll = page.evaluate(f"""
                (() => {{
                    const el = document.getElementById('hl-{i}');
                    if (!el) return -1;
                    const wrap = document.getElementById('scoreWrap');
                    const rect = el.getBoundingClientRect();
                    const wrapRect = wrap.getBoundingClientRect();
                    const relY = rect.top - wrapRect.top + wrap.scrollTop;
                    const target = relY - wrap.clientHeight / 2;
                    return Math.max(0, target);
                }})()
            """)

            # ── 展示高亮 + 更新进度条 ──
            page.evaluate(f"""
                (() => {{
                    document.querySelectorAll('[id^="hl-"]').forEach(e => {{
                        e.classList.remove('hl-on');
                        e.style.visibility = 'hidden';
                    }});
                    const el = document.getElementById('hl-{i}');
                    if (el) {{
                        el.classList.add('hl-on');
                        el.style.visibility = 'visible';
                    }}
                    const fill = document.getElementById('progFill');
                    if (fill) fill.style.width = '{(i+1)/len(notes)*100:.1f}%';
                }})()
            """)

            # ── 平滑滚动过渡帧（时长从主帧扣除，保证总时长 = dur）──
            actual_scroll_time = 0.0
            need_scroll = (js_scroll >= 0
                           and abs(js_scroll - prev_scroll) > 40
                           and i > 0
                           and scroll_frames > 0
                           and dur > scroll_frames * 0.05 + 0.08)

            if need_scroll:
                step_t = 0.05
                for sf in range(1, scroll_frames + 1):
                    t = sf / (scroll_frames + 1)
                    mid_scroll = prev_scroll + (js_scroll - prev_scroll) * t
                    page.evaluate(
                        f"document.getElementById('scoreWrap').scrollTop = {mid_scroll:.1f};"
                    )
                    frame_path = os.path.join(frames_dir, f'frame_{i:05d}_s{sf:02d}.png')
                    page.screenshot(path=frame_path)
                    frame_list.append((frame_path, step_t))
                    actual_scroll_time += step_t

            # ── 滚动到目标位置 ──
            if js_scroll >= 0:
                page.evaluate(
                    f"document.getElementById('scoreWrap').scrollTop = {js_scroll:.1f};"
                )
                prev_scroll = js_scroll

            # ── 主帧（精确时长 = dur - 过渡帧时长）──
            main_duration = max(0.06, dur - actual_scroll_time)
            frame_path = os.path.join(frames_dir, f'frame_{i:05d}_m.png')
            page.screenshot(path=frame_path)
            frame_list.append((frame_path, main_duration))

        # 最后多停 1.5 秒（余音）
        if frame_list:
            last_path, last_dur = frame_list[-1]
            frame_list[-1] = (last_path, last_dur + 1.5)

        browser.close()

    elapsed = time.time() - t0
    print(f'  ✅ {len(frame_list)} 帧截图完成 ({elapsed:.1f}s)')
    return frame_list


# ────────────────────────────────────────────────────────────────────────────
# 步骤 4：从 HTML 提取嵌入的 FluidSynth 采样，重建完整音频
# ────────────────────────────────────────────────────────────────────────────

def extract_samples_from_html(html_path):
    """从 HTML 里提取 AUDIO_SAMPLES dict → {midi_str: base64_mp3}。"""
    with open(html_path, 'r', encoding='utf-8') as f:
        html = f.read()
    m = re.search(r'const AUDIO_SAMPLES\s*=\s*(\{.*?\});', html, re.DOTALL)
    if not m:
        raise ValueError('HTML 中找不到 AUDIO_SAMPLES')
    raw = m.group(1)
    # key 可能是裸数字或带引号
    raw = re.sub(r'([{,])\s*(\d+)\s*:', r'\1"\2":', raw)
    # value 是 base64 字符串，已经带引号
    samples = json.loads(raw)
    return samples   # {midi_str: "base64_mp3_without_header"}


def render_audio_from_samples(html_path, notes, bpm, wav_path, sr=44100):
    """
    用 HTML 内嵌的 FluidSynth 采样重建完整音频（与 HTML 播放器完全一致）。
    步骤：
      1. 从 HTML 提取每个 MIDI 音符的 base64 MP3
      2. 用 ffmpeg 把每条 MP3 解码为 PCM float
      3. 按 notes 时序混合到同一个 numpy 数组
      4. 写 WAV
    """
    import numpy as np

    print('  🎵 从 HTML 提取采样重建音频 ...', end='', flush=True)
    t0 = time.time()

    samples_b64 = extract_samples_from_html(html_path)
    beat_sec = 60.0 / bpm

    # 解码每条采样 → numpy float32 array
    ffmpeg_bin = shutil.which('ffmpeg') or '/opt/homebrew/bin/ffmpeg'
    decoded = {}   # midi_int → np.ndarray (float32, mono, sr)
    tmpdir = tempfile.mkdtemp(prefix='phvid_smp_')
    try:
        for midi_str, b64_data in samples_b64.items():
            mp3_path = os.path.join(tmpdir, f'{midi_str}.mp3')
            # base64 字符串可能带 data:audio/... 头
            if ',' in b64_data:
                b64_data = b64_data.split(',', 1)[1]
            with open(mp3_path, 'wb') as f:
                f.write(base64.b64decode(b64_data))
            # ffmpeg 解码为 raw f32le PCM
            cmd = [
                ffmpeg_bin, '-y', '-i', mp3_path,
                '-ac', '1', '-ar', str(sr),
                '-f', 'f32le', '-',
            ]
            r = subprocess.run(cmd, capture_output=True)
            if r.returncode == 0 and len(r.stdout) > 0:
                arr = np.frombuffer(r.stdout, dtype=np.float32).copy()
                decoded[int(midi_str)] = arr
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    # 计算总时长
    total_beats = sum(n['beats'] for n in notes)
    tail_sec = 2.0   # 最后一个音符后的余音
    total_sec = total_beats * beat_sec + tail_sec
    mix = np.zeros(int(total_sec * sr), dtype=np.float32)

    cursor_sec = 0.0
    for note in notes:
        midi = note.get('midi', -1)
        dur_sec = note['beats'] * beat_sec
        if midi > 0 and midi in decoded:
            arr = decoded[midi]
            start = int(cursor_sec * sr)
            end = min(start + len(arr), len(mix))
            mix[start:end] += arr[:end - start]
        cursor_sec += dur_sec

    # 归一化防爆音
    peak = np.max(np.abs(mix))
    if peak > 0.95:
        mix = mix * (0.90 / peak)
    elif peak > 0:
        mix = mix * min(1.0, 0.85 / peak)   # 轻微拉满

    # 写 WAV (int16)
    from scipy.io import wavfile
    mix_int16 = (mix * 32767).astype(np.int16)
    wavfile.write(wav_path, sr, mix_int16)
    audio_dur = len(mix) / sr
    print(f' {time.time()-t0:.1f}s  ({audio_dur:.1f}s 音频, {len(decoded)} 采样)')
    return wav_path


# ────────────────────────────────────────────────────────────────────────────
# 步骤 5：ffmpeg 合成视频
# ────────────────────────────────────────────────────────────────────────────

def encode_video(frame_list, audio_path, output_path, scale_w=VIDEO_W, scale_h=VIDEO_H):
    """用 ffmpeg concat demuxer 合成截图序列 + 音频为 MP4。"""
    ffmpeg = shutil.which('ffmpeg') or '/opt/homebrew/bin/ffmpeg'

    # 写 concat 文件
    concat_path = os.path.join(os.path.dirname(frame_list[0][0]), 'concat.txt')
    with open(concat_path, 'w') as f:
        for fpath, dur in frame_list:
            f.write(f"file '{fpath}'\n")
            f.write(f"duration {dur:.6f}\n")
        # ffmpeg concat 需要最后一帧再写一次（无 duration）
        f.write(f"file '{frame_list[-1][0]}'\n")

    print(f'  🎬 ffmpeg 编码视频 → {output_path} ...', end='', flush=True)
    t0 = time.time()

    cmd = [
        ffmpeg, '-y',
        '-f', 'concat', '-safe', '0', '-i', concat_path,
    ]
    if audio_path and os.path.exists(audio_path):
        cmd += ['-i', audio_path]

    cmd += [
        '-vf', f'scale={scale_w}:{scale_h}:flags=lanczos',
        '-c:v', 'libx264', '-preset', 'fast', '-crf', '18',
        '-pix_fmt', 'yuv420p',
    ]
    if audio_path and os.path.exists(audio_path):
        cmd += ['-c:a', 'aac', '-b:a', '192k', '-shortest']

    cmd += [output_path]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f'ffmpeg 失败:\n{r.stderr.decode()[-500:]}')
    size_mb = os.path.getsize(output_path) / 1024 / 1024
    print(f' {time.time()-t0:.1f}s  ({size_mb:.1f}MB)')
    return output_path


# ────────────────────────────────────────────────────────────────────────────
# 主流程
# ────────────────────────────────────────────────────────────────────────────

def generate_video(
    musicxml=None,
    html_path=None,
    title='练习曲',
    composer='',
    output='output.mp4',
    bpm_override=0,
    scroll_frames=3,
    keep_frames=False,
):
    tmpdir = tempfile.mkdtemp(prefix='phone_vid_')
    try:
        # ── 1. 生成 HTML（如果没有提供）──
        if html_path is None:
            if musicxml is None:
                raise ValueError('必须提供 --musicxml 或 --html')
            html_path = os.path.join(tmpdir, 'player.html')
            print('🌐 生成 HTML 播放器 ...')
            build_html(musicxml, title, composer, html_path)

        # ── 2. 提取音符序列和 BPM ──
        print('📋 读取 HTML 音符信息 ...')
        notes, bpm = extract_notes_from_html(html_path)
        if bpm_override > 0:
            bpm = bpm_override
        total_sec = sum(n['beats'] for n in notes) * 60 / bpm
        print(f'  → {len(notes)} 音符，{bpm} BPM，时长约 {total_sec:.1f}s')

        # ── 3. 截图 ──
        frames_dir = os.path.join(tmpdir, 'frames') if not keep_frames else \
                     os.path.join(os.path.dirname(output), 'frames')
        frame_list = screenshot_notes(html_path, notes, bpm, frames_dir, scroll_frames)

        # ── 4. 从 HTML 提取采样重建音频（与 HTML 播放器完全一致）──
        wav_path = os.path.join(tmpdir, 'audio.wav')
        render_audio_from_samples(html_path, notes, bpm, wav_path)

        # ── 5. 合成视频 ──
        print(f'🎬 合成 {VIDEO_W}×{VIDEO_H} 视频 ...')
        encode_video(frame_list, wav_path, output)

        total_frames_dur = sum(d for _, d in frame_list)
        print(f'\n✅ 视频已生成：{output}')
        print(f'   时长 {total_frames_dur:.1f}s  |  {len(frame_list)} 帧  |  {len(notes)} 音符')

    finally:
        if not keep_frames:
            shutil.rmtree(tmpdir, ignore_errors=True)


# ────────────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='手机竖版简谱视频生成器（录制 HTML 播放器）')
    parser.add_argument('--musicxml', default=None, help='MusicXML 文件路径')
    parser.add_argument('--html',     default=None, help='已有 HTML 播放器路径（跳过生成）')
    parser.add_argument('--title',    default='练习曲')
    parser.add_argument('--composer', default='')
    parser.add_argument('--bpm',      type=int, default=0, help='覆盖 BPM（0=从 HTML 读取）')
    parser.add_argument('--scroll-frames', type=int, default=3, help='音符间过渡帧数（默认3）')
    parser.add_argument('--keep-frames', action='store_true', help='保留截图帧（调试用）')
    parser.add_argument('--output',   default='output.mp4')
    args = parser.parse_args()

    generate_video(
        musicxml=args.musicxml,
        html_path=args.html,
        title=args.title,
        composer=args.composer,
        output=args.output,
        bpm_override=args.bpm,
        scroll_frames=args.scroll_frames,
        keep_frames=args.keep_frames,
    )


if __name__ == '__main__':
    main()
