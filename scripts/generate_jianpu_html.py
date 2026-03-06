#!/usr/bin/env python3
"""
generate_jianpu_html.py
生成自包含的简谱练习 HTML 播放器。

整合：
  - LilyPond 专业排版（精确简谱）→ SVG 内嵌，<rect> 光标天然对齐
  - FluidSynth 真实钢琴采样（每个音符 → MP3 base64 嵌入）
  - Web Audio API 零延迟播放，BPM 实时调节

用法：
  # 最简单：MusicXML 一键生成（手机版）
  python3 generate_jianpu_html.py --musicxml 送别.musicxml --output 送别.html

  # 手机品牌风格（默认）vs 深色桌面风格
  python3 generate_jianpu_html.py --musicxml X.musicxml --style phone  --output phone.html
  python3 generate_jianpu_html.py --musicxml X.musicxml --style dark   --output dark.html

  # 手动 notation
  python3 generate_jianpu_html.py --notation "5 /3 /5 ^1= ..." --key bE --bpm 80 --title 送别
"""

import os, sys, re, json, subprocess, tempfile, argparse, shutil, base64, time
from concurrent.futures import ThreadPoolExecutor, as_completed

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from generate_solfege_video import (
    parse_notation, notes_to_jianpu_ly, _build_ly_content,
)

# ─────────────────────────────────────────────────────────────────
# 工具：调号 → MIDI 基音
# ─────────────────────────────────────────────────────────────────

_KEY_TONIC_MIDI = {
    'C': 60, 'D': 62, 'E': 64, 'F': 65,
    'G': 67, 'A': 69, 'B': 71,
    'bB': 58, 'bE': 63, 'bA': 68,
}
_DEGREE_SEMIS = {1: 0, 2: 2, 3: 4, 4: 5, 5: 7, 6: 9, 7: 11}
_KEY_DISPLAY = {
    'C': 'C', 'D': 'D', 'E': 'E', 'F': 'F',
    'G': 'G', 'A': 'A', 'B': 'B',
    'bB': 'B♭', 'bE': 'E♭', 'bA': 'A♭',
}


def note_to_midi(degree, octave, sharp, key):
    if degree == 0:
        return -1
    tonic = _KEY_TONIC_MIDI.get(key, 60)
    semi = _DEGREE_SEMIS.get(degree, 0) + (1 if sharp else 0)
    return tonic + semi + octave * 12


def notes_to_js_array(notes, key):
    """转换为 JS notes 数组，每项含 {degree, octave, beats, midi}。"""
    items = []
    for n in notes:
        d = n.get('pitch') or 0
        oct_off = n.get('octave', 0)
        beats = n.get('beats', 1.0)
        sharp = n.get('sharp', False)
        midi = note_to_midi(d, oct_off, sharp, key)
        items.append(f'{{degree:{d},octave:{oct_off},beats:{beats},midi:{midi}}}')
    return '[' + ','.join(items) + ']'


# ─────────────────────────────────────────────────────────────────
# FluidSynth 每音采样 → base64 MP3
# ─────────────────────────────────────────────────────────────────

