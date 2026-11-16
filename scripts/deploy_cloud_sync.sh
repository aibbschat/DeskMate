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

# ============================================================================
# 主程序包集合：全量自动发现 + 显式排除（2026-11-16 治本改造，萧哥拍板）
# ============================================================================
# 病根：本脚本过去用 16 个数组**逐文件白名单**登记要上云的 .py。每加一个文件
# 都得人肉记得进清单，忘了 = 云端缺文件/跑旧码，ImportError 被 fail-open 吞掉
# ⇒ 静默失效（2026-11-16 复核实测：在册约 193 个 vs 生产 425 个，232 个未登记，
# 含被生产 import 的 deskmate/memory/sync.py、coworker/automation/store.py 等）。
# 处置（原则反转）：**默认全传，「不传的」才登记**——
#   MAIN_SET    = find deskmate coworker 全量生产 .py（排除 test_* 与 __pycache__）；
#   MAIN_EXCLUDE = 显式排除表，当前为空。今后确有「只本地不上云」的文件，必须
#                 在这里登记并写明理由；漏登记从此结构上不可能发生。
# 桌面本地专属模块（stt.py / tts.py / xiaomi_* 等）也照传：项目既有先例——
# 「多放一个不 import 的模块无害，少放一个就是 fail-open 静默失效」，且红线要求
# 两端代码完全一致。
# 2026-10 ~ 2026-11 各批「必须同批同步」的事故注释（user_fact_context / world_context /
# manager / inbox / constants 包 / …）已随 16 个数组一并移除：全量同批后「忘了同批」
# 不再可能。历史教训可考：git 历史中本文件 2026-11-16 之前的版本，与
# .workbuddy/memory/PITFALLS.md。
MAIN_EXCLUDE=(
  # （当前无排除项。加条目必须写理由；条目会同步作用于末尾 _reconcile 对账口径。）
)
MAIN_SET=()
while IFS= read -r _f; do
  _skip=0
  for _x in ${MAIN_EXCLUDE[@]+"${MAIN_EXCLUDE[@]}"}; do
    [[ "$_f" == "$_x" ]] && _skip=1
  done
  if [[ "$_skip" -eq 0 ]]; then
    MAIN_SET+=("$_f")
  fi
done < <(find deskmate coworker -name '*.py' -not -path '*__pycache__*' -not -name 'test_*' -not -path '*/tests/*' | sort)

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

# 主程序包镜像根（MAIN_SET 落点）：
#   deskmate/xxx.py → $CLOUD_DESKMATE_ROOT/xxx.py；coworker/xxx.py → $CLOUD_COWORKER_ROOT/xxx.py
CLOUD_DESKMATE_ROOT="/opt/openworker/deskmate"
CLOUD_COWORKER_ROOT="/opt/openworker/coworker"

