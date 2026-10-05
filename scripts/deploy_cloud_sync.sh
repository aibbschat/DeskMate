#!/usr/bin/env bash
# 本地 → 云端 sync 代码自动对齐脚本（一致性红线兜底）
#
# 背景：小桔已上云，本地（仓库 deskmate/memory/sync_*.py + scripts/sync_daemon_runner.py）
# 与云端平铺包（/var/lib/openworker/.deskmate/sync/ + sync_runner.py）必须是同一份代码。
# 两端 import 已同形（云端 sync/ 是带 __init__.py 的包，模块用相对导入；runner 靠
# --cloud-pkg-path 自动选 sync.* 或 deskmate.memory.*），所以对齐 = 直接拷贝 + 清 pycache。
# 差异一旦累积，每次刷新都像一场灾难（萧哥 2026-10-05 拍板的红线）。
#
# 红线：
# - 本脚本只 scp 已提交（committed）的代码；工作区脏则拒绝（强制先 commit 进 fork/base）。
# - 绝不 push origin（只把文件推到云端服务器，不动 git remote）。
# - 云端文件属 openworker 用户，经 /tmp 中转 + sudo -u openworker cp（不直接覆盖）。
#
# 用法：
#   bash scripts/deploy_cloud_sync.sh            # 默认 host=openworker-mirror
#   SSH_HOST=myhost bash scripts/deploy_cloud_sync.sh
#   bash scripts/deploy_cloud_sync.sh --force    # 跳过「必须已提交」校验（不推荐）
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

SSH_HOST="${SSH_HOST:-openworker-mirror}"
CLOUD_DESKMATE="/var/lib/openworker/.deskmate"
CLOUD_SYNC_DIR="$CLOUD_DESKMATE/sync"
CLOUD_RUNNER="$CLOUD_DESKMATE/sync_runner.py"

# 本地源文件 → 云端目标文件（一一对应）
LOCAL_MODULES=(
  "deskmate/memory/sync_transport.py"
  "deskmate/memory/sync_daemon.py"
  "deskmate/memory/sync_store_sqlite.py"
)
LOCAL_RUNNER="scripts/sync_daemon_runner.py"

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

echo "==> 仓库根: $REPO_ROOT"
echo "==> 云端 host: $SSH_HOST"
echo "==> 云端同步包: $CLOUD_SYNC_DIR + $CLOUD_RUNNER"

# ---- 0. 连通性 + 云端路径校验 ----
if ! ssh -o ConnectTimeout=15 -o BatchMode=yes "$SSH_HOST" "test -d '$CLOUD_SYNC_DIR' && test -f '$CLOUD_RUNNER'" >/dev/null 2>&1; then
  echo "✗ 无法连到 $SSH_HOST 或云端同步包路径不存在（$CLOUD_SYNC_DIR / $CLOUD_RUNNER）" >&2
  exit 1
fi

# ---- 1. 必须已提交（红线：本地改动先 commit 进 fork/base）----
ALL_FILES=("${LOCAL_MODULES[@]}" "$LOCAL_RUNNER")
if [[ "$FORCE" -eq 0 ]]; then
  DIRTY=""
  for f in "${ALL_FILES[@]}"; do
    if ! git diff --quiet -- "$f" 2>/dev/null; then
      DIRTY="$DIRTY  $f (工作区未提交)\n"
    fi
    if ! git diff --cached --quiet -- "$f" 2>/dev/null; then
      DIRTY="$DIRTY  $f (已暂存未提交)\n"
    fi
    # 文件不在 HEAD（全新未跟踪）也算脏
    if ! git ls-files --error-unmatch "$f" >/dev/null 2>&1; then
      DIRTY="$DIRTY  $f (未纳入版本控制)\n"
    fi
  done
  if [[ -n "$DIRTY" ]]; then
    echo "✗ 以下同步文件尚未提交，先 commit 进 fork/base 再对齐（或用 --force 绕过）：" >&2
    printf "%b" "$DIRTY" >&2
    exit 1
  fi
  echo "==> 预检通过：4 个同步文件均为已提交状态"
else
  echo "==> --force：跳过「必须已提交」校验（仅在你明确知道在做什么时用）"
fi

# ---- 2. 语法预检（本地）----
echo "==> 本地语法预检"
for f in "${ALL_FILES[@]}"; do
  python3 -c "import ast,sys; ast.parse(open('$f', encoding='utf-8').read())" \
    || { echo "✗ 语法错误: $f" >&2; exit 1; }
done

# ---- 3. scp 经 /tmp 中转 + sudo -u openworker cp ----
# 注意：scp 把 /tmp 中转文件建成登录用户（ubuntu）所有，故清理用登录用户直接 rm；
# 落到云端的 cp 必须 sudo -u openworker（目标文件属 openworker）。两步分开，互不牵连。
echo "==> 推送模块文件到云端 $CLOUD_SYNC_DIR"
for f in "${LOCAL_MODULES[@]}"; do
  base="$(basename "$f")"
  scp -q -o ConnectTimeout=15 "$f" "$SSH_HOST:/tmp/sync_deploy_$base"
  ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo -n -u openworker cp '/tmp/sync_deploy_$base' '$CLOUD_SYNC_DIR/$base'"
  ssh -o ConnectTimeout=15 "$SSH_HOST" "rm -f '/tmp/sync_deploy_$base'" || true
  echo "    ✓ $base"
done

echo "==> 推送 runner 到云端 $CLOUD_RUNNER"
scp -q -o ConnectTimeout=15 "$LOCAL_RUNNER" "$SSH_HOST:/tmp/sync_deploy_runner.py"
ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo -n -u openworker cp '/tmp/sync_deploy_runner.py' '$CLOUD_RUNNER'"
ssh -o ConnectTimeout=15 "$SSH_HOST" "rm -f '/tmp/sync_deploy_runner.py'" || true
echo "    ✓ sync_runner.py"

# ---- 4. 清云端 __pycache__（强制下次窗口重编译新码）----
echo "==> 清云端 __pycache__"
ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo -n -u openworker rm -rf '$CLOUD_SYNC_DIR/__pycache__' && echo '    ✓ cleared'"

# ---- 5. 摘要（md5 对齐核对）----
echo "==> 对齐核对（本地 vs 云端 md5）"
for f in "${LOCAL_MODULES[@]}"; do
  base="$(basename "$f")"
  local_md5="$(md5 -q "$f")"
  cloud_md5="$(ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo -n -u openworker md5sum '$CLOUD_SYNC_DIR/$base' 2>/dev/null | awk '{print \$1}'")"
  if [[ "$local_md5" == "$cloud_md5" ]]; then
    echo "    ✓ $base  $local_md5"
  else
    echo "    ✗ $base  本地=$local_md5 云端=$cloud_md5  (不一致!)" >&2
    exit 1
  fi
done
local_md5="$(md5 -q "$LOCAL_RUNNER")"
cloud_md5="$(ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo -n -u openworker md5sum '$CLOUD_RUNNER' 2>/dev/null | awk '{print \$1}'")"
if [[ "$local_md5" == "$cloud_md5" ]]; then
  echo "    ✓ sync_runner.py  $local_md5"
else
  echo "    ✗ sync_runner.py  本地=$local_md5 云端=$cloud_md5  (不一致!)" >&2
  exit 1
fi

echo ""
echo "✅ 本地→云端 sync 代码已对齐（4 文件 md5 一致）。"
echo "   云端同步 daemon 仅在每小时 :02 错峰窗口启动，下次窗口即加载新码；无需手动重启。"
echo "   如需立即生效，可手动跑一次：sudo /usr/local/sbin/xiaoju-sync-window.sh"