def _render_one_sample(midi_note, sf2, sr=22050, duration_beats=4, bpm=100):
    """渲染单个 MIDI 音符为 MP3 base64 字符串。"""
    import mido
    tmpdir = tempfile.mkdtemp(prefix='jh_smp_')
    try:
        mid = mido.MidiFile()
        tpb = mid.ticks_per_beat
        track = mido.MidiTrack()
        mid.tracks.append(track)
        # 力度固定 80（loudnorm 会归一化到 -14 LUFS，高音刺耳交给低通滤波处理）
        vel = 80
        track.append(mido.Message('program_change', program=0, time=0))
        track.append(mido.Message('note_on', note=midi_note, velocity=vel, time=0))
        tick_dur = int(tpb * duration_beats * (120 / bpm))
        track.append(mido.Message('note_off', note=midi_note, velocity=0, time=tick_dur))

        mid_path = os.path.join(tmpdir, 'n.mid')
        wav_path = os.path.join(tmpdir, 'n.wav')
        mp3_path = os.path.join(tmpdir, 'n.mp3')
        mid.save(mid_path)

        fluidsynth = shutil.which('fluidsynth') or '/opt/homebrew/bin/fluidsynth'
        ffmpeg = shutil.which('ffmpeg') or '/opt/homebrew/bin/ffmpeg'

        subprocess.run(
            [fluidsynth, '-ni', '-F', wav_path, '-r', str(sr), sf2, mid_path],
            capture_output=True, timeout=15
        )
        if not os.path.exists(wav_path):
            return None

        subprocess.run(
            [ffmpeg, '-y', '-i', wav_path,
             '-af', 'loudnorm=I=-14:LRA=11:TP=-1',
             '-ac', '1', '-b:a', '64k', mp3_path],
            capture_output=True, timeout=15
        )
        if not os.path.exists(mp3_path):
            return None

        return base64.b64encode(open(mp3_path, 'rb').read()).decode()
    except Exception as e:
        print(f'  ⚠ 采样生成失败 midi={midi_note}: {e}', file=sys.stderr)
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def render_note_samples(notes, key, sf2_path=None):
    """
    为歌曲中所有独特 MIDI 音符生成 FluidSynth 采样。
    返回 {midi_note: 'base64_mp3', ...}
    """
    if sf2_path is None:
        # 与 generate_phone_video._find_sf2() 保持一致：优先 FluidR3_GM，跳过软链接
        import glob as _glob
        # 已知固定路径（真实 FluidR3_GM）
        for c in [
            os.path.expanduser('~/.cache/solfege_soundfonts/FluidR3_GM.sf2'),
            '/opt/homebrew/share/fluid-synth/sf2/FluidR3_GM.sf2',
            '/usr/share/sounds/sf2/FluidR3_GM.sf2',
            '/usr/share/sounds/sf2/FluidR3_GS.sf2',
            os.path.expanduser('~/Library/Audio/Sounds/Banks/FluidR3_GM.sf2'),
        ]:
            if os.path.exists(c) and not os.path.islink(c) and os.path.getsize(c) > 10_000_000:
                sf2_path = c
                break
        # Homebrew Cellar 扫描（跳过软链接，优先 FluidR3 名称）
        if sf2_path is None:
            for cellar_base in ['/opt/homebrew/Cellar/fluid-synth', '/usr/local/Cellar/fluid-synth']:
                if not os.path.isdir(cellar_base):
                    continue
                all_sf = _glob.glob(os.path.join(cellar_base, '*', 'share', '**', '*.sf2'),
                                    recursive=True)
                for p in sorted(all_sf):
                    if 'FluidR3' in os.path.basename(p) and not os.path.islink(p):
                        try:
                            if os.path.getsize(p) > 10_000_000:
                                sf2_path = p; break
                        except OSError:
                            pass
                if sf2_path:
                    break
                # 任意非链接 sf2
                for p in sorted(all_sf):
                    if not os.path.islink(p):
                        try:
                            if os.path.getsize(p) > 10_000_000:
                                sf2_path = p; break
                        except OSError:
                            pass
                if sf2_path:
                    break
                # sf3 fallback
                for p in sorted(_glob.glob(os.path.join(cellar_base, '*', 'share', '**', '*.sf3'),
                                           recursive=True)):
                    if not os.path.islink(p):
                        try:
                            if os.path.getsize(p) > 1_000_000:
                                sf2_path = p; break
                        except OSError:
                            pass
                if sf2_path:
                    break
    if sf2_path is None:
        print('  ⚠ 未找到 SF2 soundfont，HTML 将使用 Web Audio 合成（视频生成时会自动兜底）', file=sys.stderr)
        print('    建议下载 FluidR3_GM.sf2 → https://keymusician01.s3.amazonaws.com/FluidR3_GM.zip'
              '\n    解压后放到 ~/.cache/solfege_soundfonts/', file=sys.stderr)
        return {}

    unique_midis = sorted(set(
        note_to_midi(n.get('pitch') or 0, n.get('octave', 0), n.get('sharp', False), key)
        for n in notes
    ) - {-1})

    if not unique_midis:
        return {}

    print(f'  🎹 FluidSynth 采样：{len(unique_midis)} 个独特音高 ...', end='', flush=True)
    t0 = time.time()

    samples = {}
    # 并发渲染（最多 4 路）
    with ThreadPoolExecutor(max_workers=4) as exe:
        futures = {exe.submit(_render_one_sample, m, sf2_path): m for m in unique_midis}
        for fut in as_completed(futures):
            midi = futures[fut]
            result = fut.result()
            if result:
                samples[midi] = result

    elapsed = time.time() - t0
    total_kb = sum(len(v) for v in samples.values()) // 1024
    print(f' {elapsed:.1f}s  ({total_kb}KB base64)')
    return samples


# ─────────────────────────────────────────────────────────────────
# LilyPond → SVG
# ─────────────────────────────────────────────────────────────────

