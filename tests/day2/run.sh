#!/usr/bin/env bash
# Day-2 campaign, unattended: start the lab (tests/lab/lab.sh), configure it, wait for the
# fabric, run tests/day2/day2.py, remove the lab (KEEP=1 leaves it running).
# Usage: tests/day2/run.sh IMAGE [day2.py options: --short, --only CASE,...]
# Needs: docker, python3 with ncclient, sshpass, gnmic. The report is written to
# tests/day2/report/ (day2.md, day2.json).
set -euo pipefail

IMAGE="${1:?usage: run.sh IMAGE [--short] [--only CASE,...]}"
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
# VRF and VXLAN devices live in the host kernel (modules vrf, vxlan).
bash "${LAB}" up "${IMAGE}"
bash "${LAB}" configure
# shellcheck disable=SC1091
. "${HERE}/../lab/.state/lab.env"
for _ in $(seq 1 90); do
    if docker exec "${LAB_PREFIX}-h1" ping -c1 -W1 10.1.100.12 >/dev/null 2>&1 &&
        docker exec "${LAB_PREFIX}-h1" ping -c1 -W1 10.10.2.1 >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
python3 "${HERE}/day2.py" "$@"