# 人格文件（2026-10-24 补；2026-11-06 扩）：deskmate.md 被云端 persona loader 读取；
# 8 位创作专家人设（含新增 brainstormer）是 Bot Mode / 圆桌共用的 builtin persona，
# 本地改厚后必须一并上云，否则 7 段 .md 集合对账会报不一致却「只查不传」。
# 它们是 .md，下面的 ast.parse 预检会误报，故**不进 ALL_FILES**，仅走 3c 段 scp
# + 末尾对账 .md 扫描。漏传 → 云端跑旧人格。
PERSONA_FILES=(
  "coworker/personas/builtin/deskmate.md"
  # ⚠ 2026-11-11 B 方案（萧哥「先打地基，然后按 B 执行」）：11 位写作专家由扁平
  # <id>.md 转成 OPE-58 bundle —— <id>/manifest.md + 各自一本独门手艺
  # <id>/skills/<id>-method/SKILL.md。扁平 .md 会让 persona_skill_scope 指向共享的
  # builtin/skills/（专家手艺漏进小桔技能池），必须走 bundle 才有物理隔离。
  # 旧的 11 个扁平路径若留在清单里，scp 会指向不存在的文件；而 .md 对账用 join
  # 只比「共有文件」，新增 bundle 文件不进清单会**静默不同步**（fail-open 同款坑）。
  # 3c 段按 dirname 推导落点，多层子目录无需改传输逻辑；basename 虽同为
  # manifest.md / SKILL.md，但循环内 scp→cp→rm 顺序执行，不会互相覆盖。
  # 🔴 云端若仍留有旧的 11 个 <id>.md，部署时需人工确认后清理：registry 先注册
  #    扁平、再被 bundle 覆盖（语义以 bundle 为准），但留着是脏数据且会干扰排查。
  "coworker/personas/builtin/brainstormer/manifest.md"
  "coworker/personas/builtin/brainstormer/skills/brainstormer-method/SKILL.md"
  "coworker/personas/builtin/lit-editor/manifest.md"
  "coworker/personas/builtin/lit-editor/skills/lit-editor-method/SKILL.md"
  "coworker/personas/builtin/adaptation/manifest.md"
  "coworker/personas/builtin/adaptation/skills/adaptation-method/SKILL.md"
  "coworker/personas/builtin/historian/manifest.md"
  "coworker/personas/builtin/historian/skills/historian-method/SKILL.md"
  "coworker/personas/builtin/proofreader/manifest.md"
  "coworker/personas/builtin/proofreader/skills/proofreader-method/SKILL.md"
  "coworker/personas/builtin/reader/manifest.md"
  "coworker/personas/builtin/reader/skills/reader-method/SKILL.md"
  "coworker/personas/builtin/publisher/manifest.md"
  "coworker/personas/builtin/publisher/skills/publisher-method/SKILL.md"
  "coworker/personas/builtin/genre/manifest.md"
  "coworker/personas/builtin/genre/skills/genre-method/SKILL.md"
  "coworker/personas/builtin/editor/manifest.md"
  "coworker/personas/builtin/editor/skills/editor-method/SKILL.md"
  "coworker/personas/builtin/outline/manifest.md"
  "coworker/personas/builtin/outline/skills/outline-method/SKILL.md"
  "coworker/personas/builtin/character/manifest.md"
  "coworker/personas/builtin/character/skills/character-method/SKILL.md"
  # 主编 / 影视线 5 个 bundle（原样保留）。
  "coworker/personas/builtin/editor-in-chief/manifest.md"
  "coworker/personas/builtin/outline-writer/manifest.md"
  "coworker/personas/builtin/screenwriter/manifest.md"
  "coworker/personas/builtin/script-reviewer/manifest.md"
  "coworker/personas/builtin/plot-architect/manifest.md"
)
CLOUD_PERSONA_DIR="$CLOUD_COWORKER_ROOT/personas/builtin"

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

echo "==> 仓库根: $REPO_ROOT"
echo "==> 云端 host: $SSH_HOST"
echo "==> 云端同步包: $CLOUD_SYNC_DIR + $CLOUD_RUNNER"
echo "==> 主程序包自动发现: ${#MAIN_SET[@]} 个生产 .py（显式排除 ${#MAIN_EXCLUDE[@]} 个）"

# ---- 0. 连通性 + 云端路径校验 ----
if ! ssh -o ConnectTimeout=15 -o BatchMode=yes "$SSH_HOST" "test -d '$CLOUD_SYNC_DIR' && test -f '$CLOUD_RUNNER'" >/dev/null 2>&1; then
  # ${} 花括号体必须：macOS bash 3.2 在 UTF-8 locale 下会把 $CLOUD_RUNNER 后面的
  # 全角「）」吞进变量名，set -u 下报误导性 unbound variable，把「连不上云」真因吞掉
  # （2026-10-20 实测复现：bash 3.2.57，`"$X）"` → `X�: unbound variable`）。
  echo "✗ 无法连到 $SSH_HOST 或云端同步包路径不存在（$CLOUD_SYNC_DIR / ${CLOUD_RUNNER}）" >&2
  exit 1
fi

