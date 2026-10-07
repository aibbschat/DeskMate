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

# 主动产出台账模块（主程序包，不是 sync 平铺包）：随 sync 文件一起上云，
# 避免每次改 proactivity 都手动 scp。目标云端 /opt/openworker/deskmate/proactivity/。
# ⚠️ confidence.py（§11.14 should_act 闸门）+ health_producers.py（P2 三生产者）务必纳入，
# 否则云端跑旧码 → §11.14 修复与 P2 离线生效（10-07 实测：漏传 confidence.py 会让
# 「没发现异常也递卡」在云端照旧复发）。
PROACTIVITY_MODULES=(
  "deskmate/proactivity/ledger.py"
  "deskmate/proactivity/tick.py"
  "deskmate/proactivity/understanding.py"
  "deskmate/proactivity/server.py"
  "deskmate/proactivity/confidence.py"
  "deskmate/proactivity/health_producers.py"
)
CLOUD_PROACTIVITY_DIR="/opt/openworker/deskmate/proactivity"

# P3-c 手动 oplog GC 工具（§11.15）：随 runner 一起上云到 /var/lib/openworker/.deskmate/，
# 与 sync_runner.py 同目录（--cloud-pkg-path 默认即该目录，可 `python3 gc_sync_oplog.py --mode cloud`）。
LOCAL_GC_SCRIPT="scripts/gc_sync_oplog.py"
CLOUD_GC_SCRIPT="$CLOUD_DESKMATE/gc_sync_oplog.py"

# 云端 L2 自主层常驻 tick（路径乙·让云端也是活的副本）：随 sync 文件一起上云到
# /var/lib/openworker/.deskmate/sync/xiaoju_agency_tick.py（systemd xiaoju-agency.service
# 每小时触发，ExecStart 即该路径）。⚠️ 它内部调用 run_producers_all（P2 三健康生产者），
# 漏传会让云端自主副本永远不跑 P2（10-07 实测：deploy 脚本原本漏了它 → 云端跑旧码不递健康卡）。
LOCAL_AGENCY_TICK="scripts/xiaoju_agency_tick.py"
CLOUD_AGENCY_TICK="$CLOUD_SYNC_DIR/xiaoju_agency_tick.py"

# 云端窗口内的 drift 检测脚本（2026-10-07 三个待决落地·清单第1/3步）：Mac 日历型 tick 后
# drift 由云端窗口接管，脚本随 sync 一起上云到 sync/sync_drift_check.py，窗口在 ALIGNED 后调用
# （旁路观测，调用处必须 `|| true`，不得因它失败把整个窗口判 failed）。
# ⚠️ 漏传会让清单第 3 步「云端窗口加 drift 调用」直接 FileNotFound → 窗口报错（10-07 实测缺）。
LOCAL_DRIFT_CHECK="scripts/sync_drift_check.py"
CLOUD_DRIFT_CHECK="$CLOUD_SYNC_DIR/sync_drift_check.py"