def render_svg(jianpu_text, work_dir, portrait=False):
    """
    运行 jianpu-ly + LilyPond --svg。
    返回 (svg_pages: list[str], viewbox, svg_paths: list[str])。
    svg_pages 是每页 SVG 文件内容的列表。
    """
    ly_path = _build_ly_content(jianpu_text, work_dir)

    with open(ly_path, 'r', encoding='utf-8') as f:
        ly = f.read()

    if portrait:
        ly = ly.replace(
            "#(set-default-paper-size \"a4\" 'landscape)",
            "#(set-default-paper-size \"a4\")"
        )

    # ── 排版修复 ──────────────────────────────────────────────────
    # 0. 关闭页码（多页 HTML 嵌入时不需要）
    ly = ly.replace(
        'ragged-last-bottom = ##t',
        'ragged-last-bottom = ##t\n  print-page-number = ##f'
    )
    # 1. 高音八度点（Script dot）增加垂直间距，修正偏右
    #    RehearsalMark.padding 增加调号/拍号与音符之间的空间
    ly = ly.replace(
        'scriptDefinitions = #default-script-alist\n  }',
        'scriptDefinitions = #default-script-alist\n'
        '    \\override Script.padding = #1.2\n'
        '    \\override Script.outside-staff-padding = #0.8\n'
        '    \\override Script.X-offset = #0.55\n'
        '    \\override RehearsalMark.padding = #2.5\n'
        '    \\override RehearsalMark.outside-staff-padding = #1.5\n  }'
    )

    with open(ly_path, 'w', encoding='utf-8') as f:
        f.write(ly)

    lilypond = shutil.which('lilypond') or '/opt/homebrew/bin/lilypond'
    svg_base = os.path.join(work_dir, 'score')
    r = subprocess.run(
        [lilypond, '--svg', '-o', svg_base, ly_path],
        capture_output=True, cwd=work_dir, timeout=60
    )
    if r.returncode != 0:
        raise RuntimeError(f'LilyPond SVG 失败:\n{r.stderr.decode()[-500:]}')

    # 单页：score.svg；多页：score-1.svg, score-2.svg ...
    single = svg_base + '.svg'
    if os.path.exists(single):
        svg_paths = [single]
    else:
        svg_paths = sorted(
            [os.path.join(work_dir, f) for f in os.listdir(work_dir)
             if re.match(r'score-\d+\.svg$', f)],
            key=lambda p: int(re.search(r'(\d+)\.svg$', p).group(1))
        )

    if not svg_paths:
        raise RuntimeError('LilyPond 未生成任何 SVG')

    print(f'  → {len(svg_paths)} 页 SVG')

    svg_contents = []
    for p in svg_paths:
        with open(p, 'r', encoding='utf-8') as f:
            svg_contents.append(f.read())

    # viewBox 取第一页
    m = re.search(r'viewBox=["\']([^"\']+)["\']', svg_contents[0])
    vb = list(map(float, m.group(1).split())) if m else [0, 0, 200, 150]

    return svg_contents, vb, svg_paths


def extract_note_positions_all_pages(svg_files_content_list, n_notes):
    """从多页 SVG 内容中提取所有音符坐标（跨页展平）。"""
    # 将多页 SVG 内容列表当作一整块处理（每页坐标独立）
    # 使用临时文件逐页提取然后合并
    all_positions = []
    import xml.etree.ElementTree as ET

    for svg_content in svg_files_content_list:
        # 写入临时文件
        tmp = tempfile.NamedTemporaryFile(suffix='.svg', delete=False, mode='w', encoding='utf-8')
        tmp.write(svg_content)
        tmp.close()
        try:
            positions = extract_note_positions_svg(tmp.name, 999)  # 999=不限制
            if positions:
                all_positions.extend(positions)
        finally:
            os.unlink(tmp.name)

    if len(all_positions) < n_notes:
        return None
    return all_positions[:n_notes]


def extract_note_positions_svg(svg_path, n_notes):
    """从 SVG 提取每个音符数字的 (x, y) SVG 坐标，阅读顺序。"""
    import xml.etree.ElementTree as ET
    try:
        tree = ET.parse(svg_path)
        root = tree.getroot()
        ns = root.tag[:root.tag.index('}')+1] if root.tag.startswith('{') else ''

        vb = list(map(float, root.get('viewBox', '0 0 200 150').split()))
        vb_h = vb[3] if len(vb) >= 4 else 150

        raw = []
        for g in root.iter(f'{ns}g'):
            m = re.match(r'translate\(([\d.eE+\-]+),\s*([\d.eE+\-]+)\)',
                         g.get('transform', ''))
            if not m:
                continue
            tx, ty = float(m.group(1)), float(m.group(2))
            for text_el in g.findall(f'{ns}text'):
                if 'sans-serif' not in text_el.get('font-family', ''):
                    continue
                if text_el.get('font-weight', '') != 'bold':
                    continue
                t_x = float(text_el.get('x', 0))
                t_y = float(text_el.get('y', 0))
                for tspan in text_el.findall(f'{ns}tspan'):
                    content = (tspan.text or '').strip()
                    if content in {'0','1','2','3','4','5','6','7'}:
                        sx = float(tspan.get('x', t_x))
                        sy = float(tspan.get('y', t_y))
                        raw.append({'x': tx+sx, 'y': ty+sy, 'digit': content})

        if not raw:
            return None

        raw.sort(key=lambda p: p['y'])
        line_gap = vb_h * 0.04
        rows, cur, prev_y = [], [], None
        for p in raw:
            if prev_y is None or abs(p['y'] - prev_y) < line_gap:
                cur.append(p)
            else:
                rows.append(sorted(cur, key=lambda q: q['x']))
                cur = [p]
            prev_y = p['y']
        if cur:
            rows.append(sorted(cur, key=lambda q: q['x']))

        ordered = [p for row in rows for p in row]
        # n_notes 作为上限而非精确要求：找到什么返回什么
        if n_notes < 999 and len(ordered) < n_notes:
            return None
        return ordered[:n_notes] if n_notes < 999 else ordered
    except Exception as e:
        print(f'  ⚠ SVG 坐标提取失败: {e}', file=sys.stderr)
        return None


def inject_highlight_rects(svg_content, positions, char_size, start_idx=0):
    """在 SVG 末尾注入 hl-{i} 高亮矩形（默认隐藏）。"""
    rects = []
    for i, p in enumerate(positions):
        gid = start_idx + i
        x = p['x'] - char_size * 0.55
        y = p['y'] - char_size * 1.55
        w = char_size * 1.3
        h = char_size * 2.1
        rects.append(
            f'<rect id="hl-{gid}" x="{x:.3f}" y="{y:.3f}" '
            f'width="{w:.3f}" height="{h:.3f}" '
            f'rx="0.3" ry="0.3" fill="{{HL_COLOR}}" visibility="hidden"/>'
        )
    inject = '\n'.join(rects) + '\n'
    return svg_content.replace('</svg>', inject + '</svg>')

