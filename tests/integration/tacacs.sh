#!/usr/bin/env bash
# TACACS+ end-to-end test against a TACACS+ test server running on the host:
#   - failover: first configured server is unreachable, second answers
#   - a TACACS+-only user (alice) can log in over SSH, a wrong password cannot
#   - the same user (not listed anywhere in Tarvos) opens a NETCONF session, authenticated by
#     TACACS+ under its own name: read allowed, write denied (default TACACS_NETCONF_ACCESS=ro)
#   - the local break-glass account (admin) still works
# Usage: tests/integration/tacacs.sh <image>   (needs python3, sshpass, docker, sudo)
set -euo pipefail

IMAGE="${1:?usage: tacacs.sh <image>}"
NAME="tarvos-tacacs-$$"
SSH_HOST_PORT="${SSH_HOST_PORT:-2223}"
NETCONF_HOST_PORT="${NETCONF_HOST_PORT:-8301}"
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
    -p "${SSH_HOST_PORT}:22" -p "${NETCONF_HOST_PORT}:830" "${IMAGE}" >/dev/null

ssh_as() { # user password command
    sshpass -p "$2" ssh "${SSH_OPTS[@]}" -p "${SSH_HOST_PORT}" "$1@127.0.0.1" "$3"
}

netconf_rpcs() { # user password -> raw NETCONF replies for <hello>, get-config and edit-config
    local ns='xmlns="urn:ietf:params:xml:ns:netconf:base:1.0"'
    {
        printf '<hello %s><capabilities><capability>urn:ietf:params:netconf:base:1.0</capability></capabilities></hello>]]>]]>\n' "${ns}"
        sleep 1
        printf '<rpc message-id="1" %s><get-config><source><running/></source></get-config></rpc>]]>]]>\n' "${ns}"
        sleep 1
        printf '<rpc message-id="2" %s><edit-config><target><running/></target><config><nacm xmlns="urn:ietf:params:xml:ns:yang:ietf-netconf-acm"><enable-external-groups>true</enable-external-groups></nacm></config></edit-config></rpc>]]>]]>\n' "${ns}"
        sleep 2
    } | timeout 20 sshpass -p "$2" ssh "${SSH_OPTS[@]}" -p "${NETCONF_HOST_PORT}" -s "$1@127.0.0.1" netconf 2>/dev/null || true
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

echo "== TACACS+ user opens a NETCONF session under its own name (read ok, write denied)"
replies="$(netconf_rpcs alice alicepw)"
grep -q 'message-id="1"[^>]*><data' <<<"${replies}" || fail "alice cannot read over NETCONF: ${replies}"
if grep -q 'message-id="2"[^>]*><ok/>' <<<"${replies}"; then fail "alice (read-only) could write over NETCONF"; fi

echo "== NETCONF: wrong TACACS+ password and unknown user are rejected"
[ -z "$(netconf_rpcs alice wrongpw)" ] || fail "NETCONF accepted a wrong password"
[ -z "$(netconf_rpcs mallory alicepw)" ] || fail "NETCONF accepted an unknown user"

echo "== local admin still works with TACACS+ configured"
[ "$(ssh_as admin admin 'echo local-ok')" = "local-ok" ] || fail "admin login failed"

echo "PASS"
