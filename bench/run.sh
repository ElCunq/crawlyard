#!/usr/bin/env bash
# Throughput benchmark against the local fixture site.
#   bench/run.sh [jobs] [worker counts...]      e.g. bench/run.sh 600 1 2 4 8
# Env: LATENCY_MS (default 200) and FAIL_RATE (default 0.05) shape the fixture.
# Resets the Redis `stats` counters and deletes rows with source='bench'; nothing else.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a

JOBS=${1:-600}; shift || true
COUNTS=("${@:-1 2 4 8}")
DC=(docker compose -f compose.yml -f bench/compose.bench.yml)
redis() { "${DC[@]}" exec -T redis redis-cli -a "$REDIS_PASSWORD" --no-auth-warning "$@"; }
fixture() { "${DC[@]}" exec -T worker python -c "import urllib.request,sys; print(urllib.request.urlopen(urllib.request.Request('http://fixture:8000/stats', method=sys.argv[1])).read().decode())" "$1"; }
sql() { "${DC[@]}" exec -T postgres psql -U "$DB_USER" -d "${DB_NAME:-crawlyard}" -tAc "$1"; }

if [ "$(redis llen scrape_queue)" != 0 ]; then
    echo "scrape_queue is not empty; let it drain first so benchmark jobs do not mix with real ones." >&2
    exit 1
fi
"${DC[@]}" up -d --build fixture >/dev/null 2>&1
echo "host: $(lscpu | sed -n 's/^Model name: *//p'), $(nproc) threads, $(free -g | awk '/Mem:/{print $2}') GB RAM"
echo "fixture: LATENCY_MS=${LATENCY_MS:-200} FAIL_RATE=${FAIL_RATE:-0.05}, $JOBS jobs x 20 items"
printf '%-8s %-8s %-10s %-12s %-14s %-12s %-6s %-12s %s\n' workers secs jobs/min items/min http_503_rate job_retries dead mem/worker cpu_total

for N in ${COUNTS[*]}; do
    WORKER_DELAY_MS=0 "${DC[@]}" up -d --no-deps --scale worker="$N" worker >/dev/null 2>&1
    sleep 3
    redis del stats >/dev/null
    sql "DELETE FROM items WHERE source = 'bench'" >/dev/null
    fixture DELETE >/dev/null

    start=$(date +%s.%N)
    "${DC[@]}" exec -T worker python enqueue.py --pages "$JOBS" --base http://fixture:8000 --source bench >/dev/null
    : > bench/results/mem.tmp
    # Sample memory/CPU in the background so the completion poll stays tight.
    ( while :; do docker stats --no-stream --format '{{.Name}} {{.MemUsage}} {{.CPUPerc}}' | grep -- '-worker-' >> bench/results/mem.tmp; done ) &
    sampler=$!
    until [ "$(redis eval "local s=redis.call('hmget','stats','jobs_ok','jobs_dead') return (tonumber(s[1]) or 0)+(tonumber(s[2]) or 0)" 0)" -ge "$JOBS" ]; do sleep 0.1; done
    secs=$(echo "$(date +%s.%N) - $start" | bc)
    kill $sampler; wait $sampler 2>/dev/null || true

    items=$(sql "SELECT count(*) FROM items WHERE source = 'bench'")
    retries=$(redis hget stats job_errors); retries=${retries:-0}
    dead=$(redis hget stats jobs_dead); dead=${dead:-0}
    rate=$(fixture GET | python3 -c "import json,sys; d=json.load(sys.stdin); print(f\"{d['failed']/max(d['requests'],1):.1%}\")")
    mem=$(awk '{v=$2; if (v ~ /GiB/) {sub("GiB","",v); v*=1024} else sub("MiB","",v); s+=v; n++} END {printf "%.0f MiB", s/n}' bench/results/mem.tmp)
    # Average over samples of the summed CPU of all workers (100% = one core).
    cpu=$(awk -v n="$N" '{sub("%","",$NF); s+=$NF} END {printf "%.0f%%", s/(NR/n)}' bench/results/mem.tmp)
    printf '%-8s %-8.1f %-10.0f %-12.0f %-14s %-12s %-6s %-12s %s\n' "$N" "$secs" "$(echo "$JOBS*60/$secs" | bc -l)" "$(echo "$items*60/$secs" | bc -l)" "$rate" "$retries" "$dead" "$mem" "$cpu"
done
rm -f bench/results/mem.tmp
# Put the workers back to the delay and count from .env.
"${DC[@]}" up -d --no-deps --scale worker=1 worker >/dev/null 2>&1