# 多页版：start_idx 为全局偏移
inject_highlight_rects_indexed = inject_highlight_rects


# ─────────────────────────────────────────────────────────────────
# HTML 模板
# ─────────────────────────────────────────────────────────────────

# ── 手机版（品牌色：米黄/深棕/红松橙，竖屏）──────────────────────
CSS_PHONE = """
* { margin:0; padding:0; box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
html, body { height:100%; }
body {
  font-family: -apple-system, 'PingFang SC', sans-serif;
  background: #FFF8EE;
  color: #5C3A1E;
  min-height: 100vh;
  display: flex; flex-direction: column;
}

/* ── 顶部信息条 ── */
.top-bar {
  background: #5C3A1E;
  color: #FFF8EE;
  padding: 10px 16px 8px;
  display: flex; align-items: center; justify-content: space-between;
  flex-shrink: 0;
  box-shadow: 0 2px 8px rgba(92,58,30,0.3);
}
.top-bar h1 { font-size: 20px; font-weight: 700; letter-spacing: 1px; }
.top-bar .meta { font-size: 12px; color: #B49678; margin-top: 1px; }
.top-bar .key-badge {
  background: #E85D26; color: #fff;
  padding: 3px 10px; border-radius: 10px;
  font-size: 13px; font-weight: 700;
}

/* ── 控制栏 ── */
.ctrl-bar {
  background: #5C3A1E;
  padding: 8px 16px 10px;
  display: flex; align-items: center; gap: 10px;
  flex-shrink: 0;
  border-bottom: 2px solid #E85D26;
}
.btn-play {
  width: 44px; height: 44px;
  background: #E85D26; color: #fff;
  border: none; border-radius: 50%;
  font-size: 18px; cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  box-shadow: 0 3px 10px rgba(232,93,38,0.4);
  flex-shrink: 0;
}
.btn-reset {
  width: 36px; height: 36px;
  background: rgba(255,248,238,0.15); color: #FFF8EE;
  border: 1.5px solid rgba(255,248,238,0.3); border-radius: 50%;
  font-size: 16px; cursor: pointer; flex-shrink: 0;
}
.slider-group {
  display: flex; align-items: center; gap: 6px;
  color: #B49678; font-size: 12px; flex: 1;
}
input[type="range"] {
  flex: 1; accent-color: #E85D26; cursor: pointer;
  height: 3px;
}
.bpm-label { color: #E8C87A; font-size: 13px; font-weight: 600; min-width: 28px; }

/* ── 进度条 ── */
.progress-bar {
  height: 3px; background: rgba(92,58,30,0.2); flex-shrink: 0;
}
.progress-fill {
  height: 100%; width: 0%;
  background: linear-gradient(90deg, #E85D26, #e8b86d);
  transition: width 0.15s linear;
}

/* ── 乐谱区（可滚动）── */
.score-wrap {
  flex: 1; overflow-y: auto; overflow-x: hidden;
  padding: 12px 8px;
  -webkit-overflow-scrolling: touch;
}
.score-wrap svg {
  width: 100%; height: auto; display: block;
}

/* ── 光标高亮 ── */
.score-wrap svg rect[id^="hl-"].hl-on {
  visibility: visible !important;
  fill: rgba(232,93,38,0.45);
  filter: drop-shadow(0 0 1px rgba(232,93,38,0.6));
}
"""

# ── 深色桌面版 ────────────────────────────────────────────────────
CSS_DARK = """
* { margin:0; padding:0; box-sizing:border-box; }
body {
  font-family: -apple-system, 'PingFang SC', sans-serif;
  background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
  color: #e0e0e0; min-height:100vh; padding:24px 16px 40px;
}
.top-bar {
  text-align:center; margin-bottom:8px;
}
.top-bar h1 { font-size:30px; color:#fff; margin-bottom:3px; }
.top-bar .meta { font-size:14px; color:#889; }
.top-bar .key-badge {
  display:inline-block; margin:0 6px;
  background:rgba(255,255,255,.06); color:#e8b86d;
  padding:3px 10px; border-radius:10px; font-size:14px;
}
.ctrl-bar {
  display:flex; justify-content:center; align-items:center;
  gap:12px; margin:16px 0 24px; flex-wrap:wrap;
}
.btn-play {
  padding:9px 28px; font-size:15px;
  background:linear-gradient(135deg,#e8b86d,#d4943a);
  color:#1a1a2e; border:none; border-radius:22px; cursor:pointer;
  font-weight:bold; box-shadow:0 4px 14px rgba(232,184,109,.3);
  transition:all .18s;
}
.btn-play:hover { transform:translateY(-2px); }
.btn-reset {
  padding:9px 20px; font-size:14px;
  background:rgba(255,255,255,.1); color:#ccc; border:none;
  border-radius:22px; cursor:pointer; transition:all .18s;
}
.btn-reset:hover { background:rgba(255,255,255,.18); }
.slider-group { display:flex; align-items:center; gap:7px; color:#aaa; font-size:13px; }
input[type="range"] { width:110px; accent-color:#e8b86d; cursor:pointer; }
.bpm-label { color:#e8b86d; font-size:13px; font-weight:600; }
.score-wrap {
  max-width:900px; margin:0 auto;
  background:rgba(255,255,255,.05); border-radius:14px;
  padding:20px 16px; border:1px solid rgba(255,255,255,.08);
  backdrop-filter:blur(8px);
}
.score-wrap svg { width:100%; height:auto; display:block; }
.score-wrap svg rect[id^="hl-"].hl-on {
  visibility:visible !important;
  fill:rgba(232,120,40,0.50);
  filter:drop-shadow(0 0 1.5px rgba(232,120,40,0.7));
}
.progress-bar {
  max-width:900px; margin:14px auto 0;
  height:4px; background:rgba(255,255,255,.08); border-radius:2px; overflow:hidden;
}
.progress-fill {
  height:100%; background:linear-gradient(90deg,#e8b86d,#d4943a);
  width:0%; border-radius:2px; transition:width .15s linear;
}
"""

