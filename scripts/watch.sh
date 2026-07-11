#!/usr/bin/env bash
# Приборная панель владельца: что происходит в общине прямо сейчас.
#   ./scripts/watch.sh        # последние 15 событий
#   ./scripts/watch.sh 40     # последние 40 событий
set -uo pipefail
cd "$(dirname "$0")/.."
N="${1:-15}"

echo "== контейнеры =="
./scripts/compose.sh ps

echo; echo "== расход LLM =="
CID="$(./scripts/compose.sh ps -q budget-guard 2>/dev/null || true)"
if [ -n "$CID" ]; then
  docker exec "$CID" python3 -c "import urllib.request,json; \
d=json.load(urllib.request.urlopen('http://localhost:8080/v1/budget')); \
print('потрачено: \$%s' % d['llm_spent_usd'])" 2>/dev/null || echo "budget-guard недоступен"
else
  echo "budget-guard не запущен"
fi

echo; echo "== очередь задач (список tasks) =="
docker exec llm-tribe-redis-1 redis-cli llen tasks 2>/dev/null | sed 's/^/ждут в очереди: /'
docker exec llm-tribe-redis-1 redis-cli lrange tasks 0 4 2>/dev/null | cut -c1-160

echo; echo "== последние $N событий =="
docker exec llm-tribe-redis-1 redis-cli lrange events -"$N" -1 2>/dev/null | python3 -c '
import sys, json, datetime
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        d = json.loads(line)
    except Exception:
        print(line[:160]); continue
    ts = datetime.datetime.fromtimestamp(d.get("ts", 0)).strftime("%m-%d %H:%M:%S")
    who = d.get("agent_id", "?")
    if d.get("topic") == "verdict":
        print("%s %-8s ВЕРДИКТ %s: %s (q=%s) %s" % (ts, who, d.get("task_id"),
              d.get("verdict"), d.get("quality"), str(d.get("reason", ""))[:90]))
        continue
    act = d.get("action") or d.get("topic", "")
    det = str(d.get("detail", ""))[:100].replace("\n", " ")
    cost = d.get("cost_usd") or 0
    tag = (" [%s]" % d.get("task_id")) if d.get("task_id") else ""
    c = " $%.2f" % cost if cost else ""
    print("%s %-8s %s%s%s  %s" % (ts, who, act, tag, c, det))
'

echo; echo "== фоновые процессы агентов (кроме главного лупа) и runner =="
for name in agent-1 agent-2 agent-3 runner; do
  docker exec "llm-tribe-$name-1" python3 -c '
import glob, pathlib, os
me = str(os.getpid())
seen = []
for p in glob.glob("/proc/[0-9]*/cmdline"):
    if p.split("/")[2] == me:
        continue
    try:
        c = pathlib.Path(p).read_bytes().decode(errors="replace").replace("\x00", " ").strip()
    except OSError:
        continue
    if c and "agent.main" not in c and not c.startswith("sh -c"):
        seen.append(c[:90])
print("; ".join(seen) or "(фоновых нет)")' 2>/dev/null \
    | sed "s/^/$name: /" || echo "$name: контейнер недоступен"
done

echo; echo "== хвосты логов в /workspace =="
docker exec llm-tribe-agent-1-1 sh -c \
  'find /workspace -maxdepth 3 -name "*.log" -not -path "*/.git/*" 2>/dev/null | \
   while read -r f; do echo "-- $f:"; tail -3 "$f"; done' 2>/dev/null \
  || echo "(агенты недоступны)"
