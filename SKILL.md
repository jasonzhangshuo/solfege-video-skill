---
name: solfege-video
description: "简谱练习视频生成：输入 MusicXML 文件，自动生成 1080×1920 手机竖版 MP4（FluidSynth 钢琴音色 + LilyPond 专业排版 + 动态高亮光标）。也支持直接输入 notation 文本生成视唱/节奏练习视频。触发场景：用户上传 MusicXML、说「把这个谱子做成视频」「MusicXML 转视频」「钢琴谱转简谱视频」「生成视唱视频」「做节奏练习视频」。"
homepage: https://github.com/your-org/solfege-video-skill
metadata:
  clawdbot:
    emoji: "🎵"
    requires:
      bins: [python3, ffmpeg, lilypond, fluidsynth]
      env: []
---

# Solfege Video Generator — 简谱练习视频生成

从 MusicXML 或文字简谱出发，自动合成手机竖版练习视频：
- **音频**：FluidSynth 钢琴音色（高质量 SF2 音色库）
- **画面**：LilyPond 专业简谱排版，动态高亮当前音符
- **输出**：1080×1920 MP4，可直接发布到小红书/抖音

---

## 触发方式

| 用户意图 | 示例话术 | 流程 |
|---|---|---|
| 上传 MusicXML 文件 | 「把这个谱子做成视频」「MusicXML 转视频」 | 流程 A |
| 钢琴谱转简谱视频 | 「钢琴谱转简谱视频」「上传了 musicxml」 | 流程 A |
| 生成视唱练习视频 | 「生成视唱视频」「把简谱做成视频」 | 流程 B |
| 节奏练习视频 | 「做节奏练习视频」「简谱 MP4」 | 流程 B |

**判断规则**：用户提供了 `.musicxml` 文件路径 → 走流程 A；只提供文字简谱 → 走流程 B。

---

## Step 1：检查依赖

```bash
python3 -c "import numpy, scipy, PIL; from playwright.sync_api import sync_playwright; print('✅ Python 依赖 OK')"
ffmpeg -version 2>/dev/null | head -1
lilypond --version 2>/dev/null | head -1
fluidsynth --version 2>/dev/null | head -1
```

缺依赖时运行安装脚本：
```bash
bash {baseDir}/setup.sh
```

---

## 流程 A：MusicXML → 手机竖版视频（主流程）

### Step A1：一步生成（推荐）

```bash
python3 {baseDir}/scripts/generate_phone_video.py \
  --musicxml "{musicxml文件路径}" \
  --title "{曲目名称}" \
  --composer "{作曲者}" \
  --output "{输出路径}.mp4"
```

脚本自动完成：
1. 解析 MusicXML → 提取主旋律、调号、BPM（自动选人声/主旋律声部）
2. LilyPond 排版 → 专业简谱 SVG（小节线、时值、高低音点全部正确）
3. FluidSynth 渲染每个音符的钢琴采样 → base64 嵌入 HTML
4. Playwright 无头浏览器逐音符截图（1080×1920，含进度条）
5. 音频采样按时序混合 → WAV
6. ffmpeg 合成截图序列 + 音频 → MP4

### Step A2：分两步（先预览 HTML 再生成视频）

```bash
# 第一步：生成交互式 HTML 播放器（可在浏览器预览确认）
python3 {baseDir}/scripts/generate_jianpu_html.py \
  --musicxml "{musicxml文件路径}" \
  --title "{曲目名称}" \
  --composer "{作曲者}" \
  --style phone \
  --output /tmp/preview.html

# 第二步：确认满意后录制成视频
python3 {baseDir}/scripts/generate_phone_video.py \
  --html /tmp/preview.html \
  --output "{输出路径}.mp4"
```

### 输出格式

```
✅ 视频已生成：output/xxx.mp4
   时长 XX.Xs  |  XXX 帧  |  XX 音符
```

---

## 流程 B：notation 文本 → 视唱/节奏练习视频（次要流程）

### 简谱格式说明

```
时值：1（四分）  1=（二分）  1==（全音符）  /1（八分）  //1（十六分）  1.（附点）  0（休止）
八度：^1（高八度）  ^^1（高两个八度）  ,1（低八度）
升号：#4（升fa）
小节线：|（仅视觉分隔，不影响时值）
```

### Step B1：确认参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--notation` | 必填 | 简谱文本 |
| `--bpm` | 60 | 练习速度 |
| `--key` | C | 调号（C/D/E/F/G/A/bB/bE 等） |
| `--repeat` | 3 | 循环遍数（建议 1，避免重复段光标漂移）|
| `--title` | 视唱练习 | 视频标题 |
| `--mode` | pitch | pitch=音高；rhythm=纯节奏 |

### Step B2：生成视频

```bash
python3 {baseDir}/scripts/generate_solfege_video.py \
  --notation "1 2 3 4 | 5 4 3 2 | 1=" \
  --bpm 60 \
  --key C \
  --repeat 1 \
  --title "今日视唱第1条" \
  --output /tmp/practice_01.mp4
```

节奏练习（不区分音高）：
```bash
python3 {baseDir}/scripts/generate_solfege_video.py \
  --notation "1 /1 /1 1 | 1 /1 /1 1=" \
  --bpm 80 \
  --mode rhythm \
  --title "节奏练习" \
  --output /tmp/rhythm_01.mp4
```

---

## 错误处理

| 错误现象 | 原因 | 处理方式 |
|---|---|---|
| `LilyPond 未生成 SVG` | lilypond 未安装 | `brew install lilypond`，或运行 `setup.sh` |
| `FluidSynth 采样生成失败` | fluidsynth 未安装或 SF2 文件缺失 | `brew install fluid-synth`；SF2 路径见脚本内 `SF2_PATHS` 列表 |
| Playwright 截图全黑 | chromium 未安装 | `playwright install chromium` |
| `ffmpeg 合成失败` | ffmpeg 未安装 | `brew install ffmpeg` |
| 光标与音频对不上 | 使用了 `--bpm` 覆盖 | 不要覆盖 BPM，让脚本从 HTML 自动读取 |
| 声部选错（选了伴奏） | MusicXML 有多声部 | 用单声部 MusicXML 文件（从 MuseScore 导出时只导出主旋律声部） |
| `import numpy/PIL` 报错 | Python 依赖缺失 | 运行 `setup.sh` 或 `pip install numpy scipy Pillow playwright` |