# ── HTML 骨架（phone 版）────────────────────────────────────────
HTML_PHONE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,user-scalable=no">
<title>{{TITLE}}</title>
<style>{{CSS}}</style>
</head>
<body>

<div class="top-bar">
  <div>
    <div class="meta">1 = {{KEY_DISPLAY}}  |  {{TIME_SIG}}  |  ♩ = <span id="bpmDisplay">{{BPM}}</span></div>
    <h1>{{TITLE}}</h1>
    <div class="meta">{{COMPOSER}}</div>
  </div>
  <div class="key-badge">{{KEY_DISPLAY}}</div>
</div>

<div class="ctrl-bar">
  <button class="btn-play" id="playBtn">▶</button>
  <button class="btn-reset" id="resetBtn">↺</button>
  <div class="slider-group">
    <span>速度</span>
    <input type="range" id="bpmSlider" min="30" max="180" value="{{BPM}}">
    <span class="bpm-label" id="bpmVal">{{BPM}}</span>
  </div>
  <div class="slider-group" style="flex:0.6">
    <span>🔊</span>
    <input type="range" id="volSlider" min="0" max="100" value="80">
  </div>
</div>

<div class="progress-bar"><div class="progress-fill" id="progFill"></div></div>

<div class="score-wrap" id="scoreWrap">
{{SVG_CONTENT}}
</div>

<script>
{{AUDIO_JS}}
{{PLAYER_JS}}
</script>
</body>
</html>
"""

# ── HTML 骨架（dark 版）────────────────────────────────────────
HTML_DARK = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{TITLE}} - 简谱练习</title>
<style>{{CSS}}</style>
</head>
<body>

<div class="top-bar">
  <h1>{{TITLE}}</h1>
  <div class="meta">
    <span class="key-badge">1 = {{KEY_DISPLAY}}</span>
    <span class="key-badge">{{TIME_SIG}} 拍</span>
    <span class="key-badge">♩ = <span id="bpmDisplay">{{BPM}}</span></span>
    <span class="key-badge">{{COMPOSER}}</span>
  </div>
</div>

<div class="ctrl-bar">
  <button class="btn-play" id="playBtn">▶ 播放</button>
  <button class="btn-reset" id="resetBtn">↺ 重置</button>
  <div class="slider-group">
    <span>速度</span>
    <input type="range" id="bpmSlider" min="30" max="180" value="{{BPM}}">
    <span class="bpm-label" id="bpmVal">{{BPM}}</span>
  </div>
  <div class="slider-group">
    <span>🔊</span>
    <input type="range" id="volSlider" min="0" max="100" value="80">
  </div>
</div>

<div class="score-wrap" id="scoreWrap">
{{SVG_CONTENT}}
</div>
<div class="progress-bar"><div class="progress-fill" id="progFill"></div></div>

<script>
{{AUDIO_JS}}
{{PLAYER_JS}}
</script>
</body>
</html>
"""


