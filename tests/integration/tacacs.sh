#!/usr/bin/env bash
# TACACS+ end-to-end test against a TACACS+ test server running on the host:
#   - failover: first configured server is unreachable, second answers
#   - a TACACS+-only user (alice) can log in over SSH, a wrong password cannot
#   - the same user (not listed anywhere in Tarvos) opens a NETCONF session with ncclient
#     (SSH "password" method), authenticated by TACACS+ under its own name:
#     default TACACS_NETCONF_ACCESS=ro -> read only; TACACS_NETCONF_ACCESS=rw -> read/write
#   - keyboard-interactive NETCONF clients go through TACACS+ too
#   - the local break-glass account (admin) still works
#   - tarvos-aaa over NETCONF: state (reachability, active server, last TACACS+ and local
#     authentications), and servers configured at runtime replace TACACS_SERVER
# Usage: tests/integration/tacacs.sh <image>   (needs python3, ncclient, sshpass, docker, sudo)
# TACACS_TEST_SERVER=<ip>: use a TACACS+ test server already running there (key testkey,
# user alice:alicepw) instead of starting one on the host.
set -euo pipefail

IMAGE="${1:?usage: tacacs.sh <image>}"
NAME="tarvos-tacacs-$$"
NAME_RW="tarvos-tacacs-rw-$$"
SSH_HOST_PORT="${SSH_HOST_PORT:-2223}"
NETCONF_HOST_PORT="${NETCONF_HOST_PORT:-8301}"
NETCONF_RW_HOST_PORT="${NETCONF_RW_HOST_PORT:-8302}"
GNMI_HOST_PORT="${GNMI_HOST_PORT:-57401}"
NETCONF_ACCESS="$(dirname "$0")/netconf_access.py"
# shellcheck disable=SC2054 # the comma is part of an ssh option value
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR
    -o PreferredAuthentications=password,keyboard-interactive -o ConnectTimeout=20)
# Raw NETCONF over OpenSSH, forced to keyboard-interactive (ncclient covers "password").
NETCONF_SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR
    -o PreferredAuthentications=keyboard-interactive -o ConnectTimeout=20)
WORKDIR="$(mktemp -d)"
TAC_PID=""

cleanup() {
    docker logs "${NAME}" 2>&1 | tail -n 60 >&2 || true
    docker logs "${NAME_RW}" 2>&1 | tail -n 30 >&2 || true
    docker rm -f "${NAME}" "${NAME_RW}" >/dev/null 2>&1 || true
    [ -z "${TAC_PID}" ] || sudo kill "${TAC_PID}" 2>/dev/null || true
    rm -rf "${WORKDIR}"
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

if [ -n "${TACACS_TEST_SERVER:-}" ]; then
    gateway="${TACACS_TEST_SERVER}"
else
    gateway="$(docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}')"
    # Minimal TACACS+ server (tests/integration/tacacs_server.py); port 49 needs root.
    sudo python3 "$(dirname "$0")/tacacs_server.py" --key testkey --port 49 --user alice:alicepw &
    TAC_PID=$!
    sleep 2
fi

run_tarvos() { # name ssh-port netconf-port [extra docker args...]
    local name="$1" ssh_port="$2" nc_port="$3"
    shift 3
    docker run -d --name "${name}" \
        --cap-add NET_ADMIN --cap-add NET_RAW --cap-add SYS_ADMIN \
        --sysctl net.ipv4.tcp_l3mdev_accept=1 --sysctl net.ipv4.udp_l3mdev_accept=1 \
        -e TACACS_SERVER="[\"192.0.2.55\", \"${gateway}\"]" \
        -e TACACS_SECRET=testkey -e TACACS_TIMEOUT=2 \
        "$@" -p "${ssh_port}:22" -p "${nc_port}:830" "${IMAGE}" >/dev/null
}

run_tarvos "${NAME}" "${SSH_HOST_PORT}" "${NETCONF_HOST_PORT}" -p "127.0.0.1:${GNMI_HOST_PORT}:57400"
# second instance, TACACS+ users with NETCONF write access (SSH port unused by the tests)
run_tarvos "${NAME_RW}" "$((SSH_HOST_PORT + 1))" "${NETCONF_RW_HOST_PORT}" -e TACACS_NETCONF_ACCESS=rw

nc_access() { # port user password -> denied|none|ro|rw
    python3 "${NETCONF_ACCESS}" 127.0.0.1 "$1" "$2" "$3"
}

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
    } | timeout 20 sshpass -p "$2" ssh "${NETCONF_SSH_OPTS[@]}" -p "${NETCONF_HOST_PORT}" -s "$1@127.0.0.1" netconf 2>/dev/null || true
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

echo "== ncclient: TACACS+ user, default access (ro): read ok, write denied"
got="$(nc_access "${NETCONF_HOST_PORT}" alice alicepw)" || fail "ncclient error for alice"
[ "${got}" = "ro" ] || fail "alice over ncclient: expected ro, got ${got}"

echo "== ncclient: wrong TACACS+ password and unknown user are rejected"
got="$(nc_access "${NETCONF_HOST_PORT}" alice wrongpw)" || fail "ncclient error (wrong password)"
[ "${got}" = "denied" ] || fail "ncclient accepted a wrong password (${got})"
got="$(nc_access "${NETCONF_HOST_PORT}" mallory alicepw)" || fail "ncclient error (unknown user)"
[ "${got}" = "denied" ] || fail "ncclient accepted an unknown user (${got})"