# LLM 通道组+ 熔断（2026-10-06，设计 §二/§3）：本地与云端**必须同码同熔断表**，
# 否则会出现「本地熔断了、云端还在撞」——与记忆里「48 条身份分裂」同类的事故。
# 目标：/opt/openworker/deskmate/{memory,proactivity}/ 与 /opt/openworker/coworker/{server,providers}/
CLOUD_DESKMATE_ROOT="/opt/openworker/deskmate"
CLOUD_COWORKER_ROOT="/opt/openworker/coworker"
CHANNEL_MODULES=(
  "deskmate/llm_circuit_breaker.py"       # 熔断表 + 错误解析
  "deskmate/llm_channels.py"              # 通道组（付费/免费 + 组内自动排）
  "deskmate/memory/llm.py"                # chat/chat_memory 接入熔断与通道组
  "coworker/providers/zen_catalog.py"     # Zen 免费模型动态清单（远端驱动）
  "coworker/server/app.py"                # /v1/llm-channels* 端点
  # ⚠️ 主程序包的同步三件套（2026-10-06 补）：云端 `/opt/openworker/deskmate/memory/`
  # 另有一份 sync_store_sqlite.py，而上面的 CHANNEL_MODULES 里的 llm.py 正是 import它。
  # 只同步平铺包（LOCAL_MODULES）会导致「主程序包跑旧 SYNC_TABLES」——
  # 实测踩过：migrate() 抛 KeyError: 'llm_circuit_breaker'，熔断表建了但没触发器/同步列。
  "deskmate/memory/sync_store_sqlite.py"
  "deskmate/memory/sync_daemon.py"
  "deskmate/memory/sync_transport.py"
  # ⚠️ 每轮上下文注入（§11.16 层一）：build_full_context_for_turn 挂「今天已递待办」块；
  # 云端聊天（openworker-server 走 manager.py 构建小桔每轮上下文）必须同码，否则小桔
  # 在云端「不知道自己递过什么」。2026-10-07 之前 deploy 脚本漏了它，云端 manager.py
  # 停在 Sep 30 旧码——本轮必须把层一注入推上去（红线：本地与云端代码须完全一致）。
  "deskmate/memory/manager.py"
  # ⚠️ P1 汇流最小版（2026-10-07）：认知层两件套。store.py 新增 memory_anchors 表 +
  # recall_items 顺锚走一跳；extractor.py 抽取时顺手挂锚。云端 openworker-server 每轮
  # 经 memory_injector → CognitiveStore.recall_items 构建上下文；漏传 → 云端跑旧码
  # （无锚表 / 不顺锚），本地同步上去的锚数据也召不回邻座（同 manager.py 漏传教训）。
  "deskmate/cognitive/store.py"
  "deskmate/cognitive/extractor.py"
  # ⚠️ P2-R3 lessons 注入（2026-10-07）：memory_injector.py 含 build_lessons_context，
  # manager.py L662 函数内 `from ... import build_lessons_context` 直接引用它。
  # 漏传 → 云端旧 memory_injector 无此函数，R3 lessons 注入因 fail-open 静默失效
  # （小桔在云端「学不会」你教的规矩）。本轮补足，确保 P2-R3 云端真生效。
  "deskmate/cognitive/memory_injector.py"
)

# 小桔手记行为（ai_circle_column.py）：含「专属表情 [xj:id] 封闭词表」注入（2026-10-07
# 247d4e2）。此前 deploy 清单不含 agency/behaviors/ 包，导致云端跑 10-01 整包旧码、手记
# 无表情块。现纳入同步 → 目标 /opt/openworker/deskmate/agency/behaviors/（与 CHANNEL_MODULES
# 同款 deskmate/* 落点 + 清 __pycache__ + md5 核对）。⚠️ 该文件改动须先 commit 进 fork/base，
# 否则预检会拒（与清单内其它文件同一红线）。
AGENCY_BEHAVIORS_MODULES=(
  "deskmate/agency/behaviors/ai_circle_column.py"
)

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
ALL_FILES=("${LOCAL_MODULES[@]}" "$LOCAL_RUNNER" "${PROACTIVITY_MODULES[@]}" "${CHANNEL_MODULES[@]}" "${AGENCY_BEHAVIORS_MODULES[@]}" "$LOCAL_AGENCY_TICK" "$LOCAL_DRIFT_CHECK")
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
  echo "==> 预检通过：sync 模块 + runner + gc 工具 + proactivity 模块 + 通道/熔断模块 + 云端 L2 agency tick + 云端 drift 脚本均为已提交状态"
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

echo "==> 推送 GC 工具到云端 $CLOUD_GC_SCRIPT"
scp -q -o ConnectTimeout=15 "$LOCAL_GC_SCRIPT" "$SSH_HOST:/tmp/sync_deploy_gc.py"
ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo -n -u openworker cp '/tmp/sync_deploy_gc.py' '$CLOUD_GC_SCRIPT'"
ssh -o ConnectTimeout=15 "$SSH_HOST" "rm -f '/tmp/sync_deploy_gc.py'" || true
echo "    ✓ gc_sync_oplog.py"