# ── JavaScript：FluidSynth 采样播放 ──────────────────────────────
JS_AUDIO_SAMPLES = r"""
// FluidSynth 采样数据（base64 MP3）
const AUDIO_SAMPLES = {{SAMPLES_JSON}};

// 预解码的 AudioBuffer 缓存
const _bufCache = {};
let audioCtx = null;
function ensureCtx() {
  if (!audioCtx) audioCtx = new (window.AudioContext||window.webkitAudioContext)();
  if (audioCtx.state === 'suspended') audioCtx.resume();
  return audioCtx;
}

// base64 MP3 → ArrayBuffer
function b64ToAB(b64) {
  const bin = atob(b64);
  const ab = new ArrayBuffer(bin.length);
  const view = new Uint8Array(ab);
  for (let i = 0; i < bin.length; i++) view[i] = bin.charCodeAt(i);
  return ab;
}

// 全局 DynamicsCompressor（防止多音符叠加过载失真）
let _masterComp = null;
function getMaster() {
  const ctx = ensureCtx();
  if (!_masterComp) {
    _masterComp = ctx.createDynamicsCompressor();
    _masterComp.threshold.value = -18;  // dB
    _masterComp.knee.value      = 6;
    _masterComp.ratio.value     = 4;
    _masterComp.attack.value    = 0.003;
    _masterComp.release.value   = 0.25;
    _masterComp.connect(ctx.destination);
  }
  return _masterComp;
}

// 预加载所有采样（返回 Promise，确保第一个音有采样）
function preloadSamples() {
  const ctx = ensureCtx();
  const promises = Object.entries(AUDIO_SAMPLES).map(([midi, b64]) => {
    return new Promise(resolve => {
      const m = parseInt(midi);
      if (_bufCache[m]) { resolve(); return; }
      ctx.decodeAudioData(b64ToAB(b64),
        buf => { _bufCache[m] = buf; resolve(); },
        err => { console.warn('decode failed midi='+midi, err); resolve(); }
      );
    });
  });
  return Promise.all(promises);
}

function playMidi(midiNote, durationSec) {
  if (midiNote < 0) return;
  const ctx = ensureCtx();
  const vol = parseInt(document.getElementById('volSlider').value) / 100 * 1.8;

  if (_bufCache[midiNote]) {
    // 使用 FluidSynth 采样
    const src = ctx.createBufferSource();
    src.buffer = _bufCache[midiNote];

    // 高频（>= C5, MIDI 72）加低通滤波，减少刺耳
    const chain = [];
    if (midiNote >= 72) {
      const lpf = ctx.createBiquadFilter();
      lpf.type = 'lowpass';
      // MIDI 72=C5 → 4500Hz，每高一个半音 cutoff 降低一点
      lpf.frequency.value = Math.max(2200, 5000 - (midiNote - 72) * 180);
      lpf.Q.value = 0.7;
      chain.push(lpf);
    }

    const gain = ctx.createGain();
    gain.gain.value = vol;
    chain.push(gain);

    // 连接信号链 → compressor → 输出
    src.connect(chain[0]);
    for (let i = 0; i < chain.length - 1; i++) chain[i].connect(chain[i+1]);
    chain[chain.length-1].connect(getMaster());

    src.start(0);  // 让采样自然衰减（不强制截断）
    // 释音包络：音符时值结束后渐弱
    const releaseAt = ctx.currentTime + durationSec;
    gain.gain.setValueAtTime(vol, releaseAt);
    gain.gain.exponentialRampToValueAtTime(0.001, releaseAt + 0.5);
    src.stop(releaseAt + 0.6);
  } else {
    // fallback：Web Audio 合成
    _synthNote(ctx, midiNote, durationSec, vol);
  }
}

function _synthNote(ctx, midi, dur, vol) {
  const freq = 440 * Math.pow(2, (midi - 69) / 12);
  const now = ctx.currentTime;
  const master = ctx.createGain();
  master.connect(ctx.destination);
  const harmonics = [{r:1,g:0.5},{r:2,g:0.22},{r:3,g:0.1},{r:4,g:0.05}];
  for (const h of harmonics) {
    const osc = ctx.createOscillator(); const gn = ctx.createGain();
    osc.type = 'sine'; osc.frequency.value = freq * h.r; gn.gain.value = h.g;
    osc.connect(gn); gn.connect(master);
    gn.gain.setValueAtTime(h.g, now);
    gn.gain.exponentialRampToValueAtTime(0.001, now + Math.max(0.1, dur * (0.4 / h.r)));
    osc.start(now); osc.stop(now + dur + 0.1);
  }
  const pk = vol * 0.7, att = 0.007, dec = 0.12, sus = 0.3;
  const rel = Math.min(0.25, dur * 0.25);
  const susEnd = now + Math.max(dur - rel, att + dec);
  master.gain.setValueAtTime(0.001, now);
  master.gain.linearRampToValueAtTime(pk, now + att);
  master.gain.exponentialRampToValueAtTime(pk * sus, now + att + dec);
  master.gain.setValueAtTime(pk * sus, susEnd);
  master.gain.exponentialRampToValueAtTime(0.001, susEnd + rel);
}
"""

# ── JavaScript：Web Audio 合成（无采样 fallback）─────────────────
JS_AUDIO_SYNTH = r"""
let audioCtx = null;
function ensureCtx() {
  if (!audioCtx) audioCtx = new (window.AudioContext||window.webkitAudioContext)();
  if (audioCtx.state==='suspended') audioCtx.resume();
  return audioCtx;
}
let _masterComp = null;
function getMaster() {
  const ctx = ensureCtx();
  if (!_masterComp) {
    _masterComp = ctx.createDynamicsCompressor();
    _masterComp.threshold.value = -18;
    _masterComp.knee.value = 6;
    _masterComp.ratio.value = 4;
    _masterComp.attack.value = 0.003;
    _masterComp.release.value = 0.25;
    _masterComp.connect(ctx.destination);
  }
  return _masterComp;
}
function preloadSamples() {}
function playMidi(midi, dur) {
  if (midi < 0) return;
  const ctx = ensureCtx();
  const freq = 440 * Math.pow(2, (midi - 69) / 12);
  const vol = parseInt(document.getElementById('volSlider').value) / 100 * 1.8;
  const now = ctx.currentTime;
  const master = ctx.createGain(); master.connect(getMaster());
  const harmonics = [{r:1,g:0.5},{r:2,g:0.22},{r:3,g:0.1},{r:4,g:0.05}];
  for (const h of harmonics) {
    const osc = ctx.createOscillator(); const gn = ctx.createGain();
    osc.type='sine'; osc.frequency.value=freq*h.r; gn.gain.value=h.g;
    osc.connect(gn); gn.connect(master);
    gn.gain.setValueAtTime(h.g, now);
    gn.gain.exponentialRampToValueAtTime(0.001, now+Math.max(0.1,dur*(0.4/h.r)));
    osc.start(now); osc.stop(now+dur+0.1);
  }
  const pk=vol,att=0.007,dec=0.12,sus=0.3,rel=Math.min(0.25,dur*0.25);
  const susEnd=now+Math.max(dur-rel,att+dec);
  master.gain.setValueAtTime(0.001,now);
  master.gain.linearRampToValueAtTime(pk,now+att);
  master.gain.exponentialRampToValueAtTime(pk*sus,now+att+dec);
  master.gain.setValueAtTime(pk*sus,susEnd);
  master.gain.exponentialRampToValueAtTime(0.001,susEnd+rel);
}
"""