# ---- 1. 必须已提交（红线：本地改动先 commit 进 fork/base）----
# ALL_FILES = 特殊目的地脚本 + MAIN_SET（自动发现的全量生产 .py）。
# PERSONA_FILES（.md）故意不进 ALL_FILES：下面的语法预检用 ast.parse，对 .md 会误报；
# 它们走 3c 段 scp + 末尾对账 .md 扫描。
ALL_FILES=("${LOCAL_MODULES[@]}" "$LOCAL_RUNNER" "${MAIN_SET[@]}" "$LOCAL_AGENCY_TICK" "$LOCAL_DRIFT_CHECK")
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
  echo "==> 预检通过：sync 模块 + runner + gc 工具 + 主程序包全量 ${#MAIN_SET[@]} 个 .py + 云端 L2 agency tick + 云端 drift 脚本均为已提交状态"
else
  echo "==> --force：跳过「必须已提交」校验（仅在你明确知道在做什么时用）"
fi

# ---- 2. 语法预检（本地，批量单进程；2026-11-16 起全量 .py 一次跑完）----
echo "==> 本地语法预检（${#ALL_FILES[@]} 个文件）"
printf '%s\n' "${ALL_FILES[@]}" | python3 -c '
import ast, sys
bad = []
for line in sys.stdin:
    f = line.rstrip("\n")
    if not f:
        continue
    try:
        with open(f, encoding="utf-8") as fh:
            ast.parse(fh.read())
    except SyntaxError as e:
        bad.append("%s: %s" % (f, e))
    except OSError as e:
        bad.append("%s: %s" % (f, e))
if bad:
    sys.stderr.write("✗ 语法错误:\n" + "\n".join(bad) + "\n")
    sys.exit(1)
'

# ---- 3. scp 经 /tmp 中转 + sudo -u openworker cp ----
# 注意：scp 把 /tmp 中转文件建成登录用户（ubuntu）所有，故清理用登录用户直接 rm；
# 落到云端的 cp 必须 sudo -u openworker（目标文件属 openworker）。两步分开，互不牵连。
echo "==> 推送模块文件到云端 $CLOUD_SYNC_DIR"
for f in "${LOCAL_MODULES[@]}"; do
  base="$(basename "$f")"
  scp -q -o ConnectTimeout=15 "$f" "$SSH_HOST:/tmp/sync_deploy_$base"
  ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo chmod 644 /tmp/sync_deploy_$base || true"   # scp 以 ubuntu 写入，默认 0600；openworker 无读权限（实测 Permission denied）
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

