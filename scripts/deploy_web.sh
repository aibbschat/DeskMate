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

echo "==> unpacking + writing runtime-config.json (token never enters the bundle)"
ssh "${SSH_OPTS[@]}" "$SSH_USER@$HOST" \
  "sudo -n bash -c 'rm -rf $FRONTEND_DIR && mkdir -p $FRONTEND_DIR && \
   tar -xzf /tmp/fe-deploy.tgz -C $FRONTEND_DIR --strip-components=1 && \
   printf %s > $FRONTEND_DIR/runtime-config.json && \
   chown -R openworker:openworker $FRONTEND_DIR && rm -f /tmp/fe-deploy.tgz' \
  \"{\\\"__COWORKER_API_TOKEN__\\\": \\\"$TOKEN\\\"}\""

echo "==> ensuring cloud ~/Documents/已出版 symlink (reading code resolves ~/Documents/已出版)"
ssh "${SSH_OPTS[@]}" "$SSH_USER@$HOST" \
  "sudo -n bash -c 'mkdir -p /var/lib/openworker/写作/原稿 && ln -sfn /var/lib/openworker/写作/原稿 /var/lib/openworker/Documents/已出版 && ls -ld /var/lib/openworker/Documents/已出版'"

echo "==> post-deploy checks on $HOST"
ssh "${SSH_OPTS[@]}" "$SSH_USER@$HOST" \
  "echo -n '  token occurrences in JS assets (must be 0): '; \
   sudo -n grep -rl '$TOKEN' $FRONTEND_DIR/assets/ 2>/dev/null | wc -l; \
   echo -n '  runtime-config.json present: '; \
   test -f $FRONTEND_DIR/runtime-config.json && echo yes || echo NO; \
   echo -n '  index.html loads config: '; \
   sudo -n grep -q 'runtime-config.json' $FRONTEND_DIR/index.html && echo yes || echo NO"

echo "==> done. Cloud now serves the same bytes as local dist/."