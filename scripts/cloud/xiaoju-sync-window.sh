#!/bin/bash
# 小桔错峰常驻 · 同步窗口（stage5 A 方案 · 修复版 2026-10-05）
# 关键修复（对应 07:02 卡死事故）：
#   1) window.service 不再声明 Conflicts=xiaoju-sync.service
#      —— 原对称反杀会让脚本里 "start xiaoju-sync" 把窗口自身杀掉，后端永远起不来
#   2) 进/出窗口的 stop/start 全部由脚本显式控制，并加 || true 防单步失败中断
#   3) stop xiaoju-sync 后追加 kill 兜底，防 daemon 不响应 SIGTERM 卡死整段
#   4) trap EXIT 兜底：无论正常结束还是被 TimeoutStartSec 杀掉，都保证后端归位到目标态
# 行为不变：窗口内短暂跑同步 daemon 独占库，游标追平即收工（上限 5 分钟）。
# 2026-10-07 增补 5b：窗口后做集合级 drift 检测（旁路观测，必须 || true）。
# 2026-10-13 停机保号修订（萧哥拍板 Token 成本控制）：
#   - 窗口结束后后端不再常驻，归位到 stopped（原「兜底拉起后端在线」是 10-05 常驻架构设计，
#     停机保号只 disable 未改本脚本，导致 openworker-server 被每小时拉起常驻、
#     内部自主循环持续烧 LLM——10-13 凌晨 02:28/05:30 手记即云端所发）。
#   - 同步窗口本身不依赖 openworker-server（sync daemon 直接操作 DB），停后端不影响同步。
set -u

LOG=/var/log/xiaoju-sync-window.log
DB=/var/lib/openworker/.deskmate/deskmate.db
CUR=$DB.sync_cursor.json
MAX_SECONDS=${MAX_SECONDS:-300}   # 5 分钟硬上限

log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

oplog_max() {
  sudo -n -u openworker python3 -c "
import sqlite3,sys
c=sqlite3.connect('file:$DB?mode=ro',uri=True,timeout=5)
print(c.execute('SELECT MAX(seq) FROM _sync_oplog').fetchone()[0] or 0)"
}

pull_cursor() {
  sudo -n -u openworker python3 -c "
import json;print(json.load(open('$CUR'))['pull_after_seq'])" 2>/dev/null || echo 0
}

# 兜底：无论如何把后端归位到停机态（2026-10-13 停机保号修订）
restore_backend() {
  log '--- 兜底：确保后端归位停机（停机保号） ---'
  sudo -n systemctl stop xiaoju-sync 2>/dev/null || true
  sudo -n systemctl kill xiaoju-sync 2>/dev/null || true
  sleep 2
  sudo -n systemctl stop openworker-server 2>/dev/null || true
  sleep 3
  log "restore: backend=$(systemctl is-active openworker-server) daemon=$(systemctl is-active xiaoju-sync)"
}
trap restore_backend EXIT

log '=== 同步窗口开始 ==='

# 1. 记录窗口前基线
BEFORE_PULL=$(pull_cursor)
BEFORE_OPLOG=$(oplog_max)
log "基线 pull=$BEFORE_PULL oplog_max=$BEFORE_OPLOG"

# 2. 进窗口：先停 sync（清场），再停后端，再起 sync 独占库
sudo -n systemctl stop xiaoju-sync 2>/dev/null || true
sleep 2
sudo -n systemctl stop openworker-server 2>/dev/null || true
sleep 2
sudo -n systemctl start xiaoju-sync 2>/dev/null || true
log 'xiaoju-sync 已启动（窗口内独占）'

# 3. 轮询等待游标追平（提前收工，不空耗满窗口）
DEADLINE=$(( $(date +%s) + MAX_SECONDS ))
CAUGHT=0
while [ $(date +%s) -lt $DEADLINE ]; do
  sleep 10
  P=$(pull_cursor)
  O=$(oplog_max)
  if [ "$P" -ge "$O" ]; then
    CAUGHT=1
    log "游标已追平 pull=$P >= oplog_max=$O —— 提前收工"
    break
  fi
done

ELAPSED=$(( $(date +%s) - (DEADLINE - MAX_SECONDS) ))
if [ "$CAUGHT" -eq 0 ]; then
  log "未在 ${MAX_SECONDS}s 内追平（用满窗口）pull=$P oplog_max=$O"
fi

# 4. 出窗口：停同步、后端归位停机（停机保号·2026-10-13）
sudo -n systemctl stop xiaoju-sync 2>/dev/null || true
sudo -n systemctl kill xiaoju-sync 2>/dev/null || true
sleep 2
sudo -n systemctl stop openworker-server 2>/dev/null || true
sleep 5

# 5. 核对（沿用 3b 方案硬要求 1 判据）
AFTER_PULL=$(pull_cursor)
AFTER_OPLOG=$(oplog_max)
BACKEND=$(systemctl is-active openworker-server)
DAEMON=$(systemctl is-active xiaoju-sync)
ALIGN=$([ "$AFTER_PULL" -ge "$AFTER_OPLOG" ] && echo ALIGNED || echo NOT_ALIGNED)
log "窗口后 pull=$AFTER_PULL oplog_max=$AFTER_OPLOG [$ALIGN] backend=$BACKEND daemon=$DAEMON 用时=${ELAPSED}s"

# 5b. 旁路观测：集合级 drift 检测（record_id 对称差，判据口径不变）。
#     必须 `|| true`——它是观测项，绝不能因其失败把整个窗口判 failed（清单第 3 步硬要求）。
sudo -n -u openworker python3 /var/lib/openworker/.deskmate/sync/sync_drift_check.py || true

log '=== 同步窗口结束 ==='
echo "$ALIGN"
