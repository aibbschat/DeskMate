#!/usr/bin/env bash
# 本地构建 OpenWorker.app —— 绕过本机 tauri build 在 binaries/sidecar/_internal 深树上的 ENOTDIR 崩溃。
#
# 背景：本机（arm64 macOS / APFS）上 `tauri build` 的 build 脚本会用 WalkDir 递归打包进
# `binaries/sidecar/` 的 PyInstaller 后端深目录树（_internal/…），在 pypdf 等 .dist-info 处
# 报 `Not a directory (os error 20)` 而失败。这是 tauri 2.11.2 在本机的 POSIX 行为怪癖，
# 项目 CI（其他环境）能正常过。本脚本用「空 sidecar 资源」避开这次遍历：产物
# Contents/Resources/sidecar 为空，运行时 app 自动回退到本机 .venv/bin/openworker-server 起后端
# （src-tauri/src/lib.rs 的 step 4 回退链），功能完全正常。
#
# 用法：
#   ./build_local_app.sh            # 仅构建到 target/release/bundle/macos/OpenWorker.app
#   ./build_local_app.sh --install  # 构建并覆盖 /Applications/OpenWorker.app（会先退出运行中的 app）
#
# 前置：
#   - Rust + Node/npm，且 surfaces/gui 依赖已装（npm ci / npm install）。
#   - 仓库根 .venv 存在且含可启动的 openworker-server（后端回退依赖它）。
#     缺了就先：python3 -m venv .venv && .venv/bin/pip install -e '.[bedrock]'
#
# 重要：此本地包不含打包的 sidecar，换机器/删 .venv 会后端起不来。
#       正式发布请走 build_dmg.sh（打真 sidecar，CI 能过）。
#
# 🔑 关键认知：本脚本后端运行时回退到 .venv/bin/openworker-server，而 .venv 里 coworker 是
#    **editable 安装**，直接 import 仓库源码。所以【改 Python 即时生效，重开 .app 即可，
#    不必重跑本脚本】；重跑只为刷新 .app 外壳/前端。只有改了 Rust/Tauri 才需要重跑。
#    （对比 build_dmg.sh：它把后端冻结进 .app，改 Python 必须重跑才生效，且会走指纹跳过重冻。）

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GUI="$ROOT/surfaces/gui"
TAURI="$GUI/src-tauri"
CONF="$TAURI/tauri.conf.json"
EMPTY="$TAURI/empty-sidecar"
APP_OUT="$TAURI/target/release/bundle/macos/OpenWorker.app"
INSTALLED="/Applications/OpenWorker.app"

INSTALL=0
[[ "${1:-}" == "--install" ]] && INSTALL=1

# ---- preflight ----
echo "==> preflight 检查"
[ -d "$GUI" ] || { echo "✗ 找不到 $GUI" >&2; exit 1; }
command -v npm >/dev/null 2>&1 || { echo "✗ 需要 npm" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "✗ 需要 python3" >&2; exit 1; }
if [ ! -x "$ROOT/.venv/bin/openworker-server" ]; then
  echo "⚠ 找不到可回退的后端 $ROOT/.venv/bin/openworker-server" >&2
  echo "  本脚本产出的 app 运行时依赖它，请先建好 .venv：" >&2
  echo "    python3 -m venv .venv && .venv/bin/pip install -e '.[bedrock]'" >&2
  exit 1
fi
echo "    ✓ gui=$GUI  后端回退=$ROOT/.venv/bin/openworker-server"

# ---- 1) 前端 ----
echo "==> [1/3] vite build 前端"
( cd "$GUI" && npm run build )

# ---- 2) 空 sidecar 资源 + 临时改写 tauri.conf.json（构建完还原）----
echo "==> [2/3] 准备空 sidecar 资源并临时改写 tauri.conf.json"
mkdir -p "$EMPTY"
BACKUP="$(mktemp)"
cp "$CONF" "$BACKUP"
cleanup() {
  cp "$BACKUP" "$CONF"
  rm -f "$BACKUP"
  rm -rf "$EMPTY"
}
trap cleanup EXIT
python3 - "$CONF" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d["bundle"]["resources"] = {"empty-sidecar": "sidecar"}
json.dump(d, open(p, "w"), indent=2, ensure_ascii=False)
print("    ✓ tauri.conf.json resources → {\"empty-sidecar\": \"sidecar\"}（构建后自动还原）")
PY

# ---- 3) tauri build ----
echo "==> [3/3] tauri build --bundles app"
( cd "$GUI" && npm run tauri build -- --bundles app )

# ---- 校验产物含新前端 ----
echo "==> 校验产物"
if [ ! -d "$APP_OUT" ]; then
  echo "✗ 未产出 $APP_OUT" >&2
  exit 1
fi
if strings "$APP_OUT/Contents/MacOS/openworker-desktop" 2>/dev/null | grep -q "safety-card"; then
  echo "    ✓ 产物已含安全与沙箱开关（safety-card）"
else
  echo "⚠ 产物未检出 safety-card，请确认前端已编译开关代码" >&2
fi

# ---- 可选：安装 ----
if [ "$INSTALL" -eq 1 ]; then
  echo "==> 安装到 ${INSTALLED:-/Applications/OpenWorker.app}"
  if pgrep -f "OpenWorker.app/Contents/MacOS/openworker-desktop" >/dev/null 2>&1; then
    echo "    退出运行中的 OpenWorker…"
    osascript -e 'tell application "OpenWorker" to quit' 2>/dev/null || true
    for _ in $(seq 1 20); do
      pgrep -f "OpenWorker.app/Contents/MacOS/openworker-desktop" >/dev/null 2>&1 || break
      sleep 0.5
    done
  fi
  rm -rf "${INSTALLED:-/Applications/OpenWorker.app}"
  cp -R "$APP_OUT" /Applications/
  echo "    ✓ 已安装到 ${INSTALLED:-/Applications/OpenWorker.app}（内嵌前端哈希："
  strings "${INSTALLED:-/Applications/OpenWorker.app}/Contents/MacOS/openworker-desktop" 2>/dev/null | grep -oE 'index-[A-Za-z0-9_-]{8}\.js' | sort -u | sed 's/^/      /'
  echo "    ）"
fi

echo "==> 完成。tauri.conf.json 与临时文件已还原/清理。"
