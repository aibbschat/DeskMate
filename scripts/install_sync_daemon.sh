#!/usr/bin/env bash
# 小桔 B 类记忆 · 常驻同步 daemon · 安装/启用脚本（阶段 1 常驻化）
#
# 用法：
#   bash scripts/install_sync_daemon.sh mac     # 本机（Mac 控制端）装 launchd
#   bash scripts/install_sync_daemon.sh cloud   # 经 SSH 把云端侧装成 systemd（需云端密钥）
#
# 安全：幂等；云端绝不启动 openworker-server（Conflicts= 已声明）；动真库前运行器自带
# .backup 冷备由 sync_store_sqlite.migrate 的调用方负责（本脚本不动库，只部署+启用）。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"

# —— 云端连接参数（与 Mac plist / 运行器默认一致）——
CLOUD_HOST="81.69.163.170"
CLOUD_USER="ubuntu"
KEY="$REPO/.workbuddy/server/aibbs.pem"
KH="$REPO/.workbuddy/server/known_hosts"

side="${1:-}"

if [[ "$side" != "mac" && "$side" != "cloud" ]]; then
    echo "用法: $0 {mac|cloud}" >&2
    exit 2
fi

if [[ "$side" == "mac" ]]; then
    PLIST_DST="$HOME/Library/LaunchAgents/com.xiaosheng.xiaoju.sync.plist"
    echo "==> 安装 Mac launchd 单元: $PLIST_DST"
    cp "$HERE/com.xiaosheng.xiaoju.sync.plist" "$PLIST_DST"
    # 已加载则先卸后装（幂等重装）
    if launchctl list 2>/dev/null | grep -q com.xiaosheng.xiaoju.sync; then
        launchctl unload "$PLIST_DST" 2>/dev/null || true
    fi
    launchctl load "$PLIST_DST"
    sleep 2
    if launchctl list 2>/dev/null | grep -q com.xiaosheng.xiaoju.sync; then
        echo "✅ Mac 常驻同步 daemon 已加载并运行"
        launchctl list | grep com.xiaosheng.xiaoju.sync
    else
        echo "❌ 加载失败，查看日志: ~/Library/LaunchAgents 与 /Users/xiaocheng/.deskmate/sync_daemon.err"
        exit 1
    fi
    exit 0
fi

# —— cloud ——
echo "==> 部署云端侧（sync 包 + 运行器 + systemd 单元）"
scp -i "$KEY" -o UserKnownHostsFile="$KH" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -o BatchMode=yes \
    "$REPO/deskmate/memory/sync_daemon.py" \
    "$REPO/deskmate/memory/sync_store_sqlite.py" \
    "$REPO/deskmate/memory/sync_transport.py" \
    "$CLOUD_USER@$CLOUD_HOST:/tmp/" 2>&1 | tail -3

scp -i "$KEY" -o UserKnownHostsFile="$KH" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -o BatchMode=yes \
    "$REPO/scripts/sync_daemon_runner.py" \
    "$CLOUD_USER@$CLOUD_HOST:/tmp/sync_runner.py" 2>&1 | tail -1

ssh -i "$KEY" -o UserKnownHostsFile="$KH" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -o BatchMode=yes \
    "$CLOUD_USER@$CLOUD_HOST" bash -s <<'REMOTE'
set -euo pipefail
sudo -u openworker mkdir -p /var/lib/openworker/.deskmate/sync
sudo -u openworker cp /tmp/sync_daemon.py /tmp/sync_store_sqlite.py /tmp/sync_transport.py /var/lib/openworker/.deskmate/sync/
sudo -u openworker cp /tmp/sync_runner.py /var/lib/openworker/.deskmate/sync_runner.py
# 确保包可被 import（相对 import 需要 __init__.py）
[ -f /var/lib/openworker/.deskmate/sync/__init__.py ] || sudo -u openworker touch /var/lib/openworker/.deskmate/sync/__init__.py
echo "sync 包 + 运行器已就位"
REMOTE

