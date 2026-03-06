#!/usr/bin/env python3
"""
musicxml_to_notation.py
将 MusicXML 文件中的主旋律（通常是第一声部/最高声部）转换为
generate_solfege_video.py 使用的 notation 字符串格式。

用法：
    python3 musicxml_to_notation.py <file.musicxml> [--part P1] [--ref-octave 5]

输出 JSON：
    {
      "notation": "...",
      "key": "bE",
      "bpm": 80,
      "time_sig": "4/4",
      "title": "送别"
    }
"""
import sys
import json
import argparse
import xml.etree.ElementTree as ET
from fractions import Fraction

# ===== 调号映射：fifths → 简谱key参数 =====
FIFTHS_TO_KEY = {
    0: 'C', 1: 'G', 2: 'D', 3: 'A', 4: 'E', 5: 'B',
    -1: 'F', -2: 'bB', -3: 'bE', -4: 'bA', -5: 'bD', -6: 'bG',
}

# ===== 每个调号的变音规则（调号内置升/降记号）=====
KEY_SIG_ALTERS = {
    # 升调号
    1: {'F': 1}, 2: {'F': 1, 'C': 1}, 3: {'F': 1, 'C': 1, 'G': 1},
    4: {'F': 1, 'C': 1, 'G': 1, 'D': 1},
    # 降调号
    -1: {'B': -1}, -2: {'B': -1, 'E': -1}, -3: {'B': -1, 'E': -1, 'A': -1},
    -4: {'B': -1, 'E': -1, 'A': -1, 'D': -1},
    0: {},
}

# ===== 各调的音阶：音名(step, 调内alter) → 简谱度数 =====
# 统一用 do-re-mi: 1=主音, 2=上方大二度 ...
# 以 C 大调为基础，其他调通过半音数推算
C_MAJOR_SEMITONES = {'C': 0, 'D': 2, 'E': 4, 'F': 5, 'G': 7, 'A': 9, 'B': 11}
SCALE_SEMITONES = [0, 2, 4, 5, 7, 9, 11]  # 大调各度对应半音数


def build_scale_map(fifths: int) -> dict:
    """
    根据调号 fifths 构建 (step, alter_int) → degree 的映射。
    返回的 degree 是 1-7（简谱唱名）。
    """
    key_name = FIFTHS_TO_KEY.get(fifths, 'C')
    key_semitones = {'C': 0, 'G': 7, 'D': 2, 'A': 9, 'E': 4, 'B': 11,
                     'F': 5, 'bB': 10, 'bE': 3, 'bA': 8, 'bD': 1, 'bG': 6}
    tonic = key_semitones.get(key_name, 0)

    result = {}
    for step, base_semi in C_MAJOR_SEMITONES.items():
        for alter in [-1, 0, 1]:
            semi = (base_semi + alter - tonic) % 12
            if semi in SCALE_SEMITONES:
                degree = SCALE_SEMITONES.index(semi) + 1
                result[(step, alter)] = degree
    return result


def beats_to_parts(beats_frac: Fraction) -> tuple:
    """
    时值（以四分音符为1拍）→ (prefix, suffix) 两部分。
    parse_notation 的格式规则：
      前缀（放在八度标记/音符前）：// = 十六分, / = 八分
      后缀（放在音符后）：= 每个延长1拍, . = 附点（×1.5）
    返回 (prefix, suffix)，拼写为 f"{prefix}{octave}{degree}{suffix}"
    """
    b = float(beats_frac)
    if abs(b - 0.25) < 0.01: return ('//', '')
    if abs(b - 0.5)  < 0.02: return ('/', '')
    if abs(b - 0.75) < 0.02: return ('/', '.')    # 附点八分 = 0.75
    if abs(b - 1.0)  < 0.02: return ('', '')
    if abs(b - 1.5)  < 0.02: return ('', '.')     # 附点四分 = 1.5
    if abs(b - 2.0)  < 0.02: return ('', '=')     # 二分 = 2
    if abs(b - 3.0)  < 0.02: return ('', '=.')    # 附点二分 = 3
    if abs(b - 4.0)  < 0.02: return ('', '===')   # 全音符 = 4
    # 近似处理
    if b < 0.4: return ('//', '')
    if b < 0.8: return ('/', '')
    if b < 1.2: return ('', '')
    if b < 1.8: return ('', '.')
    if b < 2.5: return ('', '=')
    return ('', '===')


