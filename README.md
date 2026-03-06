# solfege-video — AI 简谱练习视频生成器

从 MusicXML 文件或文字简谱，自动生成 **1080×1920 手机竖版练习 MP4**。

- 🎼 **LilyPond** 专业简谱排版（小节线、时值、高低音点全部正确）
- 🎹 **FluidSynth** 钢琴音色（高质量 SF2 采样，非合成音）
- 🎯 动态橙色高亮光标，与音频严格同步
- 📱 1080×1920 竖屏，可直接发布小红书/抖音

## 效果预览

```
MusicXML 文件（送别.musicxml）
      ↓ 约 15-20 秒
1080×1920 MP4（FluidSynth 钢琴音色 + 动态简谱高亮）
```

---

## 快速开始

### 1. 克隆仓库

```bash
git clone https://github.com/your-org/solfege-video-skill.git
cd solfege-video-skill
```

### 2. 一键安装依赖

```bash
bash setup.sh
```

`setup.sh` 自动安装：
- `ffmpeg`、`lilypond`、`fluid-synth`（通过 Homebrew）
- Python 包：`numpy scipy Pillow mido playwright`
- Playwright Chromium 浏览器
- 检测 FluidR3_GM.sf2 音色库

### 3. 生成视频

```bash
# 激活 Python 虚拟环境（setup.sh 创建）
source .venv/bin/activate

# 从 MusicXML 生成手机视频（一步完成）
python3 scripts/generate_phone_video.py \
  --musicxml 你的曲子.musicxml \
  --title "曲目名称" \
  --composer "作曲者" \
  --output output.mp4
```

---

## 作为 OpenClaw Skill 安装

如果你使用 [OpenClaw](https://openclaw.ai)，可以把这个 skill 安装到你的 agent workspace：

```bash
# 复制到对应 workspace 的 skills 目录
cp -r solfege-video-skill /path/to/.openclaw/workspace-xxx/skills/solfege-video
```

然后在对应 workspace 的 `AGENTS.md` → `## Tools` 区块追加：

```markdown
- **solfege-video**：简谱练习视频生成。触发词：「上传 MusicXML」「把这个谱子做成视频」
  「MusicXML 转视频」「生成视唱视频」。读 `skills/solfege-video/SKILL.md`。
```

以及 `SOUL.md` 的技能列表加入：

```markdown
- **solfege-video**：MusicXML 一键生成手机竖版练习视频；也支持 notation 文本 → 视唱/节奏 MP4
```

然后运行 `python3 scripts/reset-session.py <agentId>` 重置 session 生效。

---

## 详细用法

### 命令行参数

| 参数 | 说明 | 默认值 |
|---|---|---|
| `--musicxml` | MusicXML 文件路径 | — |
| `--html` | 已有 HTML 播放器路径（跳过 HTML 生成步骤） | — |
| `--title` | 曲目名称 | 练习曲 |
| `--composer` | 作曲者 | （空） |
| `--bpm` | 覆盖 BPM（0=从文件自动读取，推荐不设置） | 0 |
| `--output` | 输出 MP4 路径 | output.mp4 |
| `--scroll-frames` | 音符间过渡帧数（0=不滚动） | 3 |
| `--keep-frames` | 保留截图帧（调试用） | 否 |

### 分两步（先预览 HTML）

```bash
# 第一步：生成交互式 HTML（可在浏览器中点击播放预览）
python3 scripts/generate_jianpu_html.py \
  --musicxml 你的曲子.musicxml \
  --title "曲目名称" \
  --style phone \
  --output preview.html

# 在浏览器中打开 preview.html 确认效果

# 第二步：满意后生成视频
python3 scripts/generate_phone_video.py \
  --html preview.html \
  --output output.mp4
```

### 批量生成

```bash
for f in *.musicxml; do
  name="${f%.musicxml}"
  python3 scripts/generate_phone_video.py \
    --musicxml "$f" \
    --title "$name" \
    --output "output/${name}.mp4"
done
```

---

## 获取 MusicXML 文件

推荐从 [MuseScore.com](https://musescore.com) 下载：

1. 在 MuseScore 网站找到目标曲子
2. 使用浏览器扩展 [dl-librescore](https://github.com/LibreScore/dl-librescore) 下载 MusicXML
3. 如果曲子有多个声部，导出时选择"主旋律/人声"声部的 MusicXML

---

## 依赖说明

| 依赖 | 用途 | 安装方式 |
|---|---|---|
| `lilypond` | 专业简谱排版 → SVG | `brew install lilypond` |
| `fluidsynth` + SF2 | 钢琴音色采样生成 | `brew install fluid-synth` |
| `ffmpeg` | 视频帧合成 + 音频编码 | `brew install ffmpeg` |
| `playwright` | 无头浏览器截图 | `pip install playwright && playwright install chromium` |
| `numpy scipy Pillow mido` | 数据处理 + 图像 + MIDI | `pip install numpy scipy Pillow mido` |

---

## 脚本说明

| 脚本 | 功能 |
|---|---|
| `generate_phone_video.py` | 主入口：MusicXML/HTML → 1080×1920 MP4 |
| `generate_jianpu_html.py` | 生成交互式 HTML 简谱播放器（含 FluidSynth 采样） |
| `musicxml_to_notation.py` | MusicXML → 内部 notation 文本格式 |
| `generate_solfege_video.py` | notation 文本 → 视唱/节奏练习 MP4（次要流程） |

---

## 已知限制

- 目前仅支持 macOS（依赖 Homebrew）
- 多声部 MusicXML 自动选取人声/主旋律声部，偶尔需要手动指定
- 重复乐段（`--repeat > 1`）第二遍后光标可能轻微漂移，建议 `--repeat 1`

---

## License

MIT