echo "==> 推送云端 L2 自主 tick 到云端 $CLOUD_AGENCY_TICK"
scp -q -o ConnectTimeout=15 "$LOCAL_AGENCY_TICK" "$SSH_HOST:/tmp/agency_deploy_tick.py"
ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo -n -u openworker cp '/tmp/agency_deploy_tick.py' '$CLOUD_AGENCY_TICK'"
ssh -o ConnectTimeout=15 "$SSH_HOST" "rm -f '/tmp/agency_deploy_tick.py'" || true
echo "    ✓ xiaoju_agency_tick.py"

echo "==> 推送云端 drift 检测脚本到云端 $CLOUD_DRIFT_CHECK"
scp -q -o ConnectTimeout=15 "$LOCAL_DRIFT_CHECK" "$SSH_HOST:/tmp/drift_deploy_check.py"
ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo -n -u openworker cp '/tmp/drift_deploy_check.py' '$CLOUD_DRIFT_CHECK'"
ssh -o ConnectTimeout=15 "$SSH_HOST" "rm -f '/tmp/drift_deploy_check.py'" || true
echo "    ✓ sync_drift_check.py"

# ---- 3b. 推送 proactivity 模块到云端主程序包 ----
echo "==> 推送 proactivity 模块到云端 $CLOUD_PROACTIVITY_DIR"
for f in "${PROACTIVITY_MODULES[@]}"; do
  base="$(basename "$f")"
  scp -q -o ConnectTimeout=15 "$f" "$SSH_HOST:/tmp/proactivity_deploy_$base"
  ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo -n -u openworker cp '/tmp/proactivity_deploy_$base' '$CLOUD_PROACTIVITY_DIR/$base'"
  ssh -o ConnectTimeout=15 "$SSH_HOST" "rm -f '/tmp/proactivity_deploy_$base'" || true
  # 清 proactivity 的 __pycache__，避免旧 .pyc 复用（与手动 scp 同风险）
  ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo -n -u openworker rm -rf '$CLOUD_PROACTIVITY_DIR/__pycache__'" || true
  echo "    ✓ $base"
done