# ---- 3c. 推送主程序包（MAIN_SET 自动发现全量）+ 人格 .md ----
# 2026-11-16 起 proactivity / 通道熔断 / agency / garden / growth / novel_memory /
# coworker 根目录 / teams / skills / perception / connectors / 咨询 / 记忆层等
# 历史数组全部并入 MAIN_SET，统一走本循环镜像上传。
echo "==> 推送主程序包 ${#MAIN_SET[@]} 个 .py + 人格 ${#PERSONA_FILES[@]} 个 .md 到云端主程序包"
for f in "${MAIN_SET[@]}" "${PERSONA_FILES[@]}"; do
  base="$(basename "$f")"
  # 目标目录按本地相对路径推（deskmate/xxx.py → $CLOUD_DESKMATE_ROOT/xxx.py；
  # coworker/xxx.py → $CLOUD_COWORKER_ROOT/xxx.py），必要时建目录
  case "$f" in
    deskmate/*)   dest_dir="$CLOUD_DESKMATE_ROOT/$(dirname "${f#deskmate/}")" ;;
    coworker/*)   dest_dir="$CLOUD_COWORKER_ROOT/$(dirname "${f#coworker/}")" ;;
    *)            dest_dir="$CLOUD_DESKMATE_ROOT" ;;
  esac
  scp -q -o ConnectTimeout=15 "$f" "$SSH_HOST:/tmp/channel_deploy_$base"
  ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo chmod 644 /tmp/channel_deploy_$base || true"   # scp 以 ubuntu 写入，默认 0600；openworker 无读权限（实测 Permission denied）
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

# ---- 5. 摘要（md5 对齐核对：特殊目的地文件）----
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

# ---- 7. 主程序包 + 人格 .md 对齐核对 ----
echo "==> 主程序包 ${#MAIN_SET[@]} 个 .py + 人格 ${#PERSONA_FILES[@]} 个 .md 对齐核对"
for f in "${MAIN_SET[@]}" "${PERSONA_FILES[@]}"; do
  base="$(basename "$f")"
  case "$f" in
    deskmate/*)   dest_dir="$CLOUD_DESKMATE_ROOT/$(dirname "${f#deskmate/}")" ;;
    coworker/*)   dest_dir="$CLOUD_COWORKER_ROOT/$(dirname "${f#coworker/}")" ;;
    *)            dest_dir="$CLOUD_DESKMATE_ROOT" ;;
  esac
  local_md5="$(md5 -q "$f")"
  cloud_md5="$(ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo -n -u openworker md5sum '$dest_dir/$base' 2>/dev/null | awk '{print \$1}'")"
  if [[ "$local_md5" == "$cloud_md5" ]]; then
    echo "    ✓ $f  $local_md5"
  else
    echo "    ✗ $f  本地=$local_md5 云端=$cloud_md5  (不一致!)" >&2
    exit 1
  fi
done

echo ""
echo "✅ 本地→云端代码已对齐（3 个 sync 文件 + runner + gc 工具 + 主程序包全量 ${#MAIN_SET[@]} 个 .py + 人格 ${#PERSONA_FILES[@]} 个 .md + 云端 L2 agency tick + 云端 drift 脚本）。"

# ---- 对账：全量集合级对账（2026-10-09 加；2026-11-16 起判红升级）----
# 起因：旧清单逐文件显式列举，漏加即静默不同步（user_fact_context.py 教训：
# 云端缺文件 → ImportError 被 fail-open 吞掉，「小桔不知道她自己知道」且不报错）。
# 2026-11-16 治本后：MAIN_SET 自动发现全量，「仅本地有」不再可能是「忘加清单」，
# 只可能是传输失败或排除表未同步口径 ⇒ 三项差异**全部判红**，脚本以非零码退出。
echo ""
echo "==> 对账：本地 vs 云端 md5 全量比对"
RECONCILE_FAILED=0
_reconcile() {
  local tmp_l tmp_c
  tmp_l="$(mktemp)"; tmp_c="$(mktemp)"
  # ⚠️ 对账范围 = **生产代码**。云端只跑 openworker-server + 同步窗口、**从不跑测试**，
  # 故 test_*.py 不参与红绿判定（仍会打印差异供人工知悉）。
  local XTRA='-not -name test_*.py'
  # 本地：deskmate/coworker 全量 .py（抓「本地有、云端没有」的漏项）。
  ( cd "$REPO_ROOT" && find deskmate coworker -name '*.py' $XTRA -not -path '*__pycache__*' -type f \
      | while read -r f; do printf '%s  %s\n' "$(md5 -q "$f")" "$f"; done ) | sort > "$tmp_l"
  # 显式排除表同口径过滤：MAIN_EXCLUDE 里的文件「本地有、云端没有」属预期，不报红。
  if [[ ${#MAIN_EXCLUDE[@]} -gt 0 ]]; then
    for x in "${MAIN_EXCLUDE[@]}"; do
      grep -vF "  $x" "$tmp_l" > "$tmp_l.f" || true
      mv "$tmp_l.f" "$tmp_l"
    done
  fi
  ssh "$SSH_HOST" "sudo find /opt/openworker/deskmate /opt/openworker/coworker -name '*.py' $XTRA -not -path '*__pycache__*' -type f 2>/dev/null | while read -r f; do printf '%s  %s\n' \"\$(sudo md5sum \"\$f\" 2>/dev/null | cut -d' ' -f1)\" \"\${f#/opt/openworker/}\"; done" 2>/dev/null | grep -vE "Warning|setlocale" | sort > "$tmp_c"
  local only_l only_c diff
  only_l="$(comm -23 <(cut -d' ' -f3- "$tmp_l" | sort) <(cut -d' ' -f3- "$tmp_c" | sort) | wc -l | tr -d ' ')"
  only_c="$(comm -13 <(cut -d' ' -f3- "$tmp_l" | sort) <(cut -d' ' -f3- "$tmp_c" | sort) | wc -l | tr -d ' ')"
  diff="$(join -j 2 -o 0,1.1,2.1 <(sort -k2 "$tmp_l") <(sort -k2 "$tmp_c") 2>/dev/null \
          | awk '$2 != $3' | wc -l | tr -d ' ')"
  echo "   共有文件内容不一致: $diff"
  echo "   仅本地有（云端缺）: $only_l"
  echo "   仅云端有（云端有旧漂移）: $only_c"
  # 测试文件差异：云端不跑测试，故仅提示不判红（列出来供人工知悉）。
  local _t
  _t="$( { comm -3 <(cd "$REPO_ROOT" && find deskmate coworker -name 'test_*.py' -not -path '*__pycache__*' -type f | sort) \
             <(ssh "$SSH_HOST" "sudo find /opt/openworker/deskmate /opt/openworker/coworker -name 'test_*.py' -not -path '*__pycache__*' -type f 2>/dev/null | sed 's|^|/opt/openworker/|'" 2>/dev/null | grep -vE "Warning|setlocale" | sed 's|^/opt/openworker/||' | sort) ; } 2>/dev/null | wc -l | tr -d ' ')"
  [ "$_t" != "0" ] && echo "   （提示）测试文件差异 $_t 处 —— 云端不跑测试，不影响运行"
  if [ "$diff" != "0" ] || [ "$only_c" != "0" ] || [ "$only_l" != "0" ]; then
    echo "   ❌ 对账未通过（三项全判红：清单已全量自动发现，任何非 0 都是传输缺口/云端旧漂移/排除表口径不一致，请逐条查。红线：须完全一致）"
    RECONCILE_FAILED=1
  else
    echo "   ✅ 对账通过：云端与本地完全一致"
  fi
  # 人格 .md 对账（2026-10-24 加）：deskmate.md 是 .md，上面的 .py 扫描抓不到，
  # 但云端 persona loader 会读它 → 漏传/漂移会让云端跑旧人格且不报错。单独比对。
  local tl_md tc_md md_diff
  tl_md="$(mktemp)"; tc_md="$(mktemp)"
  ( cd "$REPO_ROOT" && find coworker/personas -name '*.md' -not -path '*__pycache__*' -type f \
      | while read -r f; do printf '%s  %s\n' "$(md5 -q "$f")" "${f#coworker/personas/}"; done ) | sort > "$tl_md"
  ssh "$SSH_HOST" "sudo find /opt/openworker/coworker/personas -name '*.md' -not -path '*__pycache__*' -type f 2>/dev/null | while read -r f; do printf '%s  %s\n' \"\$(sudo md5sum \"\$f\" 2>/dev/null | cut -d' ' -f1)\" \"\${f#/opt/openworker/coworker/personas/}\"; done" 2>/dev/null | grep -vE "Warning|setlocale" | sort > "$tc_md"
  md_diff="$(join -j 2 -o 0,1.1,2.1 <(sort -k2 "$tl_md") <(sort -k2 "$tc_md") 2>/dev/null | awk '$2 != $3' | wc -l | tr -d ' ')"
  if [ "$md_diff" != "0" ]; then
    echo "   ❌ 人格 .md 对账未通过（deskmate.md 等与云端不一致）: $md_diff"
    RECONCILE_FAILED=1
  else
    echo "   ✅ 人格 .md 对账通过"
  fi
  rm -f "$tl_md" "$tc_md"
  rm -f "$tmp_l" "$tmp_c"
}
_reconcile
echo "   同步平铺包仅每小时 :02 窗口加载；主程序包需**重启 openworker-server** 才生效："
echo "     sudo systemctl restart openworker-server"
echo "   ⚠️ 通道/熔断模块若与云端不一致，会出现「本地熔断了、云端还在撞」"
echo "      ——与记忆里「48 条身份分裂」同类事故，故务必跑本脚本对齐。"
echo "   如需立即生效，可手动跑一次：sudo /usr/local/sbin/xiaoju-sync-window.sh"

if [[ "$RECONCILE_FAILED" != "0" ]]; then
  echo "✗ 对账存在红项，本次部署判失败（红线：云端与本地须完全一致）" >&2
  exit 1
fi
