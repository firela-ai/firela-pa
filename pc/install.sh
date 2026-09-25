#!/usr/bin/env bash
# firela-pa PC 一键安装 — P3（docs/pc-package-plan.md 验收：干净机器 ≤1 命令 + token 填空 → 第一句回答）
# 分发：HF 公开仓（app tarball + GGUF + sha256 sidecar）。幂等：每步查现状，已就绪即跳过。
# 用法：bash install.sh [--no-gen] [--voice]（--no-gen 跳过生成模型；--voice 加装全本地语音链）
set -euo pipefail

HF_REPO="${FIRELA_HF_REPO:-firela-ai/firela-pa-pc}"
HF="https://huggingface.co/${HF_REPO}/resolve/main"
DEST="$HOME/.firela-pa"
GEN_MODEL="${FIRELA_GEN_MODEL:-qwen2.5:3b-instruct}"
ROUTER_GGUF="router-merged-Q4_K_M.gguf"   # 默认分发档（P1 裁定：Q4 过门 → 默认；Q8 质量档可选）
say() { printf '\033[1;32m[firela-pa]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[firela-pa]\033[0m %s\n' "$*" >&2; exit 1; }

NO_GEN=0; VOICE=0
for a in "$@"; do case "$a" in
  --no-gen) NO_GEN=1 ;;
  --voice)  VOICE=1 ;;
  *) die "未知参数 ${a}（支持 --no-gen / --voice）" ;;
esac done

case "$(uname -s)" in
  Darwin) OS=mac ;;
  Linux)  OS=linux ;;
  *) die "不支持的平台 $(uname -s)——Windows 请经 WSL2 运行本脚本" ;;
esac

if command -v shasum >/dev/null 2>&1; then SHA="shasum -a 256"; else SHA="sha256sum"; fi
file_sha() { $SHA "$1" | awk '{print $1}'; }

# ---------- 1. Ollama ----------
if ! command -v ollama >/dev/null 2>&1; then
  say "安装 Ollama…"
  if [ "$OS" = mac ]; then
    command -v brew >/dev/null 2>&1 || die "未找到 Homebrew——先装 brew（https://brew.sh）再重跑"
    brew install ollama
  else
    curl -fsSL https://ollama.com/install.sh | sh
  fi
fi
if ! curl -sf localhost:11434/api/version >/dev/null 2>&1; then
  say "启动 Ollama 服务…"
  if [ "$OS" = mac ]; then brew services start ollama; else (nohup ollama serve >/dev/null 2>&1 &); fi
  sleep 3
  curl -sf localhost:11434/api/version >/dev/null 2>&1 || die "Ollama 服务未就绪——请手查 ollama serve"
fi
# #15：版本下限断言（raw 契约硬前提 ≥0.34——空 think 剥离/TEMPLATE 行为实测依赖）
OLLAMA_VER=$(curl -sf localhost:11434/api/version | sed -n 's/.*"version":"\([^"]*\)".*/\1/p')
say "Ollama ${OLLAMA_VER:-?}"
case "${OLLAMA_VER:-0.0.0}" in
  0.[0-9].*|0.1[0-9].*|0.2[0-9].*|0.3[0-3].*) die "Ollama ${OLLAMA_VER} < 0.34（raw 契约前提）——brew upgrade ollama 后重跑";;
esac

# ---------- 2. 应用文件 ----------
mkdir -p "$DEST/models" "$DEST/bin"
OLD_STAMP=$(cat "$DEST/VERSION" 2>/dev/null || true)     # #25：解包前留旧戳（解包会覆盖）
say "下载应用（${HF_REPO}）…"
curl -fsSL "$HF/pc-app.tar.gz" -o "$DEST/pc-app.tar.gz"
curl -fsSL "$HF/pc-app.tar.gz.sha256" -o "$DEST/pc-app.tar.gz.sha256"
NEW_SHA=$(file_sha "$DEST/pc-app.tar.gz")
[ "$NEW_SHA" = "$(awk '{print $1}' "$DEST/pc-app.tar.gz.sha256")" ] \
  || die "应用包 sha256 不符——重跑本脚本重试"
tar xzf "$DEST/pc-app.tar.gz" -C "$DEST"
[ -f "$DEST/orchestrator.py" ] || die "应用包解包失败"

