#!/usr/bin/env bash
set -u

ROOT=$(cd "$(dirname "$0")/.." && pwd)
PY="$ROOT/.venv/bin/python"
BASE=${CHAOS_DIR:-/tmp/nutmap-chaos}
ROUNDS=${CHAOS_ROUNDS:-400}
PROB=${CHAOS_PROB:-0.02}
PASSES=${CHAOS_PASSES:-3}

export NUTMAP_CONFIG="$ROOT/config.chaos.toml"
export NUTMAP_FAKE_LLM=1
export NUTMAP_FAKE_EMBED=1

rm -rf "$BASE"
mkdir -p "$BASE"
cd "$ROOT" || exit 1

tear_live_journal() {
    local files
    mapfile -t files < <(find "$1" -name '*.journal.jsonl' -size +0 -newer "$2")
    if [ ${#files[@]} -gt 0 ]; then
        printf '{"key": "torn-' >> "${files[RANDOM % ${#files[@]}]}"
        return 0
    fi
    return 1
}

drive() {
    local data=$1 prob=$2 violent=$3 log="$1.log" marker="$1.round"
    local kills=0 ints=0 crashes=0 tears=0 rc killed
    : > "$log"
    for round in $(seq 1 "$ROUNDS"); do
        touch "$marker"
        NUTMAP_DATA="$data" NUTMAP_CHAOS="$prob" "$PY" -m nutmap run >> "$log" 2>&1 &
        local pid=$!
        killed=0
        if [ "$violent" = 1 ] && [ $((RANDOM % 10)) -lt 7 ]; then
            sleep "$(awk -v r=$RANDOM 'BEGIN { printf "%.2f", 0.3 + (r % 400) / 100 }')"
            if kill -0 "$pid" 2> /dev/null; then
                if [ $((RANDOM % 2)) -eq 0 ]; then
                    kill -KILL "$pid" 2> /dev/null && kills=$((kills + 1)) && killed=1
                else
                    kill -INT "$pid" 2> /dev/null && ints=$((ints + 1))
                fi
            fi
        fi
        wait "$pid" 2> /dev/null
        rc=$?
        if [ "$killed" = 1 ] && [ $((RANDOM % 2)) -eq 0 ] && tear_live_journal "$data" "$marker"; then
            tears=$((tears + 1))
            echo "harness: tore a journal line" >> "$log"
        fi
        case $rc in
            0)
                echo "$data: finished after $round rounds (SIGKILL $kills, SIGINT $ints, injected crashes $crashes, torn lines $tears)"
                return 0
                ;;
            3) NUTMAP_DATA="$data" "$PY" -m nutmap approve >> "$log" 2>&1 ;;
            130 | 137 | 143) ;;
            *) crashes=$((crashes + 1)) ;;
        esac
    done
    echo "$data: did not finish within $ROUNDS rounds"
    return 1
}

drive "$BASE/clean" 0 0 || exit 1
status=0
for pass in $(seq 1 "$PASSES"); do
    drive "$BASE/chaos$pass" "$PROB" 1 || exit 1
    "$PY" -I "$ROOT/scripts/chaos_check.py" "$BASE/clean" "$BASE/chaos$pass" || status=1
    echo "recovery events: $(grep -ohE 'adopted orphaned|never reached the server|torn trailing|interrupted; re-run' "$BASE/chaos$pass.log" | sort | uniq -c | tr -s ' ' | paste -sd ',')"
done
exit $status
