#!/usr/bin/env bash
# Chaos suite, unattended: start and configure the lab (tests/lab/lab.sh), run
# tests/chaos/chaos.py, remove the lab (KEEP=1 leaves it running).
# Usage: tests/chaos/run.sh IMAGE [chaos.py options]
# Environment: CHAOS_SEED (replay a run), CHAOS_REPEAT (kills during writes, default 20),
# CHAOS_MONKEY_MINUTES (default 5), CHAOS_DEADLINE (recovery deadline, default 60 s).
# Report: tests/chaos/report/chaos-<seed>.md and .json.
set -euo pipefail

IMAGE="${1:?usage: run.sh IMAGE [chaos.py options]}"
shift
HERE="$(cd "$(dirname "$0")" && pwd)"
LAB="${HERE}/../lab/lab.sh"

cleanup() {
    status=$?
    if [ -n "${KEEP:-}" ]; then
        echo "KEEP set: lab left running (tests/lab/lab.sh down to remove it)" >&2
    else
        bash "${LAB}" down
    fi
    exit "${status}"
}
trap cleanup EXIT

bash "${LAB}" down
bash "${LAB}" up "${IMAGE}"
bash "${LAB}" configure
# shellcheck disable=SC1091
. "${HERE}/../lab/.state/lab.env"
for _ in $(seq 1 90); do
    docker exec "${LAB_PREFIX}-h1" ping -c1 -W1 10.1.100.12 >/dev/null 2>&1 && break
    sleep 1
done
python3 "${HERE}/chaos.py" "$@"