# 本机把 unit 文件传上去并启用（需 sudo）
scp -i "$KEY" -o UserKnownHostsFile="$KH" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -o BatchMode=yes \
    "$HERE/xiaoju-sync.service" "$CLOUD_USER@$CLOUD_HOST:/tmp/xiaoju-sync.service" 2>&1 | tail -1

ssh -i "$KEY" -o UserKnownHostsFile="$KH" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -o BatchMode=yes \
    "$CLOUD_USER@$CLOUD_HOST" bash -s <<'REMOTE'
set -euo pipefail
# —— 安全前置（动真库前）——
# 1) 云端 python 版本：Mac 侧曾因系统 3.9 踩坑（已切 .venv 3.12），云端若同款先告警。
PYV=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo unknown)
echo "云端 python3 = $PYV"
case "$PYV" in
  3.9*|3.8*|3.7*|unknown) echo "⚠️ 云端 python 为 $PYV，与 Mac 侧 3.9 踩坑同源，建议先升到 3.12 再 enable" >&2 ;;
esac

# 2) 冷备：migrate 会改云端真库（加同步列 + uuid5 回填），启用前必须留可回滚副本。
#    幂等：已存在则跳过；属主保持 openworker（与 db / 服务一致）。
DB=/var/lib/openworker/.deskmate/deskmate.db
BAK=/var/lib/openworker/.deskmate/deskmate.db.pre_sync_20261003_184453
# 注意：/var/lib/openworker 是 700 属主 openworker，ubuntu(ssh 登录用户)看不到，
# 存在性判定必须用 openworker 身份，否则会误判"新库"而漏备份。
if sudo -u openworker test -f "$DB"; then
  if sudo -u openworker test ! -f "$BAK"; then
    # 红线：SQLite 活库备份禁用 cp —— 云端库为 WAL 模式（实测 journal_mode=wal
    # 且有未 checkpoint 的 -wal），cp 单个 .db 会丢 WAL 中数据、得到不一致副本。
    # 云端多无 sqlite3 CLI，故用 python3 的 sqlite3 backup API（源只读打开，
    # 先写 .tmp 再原子替换）。
    sudo -u openworker python3 - "$DB" "$BAK" <<'PY'
import os, sqlite3, sys
src_path, bak = sys.argv[1], sys.argv[2]
tmp = bak + ".tmp"
if os.path.exists(tmp):
    os.remove(tmp)
src = sqlite3.connect("file:%s?mode=ro" % src_path, uri=True)
dst = sqlite3.connect(tmp)
try:
    with dst:
        src.backup(dst)
finally:
    dst.close()
    src.close()
os.replace(tmp, bak)
print("backup bytes:", os.path.getsize(bak))
PY
    echo "✅ 云端冷备已生成(backup API, 非 cp): $BAK"
  else
    echo "ℹ️ 冷备已存在，跳过: $BAK（$(sudo -u openworker stat -c %y "$BAK" 2>/dev/null | cut -d. -f1)）"
    echo "   ⚠️ 若该备份之后真库又有变更，动库前请另做一次冷备（VACUUM INTO / backup API）。" >&2
  fi
else
  echo "ℹ️ 云端 deskmate.db 不存在（新库），跳过冷备"
fi

sudo cp /tmp/xiaoju-sync.service /etc/systemd/system/xiaoju-sync.service
sudo systemctl daemon-reload
# 绝不与 openworker-server 同跑
sudo systemctl stop openworker-server.service 2>/dev/null || true
sudo systemctl disable openworker-server.service 2>/dev/null || true
sudo systemctl enable --now xiaoju-sync.service
sleep 2
if systemctl is-active --quiet xiaoju-sync.service; then
    echo "✅ 云端常驻同步 daemon 已启用并运行"
else
    echo "❌ 云端 daemon 未起来，查看: sudo journalctl -u xiaoju-sync -n 50"
    exit 1
fi
REMOTE
echo "✅ 常驻化两端部署完成"
