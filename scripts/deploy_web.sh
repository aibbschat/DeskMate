#!/usr/bin/env bash
#
# Deploy the built GUI to a hosted environment (xj.aibbs.chat).
#
# Contract (docs/DEPLOY-PARITY.md):
#   1. Build ONCE, locally:   cd surfaces/gui && npm run build
#   2. Ship that exact dist/ here. NEVER build on the host: a host-built bundle drifts
#      from the local one and the two can no longer be reconciled.
#   3. Environment differences live in runtime-config.json (written by this script),
#      never in the bundle. Rotating a token = edit config files, no rebuild.
#
# Usage: scripts/deploy_web.sh <host> <ssh-key> <dist.tar.gz> [ssh-user]
#
set -euo pipefail

HOST="${1:?host required}"
KEY="${2:?ssh key required}"
TARBALL="${3:?dist tarball required}"
SSH_USER="${4:-ubuntu}"
SSH_OPTS=(-i "$KEY" -o BatchMode=yes -o ConnectTimeout=12 -o StrictHostKeyChecking=accept-new)
FRONTEND_DIR=/opt/openworker/frontend
ENV_FILE=/etc/openworker/env

# Single source of truth: the token already configured for the backend on that host.
TOKEN="$(ssh "${SSH_OPTS[@]}" "$SSH_USER@$HOST" \
  "sudo -n grep -E '^COWORKER_API_TOKEN=' $ENV_FILE | cut -d= -f2-")"

if [ -z "$TOKEN" ]; then
  echo "error: COWORKER_API_TOKEN is empty in $ENV_FILE on $HOST" >&2
  echo "       set it there first; it must match what runtime-config.json serves." >&2
  exit 1
fi

echo "==> uploading $TARBALL to $HOST"
scp "${SSH_OPTS[@]}" "$TARBALL" "$SSH_USER@$HOST:/tmp/fe-deploy.tgz"

echo "==> unpacking the bundle"
ssh "${SSH_OPTS[@]}" "$SSH_USER@$HOST" \
  "sudo -n bash -c 'rm -rf $FRONTEND_DIR && mkdir -p $FRONTEND_DIR && \
   tar -xzf /tmp/fe-deploy.tgz -C $FRONTEND_DIR --strip-components=1 && \
   chown -R openworker:openworker $FRONTEND_DIR && rm -f /tmp/fe-deploy.tgz'"

# runtime-config.json 走 stdin 写，绝不拼进远端命令行。
# ⚠ 旧写法 `ssh "sudo bash -c '… printf %s > runtime-config.json' \"{json}\""` 会**静默写出空文件**：
#   那段 JSON 是 bash -c 的 $0（脚本名位），printf 拿不到参数 → 云端 token 丢失 → 前端所有
#   受限接口 401（2026-10-05 部署手机端时实测踩到，正是下面那条 NON-EMPTY 断言抓出来的）。
#   走 stdin 还顺带让 token 不出现在远端进程参数里。
echo "==> writing runtime-config.json (token via stdin, never in the bundle nor in argv)"
printf '{"__COWORKER_API_TOKEN__": "%s"}' "$TOKEN" | ssh "${SSH_OPTS[@]}" "$SSH_USER@$HOST" \
  "sudo -n tee $FRONTEND_DIR/runtime-config.json >/dev/null && sudo -n chown openworker:openworker $FRONTEND_DIR/runtime-config.json"

echo "==> ensuring cloud ~/Documents/已出版 symlink (reading code resolves ~/Documents/已出版)"
ssh "${SSH_OPTS[@]}" "$SSH_USER@$HOST" \
  "sudo -n bash -c 'mkdir -p /var/lib/openworker/写作/原稿 && ln -sfn /var/lib/openworker/写作/原稿 /var/lib/openworker/Documents/已出版 && ls -ld /var/lib/openworker/Documents/已出版'"

