#!/bin/bash
# setup.sh — solfege-video skill 一键安装脚本
# 支持 macOS (Homebrew) + Python 虚拟环境
# 用法：bash setup.sh

set -e
SKILL_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "=== solfege-video skill 安装 ==="
echo "目录: $SKILL_DIR"

# ─── 检测系统 ───────────────────────────────────────────────────────────────
if [[ "$OSTYPE" != "darwin"* ]]; then
  echo "⚠️  当前只支持 macOS，Linux 版本请参考 README.md 手动安装"
  exit 1
fi

# ─── Homebrew ────────────────────────────────────────────────────────────────
if ! command -v brew &>/dev/null; then
  echo "❌ 未找到 Homebrew，请先安装：https://brew.sh"
  exit 1
fi
echo "✅ Homebrew 已安装"

# ─── 系统依赖 ────────────────────────────────────────────────────────────────
echo ""
echo "--- 安装系统依赖 ---"

install_if_missing() {
  local cmd="$1"
  local pkg="${2:-$1}"
  if ! command -v "$cmd" &>/dev/null; then
    echo "  安装 $pkg ..."
    brew install "$pkg"
  else
    echo "  ✅ $cmd 已安装（$(command -v "$cmd")）"
  fi
}

install_if_missing ffmpeg
install_if_missing lilypond
install_if_missing fluidsynth fluid-synth

# ─── Python 虚拟环境 ─────────────────────────────────────────────────────────
echo ""
echo "--- Python 虚拟环境 ---"

VENV_DIR="$SKILL_DIR/.venv"
if [[ ! -d "$VENV_DIR" ]]; then
  echo "  创建虚拟环境 $VENV_DIR ..."
  python3 -m venv "$VENV_DIR"
fi
echo "  激活虚拟环境 ..."
source "$VENV_DIR/bin/activate"

echo "  安装 Python 依赖 ..."
pip install --quiet --upgrade pip
pip install --quiet numpy scipy Pillow mido playwright

echo "  安装 Playwright Chromium ..."
playwright install chromium

echo "  ✅ Python 依赖安装完成"

# ─── SF2 音色库检测 ──────────────────────────────────────────────────────────
echo ""
echo "--- 检查 FluidSynth SF2 音色库 ---"

SF2_PATHS=(
  "/usr/share/sounds/sf2/FluidR3_GM.sf2"
  "/opt/homebrew/share/sounds/sf2/FluidR3_GM.sf2"
  "/usr/local/share/sounds/sf2/FluidR3_GM.sf2"
  "$HOME/Library/Audio/Sounds/Banks/FluidR3_GM.sf2"
)

SF2_FOUND=""
for p in "${SF2_PATHS[@]}"; do
  if [[ -f "$p" ]]; then
    SF2_FOUND="$p"
    break
  fi
done

if [[ -n "$SF2_FOUND" ]]; then
  echo "  ✅ 找到 SF2 音色库: $SF2_FOUND"
else
  echo "  ⚠️  未找到 FluidR3_GM.sf2，尝试从 fluid-synth 安装..."
  # Homebrew fluid-synth 通常包含 soundfont
  BREW_SF2=$(find /opt/homebrew /usr/local -name "*.sf2" 2>/dev/null | head -1)
  if [[ -n "$BREW_SF2" ]]; then
    echo "  ✅ 找到 Homebrew SF2: $BREW_SF2"
  else
    echo "  ℹ️  请手动下载 FluidR3_GM.sf2 并放到以下路径之一："
    for p in "${SF2_PATHS[@]}"; do echo "       $p"; done
    echo "  下载地址：https://keymusician01.s3.amazonaws.com/FluidR3_GM.zip"
  fi
fi

# ─── 冒烟测试 ────────────────────────────────────────────────────────────────
echo ""
echo "--- 冒烟测试 ---"
python3 -c "
import numpy, scipy, PIL, mido
from playwright.sync_api import sync_playwright
print('  ✅ Python 依赖全部可用')
"

# ─── 完成提示 ────────────────────────────────────────────────────────────────
echo ""
echo "=== 安装完成 ==="
echo ""
echo "使用方法："
echo "  1. 激活虚拟环境：source $VENV_DIR/bin/activate"
echo "  2. 生成视频："
echo "     python3 $SKILL_DIR/scripts/generate_phone_video.py \\"
echo "       --musicxml 你的曲子.musicxml --title '曲名' --output 输出.mp4"
echo ""
echo "安装到 OpenClaw workspace："
echo "  cp -r $SKILL_DIR /path/to/.openclaw/workspace-xxx/skills/solfege-video"
echo "  然后在 AGENTS.md 的 Tools 区块注册触发规则"