echo "== ncclient: local admin keeps full access"
got="$(nc_access "${NETCONF_HOST_PORT}" admin admin)" || fail "ncclient error for admin"
[ "${got}" = "rw" ] || fail "admin over ncclient: expected rw, got ${got}"

echo "== keyboard-interactive NETCONF client: TACACS+ user can read, cannot write (ro)"
replies="$(netconf_rpcs alice alicepw)"
grep -q 'message-id="1"[^>]*><data' <<<"${replies}" || fail "alice cannot read over NETCONF: ${replies}"
if grep -q 'message-id="2"[^>]*><ok/>' <<<"${replies}"; then fail "alice (read-only) could write over NETCONF"; fi
[ -z "$(netconf_rpcs alice wrongpw)" ] || fail "keyboard-interactive NETCONF accepted a wrong password"

echo "== ncclient: TACACS_NETCONF_ACCESS=rw gives the TACACS+ user read/write"
got=""
for _ in $(seq 1 60); do
    got="$(nc_access "${NETCONF_RW_HOST_PORT}" alice alicepw 2>/dev/null)" && break
    sleep 1
done
[ "${got}" = "rw" ] || fail "alice over ncclient with TACACS_NETCONF_ACCESS=rw: expected rw, got '${got}'"

echo "== local admin still works with TACACS+ configured"
[ "$(ssh_as admin admin 'echo local-ok')" = "local-ok" ] || fail "admin login failed"

echo "== gNMI: TACACS+ user authenticated through PAM (service gnmi), wrong password rejected"
gnmi_as() { gnmic -a "127.0.0.1:${GNMI_HOST_PORT}" -u "$1" -p "$2" --skip-verify --timeout 20s capabilities; }
for _ in $(seq 1 30); do gnmi_as admin admin >/dev/null 2>&1 && break; sleep 1; done
gnmi_as alice alicepw | grep -q 'tarvos-aaa' || fail "alice cannot use gNMI"
if gnmi_as alice wrongpw >/dev/null 2>&1; then fail "gNMI accepted a wrong TACACS+ password"; fi

echo "== tarvos-aaa state over NETCONF: probes, active server, last authentications"
[ "$(ssh_as alice alicepw 'echo again')" = "again" ] || fail "alice could not log in"
aaa_state() {
    python3 "$(dirname "$0")/netconf_get.py" 127.0.0.1 "${NETCONF_HOST_PORT}" \
        '<aaa xmlns="urn:tarvos:params:xml:ns:yang:tarvos-aaa"><state/></aaa>'
}
aaa_field() { # path -> text of the first match (local names, "/"-separated)
    python3 -c '
import sys, xml.etree.ElementTree as ET
nodes = [ET.fromstring(sys.stdin.read().encode())]
for name in sys.argv[1].split("/"):
    nodes = [c for n in nodes for c in n.iter() if c.tag.split("}")[-1] == name]
print(nodes[0].text if nodes else "")' "$1"
}
state="$(aaa_state)"
[ "$(aaa_field server-source <<<"${state}")" = "environment" ] || fail "aaa: ${state}"
[ "$(aaa_field active-server <<<"${state}")" = "${gateway}" ] ||
    fail "aaa: active server is not ${gateway} (192.0.2.55 is unreachable): ${state}"
for f in result user service; do
    echo "${f}=$(aaa_field "last-tacacs-authentication/${f}" <<<"${state}")"
done | tr '\n' ' ' | grep -q 'result=success user=alice service=sshd' ||
    fail "aaa: last TACACS+ authentication: ${state}"
[ "$(aaa_field last-local-authentication/user <<<"${state}")" = "admin" ] ||
    fail "aaa: last local authentication is not admin's"

echo "== tarvos-aaa: servers configured over NETCONF replace TACACS_SERVER"
aaa_edit() {
    python3 "$(dirname "$0")/netconf_edit.py" 127.0.0.1 "${NETCONF_HOST_PORT}" <<<"$1" ||
        fail "tarvos-aaa edit refused"
}
NSA='xmlns="urn:tarvos:params:xml:ns:yang:tarvos-aaa"'
aaa_edit "<aaa ${NSA}><tacacs><server><address>${gateway}</address><secret>wrongkey</secret></server></tacacs></aaa>"
if ssh_as alice alicepw 'echo should-not-run' >/dev/null 2>&1; then
    fail "alice logged in although the configured secret is wrong"
fi
aaa_edit "<aaa ${NSA}><tacacs><server><address>${gateway}</address><secret>testkey</secret></server></tacacs></aaa>"
[ "$(ssh_as alice alicepw 'echo runtime-ok')" = "runtime-ok" ] ||
    fail "alice could not log in through the NETCONF-configured server"
state="$(aaa_state)"
if [ "$(aaa_field server-source <<<"${state}")" != "netconf" ] ||
    [ "$(aaa_field active-servers <<<"${state}")" != "1" ]; then
    fail "aaa: ${state}"
fi
aaa_edit "<aaa ${NSA} xmlns:nc=\"urn:ietf:params:xml:ns:netconf:base:1.0\" nc:operation=\"remove\"/>"
[ "$(aaa_field server-source <<<"$(aaa_state)")" = "environment" ] ||
    fail "aaa: removing the servers did not restore TACACS_SERVER"
[ "$(ssh_as alice alicepw 'echo env-ok')" = "env-ok" ] || fail "alice login after restore"

echo "PASS"