# A tar made on macOS carries AppleDouble sidecars (._<name>, one per file) as real archive
# members. They are never referenced by index.html but they DO land in the served root and
# inflate the upload (~40% of the entries on a dist this size). Strip them after unpack.
echo "==> stripping macOS AppleDouble sidecars (._*) from the deployed bundle"
ssh "${SSH_OPTS[@]}" "$SSH_USER@$HOST" \
  "sudo -n find $FRONTEND_DIR -name '._*' -type f -delete; \
   echo -n '  sidecars left: '; sudo -n find $FRONTEND_DIR -name '._*' -type f | wc -l"

echo "==> post-deploy checks on $HOST"
ssh "${SSH_OPTS[@]}" "$SSH_USER@$HOST" \
  "echo -n '  token occurrences in the served bundle (must be 0): '; \
   sudo -n grep -rl '$TOKEN' $FRONTEND_DIR --exclude=runtime-config.json 2>/dev/null | wc -l; \
   echo -n '  runtime-config.json present: '; \
   test -f $FRONTEND_DIR/runtime-config.json && echo yes || echo NO; \
   echo -n '  runtime-config.json NON-EMPTY (empty ⇒ token lost ⇒全息屏/受限接口静默401): '; \
   test -s $FRONTEND_DIR/runtime-config.json && echo yes || echo 'NO -- BROKEN'; \
   echo -n '  index.html loads config: '; \
   sudo -n grep -q 'runtime-config.json' $FRONTEND_DIR/index.html && echo yes || echo NO; \
   echo -n '  mobile.html shipped + loads config (手机端入口): '; \
   test -f $FRONTEND_DIR/mobile.html && sudo -n grep -q 'runtime-config.json' $FRONTEND_DIR/mobile.html && echo yes || echo 'NO -- 手机端没随包上去'; \
   echo -n '  mobile assets reachable (图标/manifest): '; \
   test -f $FRONTEND_DIR/m.webmanifest && test -f $FRONTEND_DIR/m-icon-192.png && echo yes || echo NO"

# 文件在位不代表安卓装得上：Chromium 只在 manifest 的 Content-Type 是 JSON 类时才认它。
# Caddy 的 mime 表里没有 .webmanifest → 整个响应没有 Content-Type → iOS 照装、安卓连安装入口
# 都不生成（2026-10-06 就是这个坑，云端 Caddyfile 补了 header @manifest Content-Type 才通）。
# 这一条从部署机走公网查，验的是**真·线上响应头**，不是目录里的文件。
PUBLIC_URL="${PUBLIC_URL:-https://xj.aibbs.chat}"
echo "==> PWA reachability from the public internet ($PUBLIC_URL)"
MCT="$(curl -s -o /dev/null -m 20 -w '%{content_type}' "$PUBLIC_URL/m.webmanifest" || true)"
MST="$(curl -s -o /dev/null -m 20 -w '%{http_code}' "$PUBLIC_URL/m.webmanifest" || true)"
echo -n "  manifest HTTP $MST, Content-Type '${MCT:-（空）}': "
if [ "$MST" = "200" ] && printf '%s' "$MCT" | grep -qi json; then
  echo "OK (安卓 Chromium 会认)"
else
  echo "FAIL -- 安卓装不上：Content-Type 必须是 application/manifest+json 或 application/json"
  echo "       去云端 /etc/caddy/Caddyfile 补：@manifest path /m.webmanifest + header @manifest Content-Type \"application/manifest+json\""
  echo "       （注意 admin off，reload 必失败，只能 systemctl restart caddy）"
  FAILED=1
fi
ICT="$(curl -s -o /dev/null -m 20 -w '%{http_code}' "$PUBLIC_URL/m-icon-192.png" || true)"
echo "  icon 192 HTTP $ICT (must be 200 without credentials: 安装弹窗拿不到口令)"
[ "${FAILED:-0}" = "1" ] && exit 1 || true

echo "==> done. Cloud now serves the same bytes as local dist/."