# ---------- 2.5 版本戳（#25：装机自报身份 + 滞后信号；对比在覆盖前） ----------
OLD_SHA=$(printf '%s\n' "$OLD_STAMP" | sed -n 's/^app_sha256=//p')
if [ -n "$OLD_STAMP" ] && [ "$OLD_SHA" = "$NEW_SHA" ]; then
  say "已是最新（$(printf '%s\n' "$OLD_STAMP" | sed -n 's/^build_date=//p') / ${NEW_SHA:0:8}）"
elif [ -n "$OLD_STAMP" ]; then
  OLD_SHA8=${OLD_SHA:0:8}
  say "升级 ${OLD_SHA8:-旧戳缺sha} → ${NEW_SHA:0:8}"
fi
BUILD_DATE=$(sed -n 's/^build_date=//p' "$DEST/VERSION" 2>/dev/null) || true   # 旧 tarball 无 VERSION → 空串容错（set -e）
GIT_HEAD=$(sed -n 's/^git_head=//p' "$DEST/VERSION" 2>/dev/null) || true
(umask 077; printf 'build_date=%s\ngit_head=%s\napp_sha256=%s\ninstall_date=%s\n' \
  "${BUILD_DATE:-unknown}" "${GIT_HEAD:-none}" "$NEW_SHA" "$(date +%F)" > "$DEST/VERSION")

# ---------- 3. 路由模型（sha256 校验，已存在且匹配则跳过） ----------
GGUF_PATH="$DEST/models/$ROUTER_GGUF"
say "路由模型 ${ROUTER_GGUF}（1.1GB，已下载则跳过）…"
curl -fsSL "$HF/$ROUTER_GGUF.sha256" -o "$DEST/models/$ROUTER_GGUF.sha256"
EXPECTED=$(awk '{print $1}' "$DEST/models/$ROUTER_GGUF.sha256")
if [ -f "$GGUF_PATH" ] && [ "$(file_sha "$GGUF_PATH")" = "$EXPECTED" ]; then
  say "已存在且校验通过，跳过下载"
else
  curl -fL --progress-bar "$HF/$ROUTER_GGUF" -o "$GGUF_PATH.part"
  [ "$(file_sha "$GGUF_PATH.part")" = "$EXPECTED" ] || die "模型 sha256 不符——重跑本脚本重试"
  mv "$GGUF_PATH.part" "$GGUF_PATH"
fi

# ---------- 4. 注册 Ollama 模型 ----------
cat > "$DEST/Modelfile" <<EOF
FROM $GGUF_PATH
PARAMETER temperature 0
PARAMETER repeat_penalty 1.0
PARAMETER num_ctx 1024
PARAMETER num_predict 64
PARAMETER stop "<|im_end|>"
EOF
say "注册 firela-router…"
(cd "$DEST" && ollama create firela-router -f Modelfile >/dev/null)

# ---------- 5. l 分支生成模型（可跳过） ----------
if [ "$NO_GEN" = 0 ] && ! ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$GEN_MODEL"; then
  say "下载生成模型 ${GEN_MODEL}（~1.9GB，--no-gen 可跳过）…"
  ollama pull "$GEN_MODEL"
fi
# #15：存在性断言（CLI 无 digest pin——tag 浮动为已记局限，issue 注记）
if [ "$NO_GEN" = 0 ]; then
  ollama show "$GEN_MODEL" >/dev/null 2>&1 || die "生成模型 $GEN_MODEL 拉取未生效——手查 ollama pull"
fi

# ---------- 6. 配置（交互填空，回车留占位；umask 077 → 创建即 600，无 chmod 竞态） ----------
if [ ! -f "$DEST/config.toml" ]; then
  say "配置（直接回车=留占位，稍后编辑 $DEST/config.toml）"
  read -r -p "  vlt API 地址 (如 https://vlt.example.com): " VLT_URL || true
  read -r -p "  vlt access token: " VLT_TOKEN || true
  read -r -p "  relay 网关地址 (如 https://relay.example.com): " RELAY_URL || true
  read -r -p "  relay API key（建议专用+限配额）: " RELAY_KEY || true
  read -r -p "  relay 模型 ID: " RELAY_MODEL || true
  read -r -p "  你/家人的称呼（逗号分隔，用于姓名涂黑）: " NAMES || true
  (umask 077; cat > "$DEST/config.toml" <<EOF
vlt_base_url = "${VLT_URL:-<vlt-host>}"
vlt_region = "cn"
vlt_access_token = "${VLT_TOKEN:-<access-token>}"
relay_base_url = "${RELAY_URL:-<relay-host>}"
relay_api_key = "${RELAY_KEY:-<relay-key>}"
relay_model = "${RELAY_MODEL:-<model-id>}"
gen_model = "$GEN_MODEL"
known_names = [$(printf '%s\n' "$NAMES" | tr ',' '\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//;/^$/d' | sed 's/^/"/;s/$/"/' | paste -sd, -)]
EOF
)
  say "配置已写入 $DEST/config.toml（600）"