# ── JavaScript：播放器核心 ─────────────────────────────────────
JS_PLAYER = r"""
const NOTES = {{NOTES_JSON}};
const N = NOTES.length;
let playing=false, noteIdx=0, timer=null, curHL=null;

function getBpm() { return parseInt(document.getElementById('bpmSlider').value); }

function highlight(idx) {
  if (curHL !== null) {
    const e = document.getElementById('hl-'+curHL);
    if (e) e.classList.remove('hl-on');
  }
  curHL = idx;
  const el = document.getElementById('hl-'+idx);
  if (el) {
    el.classList.add('hl-on');
    el.scrollIntoView({behavior:'smooth', block:'nearest'});
  }
}
function clearHL() {
  if (curHL !== null) {
    const e = document.getElementById('hl-'+curHL); if (e) e.classList.remove('hl-on');
    curHL = null;
  }
}

function tick() {
  if (!playing) return;
  if (noteIdx >= N) { resetPlay(); return; }
  const n = NOTES[noteIdx];
  highlight(noteIdx);
  document.getElementById('progFill').style.width = ((noteIdx+1)/N*100)+'%';
  const beatMs = (60/getBpm())*1000;
  const durMs  = beatMs * n.beats;
  if (n.midi >= 0) playMidi(n.midi, durMs/1000);
  noteIdx++;
  timer = setTimeout(tick, durMs);
}

async function startPlay() {
  const btn = document.getElementById('playBtn');
  btn.textContent = btn.textContent.includes('播放') ? '⏳ 加载...' : '⏳';
  btn.disabled = true;
  ensureCtx();
  await preloadSamples();
  btn.disabled = false;
  playing = true;
  btn.textContent = btn.textContent.includes('加载') ? '⏸ 暂停' : '⏸';
  tick();
}
function pausePlay() {
  playing = false;
  if (timer) { clearTimeout(timer); timer = null; }
  const btn = document.getElementById('playBtn');
  btn.textContent = btn.textContent.includes('暂停') ? '▶ 播放' : '▶';
}
function resetPlay() {
  pausePlay(); noteIdx=0; clearHL();
  document.getElementById('progFill').style.width='0%';
}

document.getElementById('playBtn').onclick = () => playing ? pausePlay() : startPlay();
document.getElementById('resetBtn').onclick = resetPlay;
document.getElementById('bpmSlider').oninput = e => {
  document.getElementById('bpmVal').textContent = e.target.value;
  document.getElementById('bpmDisplay').textContent = e.target.value;
};
"""


# ─────────────────────────────────────────────────────────────────
# 主生成函数
# ─────────────────────────────────────────────────────────────────