def parse_musicxml(filepath: str, part_id: str = None, ref_octave: int = 5) -> dict:
    """
    解析 MusicXML 文件，返回 notation 字符串和元数据。
    part_id: 指定声部 ID（如 'P1'），None 则自动选最少休止符的声部（通常是旋律声部）
    ref_octave: 参考八度（默认5，即中央C所在八度）
    """
    tree = ET.parse(filepath)
    root = tree.getroot()

    # ===== 选择声部 =====
    all_parts = root.findall('.//part')
    if part_id:
        target_part = root.find(f'.//part[@id="{part_id}"]')
        if target_part is None:
            raise ValueError(f"找不到声部 {part_id}")
    else:
        # 自动选主旋律声部：
        # 1. 优先选乐器名含 voice/soprano/tenor/alto/flute/violin/melody 的声部
        # 2. 其次选有音符但不是纯钢琴/伴奏的声部（通常音符最少的非空声部）
        # 3. 最后 fallback 到第一个非空声部

        MELODY_KEYWORDS = {'voice', 'soprano', 'tenor', 'alto', 'mezzo', 'flute',
                           'violin', 'oboe', 'clarinet', 'trumpet', 'horn',
                           'melody', '旋律', '人声', '主旋律'}
        ACCOMP_KEYWORDS = {'piano', 'keyboard', 'guitar', 'bass', 'drum',
                           'chord', '伴奏', '钢琴', '吉他', '鼓'}

        def part_pitch_count(p):
            return sum(1 for n in p.findall('.//note') if n.find('pitch') is not None)

        # 查找每个 part 对应的乐器名
        part_names = {}
        for sp in root.findall('.//score-part'):
            pid = sp.get('id', '')
            name = (sp.findtext('part-name', '') + ' ' + sp.findtext('instrument-name', '')).lower()
            part_names[pid] = name

        # 过滤出有实际音符的声部
        valid_parts = [(p, part_pitch_count(p)) for p in all_parts if part_pitch_count(p) > 0]
        if not valid_parts:
            raise ValueError("找不到有任何音符的声部")

        # 1. 优先旋律类乐器
        melody_parts = [
            (p, cnt) for p, cnt in valid_parts
            if any(kw in part_names.get(p.get('id', ''), '') for kw in MELODY_KEYWORDS)
        ]
        # 2. 排除伴奏类乐器
        non_accomp_parts = [
            (p, cnt) for p, cnt in valid_parts
            if not any(kw in part_names.get(p.get('id', ''), '') for kw in ACCOMP_KEYWORDS)
        ]

        if melody_parts:
            # 旋律声部中选音符最少的（通常最简洁）
            target_part = min(melody_parts, key=lambda x: x[1])[0]
        elif non_accomp_parts:
            target_part = min(non_accomp_parts, key=lambda x: x[1])[0]
        else:
            # fallback：所有有效声部中音符最少的（通常是旋律）
            target_part = min(valid_parts, key=lambda x: x[1])[0]

        selected_name = part_names.get(target_part.get('id', ''), '未知')
        print(f"  自动选择声部: {target_part.get('id')} ({selected_name.strip()})", file=sys.stderr)

    # ===== 读取全局元数据 =====
    fifths = int(root.findtext('.//key/fifths', '0'))
    beats_per_bar = int(root.findtext('.//time/beats', '4'))
    beat_type = int(root.findtext('.//time/beat-type', '4'))
    key_name = FIFTHS_TO_KEY.get(fifths, 'C')
    time_sig = f"{beats_per_bar}/{beat_type}"
    bpm = 80
    for s in root.findall('.//sound[@tempo]'):
        bpm = float(s.get('tempo'))
        break

    # 标题（从 credit 或 movement-title 取）
    title = root.findtext('.//movement-title', '') or root.findtext('.//credit-words', '') or '未命名'

    # 调内变音规则
    key_alters = KEY_SIG_ALTERS.get(fifths, {})
    scale_map = build_scale_map(fifths)

    # 主音 MIDI（用于正确计算简谱八度偏移）
    # oct_offset = (note_midi - tonic_midi) // 12  比直接减 ref_octave 准确
    # 因为 Eb 大调的 7 音是 D，在同八度内比 Eb 低，必须 floor div 才能得 -1
    _KEY_TONIC_SEMI = {
        'C': 0, 'G': 7, 'D': 2, 'A': 9, 'E': 4, 'B': 11,
        'F': 5, 'bB': 10, 'bE': 3, 'bA': 8, 'bD': 1, 'bG': 6,
    }
    _CHROMATIC = {'C': 0, 'D': 2, 'E': 4, 'F': 5, 'G': 7, 'A': 9, 'B': 11}
    tonic_semi = _KEY_TONIC_SEMI.get(key_name, 0)
    tonic_midi_base = 12 * (ref_octave + 1) + tonic_semi

    # ===== 遍历所有音符，处理连音线 =====
    events = []  # list of (degree_or_0, oct_offset, beats, is_rest)

    divisions = 1
    pending_tie = None  # (degree, oct_offset, accumulated_beats)

    for measure in target_part.findall('measure'):
        # 更新 divisions
        d = measure.findtext('.//divisions')
        if d:
            divisions = int(d)

        # 更新调号（如果中途变调）
        kf = measure.findtext('.//key/fifths')
        if kf is not None:
            fifths = int(kf)
            key_alters = KEY_SIG_ALTERS.get(fifths, {})
            scale_map = build_scale_map(fifths)
            key_name = FIFTHS_TO_KEY.get(fifths, 'C')
            tonic_semi = _KEY_TONIC_SEMI.get(key_name, 0)
            tonic_midi_base = 12 * (ref_octave + 1) + tonic_semi

        # 更新拍号
        b = measure.findtext('.//time/beats')
        if b: beats_per_bar = int(b)
        bt = measure.findtext('.//time/beat-type')
        if bt: beat_type = int(bt)

        for note in measure.findall('note'):
            # 跳过和弦第二音（只取最高声部第一音）
            if note.find('chord') is not None:
                continue

            dur = int(note.findtext('duration', '0'))
            beats = Fraction(dur, divisions)

            # 休止符
            if note.find('rest') is not None:
                if pending_tie is not None:
                    # 连音线不跨休止符，先提交待处理音
                    events.append(pending_tie)
                    pending_tie = None
                events.append((0, 0, beats, True))
                continue

            step = note.findtext('pitch/step', 'C')
            alter_raw = float(note.findtext('pitch/alter', '0'))
            alter_int = int(round(alter_raw))
            octave = int(note.findtext('pitch/octave', '5'))

            # 有效变音 = 临时记号（显式）或调号内置
            eff_alter = alter_int if alter_raw != 0 else key_alters.get(step, 0)
            degree = scale_map.get((step, eff_alter))
            if degree is None:
                # fallback：忽略变音
                degree = scale_map.get((step, 0), 1)

            # 用 MIDI 差值计算简谱八度偏移（floor div 保证 D4 在 bE 调里算 -1）
            note_midi = 12 * (octave + 1) + _CHROMATIC.get(step, 0) + eff_alter
            oct_offset = (note_midi - tonic_midi_base) // 12

            # 处理连音线
            tie_start = note.find('tie[@type="start"]')
            tie_stop = note.find('tie[@type="stop"]')

            if tie_stop is not None and pending_tie is not None:
                # 延续连音线
                pd, po, pb, _ = pending_tie
                pending_tie = (pd, po, pb + beats, False)
                if tie_start is None:
                    # 连音线结束，提交
                    events.append(pending_tie)
                    pending_tie = None
            elif tie_start is not None:
                # 连音线开始
                if pending_tie is not None:
                    events.append(pending_tie)
                pending_tie = (degree, oct_offset, beats, False)
            else:
                # 普通音符
                if pending_tie is not None:
                    events.append(pending_tie)
                    pending_tie = None
                events.append((degree, oct_offset, beats, False))

    # 提交剩余
    if pending_tie is not None:
        events.append(pending_tie)

    # ===== 移除首尾的全小节休止符 =====
    measure_beats = Fraction(beats_per_bar * 4, beat_type)  # 每小节拍数（四分为单位）

    def is_full_bar_rest(ev):
        deg, oct_off, beats, is_rest = ev
        return is_rest and beats >= measure_beats * Fraction(9, 10)  # 允许10%误差

    # 移除开头
    while events and is_full_bar_rest(events[0]):
        events.pop(0)
    # 移除结尾
    while events and is_full_bar_rest(events[-1]):
        events.pop()

    # ===== 转换为 notation 字符串 =====
    tokens = []
    for degree, oct_offset, beats, is_rest in events:
        prefix, suffix = beats_to_parts(beats)
        if is_rest:
            tokens.append(f'{prefix}0{suffix}')
        else:
            # 八度标记：^ 升八度，, 降八度（相对参考八度）
            if oct_offset > 0:
                oct_mark = '^' * oct_offset
            elif oct_offset < 0:
                oct_mark = ',' * (-oct_offset)
            else:
                oct_mark = ''
            # 格式：{prefix}{oct_mark}{degree}{suffix}
            tokens.append(f'{prefix}{oct_mark}{degree}{suffix}')

    notation = ' '.join(tokens)

    return {
        'title': title.strip(),
        'notation': notation,
        'key': key_name,
        'bpm': int(bpm),
        'time_sig': time_sig,
        'part_id': target_part.get('id'),
        'tokens': len(tokens),
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='MusicXML → notation 字符串转换器')
    parser.add_argument('file', help='MusicXML 文件路径')
    parser.add_argument('--part', default=None, help='指定声部 ID（如 P1），默认自动选主旋律')
    parser.add_argument('--ref-octave', type=int, default=5, help='参考八度（默认5）')
    parser.add_argument('--no-trim', action='store_true', help='不移除首尾空小节')
    args = parser.parse_args()

    result = parse_musicxml(args.file, part_id=args.part, ref_octave=args.ref_octave)
    print(json.dumps(result, ensure_ascii=False, indent=2))
