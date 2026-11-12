#!/usr/bin/env bash
# TACACS+ end-to-end test against a TACACS+ test server running on the host:
#   - failover: first configured server is unreachable, second answers
#   - a TACACS+-only user (alice) can log in over SSH, a wrong password cannot
#   - the local break-glass account (admin) still works
# Usage: tests/integration/tacacs.sh <image>   (needs python3, sshpass, docker, sudo)
set -euo pipefail

IMAGE="${1:?usage: tacacs.sh <image>}"
NAME="tarvos-tacacs-$$"
SSH_HOST_PORT="${SSH_HOST_PORT:-2223}"
# shellcheck disable=SC2054 # the comma is part of an ssh option value
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR
    -o PreferredAuthentications=password,keyboard-interactive -o ConnectTimeout=20)
WORKDIR="$(mktemp -d)"
TAC_PID=""

cleanup() {
    docker logs "${NAME}" 2>&1 | tail -n 60 >&2 || true
    docker rm -f "${NAME}" >/dev/null 2>&1 || true
    [ -z "${TAC_PID}" ] || sudo kill "${TAC_PID}" 2>/dev/null || true
    rm -rf "${WORKDIR}"
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

gateway="$(docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}')"

# Minimal TACACS+ server (tests/integration/tacacs_server.py); port 49 needs root.
sudo python3 "$(dirname "$0")/tacacs_server.py" --key testkey --port 49 --user alice:alicepw &
TAC_PID=$!
sleep 2

docker run -d --name "${NAME}" \
    --cap-add NET_ADMIN --cap-add NET_RAW --cap-add SYS_ADMIN \
    --sysctl net.ipv4.tcp_l3mdev_accept=1 --sysctl net.ipv4.udp_l3mdev_accept=1 \
    -e TACACS_SERVER="[\"192.0.2.55\", \"${gateway}\"]" \
    -e TACACS_SECRET=testkey -e TACACS_TIMEOUT=2 \
    -p "${SSH_HOST_PORT}:22" "${IMAGE}" >/dev/null

ssh_as() { # user password command
    sshpass -p "$2" ssh "${SSH_OPTS[@]}" -p "${SSH_HOST_PORT}" "$1@127.0.0.1" "$3"
}

# admin skips TACACS+, so it is the readiness probe
ready=0
for _ in $(seq 1 60); do
    if [ "$(ssh_as admin admin 'echo up' 2>/dev/null || true)" = "up" ]; then ready=1; break; fi
    sleep 1
done
[ "${ready}" -eq 1 ] || fail "container did not become ready"

echo "== TACACS+ user logs in (failover past an unreachable first server)"
[ "$(ssh_as alice alicepw 'echo tacacs-ok')" = "tacacs-ok" ] || fail "alice could not log in"

echo "== wrong TACACS+ password is rejected"
if ssh_as alice wrongpw 'echo should-not-run' >/dev/null 2>&1; then
    fail "alice logged in with a wrong password"
fi

echo "== unknown user is rejected"
if ssh_as mallory alicepw 'echo should-not-run' >/dev/null 2>&1; then
    fail "unknown user logged in"
fi

echo "== local admin still works with TACACS+ configured"
[ "$(ssh_as admin admin 'echo local-ok')" = "local-ok" ] || fail "admin login failed"

echo "PASS"