fi

# ---------- 7. 备份排除（macOS Time Machine，best-effort） ----------
[ "$OS" = mac ] && tmutil addexclusion "$DEST" >/dev/null 2>&1 || true

# ---------- 7.5 语音链（--voice 可选；红线：ASR/TTS 全本地） ----------
if [ "$VOICE" = 1 ]; then
  say "语音链（增量 ~360MB：whisper.cpp + piper + 双模型）…"
  if ! command -v whisper-cli >/dev/null 2>&1; then
    if [ "$OS" = mac ]; then
      command -v brew >/dev/null 2>&1 || die "未找到 Homebrew——先装 brew 再重跑"
      brew install whisper.cpp            # 公式已改名 whisper.cpp（旧名 whisper-cpp 仍解析）
    else
      die "Linux 语音链暂需自装 whisper.cpp（brew install whisper.cpp 或源码编译）后重跑"
    fi
  fi
  if ! command -v ffmpeg >/dev/null 2>&1; then
    if [ "$OS" = mac ]; then brew install ffmpeg; else die "缺少 ffmpeg（麦采需要）——apt/dnf 安装后重跑"; fi
  fi
  mkdir -p "$DEST/voices"
  if [ ! -f "$DEST/models/ggml-base.bin" ]; then
    say "下载 whisper ggml-base（141MB）…"
    curl -fL --progress-bar "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin" -o "$DEST/models/ggml-base.bin.part"
    mv "$DEST/models/ggml-base.bin.part" "$DEST/models/ggml-base.bin"
  fi
  PIPER_HF="https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/zh/zh_CN/huayan/medium"
  for f in zh_CN-huayan-medium.onnx zh_CN-huayan-medium.onnx.json; do
    [ -f "$DEST/voices/$f" ] || { curl -fL --progress-bar "$PIPER_HF/$f" -o "$DEST/voices/$f.part" && mv "$DEST/voices/$f.part" "$DEST/voices/$f"; }
  done
  if [ ! -x "$DEST/venv/bin/piper" ]; then
    say "安装 piper-tts + opencc（venv）…"
    python3 -m venv "$DEST/venv"
    "$DEST/venv/bin/pip" install -q piper-tts opencc
  fi
  say "语音链自检（piper 合成 → whisper 转写往返）…"
  python3 "$DEST/voice.py" --test || die "语音自检失败——看上方报错（麦权限 TCC 提示见文档）"
fi

# ---------- 8. launcher ----------
cat > "$DEST/bin/firela-pa" <<EOF
#!/usr/bin/env bash
exec python3 "$DEST/orchestrator.py" "\$@"
EOF
chmod +x "$DEST/bin/firela-pa"
if [ -w /usr/local/bin ]; then ln -sf "$DEST/bin/firela-pa" /usr/local/bin/firela-pa; BIN_OK=1
elif [[ ":$PATH:" == *":$HOME/.local/bin:"* ]]; then mkdir -p "$HOME/.local/bin"; ln -sf "$DEST/bin/firela-pa" "$HOME/.local/bin/firela-pa"; BIN_OK=1
fi
if [ "${BIN_OK:-0}" = 1 ]; then say "命令已就绪：firela-pa \"你的问题\"（或 python3 $DEST/orchestrator.py）"
else say "把以下行加进 shell 配置后即可用 firela-pa 命令：\n  export PATH=\"\$PATH:$DEST/bin\""
fi

# ---------- 9. 冒烟（l 分支 = 无需任何 token 即答） ----------
if [ "${FIRELA_NO_SMOKE:-0}" != 1 ] && [ "$NO_GEN" = 0 ]; then
  say "冒烟测试…"
  python3 "$DEST/orchestrator.py" "用一句话介绍你自己" || die "冒烟失败——查看上方报错"
  say "✅ 安装完成。试试：firela-pa \"这个月外卖花了多少\"（查账需先填好 config.toml 的 vlt 配置）"
fi
