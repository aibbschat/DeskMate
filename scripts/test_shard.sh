#!/usr/bin/env bash
# 4 片并行跑 pytest（按目录分片，不用 xdist）—— 2026-11-19 PR15（#32）。
#
#   scripts/test_shard.sh              # 全量分 4 片并行
#   scripts/test_shard.sh tests/test_bot   # 只跑某个子集
#
# 为什么不用 `pytest -n auto`：SQLite 临时库 / 端口绑定 / cron 时间窗口三类用例
# 并行时互相抢资源，先做「文件分片」这种零风险并行；分片间互不共享 tmp_path。
set -u
cd "$(dirname "$0")/.." || exit 1
PY=${PY:-.venv/bin/python}
SHARDS=${SHARDS:-4}
TARGETS=${*:-tests}

mkdir -p .pytest_shards
rm -f .pytest_shards/*.log .pytest_shards/*.rc
files=$(find $TARGETS -name 'test_*.py' -type f | sort)
total=$(echo "$files" | wc -l | tr -d ' ')
echo "共 $total 个测试文件，分 $SHARDS 片"

i=0
for f in $files; do
  slot=$((i % SHARDS))
  echo "$f" >> ".pytest_shards/shard_${slot}.txt"
  i=$((i + 1))
done

rc=0
for slot in $(seq 0 $((SHARDS - 1))); do
  lst=".pytest_shards/shard_${slot}.txt"
  [ -f "$lst" ] || continue
  (
    xargs $PY -m pytest -q -p no:cacheprovider < "$lst" > ".pytest_shards/shard_${slot}.log" 2>&1
    echo $? > ".pytest_shards/shard_${slot}.rc"
  ) &
done
wait

for slot in $(seq 0 $((SHARDS - 1))); do
  rcfile=".pytest_shards/shard_${slot}.rc"
  [ -f "$rcfile" ] || continue
  code=$(cat "$rcfile")
  tail -1 ".pytest_shards/shard_${slot}.log" | sed "s/^/[shard $slot] /"
  [ "$code" = "0" ] || rc=1
done
rm -f .pytest_shards/shard_*.txt
exit $rc