def generate_html(
    notation, key='C', bpm=80, time_sig='4/4',
    title='练习曲', composer='',
    style='phone',          # 'phone' | 'dark'
    output='jianpu.html',
    bars_per_line=None,
    sf2_path=None,
    no_audio_embed=False,
):
    notes = parse_notation(notation)
    if not notes:
        raise ValueError('notation 解析为空')

    portrait = (style == 'phone')
    bpl = bars_per_line or (2 if portrait else 4)

    print(f'  → {len(notes)} 音符 | {bpm} BPM | {key} 大调 | {time_sig} | 样式={style}')

    # 1. 渲染 LilyPond SVG
    jianpu_text = notes_to_jianpu_ly(
        notes, key=key, time_sig=time_sig,
        title=title, bpm=bpm, bars_per_line=bpl
    )

    with tempfile.TemporaryDirectory(prefix='jh_') as tmpdir:
        print('🎼 渲染 LilyPond SVG ...')
        svg_pages, vb, svg_paths = render_svg(jianpu_text, tmpdir, portrait=portrait)

        char_size = vb[3] * 0.025 if len(vb) >= 4 else 3.0
        hl_color = 'rgba(232,93,38,0.45)' if style == 'phone' else 'rgba(232,120,40,0.50)'

        # 2. 提取音符坐标（多页）
        print('📍 提取音符坐标 ...')
        # 用文件路径逐页提取，合并
        all_positions = []
        per_page_counts = []
        for svg_path in svg_paths:
            pp = extract_note_positions_svg(svg_path, 999)
            cnt = len(pp) if pp else 0
            per_page_counts.append(cnt)
            if pp:
                all_positions.extend(pp)

        if len(all_positions) >= len(notes):
            positions = all_positions[:len(notes)]
            print(f'  ✅ {len(positions)} 个精确坐标（{len(svg_paths)} 页：{per_page_counts}）')
            # 对每页注入高亮 rect（全局 id 偏移）
            pages_with_hl = []
            global_offset = 0
            for i, pg in enumerate(svg_pages):
                cnt = per_page_counts[i]
                page_pos = positions[global_offset:global_offset+cnt]
                pg_hl = inject_highlight_rects(pg, page_pos, char_size, start_idx=global_offset)
                pg_hl = pg_hl.replace('{HL_COLOR}', hl_color)
                pages_with_hl.append(pg_hl)
                global_offset += cnt
            svg_with_hl = '\n'.join(pages_with_hl)
        else:
            print(f'  ⚠ 坐标不足（{len(all_positions)}/{len(notes)}），高亮不可用', file=sys.stderr)
            svg_with_hl = '\n'.join(svg_pages).replace('{HL_COLOR}', hl_color)

        # 清理 SVG
        svg_clean = re.sub(r'<\?xml[^?]*\?>', '', svg_with_hl)
        svg_clean = re.sub(r'<!DOCTYPE[^>]*>', '', svg_clean).strip()

        # 3. FluidSynth 采样嵌入
        if not no_audio_embed:
            print('🎹 生成 FluidSynth 采样 ...')
            samples = render_note_samples(notes, key, sf2_path)
        else:
            samples = {}

        # 4. 构建 JS
        notes_json = notes_to_js_array(notes, key)
        if samples:
            samples_json = json.dumps({str(k): v for k, v in samples.items()})
            audio_js = JS_AUDIO_SAMPLES.replace('{{SAMPLES_JSON}}', samples_json)
        else:
            audio_js = JS_AUDIO_SYNTH

        player_js = JS_PLAYER.replace('{{NOTES_JSON}}', notes_json)

        # 5. 选模板
        css = CSS_PHONE if style == 'phone' else CSS_DARK
        tmpl = HTML_PHONE if style == 'phone' else HTML_DARK

        html = tmpl
        html = html.replace('{{CSS}}', css)
        html = html.replace('{{TITLE}}', title)
        html = html.replace('{{COMPOSER}}', composer)
        html = html.replace('{{KEY_DISPLAY}}', _KEY_DISPLAY.get(key, key))
        html = html.replace('{{TIME_SIG}}', time_sig)
        html = html.replace('{{BPM}}', str(bpm))
        html = html.replace('{{SVG_CONTENT}}', svg_clean)
        html = html.replace('{{AUDIO_JS}}', audio_js)
        html = html.replace('{{PLAYER_JS}}', player_js)

        with open(output, 'w', encoding='utf-8') as f:
            f.write(html)

        size_kb = os.path.getsize(output) // 1024
        print(f'✅ HTML 已生成: {output}  ({size_kb} KB)')
        return output


# ─────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='简谱 HTML 播放器生成器')
    parser.add_argument('--musicxml', default='')
    parser.add_argument('--musicxml-part', default=None)
    parser.add_argument('--musicxml-ref-octave', type=int, default=4)
    parser.add_argument('--notation', default='')
    parser.add_argument('--key', default='')
    parser.add_argument('--bpm', type=int, default=0)
    parser.add_argument('--time-sig', default='')
    parser.add_argument('--title', default='')
    parser.add_argument('--composer', default='')
    parser.add_argument('--style', choices=['phone', 'dark'], default='phone',
                        help='phone=手机品牌风格（默认），dark=深色桌面风格')
    parser.add_argument('--bars-per-line', type=int, default=None)
    parser.add_argument('--soundfont', default=None)
    parser.add_argument('--no-audio-embed', action='store_true',
                        help='跳过 FluidSynth 采样嵌入，使用 Web Audio 合成')
    parser.add_argument('--output', default='jianpu.html')
    args = parser.parse_args()

    if args.musicxml:
        from musicxml_to_notation import parse_musicxml
        result = parse_musicxml(
            args.musicxml,
            part_id=args.musicxml_part,
            ref_octave=args.musicxml_ref_octave
        )
        notation = result['notation']
        key      = args.key  or result['key']
        bpm      = args.bpm  or result['bpm']
        time_sig = args.time_sig or result['time_sig']
        title    = args.title or result.get('title') or '练习曲'
        print(f'🎵 MusicXML: {result["tokens"]} 音符, {key} 调, {bpm} BPM')
    elif args.notation:
        notation = args.notation
        key      = args.key or 'C'
        bpm      = args.bpm or 80
        time_sig = args.time_sig or '4/4'
        title    = args.title or '练习曲'
    else:
        parser.error('请提供 --musicxml 或 --notation')

    generate_html(
        notation=notation, key=key, bpm=bpm, time_sig=time_sig,
        title=title, composer=args.composer,
        style=args.style, output=args.output,
        bars_per_line=args.bars_per_line,
        sf2_path=args.soundfont,
        no_audio_embed=args.no_audio_embed,
    )


if __name__ == '__main__':
    main()