# ---- 3c. 推送通道组/ 熔断模块（本地与云端必须同码，见 CHANNEL_MODULES 注释）----
echo "==> 推送通道组/熔断模块 + 自主行为包到云端主程序包"
for f in "${CHANNEL_MODULES[@]}" "${AGENCY_BEHAVIORS_MODULES[@]}"; do
  base="$(basename "$f")"
  # 目标目录按本地相对路径推（deskmate/xxx.py → $CLOUD_DESKMATE_ROOT/xxx.py；
  # coworker/xxx.py → $CLOUD_COWORKER_ROOT/xxx.py），必要时建目录
  case "$f" in
    deskmate/*)   dest_dir="$CLOUD_DESKMATE_ROOT/$(dirname "${f#deskmate/}")" ;;
    coworker/*)   dest_dir="$CLOUD_COWORKER_ROOT/$(dirname "${f#coworker/}")" ;;
    *)            dest_dir="$CLOUD_DESKMATE_ROOT" ;;
  esac
  scp -q -o ConnectTimeout=15 "$f" "$SSH_HOST:/tmp/channel_deploy_$base"
  ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo -n -u openworker mkdir -p '$dest_dir'"
  ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo -n -u openworker cp '/tmp/channel_deploy_$base' '$dest_dir/$base'"
  ssh -o ConnectTimeout=15 "$SSH_HOST" "rm -f '/tmp/channel_deploy_$base'" || true
  # 清该目录 __pycache__（否则旧 .pyc 复用 = 云端跑旧码，10-05 已踩）
  ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo -n -u openworker rm -rf '$dest_dir/__pycache__'" || true
  echo "    ✓ $f → $dest_dir"
done

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
local_md5="$(md5 -q "$LOCAL_GC_SCRIPT")"
cloud_md5="$(ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo -n -u openworker md5sum '$CLOUD_GC_SCRIPT' 2>/dev/null | awk '{print \$1}'")"
if [[ "$local_md5" == "$cloud_md5" ]]; then
  echo "    ✓ gc_sync_oplog.py  $local_md5"
else
  echo "    ✗ gc_sync_oplog.py  本地=$local_md5 云端=$cloud_md5  (不一致!)" >&2
  exit 1
fi
local_md5="$(md5 -q "$LOCAL_AGENCY_TICK")"
cloud_md5="$(ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo -n -u openworker md5sum '$CLOUD_AGENCY_TICK' 2>/dev/null | awk '{print \$1}'")"
if [[ "$local_md5" == "$cloud_md5" ]]; then
  echo "    ✓ xiaoju_agency_tick.py  $local_md5"
else
  echo "    ✗ xiaoju_agency_tick.py  本地=$local_md5 云端=$cloud_md5  (不一致!)" >&2
  exit 1
fi
local_md5="$(md5 -q "$LOCAL_DRIFT_CHECK")"
cloud_md5="$(ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo -n -u openworker md5sum '$CLOUD_DRIFT_CHECK' 2>/dev/null | awk '{print \$1}'")"
if [[ "$local_md5" == "$cloud_md5" ]]; then
  echo "    ✓ sync_drift_check.py  $local_md5"
else
  echo "    ✗ sync_drift_check.py  本地=$local_md5 云端=$cloud_md5  (不一致!)" >&2
  exit 1
fi

# ---- 6. proactivity 模块对齐核对 ----
echo "==> proactivity 模块对齐核对"
for f in "${PROACTIVITY_MODULES[@]}"; do
  base="$(basename "$f")"
  local_md5="$(md5 -q "$f")"
  cloud_md5="$(ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo -n -u openworker md5sum '$CLOUD_PROACTIVITY_DIR/$base' 2>/dev/null | awk '{print \$1}'")"
  if [[ "$local_md5" == "$cloud_md5" ]]; then
    echo "    ✓ $base  $local_md5"
  else
    echo "    ✗ $base  本地=$local_md5 云端=$cloud_md5  (不一致!)" >&2
    exit 1
  fi
done

# ---- 7. 通道组/熔断模块对齐核对 ----
echo "==> 通道组/熔断模块 + 自主行为包对齐核对"
for f in "${CHANNEL_MODULES[@]}" "${AGENCY_BEHAVIORS_MODULES[@]}"; do
  base="$(basename "$f")"
  case "$f" in
    deskmate/*)   dest_dir="$CLOUD_DESKMATE_ROOT/$(dirname "${f#deskmate/}")" ;;
    coworker/*)   dest_dir="$CLOUD_COWORKER_ROOT/$(dirname "${f#coworker/}")" ;;
    *)            dest_dir="$CLOUD_DESKMATE_ROOT" ;;
  esac
  local_md5="$(md5 -q "$f")"
  cloud_md5="$(ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo -n -u openworker md5sum '$dest_dir/$base' 2>/dev/null | awk '{print \$1}'")"
  if [[ "$local_md5" == "$cloud_md5" ]]; then
    echo "    ✓ $base  $local_md5"
  else
    echo "    ✗ $base  本地=$local_md5 云端=$cloud_md5  (不一致!)" >&2
    exit 1
  fi
done

echo ""
echo "✅ 本地→云端代码已对齐（3 个 sync 文件 + runner + gc 工具 + 6 个 proactivity + 5 个通道/熔断模块 + 云端 L2 agency tick + 云端 drift 脚本）。"
echo "   同步平铺包仅每小时 :02 窗口加载；主程序包需**重启 openworker-server** 才生效："
echo "     sudo systemctl restart openworker-server"
echo "   ⚠️ 通道/熔断模块若与云端不一致，会出现「本地熔断了、云端还在撞」"
echo "      ——与记忆里「48 条身份分裂」同类事故，故务必跑本脚本对齐。"
echo "   如需立即生效，可手动跑一次：sudo /usr/local/sbin/xiaoju-sync-window.sh"
